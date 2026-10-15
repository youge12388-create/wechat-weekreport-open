"""鉴权：登录流程 + 网关层拦截（在开启鉴权的环境下验证）。

既有接口测试统一在 APP_ENV=test 下跑（中间件关闭），这里单独把 app_env 设为 dev
以启用 AuthMiddleware，确保「未登录 401 / 登录后可访问 / 登出失效」闭环成立。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import dispose_engine, get_engine
from app.main import create_app


@pytest.fixture
def auth_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # 开启鉴权：app_env 非 test
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret123")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("WECOM_CORP_ID", "test_corp")
    monkeypatch.setenv("WECOM_CORP_SECRET", "test_secret")
    monkeypatch.setenv("DB_URL", "sqlite://")
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": ()}
    )
    get_settings.cache_clear()
    dispose_engine()
    engine = get_engine(get_settings())
    Base.metadata.create_all(engine)
    try:
        yield TestClient(create_app())
    finally:
        get_settings.cache_clear()
        dispose_engine()


def test_protected_endpoint_requires_auth(auth_client: TestClient) -> None:
    # /api/sync/jobs 在白名单之外，未登录必须 401
    assert auth_client.get("/api/sync/jobs").status_code == 401


def test_login_wrong_password_rejected(auth_client: TestClient) -> None:
    resp = auth_client.post("/api/auth/login", json={"password": "nope"})
    assert resp.status_code == 401
    # 登录失败不应下发会话 Cookie，且之后仍被拦截
    assert auth_client.get("/api/sync/jobs").status_code == 401


def test_login_then_access_then_logout(auth_client: TestClient) -> None:
    resp = auth_client.post("/api/auth/login", json={"password": "secret123"})
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is True
    # TestClient 自动在后续请求带上 Set-Cookie 的会话
    assert auth_client.get("/api/sync/jobs").status_code == 200
    # 登出后会话失效
    assert auth_client.post("/api/auth/logout").status_code == 200
    assert auth_client.get("/api/sync/jobs").status_code == 401


def test_public_endpoints_open(auth_client: TestClient) -> None:
    # 健康检查与配置接口在白名单内，无需登录
    assert auth_client.get("/api/health").status_code == 200
    assert auth_client.get("/api/config").status_code == 200


def test_non_ascii_password_is_401_not_500(auth_client: TestClient) -> None:
    """含非 ASCII 的密码必须走「密码错误」而不是抛 TypeError 变 500。

    旧实现用 hmac.compare_digest 直接比 str，遇到非 ASCII 会抛 TypeError，
    于是输入中文密码得到的是服务端异常，而不是可理解的鉴权失败。
    """
    resp = auth_client.post("/api/auth/login", json={"password": "我的密码"})
    assert resp.status_code == 401


def test_prod_weak_credentials_refuse_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产环境用默认密钥/默认密码必须启动即失败，而不是只打一行警告。

    用默认密钥时签名算法是公开的，任何人都能自签管理员会话绕过密码。
    """
    from app.core.security import ensure_secure_settings

    base = {
        "app_env": "prod",
        "admin_password": "a-strong-admin-password",
        "session_secret": "s" * 48,
        "cookie_secure": True,
    }
    strong = Settings(_env_file=None, **base)
    assert ensure_secure_settings(strong) == []

    weak_secret = Settings(_env_file=None, **{**base, "session_secret": "dev-insecure-session-secret"})
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        ensure_secure_settings(weak_secret)

    weak_password = Settings(_env_file=None, **{**base, "admin_password": "changeme"})
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        ensure_secure_settings(weak_password)

    short_secret = Settings(_env_file=None, **{**base, "session_secret": "short"})
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        ensure_secure_settings(short_secret)


def test_prod_disables_api_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    """/docs 与 /openapi.json 在生产必须关闭：它们未经鉴权暴露完整 API 面。"""
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-strong-admin-password")
    monkeypatch.setenv("SESSION_SECRET", "s" * 48)
    monkeypatch.setenv("COOKIE_SECURE", "true")
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": ()}
    )
    get_settings.cache_clear()
    try:
        app = create_app()
        assert app.openapi_url is None
        assert app.docs_url is None
    finally:
        get_settings.cache_clear()


def _token_signed_with(secret: str, payload: dict) -> str:
    """按 security.py 的格式自签一个令牌，用于验证校验逻辑本身。"""
    import base64
    import hashlib
    import hmac
    import json

    from app.core.security import SESSION_COOKIE  # noqa: F401  仅确保模块已加载

    raw = base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    sig = hmac.new(secret.encode("utf-8"), raw.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


@pytest.fixture
def strong_secret_env(monkeypatch: pytest.MonkeyPatch) -> str:
    """固定一个足够强的密钥并让 app_env=test（可预测），便于自签令牌。"""
    secret = "k" * 48
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SESSION_SECRET", secret)
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": ()}
    )
    get_settings.cache_clear()
    try:
        yield secret
    finally:
        get_settings.cache_clear()


