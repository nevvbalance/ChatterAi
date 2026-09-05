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


def format_numbers(data: Any) -> str:
    """Format the available-rental response without exposing a huge raw JSON dump."""
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # The API may wrap the items under a common collection key.
        items = next((value for value in data.values() if isinstance(value, list)), None)
        if items is None:
            return format_result(data)
    else:
        return format_result(data)

    if not items:
        return "📱 Сейчас доступных номеров для аренды не найдено."

    lines = [f"📱 Доступно номеров: {len(items)}", ""]
    for index, item in enumerate(items[:10], 1):
        if isinstance(item, dict):
            useful = []
            for key, value in item.items():
                if isinstance(value, (str, int, float, bool)) or value is None:
                    useful.append(f"{key}: {value}")
                if len(useful) >= 5:
                    break
            details = " | ".join(useful) if useful else "данные объекта доступны"
            lines.append(f"{index}. {details}")
        else:
            lines.append(f"{index}. {item}")

    if len(items) > 10:
        lines.append(f"\nПоказаны первые 10 из {len(items)}.")

    return "\n".join(lines)
