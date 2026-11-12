from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.services.xui.client import XUIClient, XUIError


@pytest.mark.asyncio
async def test_xui_client_login_and_auto_relogin_on_401() -> None:
    calls: list[str] = []
    add_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_attempts
        path = request.url.path
        calls.append(path)

        if path.endswith("/login"):
            return httpx.Response(
                200,
                json={"success": True, "msg": "logged in"},
                headers={"content-type": "application/json"},
            )

        if path.endswith("/panel/api/inbounds/addClient"):
            add_attempts += 1
            if add_attempts == 1:
                # Имитируем потерю сессии 3X-UI (401 Unauthorized)
                return httpx.Response(401, text="Unauthorized")

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

    assert calls == [
        "/secret/login",
        "/secret/panel/api/inbounds/addClient",
        "/secret/login",
        "/secret/panel/api/inbounds/addClient",
    ]


@pytest.mark.asyncio
async def test_xui_client_raises_on_unsuccessful_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
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
