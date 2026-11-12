from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.logging import logger
from app.db.models import Server


class XUIError(Exception):
    """Ошибка взаимодействия с панелью 3X-UI."""


@dataclass
class XUIServerStatus:
    online: bool
    cpu_percent: float = 0.0
    mem_used_mb: float = 0.0
    mem_total_mb: float = 0.0
    xray_state: str = "unknown"
    uptime_seconds: int = 0


class XUIClient:
    """
    Асинхронный клиент для панели 3X-UI (v3.11.0).
    Поддерживает авторизацию по cookie, автоматический ре-логин при 401/403/потере сессии
    и корректную сериализацию JSON-строки `settings` для VLESS + XTLS-RPRX-Vision + Reality.
    """

    def __init__(
        self,
        host: str,
        web_base_path: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        clean_path = "/" + web_base_path.strip("/") if web_base_path.strip("/") else ""
        self.base_url = f"{self.host}{clean_path}"
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self._lock = asyncio.Lock()
        self._logged_in = False
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            verify=verify_ssl,
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        )

    @classmethod
    def from_server(
        cls, server: Server, http_client: httpx.AsyncClient | None = None
    ) -> "XUIClient":
        return cls(
            host=server.host,
            web_base_path=server.web_base_path,
            username=server.username,
            password=server.password,
            verify_ssl=server.verify_ssl,
            http_client=http_client,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "XUIClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def login(self) -> None:
        async with self._lock:
            url = f"{self.base_url}/login"
            try:
                resp = await self._client.post(
                    url,
                    data={"username": self.username, "password": self.password},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                self._logged_in = False
                raise XUIError(f"Сбой авторизации 3X-UI ({url}): {exc}") from exc

            if not data.get("success"):
                self._logged_in = False
                raise XUIError(f"3X-UI отклонил логин: {data.get('msg', 'unknown error')}")

            self._logged_in = True
            logger.debug("Успешный вход в панель 3X-UI (%s)", self.base_url)

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        if not self._logged_in:
            await self.login()

        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        try:
            resp = await self._client.request(
                method=method,
                url=url,
                json=json_body,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise XUIError(f"Сетевая ошибка при обращении к 3X-UI ({url}): {exc}") from exc

        # Если сессия истекла, 3X-UI может вернуть 401/403/404 или редирект на HTML страницу логина
        content_type = resp.headers.get("content-type", "")
        if resp.status_code in (401, 403) or "application/json" not in content_type.lower():
            if retry_auth:
                logger.info("Сессия 3X-UI истекла (status=%s), выполняем повторный вход", resp.status_code)
                self._logged_in = False
                await self.login()
                return await self._request(
                    method, endpoint, json_body=json_body, retry_auth=False
                )
            raise XUIError(
                f"Некорректный ответ 3X-UI после ре-логина (status={resp.status_code})"
            )

        try:
            data: dict[str, Any] = resp.json()
        except Exception as exc:
            raise XUIError(f"Невалидный JSON от 3X-UI ({url}): {exc}") from exc

        return data

    @staticmethod
    def build_client_settings(
        *,
        client_uuid: str,
        email: str,
        sub_id: str,
        expiry_time_ms: int,
        limit_ip: int = 3,
        total_gb: int = 0,
        enable: bool = True,
        tg_id: str = "",
        flow: str = "xtls-rprx-vision",
    ) -> str:
        """Формирует сериализованную JSON-строку `settings` для 3X-UI v3.11.0."""
        payload = {
            "clients": [
                {
                    "id": client_uuid,
                    "flow": flow,
                    "email": email,
                    "limitIp": limit_ip,
                    "totalGB": total_gb,
                    "expiryTime": expiry_time_ms,
                    "enable": enable,
                    "tgId": str(tg_id),
                    "subId": sub_id,
                    "reset": 0,
                }
            ]
        }
        return json.dumps(payload, separators=(",", ":"))

    async def add_client(
        self,
        *,
        inbound_id: int,
        client_uuid: str,
        email: str,
        sub_id: str,
        expiry_time_ms: int,
        limit_ip: int = 3,
        total_gb: int = 0,
        enable: bool = True,
        tg_id: str = "",
    ) -> None:
        settings_str = self.build_client_settings(
            client_uuid=client_uuid,
            email=email,
            sub_id=sub_id,
            expiry_time_ms=expiry_time_ms,
            limit_ip=limit_ip,
            total_gb=total_gb,
            enable=enable,
            tg_id=tg_id,
        )
        data = await self._request(
            "POST",
            "panel/api/inbounds/addClient",
            json_body={"id": inbound_id, "settings": settings_str},
        )
        if not data.get("success"):
            msg = str(data.get("msg", ""))
            raise XUIError(f"Ошибка addClient в 3X-UI: {msg}")

    async def update_client(
        self,
        *,
        inbound_id: int,
        client_uuid: str,
        email: str,
        sub_id: str,
        expiry_time_ms: int,
        limit_ip: int = 3,
        total_gb: int = 0,
        enable: bool = True,
        tg_id: str = "",
    ) -> None:
        settings_str = self.build_client_settings(
            client_uuid=client_uuid,
            email=email,
            sub_id=sub_id,
            expiry_time_ms=expiry_time_ms,
            limit_ip=limit_ip,
            total_gb=total_gb,
            enable=enable,
            tg_id=tg_id,
        )
        data = await self._request(
            "POST",
            f"panel/api/inbounds/updateClient/{client_uuid}",
            json_body={"id": inbound_id, "settings": settings_str},
        )
        if not data.get("success"):
            msg = str(data.get("msg", ""))
            raise XUIError(f"Ошибка updateClient в 3X-UI: {msg}")

    async def delete_client_by_uuid(self, *, inbound_id: int, client_uuid: str) -> None:
        data = await self._request(
            "POST",
            f"panel/api/inbounds/{inbound_id}/delClient/{client_uuid}",
        )
        if not data.get("success"):
            msg = str(data.get("msg", ""))
            raise XUIError(f"Ошибка delClient в 3X-UI: {msg}")

    async def get_server_status(self) -> XUIServerStatus:
        """Запрашивает CPU / RAM / состояние Xray из 3X-UI (`panel/api/server/status` или `server/status`)."""
        for path in ("panel/api/server/status", "server/status"):
            try:
                data = await self._request("POST", path)
                if data.get("success") and isinstance(data.get("obj"), dict):
                    obj = data["obj"]
                    cpu = float(obj.get("cpu", 0.0))
                    mem = obj.get("mem", {}) or {}
                    mem_used = float(mem.get("current", 0)) / (1024 * 1024)
                    mem_total = float(mem.get("total", 0)) / (1024 * 1024)
                    xray = obj.get("xray", {}) or {}
                    xray_state = str(xray.get("state", "running"))
                    uptime = int(obj.get("uptime", 0))
                    return XUIServerStatus(
                        online=True,
                        cpu_percent=round(cpu, 1),
                        mem_used_mb=round(mem_used, 1),
                        mem_total_mb=round(mem_total, 1),
                        xray_state=xray_state,
                        uptime_seconds=uptime,
                    )
            except Exception as exc:
                logger.debug("Не удалось получить статус 3X-UI по пути %s: %s", path, exc)
        return XUIServerStatus(online=False)
