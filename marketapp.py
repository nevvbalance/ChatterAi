import os
from typing import Any

import aiohttp


BASE_URL = "https://api.marketapp.org"


async def get_collections() -> Any:
    """Make a simple authenticated request to Marketapp API."""
    token = os.environ.get("MARKETAPP_API_TOKEN")
    if not token:
        raise RuntimeError("MARKETAPP_API_TOKEN is not configured")

    headers = {"Authorization": token}
    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{BASE_URL}/v1/collections/", headers=headers) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"Marketapp API HTTP {response.status}: {text[:500]}")

            try:
                return await response.json()
            except Exception:
                return text


def format_result(data: Any) -> str:
    """Turn the API response into a compact Telegram-friendly message."""
    if isinstance(data, list):
        return f"Marketapp API ответил успешно. Получено объектов: {len(data)}"

    if isinstance(data, dict):
        if not data:
            return "Marketapp API ответил успешно, но вернул пустой объект."

        # Show useful top-level information without dumping a huge JSON response.
        parts = []
        for key, value in list(data.items())[:8]:
            if isinstance(value, (str, int, float, bool)) or value is None:
                parts.append(f"{key}: {value}")
            elif isinstance(value, list):
                parts.append(f"{key}: список из {len(value)} элементов")
            else:
                parts.append(f"{key}: объект")
        return "Marketapp API ответил успешно:\n\n" + "\n".join(parts)

    return f"Marketapp API ответил успешно:\n\n{str(data)[:1500]}"
