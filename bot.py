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

    async def poll_history(self, application: Application) -> list[str]:
        """Poll recent rent events so short-lived rentals are not missed."""
        try:
            data = await get_numbers_history()
            history = extract_history_items(data)
        except Exception as exc:
            print(f"Marketapp history poll ERROR: {exc!r}", flush=True)
            return []

        # Keep a bounded recent-event set. The API returns the newest events first.
        history = history[:HISTORY_LOOKBACK]
        current_ids = [history_event_key(item) for item in history]

        if not self.history_seen:
            self.history_seen.update(current_ids)
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

        # Avoid unbounded memory growth while retaining enough recent IDs to
        # deduplicate events returned by the API on subsequent polls.
        if len(self.history_seen) > HISTORY_LOOKBACK * 3:
            self.history_seen = set(current_ids)

        if new_items:
            print(
                f"Marketapp history poll: detected {len(new_items)} new rent event(s)",
                flush=True,
            )

        return [format_history_monitor_event(item) for item in reversed(new_items)]

    async def poll(self, application: Application) -> None:
        self.cycle += 1
        cycle_started = time.monotonic()
        print(
            f"Marketapp monitor poll #{self.cycle}: checking pool...",
            flush=True,
        )

        history_events = await self.poll_history(application)

        try:
            data = await get_rent_numbers()
            current = build_number_snapshot(data)
        except Exception as exc:
            print(
                f"Marketapp monitor poll #{self.cycle}: ERROR {exc!r}",
                flush=True,
            )
            return

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
            events: list[str] = []

            for key in sorted(current_keys - previous_keys):
                kind = "returned" if key in self.ever_seen else "new"
                events.append(format_monitor_event(kind, current[key]))

            for key in sorted(previous_keys - current_keys):
                events.append(format_monitor_event("removed", None, previous[key]))

            for key in sorted(current_keys & previous_keys):
                old = previous[key]
                new = current[key]
                if old.get("price") != new.get("price") or old.get("currency") != new.get("currency"):
                    events.append(format_monitor_event("price", new, old))

            self.ever_seen.update(current_keys)
            self.previous = current

            if events:
                print(
                    f"Marketapp monitor poll #{self.cycle}: detected {len(events)} pool event(s)",
                    flush=True,
                )
            else:
                print(
                    f"Marketapp monitor poll #{self.cycle}: no pool changes",
                    flush=True,
                )

            history_events.extend(events)

        if not self.chat_id or not history_events:
            return

        for event in history_events[:20]:
            try:
                await application.bot.send_message(
                    chat_id=self.chat_id,
                    text=event,
                    parse_mode="HTML",
                )
            except Exception as exc:
                print(f"Marketapp monitor Telegram error: {exc!r}", flush=True)

    async def loop(self, application: Application) -> None:
        self.running = True
        print(f"Marketapp monitor started, interval={MONITOR_INTERVAL}s", flush=True)
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
            print(f"Marketapp monitor loop CRASHED: {exc!r}", flush=True)
            raise
        finally:
            self.running = False
            print("Marketapp monitor stopped", flush=True)

    def start(self, application: Application) -> None:
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.loop(application))

    async def stop(self) -> None:
        self.running = False
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None


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
        "Первый запрос используется как базовый снимок, поэтому старые события сразу не посыпятся уведомлениями."
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
