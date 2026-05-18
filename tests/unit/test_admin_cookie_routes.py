from __future__ import annotations

from types import SimpleNamespace

from starlette.requests import Request

from app.common.errors import ValidationError
from app.web.routes import admin


class RejectingChannelService:
    def register_channel(
        self,
        source_input: str,
        cookie_text: str | None = None,
        *,
        transcription_enabled: bool = False,
    ):
        assert source_input == "@demo"
        assert cookie_text == "SID=secret"
        assert transcription_enabled is False
        raise ValidationError("bad cookie")


class CookieUpdateChannelService:
    def __init__(self) -> None:
        self.updated_cookie_text = None
        self.deleted = False

    def update_channel_cookie(self, channel_slug: str, cookie_text: str | None) -> bool:
        assert channel_slug == "demo"
        self.updated_cookie_text = cookie_text
        return True

    def delete_channel_cookie(self, channel_slug: str) -> bool:
        assert channel_slug == "demo"
        self.deleted = True
        return True


def build_request(channel_service) -> Request:
    app = SimpleNamespace(state=SimpleNamespace(channel_service=channel_service))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/channels",
            "headers": [],
            "app": app,
        }
    )


def test_create_channel_error_does_not_echo_cookie_text() -> None:
    response = admin.create_channel(
        build_request(RejectingChannelService()),
        source_input="@demo",
        cookie_text="SID=secret",
        transcription_enabled=False,
    )

    assert response.status_code == 400
    body = response.body.decode()
    assert "bad cookie" in body
    assert "@demo" in body
    assert "SID=secret" not in body


def test_update_channel_cookie_route_redirects_without_echoing_cookie_text() -> None:
    channel_service = CookieUpdateChannelService()
    response = admin.update_channel_cookie(
        build_request(channel_service),
        channel_slug="demo",
        cookie_text="# Netscape HTTP Cookie File\n",
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/channels/demo"
    assert channel_service.updated_cookie_text == "# Netscape HTTP Cookie File\n"


def test_delete_channel_cookie_route_redirects() -> None:
    channel_service = CookieUpdateChannelService()
    response = admin.delete_channel_cookie(build_request(channel_service), channel_slug="demo")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/channels/demo"
    assert channel_service.deleted is True
