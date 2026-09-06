import asyncio
import hashlib
import os
import time

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from data.proverb_loader import search_proverbs
from marketapp import (
    build_number_snapshot,
    extract_history_items,
    format_debug,
    format_history_monitor_event,
    format_monitor_event,
    format_numbers,
    format_result,
    get_collections,
    get_numbers_history,
    get_rent_numbers,
    history_event_key,
)

MONITOR_INTERVAL = int(os.environ.get("MARKETAPP_MONITOR_INTERVAL", "90"))
HISTORY_LOOKBACK = int(os.environ.get("MARKETAPP_HISTORY_LOOKBACK", "100"))


class NumberMonitor:
    def __init__(self) -> None:
        self.chat_id: int | None = None
        self.previous: dict[str, dict] | None = None
        self.ever_seen: set[str] = set()
        self.history_seen: set[str] = set()
        self.task: asyncio.Task | None = None
        self.running = False
        self.cycle = 0
        self.last_poll_at: float | None = None
        self.last_pool_count = 0
        self.last_history_count = 0
        self.last_history_new = 0
        self.last_pool_events = 0
        self.last_error: str | None = None

    async def poll_history(self, application: Application) -> list[str]:
        """Poll recent rent events so short-lived rentals are not missed."""
        try:
            print(
                f"Marketapp history poll #{self.cycle}: requesting history...",
                flush=True,
            )
            data = await get_numbers_history()
            history = extract_history_items(data)
            self.last_error = None
        except Exception as exc:
            self.last_error = f"history: {exc!r}"
            print(f"Marketapp history poll ERROR: {exc!r}", flush=True)
            return []

        history = history[:HISTORY_LOOKBACK]
        self.last_history_count = len(history)
        current_ids = [history_event_key(item) for item in history]
        print(
            f"Marketapp history poll #{self.cycle}: received {len(history)} event(s)",
            flush=True,
        )

        if not self.history_seen:
            self.history_seen.update(current_ids)
            self.last_history_new = 0
            print(
                f"Marketapp history baseline: {len(history)} recent event(s)",
                flush=True,
            )
            return []

        new_items: list[dict] = []
        for item, event_id in zip(history, current_ids):
            if event_id not in self.history_seen:
                new_items.append(item)

        self.history_seen.update(current_ids)
        self.last_history_new = len(new_items)

        if len(self.history_seen) > HISTORY_LOOKBACK * 3:
            self.history_seen = set(current_ids)

        if new_items:
            print(
                f"Marketapp history poll #{self.cycle}: detected {len(new_items)} new rent event(s)",
                flush=True,
            )
        else:
            print(
                f"Marketapp history poll #{self.cycle}: no new rent events",
                flush=True,
            )

        return [format_history_monitor_event(item) for item in reversed(new_items)]

    async def send_events(self, application: Application, events: list[str]) -> None:
        if not self.chat_id:
            print("Marketapp monitor: cannot send events, chat_id is not set", flush=True)
            return

        for event in events[:20]:
            try:
                await application.bot.send_message(
                    chat_id=self.chat_id,
                    text=event,
                    parse_mode="HTML",
                )
                print(
                    f"Marketapp monitor: Telegram notification sent to chat {self.chat_id}",
                    flush=True,
                )
            except Exception as exc:
                self.last_error = f"telegram: {exc!r}"
                print(f"Marketapp monitor Telegram error: {exc!r}", flush=True)

    async def poll(self, application: Application) -> None:
        self.cycle += 1
        self.last_poll_at = time.time()
        cycle_started = time.monotonic()
        self.last_pool_events = 0
        print(
            f"Marketapp monitor poll #{self.cycle}: checking pool...",
            flush=True,
        )

        # History is independent from the pool snapshot. Keep its events even
        # when the pool endpoint fails, so a transient pool error cannot swallow
        # a real rent notification.
        history_events = await self.poll_history(application)

        pool_events: list[str] = []
        try:
            data = await get_rent_numbers()
            current = build_number_snapshot(data)
            self.last_error = None
        except Exception as exc:
            self.last_error = f"pool: {exc!r}"
            print(
                f"Marketapp monitor poll #{self.cycle}: pool ERROR {exc!r}",
                flush=True,
            )
            await self.send_events(application, history_events)
            return

        self.last_pool_count = len(current)
        elapsed = time.monotonic() - cycle_started
        print(
            f"Marketapp monitor poll #{self.cycle}: received {len(current)} numbers "
            f"in {elapsed:.2f}s",
            flush=True,
        )

        if self.previous is None:
            self.previous = current
            self.ever_seen.update(current)
            print(
                f"Marketapp monitor baseline: {len(current)} numbers",
                flush=True,
            )
        else:
            previous = self.previous
            current_keys = set(current)
            previous_keys = set(previous)

            for key in sorted(current_keys - previous_keys):
                kind = "returned" if key in self.ever_seen else "new"
                pool_events.append(format_monitor_event(kind, current[key]))

            for key in sorted(previous_keys - current_keys):
                pool_events.append(format_monitor_event("removed", None, previous[key]))

            for key in sorted(current_keys & previous_keys):
                old = previous[key]
                new = current[key]
                if old.get("price") != new.get("price") or old.get("currency") != new.get("currency"):
                    pool_events.append(format_monitor_event("price", new, old))

            self.ever_seen.update(current_keys)
            self.previous = current

            self.last_pool_events = len(pool_events)
            if pool_events:
                print(
                    f"Marketapp monitor poll #{self.cycle}: detected {len(pool_events)} pool event(s)",
                    flush=True,
                )
            else:
                print(
                    f"Marketapp monitor poll #{self.cycle}: no pool changes",
                    flush=True,
                )

        all_events = history_events + pool_events
        if all_events:
            print(
                f"Marketapp monitor poll #{self.cycle}: sending {len(all_events)} notification(s)",
                flush=True,
            )
            await self.send_events(application, all_events)

    async def loop(self, application: Application) -> None:
        self.running = True
        print(f"Marketapp monitor started, interval={MONITOR_INTERVAL}s", flush=True)
        if self.chat_id:
            try:
                await application.bot.send_message(
                    chat_id=self.chat_id,
                    text="🟢 <b>Marketapp монитор реально запущен</b>\nФоновая задача работает. Сейчас выполняю первый цикл проверки.",
                    parse_mode="HTML",
                )
                print(
                    f"Marketapp monitor startup notification sent to chat {self.chat_id}",
                    flush=True,
                )
            except Exception as exc:
                self.last_error = f"startup telegram: {exc!r}"
                print(f"Marketapp monitor startup Telegram error: {exc!r}", flush=True)

        try:
            while self.running:
                await self.poll(application)
                print(
                    f"Marketapp monitor: next poll in {MONITOR_INTERVAL}s",
                    flush=True,
                )
                await asyncio.sleep(MONITOR_INTERVAL)
        except asyncio.CancelledError:
            print("Marketapp monitor cancelled", flush=True)
            raise
        except Exception as exc:
            self.last_error = f"loop: {exc!r}"
            print(f"Marketapp monitor loop CRASHED: {exc!r}", flush=True)
            raise
        finally:
            self.running = False
            print("Marketapp monitor stopped", flush=True)

    def start(self, application: Application) -> None:
        if self.task and not self.task.done():
            print("Marketapp monitor start requested, but task is already running", flush=True)
            return
        self.task = asyncio.create_task(self.loop(application))
        print(f"Marketapp monitor task created: {self.task!r}", flush=True)

    async def stop(self) -> None:
        self.running = False
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None

    def status_text(self) -> str:
        task_state = "нет задачи"
        if self.task:
            if self.task.done():
                task_state = "завершена"
            else:
                task_state = "жива"

        last_poll = "ещё не было"
        if self.last_poll_at:
            last_poll = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self.last_poll_at))

        error = self.last_error or "нет"
        return (
            "📊 <b>Статус Marketapp монитора</b>\n\n"
            f"🟢 running: <b>{self.running}</b>\n"
            f"⚙️ task: <b>{task_state}</b>\n"
            f"🔄 цикл: <b>{self.cycle}</b>\n"
            f"🕒 последний опрос: <b>{last_poll}</b>\n"
            f"📱 номеров в пуле: <b>{self.last_pool_count}</b>\n"
            f"🧾 событий истории: <b>{self.last_history_count}</b>\n"
            f"🔴 новых аренд в последнем цикле: <b>{self.last_history_new}</b>\n"
            f"📦 изменений пула в последнем цикле: <b>{self.last_pool_events}</b>\n"
            f"💬 chat_id: <code>{self.chat_id}</code>\n"
            f"⚠️ последняя ошибка: <code>{error}</code>"
        )


