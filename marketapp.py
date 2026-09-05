import html
import json
import os
from typing import Any

import aiohttp


BASE_URL = "https://api.marketapp.org"
TELEGRAM_MESSAGE_LIMIT = 4096


async def _get(endpoint: str) -> Any:
    """Make an authenticated GET request to Marketapp API."""
    token = os.environ.get("MARKETAPP_API_TOKEN")
    if not token:
        raise RuntimeError("MARKETAPP_API_TOKEN is not configured")

    headers = {"Authorization": token}
    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{BASE_URL}{endpoint}", headers=headers) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"Marketapp API HTTP {response.status}: {text[:500]}")

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text


async def get_collections() -> Any:
    return await _get("/v1/collections/")


async def get_rent_numbers() -> Any:
    return await _get("/v1/rent/numbers/")


async def get_numbers_history() -> Any:
    return await _get("/v1/rent/numbers/history/")


def _format_value(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)
    if isinstance(value, list):
        return f"список из {len(value)} элементов"
    return "объект"


def format_result(data: Any) -> str:
    if isinstance(data, list):
        return f"Marketapp API ответил успешно. Получено объектов: {len(data)}"

    if isinstance(data, dict):
        if not data:
            return "Marketapp API ответил успешно, но вернул пустой объект."

        parts = [f"{key}: {_format_value(value)}" for key, value in list(data.items())[:8]]
        return "Marketapp API ответил успешно:\n\n" + "\n".join(parts)

    return f"Marketapp API ответил успешно:\n\n{str(data)[:1500]}"


def format_debug(data: Any) -> list[str]:
    if isinstance(data, str):
        raw = data
    else:
        raw = json.dumps(data, ensure_ascii=False, indent=2, default=str)

    chunks: list[str] = []
    for start in range(0, len(raw), TELEGRAM_MESSAGE_LIMIT):
        chunks.append(raw[start:start + TELEGRAM_MESSAGE_LIMIT])
    return chunks or ["<пустой ответ>"]


def _format_duration(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return str(seconds)

    days = total // 86400
    if days == 0:
        hours = total // 3600
        return f"{hours} ч."
    if days == 1:
        return "1 день"
    if 2 <= days <= 4:
        return f"{days} дня"
    return f"{days} дней"


def _short_address(address: Any) -> str:
    if not isinstance(address, str) or len(address) < 12:
        return str(address)
    return f"{address[:6]}…{address[-6:]}"


def _extract_items(data: Any) -> tuple[list[Any], bool]:
    """Extract an API item list from common response shapes."""
    if isinstance(data, list):
        return data, True
    if not isinstance(data, dict):
        return [], False

    for key in ("items", "results", "data", "nfts", "numbers"):
        value = data.get(key)
        if isinstance(value, list):
            return value, False

    for value in data.values():
        if isinstance(value, list):
            return value, False

    return [], False


def number_key(item: Any) -> str:
    """Return a stable identifier for an available number."""
    if not isinstance(item, dict):
        return str(item)
    return str(
        item.get("address")
        or item.get("nft_address")
        or item.get("id")
        or item.get("name")
        or item.get("number")
        or "unknown"
    )


def number_name(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    return str(item.get("name") or item.get("nft_name") or item.get("number") or number_key(item))


def number_price(item: Any) -> tuple[Any, str]:
    if not isinstance(item, dict):
        return None, ""
    for key in ("price", "rent_price", "price_ton", "price_gram"):
        if item.get(key) is not None:
            return item.get(key), str(item.get("currency") or ("GRAM" if key == "price_gram" else "TON"))
    return None, str(item.get("currency") or "")


def _format_number_item(index: int, item: Any) -> str:
    if not isinstance(item, dict):
        return f"<b>{index}. 📱 {html.escape(str(item))}</b>"

    number = html.escape(number_name(item))
    min_duration = item.get("min_duration")
    max_duration = item.get("max_duration")
    owner = item.get("owner")
    nft_address = item.get("nft_address") or item.get("address")
    price, currency = number_price(item)

    if min_duration is not None and max_duration is not None:
        duration = f"{_format_duration(min_duration)} → {_format_duration(max_duration)}"
    elif min_duration is not None:
        duration = f"от {_format_duration(min_duration)}"
    elif max_duration is not None:
        duration = f"до {_format_duration(max_duration)}"
    else:
        duration = "срок не указан"

    lines = [f"<b>{index}. 📱 {number}</b>"]
    if price is not None:
        suffix = f" {html.escape(currency)}" if currency else ""
        lines.append(f"   💰 Цена: <b>{html.escape(str(price))}{suffix}</b>")
    lines.append(f"   ⏱ {duration}")

    if owner:
        lines.append(f"   👤 <code>{html.escape(_short_address(owner))}</code>")
    if nft_address:
        lines.append(f"   🔗 <code>{html.escape(_short_address(nft_address))}</code>")

    return "\n".join(lines)


def format_numbers(data: Any) -> list[str]:
    items, _ = _extract_items(data)
    if not items:
        return [format_result(data)]

    header = f"📱 <b>Доступно номеров: {len(items)}</b>\n━━━━━━━━━━━━━━━━━━━━"
    chunks: list[str] = []
    current = header

    for index, item in enumerate(items, 1):
        entry = _format_number_item(index, item)
        candidate = f"{current}\n\n{entry}"
        if len(candidate) > TELEGRAM_MESSAGE_LIMIT:
            chunks.append(current)
            current = entry
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


def build_number_snapshot(data: Any) -> dict[str, dict[str, Any]]:
    """Build a stable snapshot of the currently available number pool."""
    items, _ = _extract_items(data)
    snapshot: dict[str, dict[str, Any]] = {}
    for item in items:
        key = number_key(item)
        if key == "unknown":
            continue
        price, currency = number_price(item)
        snapshot[key] = {
            "name": number_name(item),
            "price": str(price) if price is not None else None,
            "currency": currency,
            "item": item,
        }
    return snapshot


def format_monitor_event(kind: str, current: dict[str, Any] | None, previous: dict[str, Any] | None = None) -> str:
    """Format a single pool-change event for Telegram."""
    item = current or previous or {}
    name = html.escape(str(item.get("name") or "номер"))
    price = item.get("price")
    currency = html.escape(str(item.get("currency") or ""))
    price_text = f"\n💰 Цена: <b>{html.escape(str(price))} {currency}</b>" if price is not None else ""

    if kind == "new":
        return f"🟢 <b>Новый номер в пуле</b>\n📱 {name}{price_text}"
    if kind == "removed":
        return f"🔴 <b>Номер исчез из пула</b>\n📱 {name}\nВозможная причина: аренда или снятие с аренды.{price_text}"
    if kind == "returned":
        return f"🔵 <b>Номер вернулся в пул</b>\n📱 {name}{price_text}"
    if kind == "price":
        old_price = previous.get("price") if previous else None
        old_currency = previous.get("currency") if previous else ""
        return (
            f"🟡 <b>Изменилась цена</b>\n📱 {name}\n"
            f"Было: <b>{html.escape(str(old_price))} {html.escape(str(old_currency))}</b>\n"
            f"Стало: <b>{html.escape(str(price))} {currency}</b>"
        )
    return f"ℹ️ <b>Изменение номера</b>\n📱 {name}{price_text}"
