from __future__ import annotations

import base64
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger, send_admin_alert
from app.db.models import (
    Plan,
    Server,
    Subscription,
    Transaction,
    TransactionStatus,
    TransactionType,
    User,
    utcnow,
)
from app.services.xui.client import XUIClient, XUIError, XUIInboundRealityInfo

if TYPE_CHECKING:
    from aiogram import Bot


ALPHABET = string.ascii_lowercase + string.digits

EU_NODE_TITLES: list[str] = [
    "⚡️ Узел 1",
    "⚡️ Узел 2",
    "⚡️ Узел 3",
    "⚡️ Узел 4",
    "⚡️ Узел 5",
]
NODE_TITLE_PRIMARY = EU_NODE_TITLES[0]
NODE_TITLE_RESERVE = EU_NODE_TITLES[1]


def format_eu_node_title(index_1_based: int) -> str:
    return f"⚡️ Узел {index_1_based}"


FRAGMENT_SPEC: dict[str, Any] = {
    "enabled": True,
    "packets": "tlshello",
    "length": "10-20",
    "interval": "10-15",
}

ROUTING_SPEC: dict[str, Any] = {
    "domainStrategy": "IPIfNonMatch",
    "rules": [
        {
            "type": "field",
            "domain": ["geosite:category-ads-all"],
            "outboundTag": "block",
            "action": "block",
        },
        {
            "type": "field",
            "domain": [
                "geosite:category-ru",
                "domain:ru",
                "domain:рф",
                "domain:xn--p1ai",
                "domain:su",
                "regexp:.*\\.(ru|рф|xn--p1ai|su)$",
            ],
            "outboundTag": "direct",
            "action": "direct",
        },
        {
            "type": "field",
            "ip": ["geoip:ru", "geoip:private"],
            "outboundTag": "direct",
            "action": "direct",
        },
        {
            "type": "field",
            "network": "tcp,udp",
            "outboundTag": "proxy",
            "action": "proxy",
        },
    ],
}


def generate_sub_id(length: int = 16) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def build_subscription_url(sub_id: str, server: Server | None = None) -> str:
    """
    Возвращает единую универсальную мульти-подписку `{WEBHOOK_BASE_URL}/sub/mobile/{sub_id}`
    (включающую оба Reality-профиля с Yandex/VK SNI и TLS Fragment для Wi-Fi и LTE).
    Если `WEBHOOK_BASE_URL` пустой — возвращает запасной фоллбэк на прямую ссылку 3X-UI (`XUI_SUB_BASE_URL/{sub_id}`).
    """
    web_base = (settings.WEBHOOK_BASE_URL or "").strip().rstrip("/")
    if web_base:
        return f"{web_base}/sub/mobile/{sub_id}"
    sub_base = (
        server.sub_base_url.rstrip("/")
        if (server is not None and server.sub_base_url)
        else settings.XUI_SUB_BASE_URL.rstrip("/")
    )
    return f"{sub_base}/{sub_id}"


def get_target_inbound_ids(server: Server | None = None) -> list[int]:
    """
    Возвращает общий список ID подключений (инбаундов) 3X-UI без дублей:
    основной `XUI_INBOUND_ID` (`server.inbound_id`), резервный `XUI_MOBILE_INBOUND_ID` (если задан)
    и все ID из `XUI_EXTRA_INBOUND_IDS` (список через запятую, например "2,3,4,5").
    """
    primary_id = (
        server.inbound_id
        if (server is not None and server.inbound_id)
        else settings.XUI_INBOUND_ID
    )
    ids: list[int] = [primary_id]

    mob_id = settings.XUI_MOBILE_INBOUND_ID
    if mob_id is not None and mob_id > 0 and mob_id not in ids:
        ids.append(mob_id)

    raw_extra = (getattr(settings, "XUI_EXTRA_INBOUND_IDS", "") or "").strip()
    if raw_extra:
        for part in raw_extra.split(","):
            cleaned = part.strip()
            if cleaned.isdigit():
                extra_id = int(cleaned)
                if extra_id > 0 and extra_id not in ids:
                    ids.append(extra_id)
    return ids


def calculate_new_expiry(
    current_expiry: datetime | None, days_to_add: int, now: datetime | None = None
) -> datetime:
    """
    Если подписка активна (current_expiry > now), дни прибавляются к current_expiry.
    Если истекла или отсутствует — дни отсчитываются от текущего момента (now).
    Поддерживает и положительное, и отрицательное количество дней (для админ-коррекции).
    """
    ref_now = now or utcnow()
    if current_expiry is not None:
        exp = (
            current_expiry
            if current_expiry.tzinfo
            else current_expiry.replace(tzinfo=timezone.utc)
        )
        base = exp if exp > ref_now else ref_now
    else:
        base = ref_now
    return base + timedelta(days=days_to_add)


def calculate_extra_device_price(sub: Subscription | None) -> Decimal:
    """
    Рассчитывает стоимость добавления +1 слота устройства до конца текущей подписки
    (пропорционально оставшимся дням, минимум 39 ₽).
    """
    monthly_rate = Decimal(str(settings.EXTRA_DEVICE_PRICE_MONTH_RUB))
    if sub is None or not sub.is_currently_valid:
        return monthly_rate.quantize(Decimal("0.01"))
    days_left = max(1, sub.days_left)
    prorated = (monthly_rate * Decimal(days_left) / Decimal(30)).quantize(Decimal("0.01"))
    return max(Decimal("39.00"), prorated)


