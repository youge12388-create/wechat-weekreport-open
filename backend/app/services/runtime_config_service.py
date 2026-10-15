"""管理员运行时 AI 配置。

环境变量始终是部署默认值；设置页保存的值以 Fernet 加密后覆盖默认值。这样
API 进程和独立 Worker 都能在下一次调用时读取相同配置，无需重启。

加密根密钥的来源（按优先级）：
1. `CONFIG_ENCRYPTION_KEY`（生产必须显式提供）；
2. 非生产环境：本地密钥文件 `config_key_file`，首次使用时自动生成。

**刻意不派生自 `SESSION_SECRET`**（旧实现的做法）：
- 默认 session_secret 是仓库里公开的常量，等于没有密钥 —— 能读数据库的人
  就能解出明文 Key；
- 轮换 SESSION_SECRET 会连带让所有已存密钥再也解不开（表现为 409「无法解密」）。
密钥文件是每台机器唯一且稳定的，两个问题都不存在。

对沿用旧派生的存量安装做**一次性自动迁移**：若密钥文件不存在但库里已有密文，
则用旧派生密钥解出明文、生成新的随机密钥重新加密写回，再把新密钥落盘。
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import BACKEND_ROOT, Settings, get_settings
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.models.system_setting import SystemSetting

logger = get_logger(__name__)

RUNTIME_KEYS = {
    "ai_api_key",
    "ai_base_url",
    "ai_model",
    # 语义检索（向量）：三项齐备即启用，任一为空自动退回关键词检索。
    "ai_embedding_api_key",
    "ai_embedding_base_url",
    "ai_embedding_model",
    "baidu_translate_app_id",
    "baidu_translate_api_key",
    "baidu_translate_secret_key",
    "sync_auto_enabled",
    "sync_reports_weekday",
    "sync_reports_hour",
    "sync_reports_minute",
    "sync_auto_period_summary_enabled",
    "ai_auto_enqueue_on_sync",
    # 周期归属天数：周期开始后前 N 天内提交仍归上一周期。周一才交上来的那篇
    # 属于上周，回溯几天取决于实际情况，必须能在设置页调（改完下一次同步即生效）。
    "report_period_attribution_days",
    "remind_auto_enabled",
    "remind_auto_weekday",
    "remind_auto_hour",
    # 催交卡片文案（标题 + 中英正文）：管理员在设置页改，下一次发送即生效。
    "remind_card_title",
    "remind_card_body_zh",
    "remind_card_body_en",
    # 跟进沟通中「负责人回复」要通知到的企微 userid（逗号分隔）。
    # 访问密码登录的管理员没有员工身份，靠这一项指定他的企微账号。
    "action_notify_admin_userids",
    # 企业微信登录：开关与回调地址属于运行期设置，改完下一次登录即生效，无需重启。
    # 注意 STATE 有效期、可信域名一类**安全边界**不在运行时设置里（见 config.py 注释）。
    "wecom_login_enabled",
    "wecom_login_redirect_uri",
    "wecom_login_frontend_url",
    "wecom_login_denied_mode",
    "ui_primary_color",
    "ui_nav_color",
    "ui_accent_color",
    "ui_background_color",
    "ui_surface_color",
}


class RuntimeConfigError(ValueError):
    """运行时配置无法安全读取或保存。"""


def _legacy_derived_key(settings: Settings) -> str:
    """旧实现的派生方式（仅用于把存量密文迁移出来）。"""
    digest = hashlib.sha256(
        f"weekly-report-runtime-config:{settings.session_secret}".encode("utf-8")
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def _key_file(settings: Settings) -> Path:
    path = Path(settings.config_key_file)
    return path if path.is_absolute() else (BACKEND_ROOT / path)


def _read_or_create_root_key(session: Session | None, settings: Settings) -> str:
    """解析加密根密钥；非生产环境首次调用时生成密钥文件。"""
    explicit = settings.config_encryption_key.strip()
    if explicit:
        return explicit
    if settings.app_env == "prod":
        raise RuntimeConfigError(
            "生产环境必须设置 CONFIG_ENCRYPTION_KEY，才能通过设置页保存密钥。"
        )

    path = _key_file(settings)
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    new_key = Fernet.generate_key().decode("ascii")
    # 已有存量密文时先做一次性迁移，避免升级后设置页读不出已保存的 Key。
    if session is not None:
        _migrate_legacy_rows(session, settings, new_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_key, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # Windows 上可能不支持，忽略即可
        pass
    logger.info("已生成本机配置加密密钥文件：%s", path)
    return new_key


def _migrate_legacy_rows(session: Session, settings: Settings, new_key: str) -> None:
    """把用旧派生密钥加密的存量行重加密为新密钥。"""
    rows = list(
        session.scalars(
            select(SystemSetting).where(SystemSetting.key.in_(RUNTIME_KEYS))
        ).all()
    )
    if not rows:
        return
    try:
        legacy = Fernet(_legacy_derived_key(settings).encode("ascii"))
    except (TypeError, ValueError):
        return
    cipher = Fernet(new_key.encode("ascii"))
    migrated = 0
    for row in rows:
        try:
            plain = legacy.decrypt(row.value_encrypted.encode("utf-8"))
        except InvalidToken:
            # 不是旧派生加密的（例如本来就用的显式密钥）：保持原样，交给读取时报错。
            continue
        row.value_encrypted = cipher.encrypt(plain).decode("utf-8")
        migrated += 1
    if migrated:
        session.commit()
        logger.warning(
            "已把 %s 项设置从 session_secret 派生密钥迁移到本机密钥文件"
            "（旧派生用的是公开默认值，能读库即可解密）",
            migrated,
        )


def _fernet(
    settings: Settings | None = None, session: Session | None = None
) -> Fernet:
    """构造 Fernet。

    传 session 时会用**同一个会话**做一次性存量迁移，这样内存库（测试）与文件库
    （生产）都作用于同一个连接，不会出现「在另一条连接上迁移、这条连接读不到」。
    """
    settings = settings or get_settings()
    owned = False
    if session is None:
        try:
            session = get_session_factory()()
            owned = True
        except Exception:  # noqa: BLE001 无库可用时只做只读解析
            session = None
    try:
        key = _read_or_create_root_key(session, settings)
    finally:
        if owned and session is not None:
            session.close()
    try:
        return Fernet(key.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigError("CONFIG_ENCRYPTION_KEY 不是有效的 Fernet 密钥。") from exc


def _stored_values(session: Session, settings: Settings) -> dict[str, str]:
    cipher = _fernet(settings, session)
    rows = session.scalars(
        select(SystemSetting).where(SystemSetting.key.in_(RUNTIME_KEYS))
    ).all()
    values: dict[str, str] = {}
    for row in rows:
        try:
            values[row.key] = cipher.decrypt(row.value_encrypted.encode("utf-8")).decode(
                "utf-8"
            )
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise RuntimeConfigError(f"设置项 {row.key} 无法解密，请重新录入。") from exc
    return values


def runtime_settings(session: Session | None = None) -> Settings:
    """返回已叠加设置页值的 Settings；调用方不会接触明文持久化实现。

    ## 为什么要有缓存

    每次调用原本都要重跑一遍 `Settings.model_validate(84 个字段)`，实测单次
    5.6 ms，其中 **4.6 ms 全在 pydantic 校验**（读密钥文件 0.3 ms、查库 0.2 ms）。
    而调用方是链式的：`/api/sync/status` 一类接口在一次请求里会经
    `sync_status → report_schedule_status → _report_schedule_due → _next_run_at`
    反复进入这里，`/api/notifications/feed` 实测 6 次、占其整体耗时近一半
    （那个接口每个打开的页面每 60 秒轮询一次）。这些值在一次请求内不可能变，
    重复校验纯属浪费。

    ## 缓存键为什么是「解密后的值」而不是时间或版本号

    用值本身做键，语义就是「值没变 → 结果一样」，所以**保存后下一次调用立即
    生效**这条约定不受影响：写库后值变了，键就变了，缓存自然失效，不需要任何
    手动清缓存或 TTL。反过来，用 TTL 或版本号就必须保证每个写入路径都记得清，
    漏一处就会读到旧配置（而这里缓存的是 API Key 与模型名，读错的代价是
    把请求发到旧主机或旧模型）。

    ## 为什么不缓存到 session 上

    与 `view_service` 里那几个请求内缓存不同，这里的重放成本高、而值与请求无关，
    所以做成进程级（Worker 与 API 各自持有一份）。这也让 Worker 每轮 3 秒轮询里
    的反复询问不再各付一次校验成本。

    缓存只存**已解密并校验过的 Settings 对象**，不存明文密钥的其他副本；键里的
    值来自本次已经读出来的字典，不额外扩大密钥的暴露面。
    """
    defaults = get_settings()
    owned_session = session is None
    if session is None:
        session = get_session_factory()()
    try:
        values = _stored_values(session, defaults)
        # 只把非空值计入键：空值在下一步被忽略（等价于「用部署默认值」），
        # 所以「有这一行但值为空」与「没有这一行」必须映射到同一个键，
        # 否则清理一条空设置会无谓地丢掉缓存。
        effective = tuple(sorted((key, value) for key, value in values.items() if value))
        cached = _lookup_cached_settings(defaults, effective)
        if cached is not None:
            # 返回深拷贝（实测 0.065 ms，相对被省下的 4.6 ms 校验可忽略）：缓存实例
            # 是进程级共享的，万一有调用方就地改字段（或改 report_type_keywords /
            # ai_allowed_base_hosts 这类可变字段内部），被污染的是**所有**后续请求
            # 的配置。浅拷贝挡不住后者 —— 那两个字段仍指向同一份 dict/list。
            return cached.model_copy(deep=True)
        merged = defaults.model_dump()
        merged.update(dict(effective))
        # 运行时值从数据库读取时都是字符串；走完整校验以正确还原 bool / int。
        result = Settings.model_validate(merged)
        _remember_cached_settings(defaults, effective, result)
        # 入缓存的是 result 本身，交出去的必须是副本 —— 否则第一次调用拿到的是
        # 缓存里那个实例，调用方就地改字段就等于改了所有人后续读到的配置。
        return result.model_copy(deep=True)
    finally:
        if owned_session:
            session.close()


# 进程级缓存：(默认 Settings 对象, 有效运行时值元组) -> 已校验的 Settings。
#
# 键里存的是**默认 Settings 对象本身**（强引用）而不是它的 id：CPython 会在对象
# 释放后复用内存地址，而测试里 `get_settings.cache_clear()` 会不断造出新的默认
# 对象 —— 用 id 做键就有机会命中「地址相同但配置不同」的陈旧条目。持有强引用后
# 这条路径不存在，缓存条目数也因此天然很少（正常只有「无覆盖」与「管理员保存过
# 的那一份」两种）。
_SETTINGS_CACHE_MAX = 32
_settings_cache: list[tuple[Settings, tuple[tuple[str, str], ...], Settings]] = []


def _lookup_cached_settings(
    defaults: Settings, effective: tuple[tuple[str, str], ...]
) -> Settings | None:
    for cached_defaults, cached_effective, cached_value in _settings_cache:
        if cached_defaults is defaults and cached_effective == effective:
            return cached_value
    return None


def _remember_cached_settings(
    defaults: Settings,
    effective: tuple[tuple[str, str], ...],
    value: Settings,
) -> None:
    # 满了就整体清空：条目数极少，LRU 的复杂度不值得，而清空只让下次多付
    # 一次 4.6 ms 校验。
    if len(_settings_cache) >= _SETTINGS_CACHE_MAX:
        _settings_cache.clear()
    _settings_cache.append((defaults, effective, value))


def invalidate_runtime_settings_cache() -> None:
    """丢弃缓存（测试用：绕开 API 直接改了 SystemSetting 表或默认配置时）。"""
    _settings_cache.clear()


def update_runtime_values(session: Session, values: Mapping[str, str | None]) -> None:
    """加密写入或删除指定运行时值；调用方负责 commit。"""
    unknown = set(values) - RUNTIME_KEYS
    if unknown:
        raise RuntimeConfigError("包含不允许保存的配置项。")
    settings = get_settings()
    cipher = _fernet(settings, session)
    existing = {
        row.key: row
        for row in session.scalars(
            select(SystemSetting).where(SystemSetting.key.in_(list(values)))
        )
    }
    for key, raw_value in values.items():
        value = (raw_value or "").strip()
        row = existing.get(key)
        if not value:
            if row is not None:
                session.delete(row)
            continue
        encrypted = cipher.encrypt(value.encode("utf-8")).decode("utf-8")
        if row is None:
            session.add(SystemSetting(key=key, value_encrypted=encrypted))
        else:
            row.value_encrypted = encrypted


def settings_snapshot(session: Session) -> dict:
    """返回管理 UI 所需的脱敏状态，绝不返回 Key/Secret 明文。"""
    settings = runtime_settings(session)
    return {
        "deepseek": {
            "provider": settings.ai_provider,
            "base_url": settings.ai_base_url,
            "model": settings.ai_model,
            "api_key_configured": bool(settings.ai_api_key),
        },
        "baidu_translation": {
            "app_id": settings.baidu_translate_app_id or None,
            "api_key_configured": bool(settings.baidu_translate_api_key),
            "secret_key_configured": bool(settings.baidu_translate_secret_key),
            "configured": bool(
                settings.baidu_translate_app_id
                and (settings.baidu_translate_api_key or settings.baidu_translate_secret_key)
            ),
        },
        "embedding": {
            "base_url": settings.ai_embedding_base_url or None,
            "model": settings.ai_embedding_model or None,
            "api_key_configured": bool(settings.ai_embedding_api_key),
            "configured": bool(
                settings.ai_embedding_base_url
                and settings.ai_embedding_api_key
                and settings.ai_embedding_model
            ),
        },
        "encryption": {
            "production_key_required": settings.app_env == "prod"
            and not bool(settings.config_encryption_key.strip()),
        },
        "reminder": {
            "auto_enabled": settings.remind_auto_enabled,
            "weekday": settings.remind_auto_weekday,
            "hour": settings.remind_auto_hour,
        },
        "wecom_login": login_snapshot(settings),
    }


def login_snapshot(settings: Settings) -> dict:
    """企业微信登录的脱敏状态（不含任何密钥）。"""
    return {
        "enabled": settings.wecom_login_enabled,
        "corp_configured": bool(settings.wecom_corp_id and settings.wecom_corp_secret),
        "agent_configured": bool(settings.wecom_agent_id),
        "agent_id": settings.wecom_agent_id or None,
        "redirect_uri": settings.wecom_login_redirect_uri or None,
        "frontend_url": settings.wecom_login_frontend_url or None,
        "denied_mode": settings.wecom_login_denied_mode,
    }


def theme_snapshot(session: Session) -> dict[str, str]:
    """返回界面调色盘的有效值，不暴露任何运行时密钥。"""
    settings = runtime_settings(session)
    return {
        "primary": settings.ui_primary_color.lower(),
        "nav": settings.ui_nav_color.lower(),
        "accent": settings.ui_accent_color.lower(),
        "background": settings.ui_background_color.lower(),
        "surface": settings.ui_surface_color.lower(),
    }
