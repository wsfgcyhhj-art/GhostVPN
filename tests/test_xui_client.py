from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.services.xui.client import XUIClient, XUIError


@pytest.mark.asyncio
async def test_xui_client_csrf_login_and_auto_relogin_on_401_403() -> None:
    calls: list[tuple[str, str, str | None]] = []
    csrf_counter = 0
    add_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal csrf_counter, add_attempts
        path = request.url.path
        method = request.method
        csrf_hdr = request.headers.get("X-CSRF-Token")
        calls.append((method, path, csrf_hdr))

        if method == "GET" and path == "/secret/":
            csrf_counter += 1
            html_page = (
                f'<!DOCTYPE html><html><head>'
                f'<meta name="csrf-token" content="csrf_token_v{csrf_counter}">'
                f'</head><body>Login</body></html>'
            )
            return httpx.Response(
                200,
                text=html_page,
                headers={"content-type": "text/html; charset=utf-8"},
            )

        if method == "POST" and path.endswith("/login"):
            assert csrf_hdr == f"csrf_token_v{csrf_counter}"
            return httpx.Response(
                200,
                json={"success": True, "msg": "logged in"},
                headers={"content-type": "application/json"},
            )

        if method == "POST" and path.endswith("/panel/api/inbounds/addClient"):
            add_attempts += 1
            if add_attempts == 1:
                assert csrf_hdr == "csrf_token_v1"
                # Имитируем истечение CSRF / сессии 3X-UI (403 Forbidden)
                return httpx.Response(403, text="Forbidden - CSRF invalid")

            assert csrf_hdr == "csrf_token_v2"
            body: dict[str, Any] = json.loads(request.content.decode("utf-8"))
            assert body["id"] == 1
            parsed_settings = json.loads(body["settings"])
            client_obj = parsed_settings["clients"][0]
            assert client_obj["flow"] == "xtls-rprx-vision"
            assert client_obj["email"] == "tg_100500"
            assert client_obj["limitIp"] == 3
            assert client_obj["totalGB"] == 0
            return httpx.Response(
                200,
                json={"success": True, "msg": "added"},
                headers={"content-type": "application/json"},
            )

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        xui = XUIClient(
            host="https://127.0.0.1:2053",
            web_base_path="/secret",
            username="admin",
            password="password",
            http_client=http_client,
        )
        await xui.add_client(
            inbound_id=1,
            client_uuid="uuid-1234",
            email="tg_100500",
            sub_id="sub1234567890abc",
            expiry_time_ms=1790000000000,
            limit_ip=3,
        )

    assert xui._csrf_token == "csrf_token_v2"
    assert calls == [
        ("GET", "/secret/", None),
        ("POST", "/secret/login", "csrf_token_v1"),
        ("POST", "/secret/panel/api/inbounds/addClient", "csrf_token_v1"),
        ("GET", "/secret/", None),
        ("POST", "/secret/login", "csrf_token_v2"),
        ("POST", "/secret/panel/api/inbounds/addClient", "csrf_token_v2"),
    ]


@pytest.mark.asyncio
async def test_xui_client_raises_on_unsuccessful_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/secret/":
            return httpx.Response(
                200,
                text='<meta content="tok123" name="csrf-token">',
                headers={"content-type": "text/html"},
            )
        if request.url.path.endswith("/login"):
            assert request.headers.get("X-CSRF-Token") == "tok123"
            return httpx.Response(
                200,
                json={"success": False, "msg": "Invalid credentials"},
                headers={"content-type": "application/json"},
            )
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        xui = XUIClient(
            host="https://127.0.0.1:2053",
            web_base_path="/secret",
            username="admin",
            password="wrong",
            http_client=http_client,
        )
        with pytest.raises(XUIError, match="Invalid credentials"):
            await xui.login()


@pytest.mark.asyncio
async def test_xui_client_get_inbound_reality_info_parses_stream_settings() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/secret/":
            return httpx.Response(
                200,
                text='<meta name="csrf-token" content="csrf_tok_reality">',
                headers={"content-type": "text/html"},
            )
        if request.method == "POST" and request.url.path.endswith("/login"):
            return httpx.Response(200, json={"success": True, "msg": "ok"})
        if request.method == "GET" and request.url.path.endswith("/panel/api/inbounds/get/2"):
            stream_settings = {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "serverNames": ["sun9-14.userapi.com", "vk.com"],
                    "shortIds": ["a1b2c3d4", ""],
                    "settings": {
                        "publicKey": "VK_REALITY_PBK_456",
                        "fingerprint": "chrome",
                    },
                },
            }
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "obj": {
                        "id": 2,
                        "port": 8443,
                        "settings": json.dumps(
                            {
                                "clients": [
                                    {"id": "uuid-existing-1", "email": "tg_1", "subId": "sub1"}
                                ]
                            }
                        ),
                        "streamSettings": json.dumps(stream_settings),
                    },
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        xui = XUIClient(
            host="https://127.0.0.1:2053",
            web_base_path="/secret",
            username="admin",
            password="password",
            http_client=http_client,
        )
        info = await xui.get_inbound_reality_info(2)

    assert info is not None
    assert info.inbound_id == 2
    assert info.port == 8443
    assert info.primary_sni == "sun9-14.userapi.com"
    assert info.server_names == ["sun9-14.userapi.com", "vk.com"]
    assert info.public_key == "VK_REALITY_PBK_456"
    assert info.short_id == "a1b2c3d4"
    assert info.fingerprint == "chrome"
    assert info.client_uuids == {"uuid-existing-1"}