def calculate_plan_renewal_price_with_slots(
    plan: Plan, extra_devices: int = 0
) -> Decimal:
    """
    Рассчитывает полную стоимость тарифа с учётом сохраняемых дополнительных слотов:
    базовая цена тарифа + 69 ₽ × кол-во доп. слотов × кол-во месяцев (days // 30).
    """
    base_price = Decimal(str(plan.price_rub))
    if extra_devices <= 0:
        return base_price.quantize(Decimal("0.01"))
    months = max(1, round(plan.days / 30))
    slot_rate = Decimal(str(settings.EXTRA_DEVICE_PRICE_MONTH_RUB))
    extra_cost = slot_rate * Decimal(extra_devices) * Decimal(months)
    return (base_price + extra_cost).quantize(Decimal("0.01"))


def _extract_server_hostname(server: Server | None) -> str:
    custom_domain = (getattr(settings, "VPN_PUBLIC_DOMAIN", "") or "").strip()
    if custom_domain:
        parsed_dom = urlparse(
            custom_domain if "://" in custom_domain else f"https://{custom_domain}"
        )
        if parsed_dom.hostname:
            return parsed_dom.hostname
        return custom_domain.strip("/")

    if server is not None:
        for raw_url in (server.sub_base_url, server.host):
            if raw_url:
                parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
                if parsed.hostname:
                    return parsed.hostname
    parsed_def = urlparse(settings.XUI_SUB_BASE_URL)
    return parsed_def.hostname or "127.0.0.1"


def _select_preferred_sni(
    server_names: list[str], preferred_keyword: str, fallback_sni: str
) -> str:
    for name in server_names:
        if preferred_keyword.lower() in name.lower():
            return name
    if server_names:
        return server_names[0]
    return fallback_sni


def _build_vless_reality_uri(
    *,
    client_uuid: str,
    host: str,
    port: int,
    sni: str,
    public_key: str,
    short_id: str,
    fingerprint: str,
    spider_x: str,
    title: str,
) -> str:
    spx_encoded = quote(spider_x or "/", safe="")
    title_encoded = quote(title, safe="")
    return (
        f"vless://{client_uuid}@{host}:{port}"
        f"?type=tcp&security=reality&pbk={public_key}&fp={fingerprint}"
        f"&sni={sni}&sid={short_id}&spx={spx_encoded}&flow=xtls-rprx-vision"
        f"&fragment=10-20,10-15,tlshello#{title_encoded}"
    )


