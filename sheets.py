import time
from typing import Any

import aiohttp

from marketapp import format_sheet_duration

SHEETS_TIMEOUT = aiohttp.ClientTimeout(total=15)


class SheetsSync:
    """Sink that mirrors monitor events into the Google Sheets Apps Script webhook."""

    def __init__(self, webhook_url: str, secret: str) -> None:
        self.webhook_url = (webhook_url or "").strip()
        self.secret = (secret or "").strip()
        self._session: aiohttp.ClientSession | None = None
        self.sent = 0
        self.failed = 0

    def bind_session(self, session: aiohttp.ClientSession | None) -> None:
        self._session = session

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url and self.secret)

    async def publish(self, events: list[dict[str, Any]]) -> None:
        if not self.enabled:
            return
        for event in events:
            payload = self._payload(event)
            if payload is not None:
                await self._post(payload)

    def _payload(self, event: dict[str, Any]) -> dict[str, Any] | None:
        source = event.get("source")
        kind = event.get("kind")
        if source == "pool":
            return self._pool_payload(event.get("item") or {}, kind)
        if source == "history":
            return self._history_payload(event.get("item") or {}, kind)
        return None

    def _pool_payload(self, item: dict[str, Any], kind: Any) -> dict[str, Any] | None:
        common = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "number": item.get("name", ""),
            "price": item.get("price"),
            "currency": item.get("currency", ""),
            "address": item.get("address", ""),
            "tx_hash": "",
            "source": "Marketapp pool",
        }
        if kind == "baseline":
            return {
                **common,
                "event": "pool_snapshot",
                "duration": format_sheet_duration(item.get("min_duration") or item.get("max_duration")),
            }
        if kind in ("new", "returned", "removed"):
            return {**common, "event": kind, "duration": ""}
        if kind == "price":
            return {**common, "event": "price_change", "duration": ""}
        return None

    def _history_payload(self, item: dict[str, Any], kind: Any) -> dict[str, Any] | None:
        if kind not in ("history_snapshot", "rent", "rent_extended"):
            return None

        timestamp = item.get("ts")
        if isinstance(timestamp, (int, float)):
            formatted_time = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(timestamp))
        else:
            formatted_time = str(timestamp or "")

        return {
            "time": formatted_time,
            "number": item.get("name") or item.get("number") or item.get("address") or "",
            "event": kind,
            "price": item.get("price") or item.get("price_nano"),
            "currency": item.get("currency") or ("TON" if item.get("price_nano") is not None else ""),
            "duration": format_sheet_duration(item.get("duration")),
            "address": item.get("address") or "",
            "tx_hash": item.get("tx_hash") or "",
            "source": "Marketapp history",
        }

    async def _post(self, payload: dict[str, Any]) -> None:
        body = dict(payload)
        body["secret"] = self.secret
        headers = {"Content-Type": "application/json"}
        try:
            if self._session is not None and not self._session.closed:
                async with self._session.post(self.webhook_url, json=body, headers=headers) as response:
                    await self._report(response)
            else:
                async with aiohttp.ClientSession(timeout=SHEETS_TIMEOUT) as session:
                    async with session.post(self.webhook_url, json=body, headers=headers) as response:
                        await self._report(response)
            self.sent += 1
        except Exception as exc:
            self.failed += 1
            print(f"Google Sheets sync ERROR: {exc!r}", flush=True)

    async def _report(self, response: aiohttp.ClientResponse) -> None:
        text = await response.text()
        if response.status >= 400:
            self.failed += 1
            print(f"Google Sheets sync HTTP {response.status}: {text[:200]}", flush=True)
        else:
            print(f"Google Sheets sync: HTTP {response.status}, response={text[:200]}", flush=True)