number_monitor = NumberMonitor()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /start handler received an update", flush=True)
    await update.message.reply_text(
        "Привет! 🤖 Я ChatterAi.\n\n"
        "Я ищу подходящие русские пословицы и использую их смысл, чтобы ответить на твою ситуацию.\n\n"
        "Напиши вопрос обычным сообщением.\n"
        "Например: «Я постоянно откладываю важные дела»."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /help handler received an update", flush=True)
    await update.message.reply_text(
        "/start — запустить бота\n"
        "/help — показать помощь\n"
        "/marketapp — проверить запрос к Marketapp API\n"
        "/numbers — показать доступные для аренды номера\n"
        "/history — посмотреть структуру истории аренды номеров\n"
        "/monitor — включить мониторинг пула номеров в этом чате\n"
        "/monitor_status — показать состояние монитора\n"
        "/monitor_test — проверить отправку уведомлений\n"
        "/monitor_off — выключить мониторинг\n\n"
        "Просто напиши ситуацию или вопрос, и я подберу подходящую народную мудрость."
    )


async def marketapp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /marketapp handler received an update", flush=True)
    await update.message.reply_text("Проверяю Marketapp API... 🔎")
    try:
        data = await get_collections()
        await update.message.reply_text(format_result(data))
    except Exception as exc:
        print(f"Marketapp /marketapp error: {exc!r}", flush=True)
        await update.message.reply_text(f"Ошибка Marketapp API: {exc}")


async def numbers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /numbers handler received an update", flush=True)
    await update.message.reply_text("Смотрю доступные номера в Marketapp... 📱🔎")
    try:
        data = await get_rent_numbers()
        for message in format_numbers(data):
            await update.message.reply_text(message, parse_mode="HTML")
    except Exception as exc:
        print(f"Marketapp /numbers error: {exc!r}", flush=True)
        await update.message.reply_text(f"Ошибка Marketapp API: {exc}")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /history handler received an update", flush=True)
    await update.message.reply_text("Запрашиваю историю аренды номеров в Marketapp... 🧾🔎")
    try:
        data = await get_numbers_history()
        for message in format_debug(data):
            await update.message.reply_text(message)
    except Exception as exc:
        print(f"Marketapp /history error: {exc!r}", flush=True)
        await update.message.reply_text(f"Ошибка Marketapp API: {exc}")


async def monitor_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /monitor handler received an update", flush=True)
    if not update.effective_chat:
        return

    number_monitor.chat_id = update.effective_chat.id
    number_monitor.start(context.application)
    status = "уже был включён" if number_monitor.previous is not None else "включён"

    await update.message.reply_text(
        f"👀 Мониторинг Marketapp {status}.\n\n"
        f"Проверяю пул каждые {MONITOR_INTERVAL} сек.\n"
        "Дополнительно проверяю историю аренд, чтобы не пропускать короткие аренды между проверками.\n"
        "Буду сообщать о новых номерах, исчезновении, возвращении, изменении цены и факте аренды.\n\n"
        "Первый запрос используется как базовый снимок, поэтому старые события сразу не посыпятся уведомлениями.\n\n"
        "После запуска пришлю отдельное 🟢 подтверждение, что фоновая задача действительно стартовала."
    )


async def monitor_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /monitor_status handler received an update", flush=True)
    await update.message.reply_text(number_monitor.status_text(), parse_mode="HTML")


async def monitor_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /monitor_test handler received an update", flush=True)
    if not update.effective_chat:
        return

    await update.message.reply_text("🧪 Отправляю тестовое уведомление через тот же канал, что использует монитор...")
    number_monitor.chat_id = update.effective_chat.id
    await number_monitor.send_events(
        context.application,
        ["🧪 <b>Тест монитора Marketapp</b>\nЕсли ты видишь это сообщение, Telegram-доставка уведомлений работает. "
         "Теперь проверяем именно обнаружение аренд."]
    )


async def monitor_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /monitor_off handler received an update", flush=True)
    if update.effective_chat and number_monitor.chat_id == update.effective_chat.id:
        number_monitor.chat_id = None
        await number_monitor.stop()
        await update.message.reply_text("🛑 Мониторинг уведомлений выключен.")
    else:
        await update.message.reply_text("Мониторинг для этого чата не был включён.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"Telegram handler error: {context.error!r}", flush=True)


def build_answer(query: str, matches: list[dict]) -> str:
    first = matches[0]
    proverb = first.get("proverb", "")
    meaning = first.get("meaning", "").strip()

    if meaning:
        answer = f"Мне кажется, здесь особенно подходит:\n\n🪶 «{proverb}»\n\n{meaning}"
    else:
        answer = f"Здесь хорошо подходит народная мудрость:\n\n🪶 «{proverb}»"

    if len(matches) > 1:
        answer += "\n\nЕщё в ту же сторону:\n"
        for item in matches[1:3]:
            answer += f"• «{item.get('proverb', '')}»\n"

    answer += "\n\nЯ не выдаю пословицу за универсальный рецепт, но она может помочь взглянуть на ситуацию с другой стороны."
    return answer


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    print("Telegram text message handler received an update", flush=True)
    query = update.message.text.strip()
    if len(query) < 3:
        await update.message.reply_text("Опиши ситуацию чуть подробнее 🙂")
        return

    matches = search_proverbs(query, limit=3)
    if not matches:
        await update.message.reply_text(
            "Пока не нашёл достаточно близкой пословицы в своей базе. 🧐\n\n"
            "Попробуй описать ситуацию другими словами или чуть подробнее."
        )
        return

    await update.message.reply_text(build_answer(query, matches))


def create_app() -> web.Application:
    token = os.environ["BOT_TOKEN"]
    public_url = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")

    webhook_secret = hashlib.sha256(token.encode()).hexdigest()
    webhook_path = f"/telegram/{webhook_secret}"
    webhook_url = f"{public_url}{webhook_path}"

    telegram_app = Application.builder().token(token).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("marketapp", marketapp_command))
    telegram_app.add_handler(CommandHandler("numbers", numbers_command))
    telegram_app.add_handler(CommandHandler("history", history_command))
    telegram_app.add_handler(CommandHandler("monitor", monitor_command))
    telegram_app.add_handler(CommandHandler("monitor_status", monitor_status_command))
    telegram_app.add_handler(CommandHandler("monitor_test", monitor_test_command))
    telegram_app.add_handler(CommandHandler("monitor_off", monitor_off_command))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    telegram_app.add_error_handler(error_handler)

    async def on_startup(app: web.Application) -> None:
        print("Telegram startup: initializing application", flush=True)
        await telegram_app.initialize()
        await telegram_app.start()
        print("Telegram startup: application started, setting webhook", flush=True)

        await telegram_app.bot.set_webhook(
            url=webhook_url,
            secret_token=webhook_secret,
            allowed_updates=Update.ALL_TYPES,
        )

        webhook_info = await telegram_app.bot.get_webhook_info()
        print(
            "Telegram webhook configured: "
            f"active={bool(webhook_info.url)}, "
            f"pending_updates={webhook_info.pending_update_count}, "
            f"last_error={webhook_info.last_error_message!r}, "
            f"last_error_date={webhook_info.last_error_date!r}",
            flush=True,
        )

    async def on_cleanup(app: web.Application) -> None:
        print("Telegram cleanup: stopping application without deleting webhook", flush=True)
        await number_monitor.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="ChatterAi is alive! 🤖")

    async def telegram_webhook(request: web.Request) -> web.Response:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != webhook_secret:
            print("Telegram webhook: rejected request with invalid secret", flush=True)
            return web.Response(status=403, text="Forbidden")

        try:
            data = await request.json()
            update = Update.de_json(data, telegram_app.bot)
            if update is None:
                print("Telegram webhook: received an empty/invalid update", flush=True)
                return web.Response(text="OK")

            print(f"Telegram webhook: received update {update.update_id}", flush=True)
            await telegram_app.update_queue.put(update)
            print(f"Telegram webhook: queued update {update.update_id}", flush=True)
            return web.Response(text="OK")
        except Exception as exc:
            print(f"Telegram webhook error: {exc!r}", flush=True)
            return web.Response(status=500, text="Webhook error")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post(webhook_path, telegram_webhook)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    web.run_app(create_app(), host="0.0.0.0", port=port)
