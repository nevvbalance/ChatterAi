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
    """Simple API connectivity test using the collections endpoint."""
    return await _get("/v1/collections/")


async def get_rent_numbers() -> Any:
    """Get anonymous numbers currently available for rent."""
    return await _get("/v1/rent/numbers/")


async def get_numbers_history() -> Any:
    """Get rental history for anonymous numbers."""
    return await _get("/v1/rent/numbers/history/")


def _format_value(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)
    if isinstance(value, list):
        return f"список из {len(value)} элементов"
    return "объект"


def format_result(data: Any) -> str:
    """Turn a generic Marketapp response into a compact Telegram message."""
    if isinstance(data, list):
        return f"Marketapp API ответил успешно. Получено объектов: {len(data)}"

    if isinstance(data, dict):
        if not data:
            return "Marketapp API ответил успешно, но вернул пустой объект."

        parts = [f"{key}: {_format_value(value)}" for key, value in list(data.items())[:8]]
        return "Marketapp API ответил успешно:\n\n" + "\n".join(parts)

    return f"Marketapp API ответил успешно:\n\n{str(data)[:1500]}"


def format_debug(data: Any) -> list[str]:
    """Return the raw API response in Telegram-safe chunks for diagnostics."""
    if isinstance(data, str):
        raw = data
    else:
        raw = json.dumps(data, ensure_ascii=False, indent=2, default=str)

    chunks: list[str] = []
    for start in range(0, len(raw), TELEGRAM_MESSAGE_LIMIT):
        chunks.append(raw[start:start + TELEGRAM_MESSAGE_LIMIT])
    return chunks or ["<пустой ответ>"]


def _format_duration(seconds: Any) -> str:
    """Convert a Marketapp duration in seconds into a human-readable period."""
    try:
        days = int(seconds) // 86400
    except (TypeError, ValueError):
        return str(seconds)

    if days == 1:
        return "1 день"
    if 2 <= days <= 4:
        return f"{days} дня"
    return f"{days} дней"


def _short_address(address: Any) -> str:
    """Keep blockchain addresses recognizable without filling the Telegram message."""
    if not isinstance(address, str) or len(address) < 12:
        return str(address)
    return f"{address[:6]}…{address[-6:]}"


def _extract_items(data: Any) -> tuple[list[Any], bool]:
    """Extract the rental list and indicate whether the response is a plain list."""
    if isinstance(data, list):
        return data, True
    if isinstance(data, dict):
        items = next((value for value in data.values() if isinstance(value, list)), None)
        if items is not None:
            return items, False
    return [], False


def _format_number_item(index: int, item: Any) -> str:
    if not isinstance(item, dict):
        return f"<b>{index}. 📱 {html.escape(str(item))}</b>"

    number = html.escape(
        str(item.get("nft_name") or item.get("name") or item.get("number") or "Без номера")
    )
    min_duration = item.get("min_duration")
    max_duration = item.get("max_duration")
    owner = item.get("owner")
    nft_address = item.get("nft_address")

    price = item.get("price")
    if price is None:
        price = item.get("rent_price")
    if price is None:
        price = item.get("price_ton")

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
        lines.append(f"   💰 Цена: <b>{html.escape(str(price))} TON</b>")

    lines.append(f"   ⏱ {duration}")

    if owner:
        lines.append(f"   👤 <code>{html.escape(_short_address(owner))}</code>")
    if nft_address:
        lines.append(f"   🔗 <code>{html.escape(_short_address(nft_address))}</code>")

    return "\n".join(lines)


def format_numbers(data: Any) -> list[str]:
    """Format all rental numbers into Telegram-safe messages, never exceeding 4096 chars."""
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