def test_public_default_secret_cannot_forge_session(strong_secret_env: str) -> None:
    """拿仓库里公开的默认密钥签出来的令牌必须无效。

    这是最直接的攻击路径：默认值是公开常量，知道它就能伪造管理员会话。
    """
    import time

    from app.core.security import verify_session_token

    now = int(time.time())
    forged = _token_signed_with(
        "dev-insecure-session-secret",
        {"iat": now, "exp": now + 3600, "sub": "admin"},
    )
    assert verify_session_token(forged) is False


def test_token_with_future_iat_rejected(strong_secret_env: str) -> None:
    """iat 落在未来的令牌必须拒绝 —— 旧实现只校验上界，这类令牌永不过期。"""
    import time

    from app.core.security import verify_session_token

    future = int(time.time()) + 86400 * 365
    forged = _token_signed_with(
        strong_secret_env, {"iat": future, "exp": future, "sub": "admin"}
    )
    assert verify_session_token(forged) is False


def test_expired_token_rejected(strong_secret_env: str) -> None:
    import time

    from app.core.security import verify_session_token

    past = int(time.time()) - 60 * 60 * 24
    forged = _token_signed_with(
        strong_secret_env, {"iat": past, "exp": past + 60, "sub": "admin"}
    )
    assert verify_session_token(forged) is False


def test_token_subject_must_be_admin(strong_secret_env: str) -> None:
    import time

    from app.core.security import verify_session_token

    now = int(time.time())
    forged = _token_signed_with(
        strong_secret_env, {"iat": now, "exp": now + 3600, "sub": "someone-else"}
    )
    assert verify_session_token(forged) is False


def test_valid_session_token_round_trips(strong_secret_env: str) -> None:
    """正确密钥 + 正确载荷必须通过，避免上面的加固把正常登录也一起挡掉。"""
    from app.core.security import create_session_token, verify_session_token

    assert verify_session_token(create_session_token()) is True


# --------------------------------------------------------------------------- #
# 登录限速：单共享密码必须防在线暴力破解
# --------------------------------------------------------------------------- #


def test_login_is_rate_limited_after_repeated_failures() -> None:
    """连续失败到阈值后返回 429 并带 Retry-After，而不是无限让猜。"""
    from app.core.login_throttle import LoginRateLimiter

    limiter = LoginRateLimiter(
        max_failures=3, window_seconds=300, max_global_failures=99
    )
    client = "10.0.0.1"
    for _ in range(3):
        assert limiter.retry_after(client) is None
        limiter.record_failure(client)

    wait = limiter.retry_after(client)
    assert wait is not None and wait > 0


def test_successful_login_clears_client_counter() -> None:
    """成功登录后该客户端计数清零：正常使用不该被自己几次手滑挡住。"""
    from app.core.login_throttle import LoginRateLimiter

    limiter = LoginRateLimiter(
        max_failures=3, window_seconds=300, max_global_failures=99
    )
    client = "10.0.0.2"
    for _ in range(2):
        limiter.record_failure(client)
    limiter.record_success(client)
    assert limiter.retry_after(client) is None


def test_global_limit_stops_ip_rotation() -> None:
    """换 IP 绕过单客户端限制时，全局线必须兜底。

    这是关键性质：如果只有按 IP 的计数，攻击者每次换一个 X-Forwarded-For
    就能无限猜下去。
    """
    from app.core.login_throttle import LoginRateLimiter

    limiter = LoginRateLimiter(
        max_failures=5, window_seconds=300, max_global_failures=4
    )
    # 每次换一个「新客户端」，单客户端计数始终没到阈值
    for index in range(4):
        client = f"10.0.1.{index}"
        assert limiter.retry_after(client) is None
        limiter.record_failure(client)

    # 全局已触顶：即便是一个全新的 IP 也必须被挡住
    assert limiter.retry_after("10.0.9.9") is not None


def test_login_endpoint_returns_429_and_retry_after(auth_client: TestClient) -> None:
    """接口层必须真的返回 429 + Retry-After 头（而不只是限速器内部有逻辑）。"""
    from app.api.routes import auth as auth_routes

    auth_routes._limiter = auth_routes.LoginRateLimiter(
        max_failures=2, window_seconds=300, max_global_failures=99
    )
    try:
        assert auth_client.post("/api/auth/login", json={"password": "bad"}).status_code == 401
        assert auth_client.post("/api/auth/login", json={"password": "bad"}).status_code == 401
        resp = auth_client.post("/api/auth/login", json={"password": "bad"})
        assert resp.status_code == 429
        assert resp.headers.get("Retry-After")
        # 即使密码正确，被限速期间也不放行（否则限速形同虚设）
        assert auth_client.post("/api/auth/login", json={"password": "secret123"}).status_code == 429
    finally:
        auth_routes._limiter = auth_routes.LoginRateLimiter(
            max_failures=5, window_seconds=300, max_global_failures=30
        )
