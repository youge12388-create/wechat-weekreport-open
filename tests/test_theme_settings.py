"""界面调色盘接口回归测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import deps
from app.main import create_app
from app.services.runtime_config_service import runtime_settings


def _theme_client(db_session):
    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    return app


def test_theme_settings_read_write_and_reset(db_session) -> None:
    app = _theme_client(db_session)
    try:
        with TestClient(app) as client:
            initial = client.get("/api/settings/theme")
            assert initial.status_code == 200
            assert initial.json() == {
                "primary": "#1a1c28",
                "nav": "#181b27",
                "accent": "#259d8d",
                "background": "#f4f6f8",
                "surface": "#ffffff",
            }

            saved = client.patch(
                "/api/settings/theme",
                json={
                    "primary": "#9B5F76",
                    "nav": "#754459",
                    "accent": "#B66D83",
                    "background": "#FAF5F7",
                    "surface": "#FFFDFD",
                },
            )
            assert saved.status_code == 200
            assert saved.json() == {
                "primary": "#9b5f76",
                "nav": "#754459",
                "accent": "#b66d83",
                "background": "#faf5f7",
                "surface": "#fffdfd",
            }

            restored = client.patch("/api/settings/theme", json={"reset": True})
            assert restored.status_code == 200
            assert restored.json() == initial.json()
    finally:
        app.dependency_overrides.clear()

    settings = runtime_settings(db_session)
    assert settings.ui_primary_color == "#1A1C28"
    assert settings.ui_nav_color == "#181B27"


def test_theme_settings_reject_non_hex_color(db_session) -> None:
    app = _theme_client(db_session)
    try:
        with TestClient(app) as client:
            response = client.patch(
                "/api/settings/theme",
                json={"primary": "rgb(0 0 0)"},
            )
            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()
