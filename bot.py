import asyncio
import hashlib
import html
import os
import time
from typing import Any, Iterable

import aiohttp
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import marketapp
import state as state_store
from data.proverb_loader import search_proverbs
from marketapp import (
    format_debug,
    format_history_monitor_event,
    format_monitor_event,
    format_numbers,
    format_result,
    get_collections,
    get_numbers_history,
    get_rent_numbers,
)
from monitor import MarketMonitor
from sheets import SheetsSync

MONITOR_INTERVAL = int(os.environ.get("MARKETAPP_MONITOR_INTERVAL", "90"))
HISTORY_LOOKBACK = int(os.environ.get("MARKETAPP_HISTORY_LOOKBACK", "100"))
SEND_BATCH = int(os.environ.get("MONITOR_SEND_BATCH", "25"))
SEND_DELAY = float(os.environ.get("MONITOR_SEND_DELAY", "0.35"))
MAX_OUTBOX = int(os.environ.get("MONITOR_MAX_OUTBOX", "500"))
MAX_SEND_ATTEMPTS = int(os.environ.get("MONITOR_SEND_ATTEMPTS", "5"))

number_monitor = MarketMonitor(interval=MONITOR_INTERVAL, lookback=HISTORY_LOOKBACK)
sheets_sync = SheetsSync(
    os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL", ""),
    os.environ.get("GOOGLE_SHEETS_SECRET", ""),
)
delivery: "TelegramDelivery | None" = None
http_session: aiohttp.ClientSession | None = None


class TelegramDelivery:
    """Queued Telegram sender: events survive a failed send and are retried."""

    def __init__(self, application: Application) -> None:
        self.application = application
        self.pending: list[list[Any]] = []
        self.sent = 0
        self.failed = 0
        self.dropped = 0

    def enqueue(self, text: str, chat_ids: Iterable[int]) -> None:
        for chat_id in chat_ids:
            self.pending.append([chat_id, text, 0])
        self._trim()

    def _trim(self) -> None:
        if len(self.pending) <= MAX_OUTBOX:
            return
        overflow = len(self.pending) - MAX_OUTBOX
        self.pending = self.pending[overflow:]
        self.dropped += overflow
        print(f"Telegram delivery: outbox overflow, dropped {overflow} oldest message(s)", flush=True)

    async def flush(self) -> None:
        if not self.pending:
            return

        batch = self.pending[:SEND_BATCH]
        self.pending = self.pending[SEND_BATCH:]

        for chat_id, text, attempts in batch:
            try:
                await self.application.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
                self.sent += 1
                print(f"Telegram delivery: sent to chat {chat_id}", flush=True)
            except Exception as exc:
                self.failed += 1
                if attempts + 1 >= MAX_SEND_ATTEMPTS:
                    self.dropped += 1
                    print(
                        f"Telegram delivery: giving up on chat {chat_id} after {attempts + 1} attempt(s): {exc!r}",
                        flush=True,
                    )
                else:
                    self.pending.append([chat_id, text, attempts + 1])
                    print(f"Telegram delivery: retry #{attempts + 1} queued for chat {chat_id}: {exc!r}", flush=True)
            if SEND_DELAY:
                await asyncio.sleep(SEND_DELAY)

    def status_line(self) -> str:
        return (
            f"📤 отправлено: <b>{self.sent}</b>\n"
            f"♻️ в очереди: <b>{len(self.pending)}</b>\n"
            f"⚠️ ошибок отправки: <b>{self.failed}</b>\n"
            f"🗑 потеряно: <b>{self.dropped}</b>"
        )


def format_event(event: dict[str, Any]) -> str | None:
    source = event.get("source")
    kind = event.get("kind")
    if source == "pool":
        if kind == "baseline":
            return None
        return format_monitor_event(str(kind), event.get("item"), event.get("previous"))
    if source == "history":
        if kind == "history_snapshot":
            return None
        return format_history_monitor_event(event.get("item") or {})
    return None


async def publish_events(events: list[dict[str, Any]]) -> None:
    if delivery is None:
        return
    for event in events:
        text = format_event(event)
        if text:
            delivery.enqueue(text, number_monitor.chats)
    await sheets_sync.publish(events)
    await delivery.flush()