def build_tls_fragment_profile(
    sub: Subscription,
    primary_info: XUIInboundRealityInfo | None = None,
    reserve_info: XUIInboundRealityInfo | None = None,
    inbound_infos: list[XUIInboundRealityInfo | None] | None = None,
) -> dict[str, Any]:
    """
    Генерирует мульти-профиль с европейскими узлами («🇪🇺 Европа • Узел 1..5») на одном VPS.
    Для каждого подключения считывает реальные serverNames (SNI), publicKey, shortId и port
    напрямую из настроек соответствующего inbound в 3X-UI, вшивая TLS Fragment
    (packets: "tlshello", length: "10-20", interval: "10-15"), принудительный IPv4 и uTLS fingerprint.
    """
    server = sub.server
    host = _extract_server_hostname(server)
    target_inbounds = get_target_inbound_ids(server)

    env_snis = [
        d.strip() for d in settings.XUI_SNI_DOMAINS.split(",") if d.strip()
    ] or ["ya.ru", "vk.com", "dl.google.com"]

    resolved_infos: list[XUIInboundRealityInfo | None]
    if inbound_infos is not None and len(inbound_infos) > 0:
        resolved_infos = list(inbound_infos)
    elif primary_info is not None or reserve_info is not None:
        resolved_infos = [primary_info, reserve_info or primary_info]
    else:
        resolved_infos = [None for _ in target_inbounds]

    # Если сконфигурирован только 1 инбаунд без доп. списка, формируем минимум 2 узла (Узел 1 и Узел 2)
    if len(resolved_infos) == 1:
        resolved_infos.append(resolved_infos[0])

    first_valid_info = next((inf for inf in resolved_infos if inf is not None), None)

    profiles: list[dict[str, Any]] = []
    links: list[str] = []
    collected_snis: list[str] = []

    for idx_zero, info in enumerate(resolved_infos):
        node_num = idx_zero + 1
        node_title = format_eu_node_title(node_num)
        eff_info = info or first_valid_info

        inbound_id = (
            info.inbound_id
            if info is not None
            else (
                target_inbounds[idx_zero]
                if idx_zero < len(target_inbounds)
                else (server.inbound_id if server else 1)
            )
        )

        names = (
            eff_info.server_names
            if (eff_info and eff_info.server_names)
            else env_snis
        )
        if info is not None and info.server_names:
            # Если у инбаунда собственные serverNames в 3X-UI, берём его реальный SNI
            if len(info.server_names) == 1:
                sni = info.server_names[0]
            elif node_num == 1:
                sni = _select_preferred_sni(names, "ya", names[0])
            elif node_num == 2 and info is first_valid_info:
                sni = _select_preferred_sni(names, "vk", names[0])
            else:
                sni = names[0]
        else:
            if node_num == 1:
                sni = _select_preferred_sni(names, "ya", "ya.ru")
            elif node_num == 2:
                sni = _select_preferred_sni(names, "vk", "vk.com")
            else:
                sni = names[(node_num - 1) % len(names)]

        port = (
            info.port
            if info is not None
            else (
                eff_info.port
                if (eff_info is not None and node_num == 1)
                else (443 if node_num == 1 else 8442 + node_num - 1)
            )
        )
        pbk = eff_info.public_key if eff_info is not None else ""
        sid = eff_info.short_id if eff_info is not None else ""
        spx = eff_info.spider_x if eff_info is not None else "/"
        fp = (
            info.fingerprint
            if (info is not None and info.fingerprint and node_num > 2)
            else ("chrome" if node_num % 2 == 1 else "ios")
        )

        uri = _build_vless_reality_uri(
            client_uuid=sub.xui_client_uuid,
            host=host,
            port=port,
            sni=sni,
            public_key=pbk,
            short_id=sid,
            fingerprint=fp,
            spider_x=spx,
            title=node_title,
        )
        node_dict = {
            "name": node_title,
            "inbound_id": inbound_id,
            "protocol": "vless",
            "address": host,
            "port": port,
            "uuid": sub.xui_client_uuid,
            "flow": "xtls-rprx-vision",
            "sni": sni,
            "serverNames": names,
            "publicKey": pbk,
            "shortId": sid,
            "spiderX": spx,
            "fingerprint": fp,
            "ip_strategy": "UseIPv4",
            "prefer_ipv4": True,
            "fragment": dict(FRAGMENT_SPEC),
            "vless_uri": uri,
        }
        profiles.append(node_dict)
        links.append(uri)
        collected_snis.append(sni)

    combined_sni = list(dict.fromkeys([*collected_snis, *env_snis]))
    raw_links = "\n".join(links)
    b64_links = base64.b64encode(raw_links.encode("utf-8")).decode("ascii")

    return {
        "remarks": f"GhostVPN 🇪🇺 Europe Anti-DPI ({sub.sub_id[:6]})",
        "subscription_url": build_subscription_url(sub.sub_id, server=server),
        "dns": {
            "servers": ["https://1.1.1.1/dns-query", "77.88.8.8"],
            "queryStrategy": "UseIPv4",
            "strategy": "prefer_ipv4",
        },
        "routing": dict(ROUTING_SPEC),
        "fragment": dict(FRAGMENT_SPEC),
        "ip_strategy": "UseIPv4",
        "prefer_ipv4": True,
        "utls_fingerprints": ["chrome", "ios"],
        "sni_whitelist": combined_sni,
        "ipv6_disabled": True,
        "profiles": profiles,
        "links": links,
        "base64_sub": b64_links,
    }


def render_mobile_yaml_profile(multi_profile: dict[str, Any]) -> str:
    """
    Генерирует YAML мульти-профиль (Clash Meta / Mihomo / Hiddify / Stash)
    со всеми европейскими узлами «🇪🇺 Европа • Узел 1..N», TLS Fragment, UseIPv4/prefer_ipv4 и uTLS.
    """
    profiles = multi_profile.get("profiles", [])
    lines = [
        "# GhostVPN Europe Anti-DPI Multi-Profile",
        "mixed-port: 7890",
        "allow-lan: false",
        "mode: rule",
        "ipv6: false",
        "dns:",
        "  enable: true",
        "  ipv6: false",
        "  prefer-h3: false",
        "  default-nameserver:",
        "    - 77.88.8.8",
        "    - 1.1.1.1",
        "  nameserver:",
        "    - https://1.1.1.1/dns-query",
        "proxies:",
    ]
    for p in profiles:
        lines.extend(
            [
                f'  - name: "{p["name"]}"',
                "    type: vless",
                f'    server: "{p["address"]}"',
                f'    port: {p["port"]}',
                f'    uuid: "{p["uuid"]}"',
                "    network: tcp",
                "    tls: true",
                "    udp: true",
                "    flow: xtls-rprx-vision",
                "    ip-version: ipv4",
                "    ip-strategy: prefer_ipv4",
                "    query-strategy: UseIPv4",
                f'    servername: "{p["sni"]}"',
                f'    client-fingerprint: "{p["fingerprint"]}"',
                "    reality-opts:",
                f'      public-key: "{p["publicKey"]}"',
                f'      short-id: "{p["shortId"]}"',
                "    tls-fragment:",
                "      enabled: true",
                '      packets: "tlshello"',
                '      length: "10-20"',
                '      interval: "10-15"',
            ]
        )
    lines.extend(
        [
            "proxy-groups:",
            '  - name: "👻 GhostVPN LTE"',
            "    type: url-test",
            "    url: http://www.gstatic.com/generate_204",
            "    interval: 120",
            "    proxies:",
        ]
    )
    for p in profiles:
        lines.append(f'      - "{p["name"]}"')
    lines.extend(
        [
            "rules:",
            "  - GEOSITE,category-ads-all,REJECT",
            "  - GEOSITE,category-ru,DIRECT",
            "  - DOMAIN-SUFFIX,ru,DIRECT",
            "  - DOMAIN-SUFFIX,рф,DIRECT",
            "  - DOMAIN-SUFFIX,xn--p1ai,DIRECT",
            "  - DOMAIN-SUFFIX,su,DIRECT",
            "  - GEOIP,private,DIRECT,no-resolve",
            "  - GEOIP,RU,DIRECT",
            "  - MATCH,👻 GhostVPN LTE",
        ]
    )
    return "\n".join(lines) + "\n"


