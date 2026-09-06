import html
import json
import os
import time
from typing import Any

import aiohttp


BASE_URL = "https://api.marketapp.org"
TELEGRAM_MESSAGE_LIMIT = 4096
SHEETS_WEBHOOK_URL = os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL", "").strip()
SHEETS_SECRET = os.environ.get("GOOGLE_SHEETS_SECRET", "").strip()

_sheets_pool_previous: dict[str, dict[str, Any]] | None = None
_sheets_ever_seen: set[str] = set()
_sheets_history_seen: set[str] = set()
_sheets_history_initialized = False


async def _post_sheets(payload: dict[str, Any]) -> None:
    """Best-effort sync to the Google Sheets Apps Script webhook."""
    if not SHEETS_WEBHOOK_URL or not SHEETS_SECRET:
        print("Google Sheets sync: disabled, webhook/secret is missing", flush=True)
        return

    body = dict(payload)
    body["secret"] = SHEETS_SECRET
    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                SHEETS_WEBHOOK_URL,
                json=body,
                headers={"Content-Type": "application/json"},
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    print(
                        f"Google Sheets sync HTTP {response.status}: {text[:500]}",
                        flush=True,
                    )
                else:
                    # Apps Script can return HTTP 200 even when doPost() reports
                    # an application-level error. Log the response body so the
                    # Render logs reveal the real webhook result.
                    print(
                        f"Google Sheets sync: HTTP {response.status}, response={text[:500]}",
                        flush=True,
                    )
    except Exception as exc:
        # Sheets must never break the Marketapp monitor itself.
        print(f"Google Sheets sync ERROR: {exc!r}", flush=True)


async def _sync_pool_to_sheets(data: Any) -> None:
    """Send only meaningful pool changes, with a first-run snapshot."""
    global _sheets_pool_previous

    items, _ = _extract_items(data)
    current = build_number_snapshot(data)

    if _sheets_pool_previous is None:
        _sheets_pool_previous = current
        _sheets_ever_seen.update(current)
        for item in items:
            if not isinstance(item, dict):
                continue
            price, currency = number_price(item)
            await _post_sheets({
                "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                "number": number_name(item),
                "event": "pool_snapshot",
                "price": price,
                "currency": currency,
                "duration": item.get("min_duration") or item.get("max_duration"),
                "tx_hash": "",
                "source": "Marketapp pool",
            })
        print(f"Google Sheets pool baseline synced: {len(current)} number(s)", flush=True)
        return

    previous = _sheets_pool_previous
    current_keys = set(current)
    previous_keys = set(previous)

    for key in sorted(current_keys - previous_keys):
        item = current[key]
        await _post_sheets({
            "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "number": item["name"],
            "event": "returned" if key in _sheets_ever_seen else "new",
            "price": item.get("price"),
            "currency": item.get("currency", ""),
            "duration": None,
            "tx_hash": "",
            "source": "Marketapp pool",
        })

    for key in sorted(previous_keys - current_keys):
        item = previous[key]
        await _post_sheets({
            "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "number": item["name"],
            "event": "removed",
            "price": item.get("price"),
            "currency": item.get("currency", ""),
            "duration": None,
            "tx_hash": "",
            "source": "Marketapp pool",
        })

    for key in sorted(current_keys & previous_keys):
        old = previous[key]
        new = current[key]
        if old.get("price") != new.get("price") or old.get("currency") != new.get("currency"):
            await _post_sheets({
                "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                "number": new["name"],
                "event": "price_change",
                "price": new.get("price"),
                "currency": new.get("currency", ""),
                "duration": None,
                "tx_hash": "",
                "source": "Marketapp pool",
            })

    _sheets_ever_seen.update(current_keys)
    _sheets_pool_previous = current


async def _sync_history_to_sheets(data: Any) -> None:
    """Send new rent-history events; first run also seeds recent history."""
    global _sheets_history_initialized

    history = extract_history_items(data)
    current_ids = [history_event_key(item) for item in history]

    if not _sheets_history_initialized:
        _sheets_history_seen.update(current_ids)
        _sheets_history_initialized = True
        seed = history[:20]
        for item in reversed(seed):
            await _post_history_item_to_sheets(item, "history_snapshot")
        print(f"Google Sheets history baseline synced: {len(seed)} event(s)", flush=True)
        return

    new_items: list[dict[str, Any]] = []
    for item, event_id in zip(history, current_ids):
        if event_id not in _sheets_history_seen:
            new_items.append(item)

    _sheets_history_seen.update(current_ids)
    for item in reversed(new_items):
        await _post_history_item_to_sheets(
            item,
            "rent_extended" if item.get("is_extend") else "rent",
        )

    if new_items:
        print(f"Google Sheets history synced: {len(new_items)} new event(s)", flush=True)


async def _post_history_item_to_sheets(item: dict[str, Any], event: str) -> None:
    timestamp = item.get("ts")
    if isinstance(timestamp, (int, float)):
        formatted_time = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(timestamp))
    else:
        formatted_time = str(timestamp or "")

    await _post_sheets({
        "time": formatted_time,
        "number": item.get("name") or item.get("address") or "",
        "event": event,
        "price": item.get("price"),
        "currency": item.get("currency") or "",
        "duration": item.get("duration"),
        "tx_hash": item.get("tx_hash") or "",
        "source": "Marketapp history",
    })


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
    data = await _get("/v1/rent/numbers/")
    await _sync_pool_to_sheets(data)
    return data


async def get_numbers_history() -> Any:
    data = await _get("/v1/rent/numbers/history/")
    await _sync_history_to_sheets(data)
    return data


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


def extract_history_items(data: Any) -> list[dict[str, Any]]:
    """Extract rent-history events from the API response."""
    items, _ = _extract_items(data)
    return [item for item in items if isinstance(item, dict)]


def history_event_key(item: dict[str, Any]) -> str:
    """Return a stable id for a rent-history event."""
    for key in ("tx_hash", "id", "event_id"):
        value = item.get(key)
        if value:
            return str(value)
    return "|".join(
        str(item.get(key, ""))
        for key in ("address", "ts", "src", "dst", "price", "duration", "is_extend")
    )


def format_history_monitor_event(item: dict[str, Any]) -> str:
    """Format a rent-history event that was not present in the previous poll."""
    name = html.escape(str(item.get("name") or item.get("address") or "номер"))
    price = item.get("price")
    currency = html.escape(str(item.get("currency") or ""))
    price_text = f"\n💰 Цена: <b>{html.escape(str(price))} {currency}</b>" if price is not None else ""
    duration = item.get("duration")
    duration_text = f"\n⏱ Срок: {_format_duration(duration)}" if duration is not None else ""
    event_text = "Продление аренды" if item.get("is_extend") else "Аренда номера"
    tx_hash = html.escape(str(item.get("tx_hash") or ""))
    tx_text = f"\n🔗 TX: <code>{tx_hash}</code>" if tx_hash else ""
    return f"📱 <b>{event_text}</b>\nНомер: <b>{name}</b>{price_text}{duration_text}{tx_text}"
