import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="chatterai-selfcheck-"))
os.environ["MONITOR_STATE_FILE"] = str(TMP / "monitor_state.json")
os.environ["MONITOR_SEND_DELAY"] = "0"
os.environ["MONITOR_MAX_OUTBOX"] = "50"
os.environ["MONITOR_STARTUP_HISTORY_SAMPLE"] = "5"

import marketapp  # noqa: E402
import monitor as monitor_module  # noqa: E402
import state as state_store  # noqa: E402
from monitor import MarketMonitor  # noqa: E402

checks = 0


def check(condition: bool, message: str) -> None:
    global checks
    if not condition:
        raise AssertionError(message)
    checks += 1
    print(f"  ok: {message}")


def number(address: str, price: float, currency: str = "TON") -> dict:
    return {
        "address": address,
        "number": f"+7{address}",
        "price": price,
        "currency": currency,
        "min_duration": 86400,
        "max_duration": 2592000,
    }


def rent_event(tx: str, name: str, is_extend: bool = False) -> dict:
    return {
        "tx_hash": tx,
        "name": name,
        "price": 3.5,
        "currency": "TON",
        "duration": 2592000,
        "ts": 1760000000,
        "is_extend": is_extend,
    }


class Collector:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.texts: list[str] = []

    async def publish(self, events: list[dict]) -> None:
        self.events.extend(events)

    async def notify(self, text: str) -> None:
        self.texts.append(text)

    async def drain(self) -> None:
        return None

    def kinds(self) -> list[tuple[str, str]]:
        return [(event["source"], event["kind"]) for event in self.events]

    def reset(self) -> None:
        self.events.clear()
        self.texts.clear()


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None) -> None:
        self.messages.append((chat_id, text))


class FakeApplication:
    def __init__(self) -> None:
        self.bot = FakeBot()


def wire(monitor: MarketMonitor, collector: Collector) -> None:
    monitor.publisher = collector.publish
    monitor.notifier = collector.notify
    monitor.drain = collector.drain


def install_fake_api(pool: list[dict], history: list[dict]) -> None:
    async def get_rent_numbers() -> dict:
        return {"items": pool}

    async def get_numbers_history() -> dict:
        return {"items": history}

    monitor_module.get_rent_numbers = get_rent_numbers
    monitor_module.get_numbers_history = get_numbers_history