def datetime_to_ms(dt: datetime) -> int:
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return int(aware.timestamp() * 1000)


class VPNService:
    """
    Оркестратор выдачи, продления, слотов устройств, сброса и деактивации подписок в БД и 3X-UI.
    Синхронизирует каждого клиента одновременно в основном (`XUI_INBOUND_ID`) и резервном LTE (`XUI_MOBILE_INBOUND_ID`) инбаундах.
    """

    def __init__(
        self, session: AsyncSession, xui_client_override: XUIClient | None = None
    ) -> None:
        self.session = session
        self._xui_override = xui_client_override

    async def _get_server(self) -> Server:
        res = await self.session.execute(
            select(Server).where(Server.is_active.is_(True)).order_by(Server.id.asc()).limit(1)
        )
        server = res.scalar_one_or_none()
        if server is None:
            raise RuntimeError("В базе данных нет ни одного активного сервера 3X-UI.")
        return server

    def _make_xui_client(self, server: Server) -> XUIClient:
        if self._xui_override is not None:
            return self._xui_override
        return XUIClient.from_server(server)

    @staticmethod
    async def _sync_add_client_all_inbounds(
        client: XUIClient,
        server: Server,
        *,
        client_uuid: str,
        email: str,
        sub_id: str,
        expiry_time_ms: int,
        limit_ip: int,
        enable: bool,
        tg_id: str,
    ) -> None:
        target_inbounds = get_target_inbound_ids(server)
        for idx, inbound_id in enumerate(target_inbounds):
            try:
                await client.add_client(
                    inbound_id=inbound_id,
                    client_uuid=client_uuid,
                    email=email,
                    sub_id=sub_id,
                    expiry_time_ms=expiry_time_ms,
                    limit_ip=limit_ip,
                    total_gb=0,
                    enable=enable,
                    tg_id=tg_id,
                )
            except XUIError as exc:
                if idx > 0 and "duplicate email" in str(exc).lower():
                    alt_email = f"{email}_inb{inbound_id}"
                    await client.add_client(
                        inbound_id=inbound_id,
                        client_uuid=client_uuid,
                        email=alt_email,
                        sub_id=sub_id,
                        expiry_time_ms=expiry_time_ms,
                        limit_ip=limit_ip,
                        total_gb=0,
                        enable=enable,
                        tg_id=tg_id,
                    )
                elif idx > 0:
                    logger.warning(
                        "Не удалось добавить клиента %s в доп. inbound #%s: %s",
                        email,
                        inbound_id,
                        exc,
                    )
                else:
                    raise

    @staticmethod
    async def _sync_update_client_all_inbounds(
        client: XUIClient,
        server: Server,
        *,
        client_uuid: str,
        email: str,
        sub_id: str,
        expiry_time_ms: int,
        limit_ip: int,
        enable: bool,
        tg_id: str,
    ) -> None:
        target_inbounds = get_target_inbound_ids(server)
        for idx, inbound_id in enumerate(target_inbounds):
            try:
                await client.update_client(
                    inbound_id=inbound_id,
                    client_uuid=client_uuid,
                    email=email,
                    sub_id=sub_id,
                    expiry_time_ms=expiry_time_ms,
                    limit_ip=limit_ip,
                    total_gb=0,
                    enable=enable,
                    tg_id=tg_id,
                )
            except XUIError as exc:
                if idx > 0:
                    # Если дополнительный inbound был подключён позже, автоматически создаём в нём клиента
                    try:
                        await client.add_client(
                            inbound_id=inbound_id,
                            client_uuid=client_uuid,
                            email=email,
                            sub_id=sub_id,
                            expiry_time_ms=expiry_time_ms,
                            limit_ip=limit_ip,
                            total_gb=0,
                            enable=enable,
                            tg_id=tg_id,
                        )
                    except XUIError as add_exc:
                        if "duplicate email" in str(add_exc).lower():
                            try:
                                await client.add_client(
                                    inbound_id=inbound_id,
                                    client_uuid=client_uuid,
                                    email=f"{email}_inb{inbound_id}",
                                    sub_id=sub_id,
                                    expiry_time_ms=expiry_time_ms,
                                    limit_ip=limit_ip,
                                    total_gb=0,
                                    enable=enable,
                                    tg_id=tg_id,
                                )
                            except Exception as retry_exc:
                                logger.warning(
                                    "Не удалось синхронизировать клиента %s в доп. inbound #%s: %s",
                                    email,
                                    inbound_id,
                                    retry_exc,
                                )
                        else:
                            logger.warning(
                                "Не удалось синхронизировать клиента %s в доп. inbound #%s: %s",
                                email,
                                inbound_id,
                                add_exc,
                            )
                    except Exception as add_exc:
                        logger.warning(
                            "Не удалось синхронизировать клиента %s в доп. inbound #%s: %s",
                            email,
                            inbound_id,
                            add_exc,
                        )
                else:
                    raise

    @staticmethod
    async def _sync_delete_client_all_inbounds(
        client: XUIClient,
        server: Server,
        *,
        client_uuid: str,
    ) -> None:
        for inbound_id in get_target_inbound_ids(server):
            try:
                await client.delete_client_by_uuid(
                    inbound_id=inbound_id,
                    client_uuid=client_uuid,
                )
            except XUIError as exc:
                logger.warning(
                    "Клиент %s не удалён из inbound #%s (возможно уже отсутствует): %s",
                    client_uuid,
                    inbound_id,
                    exc,
                )

    @staticmethod
    async def _ensure_client_in_inbound(
        client: XUIClient,
        *,
        inbound_id: int,
        client_uuid: str,
        email: str,
        sub_id: str,
        expiry_time_ms: int,
        limit_ip: int,
        enable: bool,
        tg_id: str,
    ) -> None:
        """
        Гарантирует наличие клиента в целевом инбаунде 3X-UI (автоматически добавляет,
        если инбаунд был создан позже, обрабатывая возможную коллизию email между инбаундами).
        """
        try:
            await client.add_client(
                inbound_id=inbound_id,
                client_uuid=client_uuid,
                email=email,
                sub_id=sub_id,
                expiry_time_ms=expiry_time_ms,
                limit_ip=limit_ip,
                total_gb=0,
                enable=enable,
                tg_id=tg_id,
            )
        except XUIError as exc:
            err_lower = str(exc).lower()
            if "duplicate email" in err_lower:
                try:
                    await client.add_client(
                        inbound_id=inbound_id,
                        client_uuid=client_uuid,
                        email=f"{email}_inb{inbound_id}",
                        sub_id=sub_id,
                        expiry_time_ms=expiry_time_ms,
                        limit_ip=limit_ip,
                        total_gb=0,
                        enable=enable,
                        tg_id=tg_id,
                    )
                except Exception as retry_exc:
                    logger.debug(
                        "Повторное добавление клиента %s в inbound #%s: %s",
                        email,
                        inbound_id,
                        retry_exc,
                    )
            elif "duplicate" not in err_lower:
                logger.warning(
                    "Не удалось автоматически добавить клиента %s в новый inbound #%s: %s",
                    email,
                    inbound_id,
                    exc,
                )
        except Exception as exc:
            logger.warning(
                "Ошибка авто-синхронизации клиента %s в inbound #%s: %s",
                email,
                inbound_id,
                exc,
            )

    async def fetch_live_mobile_profile(self, sub: Subscription) -> dict[str, Any]:
        """
        Запрашивает реальные настройки `serverNames` (SNI), `publicKey`, `shortId`, `port`
        напрямую из всех целевых подключений (`get_target_inbound_ids(server)`) в панели 3X-UI.
        Если в новых подключениях активный и не заблокированный клиент ещё отсутствует —
        автоматически добавляет его туда с текущими `uuid`, `subId`, `email`, `limitIp` и `expiryTime`,
        после чего возвращает мульти-профиль с европейскими узлами «🇪🇺 Европа • Узел 1..N».
        """
        server = sub.server or await self._get_server()
        user = sub.user or await self.session.get(User, sub.user_id)
        should_sync_missing = bool(sub.is_currently_valid and not (user and user.is_banned))
        tg_id_str = str(user.telegram_id) if user else ""
        expiry_ms = datetime_to_ms(sub.expires_at)

        target_inbounds = get_target_inbound_ids(server)
        inbound_infos: list[XUIInboundRealityInfo | None] = []

        client = self._make_xui_client(server)
        try:
            for inbound_id in target_inbounds:
                try:
                    info = await client.get_inbound_reality_info(inbound_id)
                except Exception as exc:
                    logger.debug(
                        "Используем кэшированные/дефолтные параметры Reality для inbound #%s: %s",
                        inbound_id,
                        exc,
                    )
                    info = None

                if (
                    should_sync_missing
                    and info is not None
                    and sub.xui_client_uuid not in info.client_uuids
                ):
                    await self._ensure_client_in_inbound(
                        client,
                        inbound_id=inbound_id,
                        client_uuid=sub.xui_client_uuid,
                        email=sub.xui_email,
                        sub_id=sub.sub_id,
                        expiry_time_ms=expiry_ms,
                        limit_ip=sub.limit_ip,
                        enable=True,
                        tg_id=tg_id_str,
                    )
                    info.client_uuids.add(sub.xui_client_uuid)

                inbound_infos.append(info)
        finally:
            if self._xui_override is None:
                await client.close()

        return build_tls_fragment_profile(
            sub,
            inbound_infos=inbound_infos,
        )

    async def grant_or_extend_subscription(
        self,
        user: User,
        days: int,
        *,
        limit_ip: int | None = None,
        is_trial: bool = False,
        plan_id: int | None = None,
    ) -> Subscription:
        """
        Создаёт клиента в 3X-UI (в основном и резервном LTE-инбаунде) при первой выдаче
        или обновляет существующего при продлении, сохраняя его `subId` и `uuid`.
        """
        now = utcnow()
        eff_limit_ip = limit_ip or settings.DEFAULT_LIMIT_IP

        res = await self.session.execute(
            select(Subscription).where(Subscription.user_id == user.id)
        )
        sub = res.scalar_one_or_none()

        if sub is None:
            server = await self._get_server()
            new_uuid = str(uuid.uuid4())
            new_sub_id = generate_sub_id(16)
            email = f"tg_{user.telegram_id}"
            new_expires_at = calculate_new_expiry(None, days, now=now)
            expiry_ms = datetime_to_ms(new_expires_at)

            client = self._make_xui_client(server)
            try:
                await self._sync_add_client_all_inbounds(
                    client,
                    server,
                    client_uuid=new_uuid,
                    email=email,
                    sub_id=new_sub_id,
                    expiry_time_ms=expiry_ms,
                    limit_ip=eff_limit_ip,
                    enable=True,
                    tg_id=str(user.telegram_id),
                )
            finally:
                if self._xui_override is None:
                    await client.close()

            sub = Subscription(
                user_id=user.id,
                server_id=server.id,
                current_plan_id=plan_id,
                xui_client_uuid=new_uuid,
                xui_email=email,
                sub_id=new_sub_id,
                limit_ip=eff_limit_ip,
                extra_devices=0,
                started_at=now,
                expires_at=new_expires_at,
                is_trial=is_trial,
                is_active=True,
                deleted_from_xui=False,
                notified_3d=False,
                notified_1d=False,
                notified_expired=False,
                notified_onboarding=False,
                notified_winback=False,
                auto_renew_attempted=False,
            )
            self.session.add(sub)
            if is_trial:
                user.has_used_trial = True
            await self.session.commit()
            await self.session.refresh(sub)
            await self.session.refresh(user, attribute_names=["subscription"])
            return sub

        # Продление существующей подписки (сохраняем старый subId и uuid!)
        server = sub.server or await self._get_server()
        was_expired = not sub.is_currently_valid
        new_expires_at = calculate_new_expiry(sub.expires_at, days, now=now)
        is_still_active = new_expires_at > now
        expiry_ms = datetime_to_ms(new_expires_at)
        total_limit_ip = max(eff_limit_ip + sub.extra_devices, sub.limit_ip)

        client = self._make_xui_client(server)
        try:
            if sub.deleted_from_xui:
                await self._sync_add_client_all_inbounds(
                    client,
                    server,
                    client_uuid=sub.xui_client_uuid,
                    email=sub.xui_email,
                    sub_id=sub.sub_id,
                    expiry_time_ms=expiry_ms,
                    limit_ip=total_limit_ip,
                    enable=is_still_active,
                    tg_id=str(user.telegram_id),
                )
                sub.deleted_from_xui = False
            else:
                await self._sync_update_client_all_inbounds(
                    client,
                    server,
                    client_uuid=sub.xui_client_uuid,
                    email=sub.xui_email,
                    sub_id=sub.sub_id,
                    expiry_time_ms=expiry_ms,
                    limit_ip=total_limit_ip,
                    enable=is_still_active,
                    tg_id=str(user.telegram_id),
                )
        finally:
            if self._xui_override is None:
                await client.close()

        if was_expired:
            sub.started_at = now
        sub.expires_at = new_expires_at
        sub.limit_ip = total_limit_ip
        sub.is_active = is_still_active
        sub.subscription_url = build_subscription_url(sub.sub_id, server=server)
        if plan_id is not None:
            sub.current_plan_id = plan_id
        if not is_trial:
            sub.is_trial = False
        if is_still_active:
            sub.notified_3d = False
            sub.notified_1d = False
            sub.notified_expired = False
            sub.notified_winback = False
            sub.auto_renew_attempted = False

        if is_trial:
            user.has_used_trial = True

        await self.session.commit()
        await self.session.refresh(sub)
        return sub

    async def add_extra_device_slot(self, user: User, slots: int = 1) -> Subscription:
        """Увеличивает `limit_ip` в БД и во всех активных инбаундах 3X-UI."""
        res = await self.session.execute(
            select(Subscription).where(Subscription.user_id == user.id)
        )
        sub = res.scalar_one_or_none()
        if sub is None:
            raise ValueError("У вас пока нет активной подписки.")

        new_limit = sub.limit_ip + slots
        if new_limit > settings.MAX_LIMIT_IP or new_limit < 1:
            raise ValueError(f"Лимит устройств должен быть от 1 до {settings.MAX_LIMIT_IP}.")

        server = sub.server or await self._get_server()
        client = self._make_xui_client(server)
        try:
            await self._sync_update_client_all_inbounds(
                client,
                server,
                client_uuid=sub.xui_client_uuid,
                email=sub.xui_email,
                sub_id=sub.sub_id,
                expiry_time_ms=datetime_to_ms(sub.expires_at),
                limit_ip=new_limit,
                enable=sub.is_currently_valid,
                tg_id=str(user.telegram_id),
            )
        finally:
            if self._xui_override is None:
                await client.close()

        sub.limit_ip = new_limit
        sub.extra_devices = max(0, sub.extra_devices + slots)
        sub.ip_violations_count = 0
        await self.session.commit()
        await self.session.refresh(sub)
        return sub

    async def reduce_extra_device_slots(
        self, user: User, target_extra_devices: int = 0
    ) -> Subscription:
        """
        Уменьшает количество дополнительных слотов в БД и синхронно в обоих инбаундах 3X-UI.
        """
        res = await self.session.execute(
            select(Subscription).where(Subscription.user_id == user.id)
        )
        sub = res.scalar_one_or_none()
        if sub is None:
            raise ValueError("У вас пока нет активной подписки.")

        safe_extra = max(0, min(target_extra_devices, sub.extra_devices))
        new_limit = settings.DEFAULT_LIMIT_IP + safe_extra

        server = sub.server or await self._get_server()
        client = self._make_xui_client(server)
        try:
            await self._sync_update_client_all_inbounds(
                client,
                server,
                client_uuid=sub.xui_client_uuid,
                email=sub.xui_email,
                sub_id=sub.sub_id,
                expiry_time_ms=datetime_to_ms(sub.expires_at),
                limit_ip=new_limit,
                enable=sub.is_currently_valid,
                tg_id=str(user.telegram_id),
            )
        finally:
            if self._xui_override is None:
                await client.close()

        sub.extra_devices = safe_extra
        sub.limit_ip = new_limit
        await self.session.commit()
        await self.session.refresh(sub)
        return sub

    async def reissue_key(self, user: User, *, force_by_admin: bool = False) -> Subscription:
        """
        Перегенерирует UUID и subId одновременно в основном и резервном LTE-инбаундах 3X-UI
        с сохранением текущего срока действия `expires_at`.
        """
        res = await self.session.execute(
            select(Subscription).where(Subscription.user_id == user.id)
        )
        sub = res.scalar_one_or_none()
        if sub is None:
            raise ValueError("У вас ещё нет подписки для сброса ключа.")

        now = utcnow()
        if not force_by_admin and sub.last_key_reset_at is not None:
            last_reset = (
                sub.last_key_reset_at
                if sub.last_key_reset_at.tzinfo
                else sub.last_key_reset_at.replace(tzinfo=timezone.utc)
            )
            cooldown = timedelta(hours=settings.KEY_RESET_COOLDOWN_HOURS)
            if now - last_reset < cooldown:
                remaining = cooldown - (now - last_reset)
                hours_left = max(1, int(remaining.total_seconds() // 3600))
                raise ValueError(
                    f"Сброс ключа доступен не чаще 1 раза в {settings.KEY_RESET_COOLDOWN_HOURS} ч. "
                    f"Попробуйте снова примерно через {hours_left} ч."
                )

        server = sub.server or await self._get_server()
        old_uuid = sub.xui_client_uuid
        new_uuid = str(uuid.uuid4())
        new_sub_id = generate_sub_id(16)
        expiry_ms = datetime_to_ms(sub.expires_at)
        enable_flag = sub.is_currently_valid

        client = self._make_xui_client(server)
        try:
            await self._sync_delete_client_all_inbounds(
                client,
                server,
                client_uuid=old_uuid,
            )
            await self._sync_add_client_all_inbounds(
                client,
                server,
                client_uuid=new_uuid,
                email=sub.xui_email,
                sub_id=new_sub_id,
                expiry_time_ms=expiry_ms,
                limit_ip=sub.limit_ip,
                enable=enable_flag,
                tg_id=str(user.telegram_id),
            )
        finally:
            if self._xui_override is None:
                await client.close()

        sub.xui_client_uuid = new_uuid
        sub.sub_id = new_sub_id
        sub.last_key_reset_at = now
        sub.deleted_from_xui = False
        await self.session.commit()
        await self.session.refresh(sub)
        return sub

    async def set_client_enabled(self, sub: Subscription, enabled: bool) -> None:
        server = sub.server or await self._get_server()
        user_res = await self.session.execute(select(User).where(User.id == sub.user_id))
        user = user_res.scalar_one()

        client = self._make_xui_client(server)
        try:
            await self._sync_update_client_all_inbounds(
                client,
                server,
                client_uuid=sub.xui_client_uuid,
                email=sub.xui_email,
                sub_id=sub.sub_id,
                expiry_time_ms=datetime_to_ms(sub.expires_at),
                limit_ip=sub.limit_ip,
                enable=enabled,
                tg_id=str(user.telegram_id),
            )
        finally:
            if self._xui_override is None:
                await client.close()

        sub.is_active = enabled
        await self.session.commit()

    async def process_successful_payment(
        self,
        tx: Transaction,
        bot: "Bot | None" = None,
    ) -> tuple[bool, Subscription | None]:
        """
        Идемпотентная обработка успешного платежа (SUBSCRIPTION, TRIAL, EXTRA_DEVICE, AUTO_RENEW).
        """
        locked_tx_res = await self.session.execute(
            select(Transaction).where(Transaction.id == tx.id).with_for_update()
        )
        tx = locked_tx_res.scalar_one()

        if tx.status == TransactionStatus.COMPLETED:
            res = await self.session.execute(
                select(Subscription).where(Subscription.user_id == tx.user_id)
            )
            return True, res.scalar_one_or_none()

        locked_user_res = await self.session.execute(
            select(User).where(User.id == tx.user_id).with_for_update()
        )
        user = locked_user_res.scalar_one_or_none()
        plan: Plan | None = tx.plan
        if plan is None and tx.plan_id is not None:
            plan = await self.session.get(Plan, tx.plan_id)

        if user is None:
            raise RuntimeError(f"Транзакция #{tx.id} ссылается на несуществующего пользователя")

        if (
            tx.tx_type == TransactionType.TRIAL
            and tx.status == TransactionStatus.PENDING
            and user.has_used_trial
        ):
            tx.status = TransactionStatus.COMPLETED
            tx.paid_at = utcnow()
            await self.session.commit()
            res = await self.session.execute(
                select(Subscription).where(Subscription.user_id == user.id)
            )
            return True, res.scalar_one_or_none()

        first_time_paid = tx.status == TransactionStatus.PENDING
        referrer_bonus = Decimal("0.00")
        referrer_user: User | None = None

        if first_time_paid:
            tx.status = TransactionStatus.PAID_PENDING_PROVISION
            tx.paid_at = utcnow()
            if tx.tx_type == TransactionType.TRIAL:
                user.has_used_trial = True

            if tx.balance_used_rub > Decimal("0.00"):
                user.balance_rub = max(
                    Decimal("0.00"), user.balance_rub - tx.balance_used_rub
                )

            if tx.discount_percent > 0:
                user.pending_discount_percent = 0

            total_paid_value = tx.amount_rub + tx.balance_used_rub
            if user.referrer_id and total_paid_value > Decimal("0.00"):
                referrer_user = await self.session.get(User, user.referrer_id)
                if referrer_user is not None:
                    referrer_bonus = (
                        total_paid_value * Decimal(settings.REFERRAL_PERCENT) / Decimal(100)
                    ).quantize(Decimal("0.01"))
                    if referrer_bonus > Decimal("0.00"):
                        referrer_user.balance_rub += referrer_bonus

            await self.session.commit()

            if bot and referrer_user and referrer_bonus > Decimal("0.00"):
                try:
                    await bot.send_message(
                        chat_id=referrer_user.telegram_id,
                        text=(
                            f"🎉 <b>Приятный бонус по реферальной программе!</b>\n\n"
                            f"Ваш друг оплатил услуги GhostVPN. Вам начислено <b>+{referrer_bonus} ₽</b> "
                            f"({settings.REFERRAL_PERCENT}%) на внутренний баланс!\n"
                            f"💰 Доступный баланс: <b>{referrer_user.balance_rub} ₽</b>"
                        ),
                        parse_mode="HTML",
                    )
                except Exception as exc:
                    logger.warning("Не удалось уведомить реферера %s: %s", referrer_user.telegram_id, exc)

            item_title = (
                f"Тариф {plan.name} ({plan.days} дн.)"
                if plan
                else (
                    f"Пробный период ({settings.TRIAL_DAYS} дн.)"
                    if tx.tx_type == TransactionType.TRIAL
                    else f"+{tx.extra_devices_qty or 1} устр."
                )
            )
            await send_admin_alert(
                bot,
                (
                    f"💰 <b>Новая оплата #{tx.id} ({tx.tx_type.value})</b>\n"
                    f"👤 Пользователь: <code>{user.telegram_id}</code> (@{user.username or 'нет'})\n"
                    f"📦 Покупка: <b>{item_title}</b>\n"
                    f"💳 Метод: <b>{tx.provider.value} ({tx.currency})</b>\n"
                    f"💵 Сумма: <b>{tx.amount_rub} ₽</b> (с баланса: {tx.balance_used_rub} ₽)"
                ),
            )

        try:
            if tx.tx_type == TransactionType.EXTRA_DEVICE:
                sub = await self.add_extra_device_slot(
                    user=user, slots=max(1, tx.extra_devices_qty or 1)
                )
            elif tx.tx_type == TransactionType.TRIAL:
                sub = await self.grant_or_extend_subscription(
                    user=user,
                    days=settings.TRIAL_DAYS,
                    limit_ip=settings.DEFAULT_LIMIT_IP,
                    is_trial=True,
                )
            else:
                if plan is None:
                    raise RuntimeError(f"Транзакция #{tx.id} не содержит тарифа")
                sub = await self.grant_or_extend_subscription(
                    user=user,
                    days=plan.days,
                    limit_ip=plan.limit_ip,
                    is_trial=False,
                    plan_id=plan.id,
                )
            tx.status = TransactionStatus.COMPLETED
            await self.session.commit()
            return True, sub
        except Exception as exc:
            logger.error(
                "Сбой выдачи 3X-UI для оплаченной транзакции #%s (оставлена в PAID_PENDING_PROVISION): %s",
                tx.id,
                exc,
            )
            await send_admin_alert(
                bot,
                (
                    f"⚠️ <b>Задержка выдачи ключа (PAID_PENDING_PROVISION)</b>\n"
                    f"Транзакция #{tx.id} для <code>{user.telegram_id}</code> оплачена, "
                    f"но панель 3X-UI вернула ошибку:\n<code>{exc}</code>\n"
                    f"Фоновый воркер автоматически повторит выдачу через 30 сек."
                ),
                critical=True,
            )
            return False, None
