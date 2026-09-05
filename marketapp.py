import os
from typing import Any

import aiohttp


BASE_URL = "https://api.marketapp.org"


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
                return await response.json()
            except Exception:
                return text


async def get_collections() -> Any:
    """Simple API connectivity test using the collections endpoint."""
    return await _get("/v1/collections/")


async def get_rent_numbers() -> Any:
    """Get anonymous numbers currently available for rent."""
    return await _get("/v1/rent/numbers/")


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


def format_numbers(data: Any) -> str:
    """Format all available rental numbers as a readable Telegram list."""
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = next((value for value in data.values() if isinstance(value, list)), None)
        if items is None:
            return format_result(data)
    else:
        return format_result(data)

    if not items:
        return "📱 Сейчас доступных номеров для аренды не найдено."

    lines = [
        f"📱 Номера в аренде: {len(items)}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            lines.append(f"\n{index}. 📱 {item}")
            continue

        number = item.get("nft_name") or item.get("name") or "Без номера"
        min_duration = item.get("min_duration")
        max_duration = item.get("max_duration")
        owner = item.get("owner")
        nft_address = item.get("nft_address")

        if min_duration is not None and max_duration is not None:
            duration = f"{_format_duration(min_duration)} → {_format_duration(max_duration)}"
        elif min_duration is not None:
            duration = f"от {_format_duration(min_duration)}"
        elif max_duration is not None:
            duration = f"до {_format_duration(max_duration)}"
        else:
            duration = "срок не указан"

        lines.append(f"\n{index}. 📱 <b>{number}</b>")
        lines.append(f"   ⏱ {duration}")

        if owner:
            lines.append(f"   👤 Владелец: <code>{_short_address(owner)}</code>")
        if nft_address:
            lines.append(f"   🔗 NFT: <code>{_short_address(nft_address)}</code>")

    return "\n".join(lines)