async def notify_chats(text: str) -> None:
    if delivery is None or not number_monitor.chats:
        print("Monitor notification skipped: no chats are subscribed", flush=True)
        return
    delivery.enqueue(text, number_monitor.chats)
    await delivery.flush()


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
        "/monitor — подписать этот чат на уведомления монитора\n"
        "/monitor_status — показать состояние монитора\n"
        "/monitor_test — проверить отправку уведомлений\n"
        "/monitor_off — отписать этот чат от уведомлений\n\n"
        "Монитор работает постоянно и сам поднимается после каждого деплоя.\n"
        "Уведомления приходят во все чаты, где включён /monitor, а также в чаты из "
        "переменной окружения MONITOR_CHAT_ID.\n\n"
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

    chat_id = update.effective_chat.id
    was_subscribed = chat_id in number_monitor.chats
    number_monitor.add_chat(chat_id)

    if number_monitor.task is None or number_monitor.task.done():
        number_monitor.start(publish_events, notify_chats, delivery.flush if delivery else _noop)

    status = "уже был подписан" if was_subscribed else "подписан"
    await update.message.reply_text(
        f"👀 Этот чат {status} на уведомления Marketapp.\n\n"
        f"Проверяю пул и историю каждые {MONITOR_INTERVAL} сек.\n"
        "Буду сообщать о новых номерах, исчезновении, возвращении, изменении цены и факте аренды.\n\n"
        f"Всего чатов на связи: {len(number_monitor.chats)}.\n"
        "Монитор работает и после деплоя: чаты хранятся в файле состояния, а MONITOR_CHAT_ID "
        "подписывает чат автоматически при старте."
    )


async def monitor_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /monitor_status handler received an update", flush=True)
    text = number_monitor.status_text()
    if delivery is not None:
        text = f"{text}\n\n{delivery.status_line()}"
    text += f"\n🗂 файл состояния: <code>{state_store.state_file()}</code>"
    await update.message.reply_text(text, parse_mode="HTML")


async def monitor_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /monitor_test handler received an update", flush=True)
    if not update.effective_chat or delivery is None:
        return

    await update.message.reply_text("🧪 Отправляю тестовое уведомление через тот же канал, что использует монитор...")
    delivery.enqueue(
        "🧪 <b>Тест монитора Marketapp</b>\nЕсли ты видишь это сообщение, Telegram-доставка уведомлений работает. "
        "Теперь проверяем именно обнаружение аренд.",
        [update.effective_chat.id],
    )
    await delivery.flush()


async def monitor_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /monitor_off handler received an update", flush=True)
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    if chat_id in number_monitor.chats:
        number_monitor.remove_chat(chat_id)
        await update.message.reply_text(
            f"🛑 Этот чат отписан от уведомлений.\nОсталось чатов на связи: {len(number_monitor.chats)}."
        )
    else:
        await update.message.reply_text("Этот чат и так не был подписан на уведомления.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"Telegram handler error: {context.error!r}", flush=True)
    await notify_chats(
        "⚠️ <b>Ошибка обработчика Telegram</b>\n"
        f"<code>{html.escape(repr(context.error))[:900]}</code>"
    )


async def _noop() -> None:
    return None


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
        global delivery, http_session

        print("Telegram startup: initializing application", flush=True)
        http_session = aiohttp.ClientSession()
        marketapp.bind_session(http_session)
        sheets_sync.bind_session(http_session)

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

        delivery = TelegramDelivery(telegram_app)
        number_monitor.load()
        for chat_id in state_store.chat_ids_from_env():
            number_monitor.chats.add(chat_id)
        print(
            f"Marketapp monitor chats after startup: {sorted(number_monitor.chats)} "
            f"(Google Sheets sync enabled={sheets_sync.enabled})",
            flush=True,
        )
        number_monitor.start(publish_events, notify_chats, delivery.flush)

    async def on_cleanup(app: web.Application) -> None:
        global http_session
        print("Telegram cleanup: stopping monitor and application", flush=True)
        await number_monitor.stop()
        if http_session is not None:
            await http_session.close()
            http_session = None
        marketapp.bind_session(None)
        sheets_sync.bind_session(None)
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