async def check_http_layer(bot) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    os.environ["BOT_TOKEN"] = "123456:selfcheck-token"
    os.environ["RENDER_EXTERNAL_URL"] = "http://127.0.0.1:9999"

    app = bot.create_app()
    app.on_startup.clear()
    app.on_cleanup.clear()

    secret = hashlib.sha256(b"123456:selfcheck-token").hexdigest()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get("/health")
        check(response.status == 200, "health отвечает 200")
        check("alive" in await response.text(), "health отдаёт текст живости")

        response = await client.post(
            f"/telegram/{secret}",
            json={},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        check(response.status == 403, "webhook отвергает запрос с неверным секретом")

        response = await client.post("/telegram/unknown-path", json={})
        check(response.status == 404, "webhook живёт только на своём пути")
    finally:
        await client.close()


async def main() -> None:
    import bot  # noqa: F401

    print("1. Первый цикл: baseline пула и истории, стартовое сообщение")
    monitor = MarketMonitor(interval=1, lookback=10)
    collector = Collector()
    monitor.chats.add(111)
    wire(monitor, collector)

    pool = [number("a", 10), number("b", 20)]
    history = [rent_event("h1", "+7a"), rent_event("h2", "+7b")]
    install_fake_api(pool, history)

    await monitor.poll()
    check(collector.kinds() == [("history", "history_snapshot"), ("history", "history_snapshot"), ("pool", "baseline"), ("pool", "baseline")], "baseline собирает снимки, а не уведомления")
    check(len(collector.texts) == 1 and "Монитор Marketapp запущен" in collector.texts[0], "стартовое сообщение отправлено")
    check("Номер арендован" in collector.texts[0], "в стартовом сообщении есть последние аренды")
    check(monitor.history_initialized and len(monitor.history_seen) == 2, "история инициализирована")
    check(monitor.last_pool_count == 2 and monitor.last_history_count == 2, "счётчики заполнены")

    print("2. Второй цикл: новые, исчезнувшие, изменившие цену и аренда")
    collector.reset()
    pool = [number("b", 25), number("c", 30)]
    history = [rent_event("h3", "+7c", is_extend=True), rent_event("h2", "+7b"), rent_event("h1", "+7a")]
    install_fake_api(pool, history)

    await monitor.poll()
    check(collector.kinds() == [("history", "rent_extended"), ("pool", "new"), ("pool", "removed"), ("pool", "price")], "события пула и истории определены верно")
    check(monitor.last_history_new == 1, "новая аренда посчитана")
    check(monitor.last_pool_events == 3, "событий пула три")

    print("3. Третий цикл: возвращение номера в пул")
    collector.reset()
    pool = [number("b", 25), number("c", 30), number("a", 10)]
    install_fake_api(pool, history)

    await monitor.poll()
    check(collector.kinds() == [("pool", "returned")], "возвращение отличено от нового номера")

    print("4. Состояние переживает перезапуск процесса")
    saved = json.loads(Path(os.environ["MONITOR_STATE_FILE"]).read_text(encoding="utf-8"))
    check(saved["chats"] == [111], "чат сохранён")
    check(len(saved["pool_previous"]) == 3, "снимок пула сохранён")
    check(saved["history_initialized"] is True, "признак инициализации истории сохранён")

    restored = MarketMonitor(interval=1, lookback=10)
    restored_collector = Collector()
    restored.load()
    wire(restored, restored_collector)
    check(restored.chats == {111}, "подписка восстановлена")
    check(restored.previous is not None and set(restored.previous) == {"a", "b", "c"}, "снимок пула восстановлен")

    install_fake_api(pool, history)
    await restored.poll()
    check(restored_collector.events == [], "после восстановления baseline не повторяется")
    check(restored.state_restored is True, "флаг восстановления выставлен")

    print("5. Потеря событий при сбое отправки исключена")
    app = FakeApplication()
    delivery = bot.TelegramDelivery(app)
    delivery.enqueue("первое", [1])
    delivery.enqueue("второе", [2])
    await delivery.flush()
    check(delivery.sent == 2 and not delivery.pending, "обычная отправка проходит")

    class FlakyBot(FakeBot):
        def __init__(self) -> None:
            super().__init__()
            self.failures = 0

        async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None) -> None:
            if self.failures == 0:
                self.failures += 1
                raise RuntimeError("temporary telegram failure")
            await super().send_message(chat_id, text, parse_mode)

    flaky = FakeApplication()
    flaky.bot = FlakyBot()
    retry_delivery = bot.TelegramDelivery(flaky)
    retry_delivery.enqueue("важное событие", [7])
    await retry_delivery.flush()
    check(len(retry_delivery.pending) == 1, "неудачная отправка осталась в очереди")
    await retry_delivery.flush()
    check(retry_delivery.sent == 1 and not retry_delivery.pending, "повторная отправка дошла")
    check(flaky.bot.messages == [(7, "важное событие")], "текст доставлен без потерь")

    print("6. Мёртвая очередь не растёт бесконечно")

    class DeadBot(FakeBot):
        async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None) -> None:
            raise RuntimeError("chat is unreachable")

    dead_delivery = bot.TelegramDelivery(FakeApplication())
    dead_delivery.application.bot = DeadBot()
    dead_delivery.enqueue("сообщение", [9])
    for _ in range(bot.MAX_SEND_ATTEMPTS + 1):
        await dead_delivery.flush()
    check(not dead_delivery.pending, "после лимита попыток сообщение снято с очереди")
    check(dead_delivery.dropped == 1, "снятое сообщение посчитано в потерях")

    print("7. Переполнение очереди не растёт бесконечно")
    overflow_delivery = bot.TelegramDelivery(FakeApplication())
    for index in range(60):
        overflow_delivery.enqueue(f"msg {index}", [1])
    check(len(overflow_delivery.pending) == 50, "очередь обрезана до предела")
    check(overflow_delivery.dropped == 10, "обрезанные сообщения посчитаны")

    print("8. Синк в Sheets выключен без переменных окружения")
    check(bot.sheets_sync.enabled is False, "Sheets отключён без webhook и секрета")
    await bot.sheets_sync.publish([{"source": "pool", "kind": "new", "item": {"name": "x"}}])
    check(bot.sheets_sync.sent == 0, "без переменных окружения запросы не уходят")

    print("9. Ранжирование пословиц работает офлайн")
    from data.proverb_loader import search_proverbs

    matches = search_proverbs("я постоянно откладываю важные дела", limit=3)
    check(isinstance(matches, list), "поиск пословиц отвечает списком")

    print("10. HTTP-слой сервиса поднимается и защищён секретом")
    await check_http_layer(bot)

    print(f"\nВсе проверки пройдены: {checks}")


if __name__ == "__main__":
    asyncio.run(main())
