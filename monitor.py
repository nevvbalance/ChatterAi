import asyncio
import os
import time
from typing import Any, Awaitable, Callable

import state as state_store
from marketapp import (
    build_number_snapshot,
    extract_history_items,
    format_history_monitor_event,
    get_numbers_history,
    get_rent_numbers,
    history_event_key,
)

Event = dict[str, Any]
Publisher = Callable[[list[Event]], Awaitable[None]]
Notifier = Callable[[str], Awaitable[None]]
Drain = Callable[[], Awaitable[None]]

STARTUP_HISTORY_SAMPLE = int(os.environ.get("MONITOR_STARTUP_HISTORY_SAMPLE", "5"))
SHEETS_HISTORY_BASELINE = 20
EVER_SEEN_LIMIT = 20000


class MarketMonitor:
    """Single source of truth for pool/history diffing, persistence and delivery."""

    def __init__(self, interval: int, lookback: int) -> None:
        self.interval = interval
        self.lookback = lookback
        self.chats: set[int] = set()
        self.previous: dict[str, dict[str, Any]] | None = None
        self.ever_seen: set[str] = set()
        self.history_seen: set[str] = set()
        self.history_initialized = False
        self.publisher: Publisher | None = None
        self.notifier: Notifier | None = None
        self.drain: Drain | None = None
        self.task: asyncio.Task | None = None
        self.running = False
        self.cycle = 0
        self.last_poll_at: float | None = None
        self.last_pool_count = 0
        self.last_history_count = 0
        self.last_history_new = 0
        self.last_pool_events = 0
        self.last_error: str | None = None
        self.started_at: float | None = None
        self.persistence_ok = True
        self.state_restored = False
        self.startup_pending = False
        self.startup_sample: list[str] = []

    def load(self) -> None:
        data = state_store.load_state()
        stored_chats = data.get("chats")
        if isinstance(stored_chats, list):
            for value in stored_chats:
                try:
                    self.chats.add(int(value))
                except (TypeError, ValueError):
                    continue

        previous = data.get("pool_previous")
        if isinstance(previous, dict) and previous:
            self.previous = {str(key): value for key, value in previous.items() if isinstance(value, dict)}
            self.state_restored = True

        ever_seen = data.get("ever_seen")
        if isinstance(ever_seen, list):
            self.ever_seen.update(str(value) for value in ever_seen)

        history_seen = data.get("history_seen")
        if isinstance(history_seen, list):
            self.history_seen.update(str(value) for value in history_seen)

        self.history_initialized = bool(data.get("history_initialized")) and bool(self.history_seen)
        print(
            f"State loaded: chats={sorted(self.chats)}, pool={len(self.previous or {})}, "
            f"ever_seen={len(self.ever_seen)}, history_seen={len(self.history_seen)}, "
            f"history_initialized={self.history_initialized}",
            flush=True,
        )

    def save(self) -> None:
        payload = {
            "chats": sorted(self.chats),
            "pool_previous": self.previous or {},
            "ever_seen": sorted(self.ever_seen)[-EVER_SEEN_LIMIT:],
            "history_seen": sorted(self.history_seen)[-self.lookback * 3:],
            "history_initialized": self.history_initialized,
            "cycle": self.cycle,
        }
        self.persistence_ok = state_store.save_state(payload)

    def add_chat(self, chat_id: int) -> None:
        self.chats.add(chat_id)
        self.save()

    def remove_chat(self, chat_id: int) -> None:
        self.chats.discard(chat_id)
        self.save()

    def start(self, publisher: Publisher, notifier: Notifier, drain: Drain) -> None:
        self.publisher = publisher
        self.notifier = notifier
        self.drain = drain
        self.started_at = time.time()
        if self.task and not self.task.done():
            print("Marketapp monitor start requested, but task is already running", flush=True)
            return
        self.task = asyncio.create_task(self.loop())
        print(f"Marketapp monitor task created: {self.task!r}", flush=True)

    async def stop(self) -> None:
        self.running = False
        task = self.task
        self.task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.save()

    async def loop(self) -> None:
        self.running = True
        print(f"Marketapp monitor started, interval={self.interval}s, chats={sorted(self.chats)}", flush=True)
        try:
            while self.running:
                await self.poll()
                print(f"Marketapp monitor: next poll in {self.interval}s", flush=True)
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            print("Marketapp monitor cancelled", flush=True)
            raise
        except Exception as exc:
            self.last_error = f"loop: {exc!r}"
            print(f"Marketapp monitor loop CRASHED: {exc!r}", flush=True)
        finally:
            self.running = False
            print("Marketapp monitor stopped", flush=True)

    async def poll(self) -> list[Event]:
        self.cycle += 1
        self.last_poll_at = time.time()
        self.last_pool_events = 0
        print(f"Marketapp monitor poll #{self.cycle}: checking pool and history...", flush=True)

        events = await self.detect()
        if events and self.publisher is not None:
            await self.publisher(events)
        await self.flush_startup()
        if self.drain is not None:
            await self.drain()
        self.save()
        return events

    async def detect(self) -> list[Event]:
        events = await self.detect_history()
        events.extend(await self.detect_pool())
        return events

    async def detect_history(self) -> list[Event]:
        try:
            data = await get_numbers_history()
            history = extract_history_items(data)[:self.lookback]
            self.last_error = None
        except Exception as exc:
            self.last_error = f"history: {exc!r}"
            print(f"Marketapp history poll ERROR: {exc!r}", flush=True)
            return []

        self.last_history_count = len(history)
        current_ids = [history_event_key(item) for item in history]

        if not self.history_initialized:
            self.history_initialized = True
            self.history_seen.update(current_ids)
            self.last_history_new = 0
            print(f"Marketapp history baseline: {len(history)} recent event(s)", flush=True)
            self.startup_sample = [format_history_monitor_event(item) for item in reversed(history[:STARTUP_HISTORY_SAMPLE])]
            self.startup_pending = True
            return [
                {"source": "history", "kind": "history_snapshot", "key": key, "item": item, "previous": None}
                for item, key in zip(history[:SHEETS_HISTORY_BASELINE], current_ids[:SHEETS_HISTORY_BASELINE])
            ]

        new_pairs = [(item, key) for item, key in zip(history, current_ids) if key not in self.history_seen]
        self.history_seen.update(current_ids)
        if len(self.history_seen) > self.lookback * 3:
            self.history_seen = set(current_ids)
        self.last_history_new = len(new_pairs)

        print(
            f"Marketapp history poll #{self.cycle}: received {len(history)} event(s), "
            f"new={len(new_pairs)}",
            flush=True,
        )

        return [
            {
                "source": "history",
                "kind": "rent_extended" if item.get("is_extend") else "rent",
                "key": key,
                "item": item,
                "previous": None,
            }
            for item, key in reversed(new_pairs)
        ]

    async def detect_pool(self) -> list[Event]:
        try:
            data = await get_rent_numbers()
            current = build_number_snapshot(data)
            self.last_error = None
        except Exception as exc:
            self.last_error = f"pool: {exc!r}"
            print(f"Marketapp pool poll #{self.cycle} ERROR: {exc!r}", flush=True)
            return []

        self.last_pool_count = len(current)
        print(
            f"Marketapp pool poll #{self.cycle}: received {len(current)} number(s)",
            flush=True,
        )

        if self.previous is None:
            self.previous = current
            self.ever_seen.update(current)
            print(f"Marketapp pool baseline: {len(current)} number(s)", flush=True)
            if not self.state_restored:
                self.startup_pending = True
            return [
                {"source": "pool", "kind": "baseline", "key": key, "item": value, "previous": None}
                for key, value in sorted(current.items())
            ]

        previous = self.previous
        current_keys = set(current)
        previous_keys = set(previous)
        events: list[Event] = []

        for key in sorted(current_keys - previous_keys):
            kind = "returned" if key in self.ever_seen else "new"
            events.append({"source": "pool", "kind": kind, "key": key, "item": current[key], "previous": None})

        for key in sorted(previous_keys - current_keys):
            events.append({"source": "pool", "kind": "removed", "key": key, "item": previous[key], "previous": None})

        for key in sorted(current_keys & previous_keys):
            old = previous[key]
            new = current[key]
            if old.get("price") != new.get("price") or old.get("currency") != new.get("currency"):
                events.append({"source": "pool", "kind": "price", "key": key, "item": new, "previous": old})

        self.ever_seen.update(current_keys)
        self.previous = current
        self.last_pool_events = len(events)

        if events:
            print(f"Marketapp pool poll #{self.cycle}: detected {len(events)} pool event(s)", flush=True)
        else:
            print(f"Marketapp pool poll #{self.cycle}: no pool changes", flush=True)

        return events

    async def flush_startup(self) -> None:
        if not self.startup_pending:
            return
        self.startup_pending = False
        history_sample = self.startup_sample
        self.startup_sample = []
        if self.notifier is None or not self.chats:
            return
        lines = [
            "🟢 <b>Монитор Marketapp запущен</b>",
            f"📱 номеров в пуле: <b>{self.last_pool_count}</b>",
            f"🧾 событий в истории: <b>{self.last_history_count}</b>",
            f"💾 состояние: <b>{'восстановлено' if self.state_restored else 'новый старт'}</b>",
            f"💬 чатов на связи: <b>{len(self.chats)}</b>",
        ]
        if history_sample:
            lines.append("")
            lines.append("Последние аренды, пока бот перезапускался:")
            lines.extend(history_sample)
        await self.notifier("\n".join(lines))

    def status_text(self) -> str:
        task_state = "нет задачи"
        if self.task:
            task_state = "завершена" if self.task.done() else "жива"

        last_poll = "ещё не было"
        if self.last_poll_at:
            last_poll = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self.last_poll_at))

        uptime = "не запущен"
        if self.started_at:
            uptime = str(time.strftime("%H:%M:%S", time.gmtime(time.time() - self.started_at)))

        chats = ", ".join(str(chat) for chat in sorted(self.chats)) or "нет"
        error = self.last_error or "нет"
        return (
            "📊 <b>Статус Marketapp монитора</b>\n\n"
            f"🟢 running: <b>{self.running}</b>\n"
            f"⚙️ task: <b>{task_state}</b>\n"
            f"🔄 цикл: <b>{self.cycle}</b>\n"
            f"⏱ работает: <b>{uptime}</b>\n"
            f"🕒 последний опрос: <b>{last_poll}</b>\n"
            f"📱 номеров в пуле: <b>{self.last_pool_count}</b>\n"
            f"🧾 событий истории: <b>{self.last_history_count}</b>\n"
            f"🔴 новых аренд в последнем цикле: <b>{self.last_history_new}</b>\n"
            f"📦 изменений пула в последнем цикле: <b>{self.last_pool_events}</b>\n"
            f"💬 чаты: <code>{chats}</code>\n"
            f"🧷 состояние сохранено: <b>{self.persistence_ok}</b>\n"
            f"⚠️ последняя ошибка: <code>{error}</code>"
        )
