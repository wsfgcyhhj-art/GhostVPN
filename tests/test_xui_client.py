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

        if method == "POST" and path.endswith("/panel/api/clients/add"):
            add_attempts += 1
            if add_attempts == 1:
                assert csrf_hdr == "csrf_token_v1"
                # Имитируем истечение CSRF / сессии 3X-UI (403 Forbidden)
                return httpx.Response(403, text="Forbidden - CSRF invalid")

            assert csrf_hdr == "csrf_token_v2"
            body: dict[str, Any] = json.loads(request.content.decode("utf-8"))
            assert body["inboundIds"] == [1]
            client_obj = body["client"]
            assert client_obj["id"] == "uuid-1234"
            assert client_obj["uuid"] == "uuid-1234"
            assert client_obj["flow"] == "xtls-rprx-vision"
            assert client_obj["email"] == "tg_100500"
            assert client_obj["limitIp"] == 3
            assert client_obj["totalGB"] == 0
            assert client_obj["expiryTime"] == 1790000000000
            assert client_obj["enable"] is True
            assert client_obj["tgId"] == 100500
            assert client_obj["subId"] == "sub1234567890abc"
            assert client_obj["reset"] == 0
            return httpx.Response(
                200,
                json={"success": True, "msg": "Client added"},
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
            tg_id="100500",
        )

    assert xui._csrf_token == "csrf_token_v2"
    assert calls == [
        ("GET", "/secret/", None),
        ("POST", "/secret/login", "csrf_token_v1"),
        ("POST", "/secret/panel/api/clients/add", "csrf_token_v1"),
        ("GET", "/secret/", None),
        ("POST", "/secret/login", "csrf_token_v2"),
        ("POST", "/secret/panel/api/clients/add", "csrf_token_v2"),
    ]


@pytest.mark.asyncio
async def test_xui_client_add_fallback_to_attach_and_double_failure() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        calls.append(f"{method} {path}")

        if method == "GET" and path == "/secret/":
            return httpx.Response(
                200,
                text='<meta name="csrf-token" content="csrf_fallback">',
                headers={"content-type": "text/html"},
            )
        if method == "POST" and path.endswith("/login"):
            return httpx.Response(200, json={"success": True, "msg": "ok"})

        if method == "POST" and path.endswith("/panel/api/clients/add"):
            # Имитируем ответ 3X-UI: клиент с таким email уже существует в другом inbound
            return httpx.Response(
                200,
                json={"success": False, "msg": "Duplicate email: tg_duplicate"},
                headers={"content-type": "application/json"},
            )

        if method == "POST" and path.endswith("/panel/api/clients/tg_duplicate/attach"):
            body = json.loads(request.content.decode("utf-8"))
            assert body["inboundIds"] == [2]
            return httpx.Response(
                200,
                json={"success": True, "msg": "Client attached"},
                headers={"content-type": "application/json"},
            )

        if method == "POST" and path.endswith("/panel/api/clients/tg_fail/attach"):
            return httpx.Response(
                200,
                json={"success": False, "msg": "Inbound not found"},
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

        # 1. Проверяем успешный fallback на attach при duplicate email
        await xui.add_client(
            inbound_id=2,
            client_uuid="uuid-dup",
            email="tg_duplicate",
            sub_id="sub_dup",
            expiry_time_ms=1800000000000,
        )
        assert "POST /secret/panel/api/clients/add" in calls
        assert "POST /secret/panel/api/clients/tg_duplicate/attach" in calls

        # 2. Проверяем выброс XUIError, если и add, и attach вернули success: False
        with pytest.raises(XUIError, match="Inbound not found"):
            await xui.add_client(
                inbound_id=2,
                client_uuid="uuid-fail",
                email="tg_fail",
                sub_id="sub_fail",
                expiry_time_ms=1800000000000,
            )


@pytest.mark.asyncio
async def test_xui_client_update_and_attach() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if method == "GET" and path == "/secret/":
            return httpx.Response(
                200,
                text='<meta name="csrf-token" content="csrf_update">',
                headers={"content-type": "text/html"},
            )
        if method == "POST" and path.endswith("/login"):
            return httpx.Response(200, json={"success": True, "msg": "ok"})

        if method == "POST" and path.endswith("/panel/api/clients/update/user_edit"):
            captured["update_body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "msg": "Client updated"},
                headers={"content-type": "application/json"},
            )

        if method == "POST" and path.endswith("/panel/api/clients/user_edit/attach"):
            captured["attach_body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "msg": "Client attached"},
                headers={"content-type": "application/json"},
            )

        if method == "POST" and path.endswith("/panel/api/clients/update/user_err"):
            return httpx.Response(
                200,
                json={"success": False, "msg": "Client not found"},
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

        # 1. Успешный update + attach
        await xui.update_client(
            inbound_id=1,
            client_uuid="uuid-edit",
            email="user_edit",
            sub_id="sub_edit",
            expiry_time_ms=1850000000000,
            limit_ip=5,
            enable=True,
            tg_id="998877",
        )
        assert captured["update_body"]["email"] == "user_edit"
        assert captured["update_body"]["limitIp"] == 5
        assert captured["update_body"]["tgId"] == 998877
        assert captured["attach_body"] == {"inboundIds": [1]}

        # 2. Ошибка при update (success: False) выбрасывает XUIError
        with pytest.raises(XUIError, match="Client not found"):
            await xui.update_client(
                inbound_id=1,
                client_uuid="uuid-err",
                email="user_err",
                sub_id="sub_err",
                expiry_time_ms=1850000000000,
            )


@pytest.mark.asyncio
async def test_xui_client_delete_finds_email_and_detaches() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if method == "GET" and path == "/secret/":
            return httpx.Response(
                200,
                text='<meta name="csrf-token" content="csrf_del">',
                headers={"content-type": "text/html"},
            )
        if method == "POST" and path.endswith("/login"):
            return httpx.Response(200, json={"success": True, "msg": "ok"})

        if method in ("GET", "POST") and path.endswith("/panel/api/inbounds/list"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "obj": [
                        {
                            "id": 1,
                            "remark": "Primary Reality",
                            "settings": json.dumps(
                                {
                                    "clients": [
                                        {"id": "uuid-other", "email": "other@vpn"},
                                        {"id": "uuid-target", "email": "target@vpn"},
                                    ]
                                }
                            ),
                        },
                        {
                            "id": 2,
                            "remark": "Backup Reality",
                            "settings": json.dumps({"clients": []}),
                        },
                    ],
                },
                headers={"content-type": "application/json"},
            )

        if method == "POST" and path.endswith("/panel/api/clients/target@vpn/detach"):
            captured["detach_body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "msg": "Client detached"},
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

        # 1. Успешный поиск email в инбаунде и отвязка (detach)
        await xui.delete_client_by_uuid(inbound_id=1, client_uuid="uuid-target")
        assert captured["detach_body"] == {"inboundIds": [1]}

        # 2. Несуществующий UUID в инбаунде выбрасывает XUIError
        with pytest.raises(XUIError, match="uuid-missing"):
            await xui.delete_client_by_uuid(inbound_id=1, client_uuid="uuid-missing")


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
