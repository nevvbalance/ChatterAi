import asyncio
import hashlib
import html
import os
import time
from pathlib import Path
from typing import Any, Iterable

import aiohttp
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

import bonus
import marketapp
import state as state_store
import stars
import ton_payments
import ton_web
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
STAR_AMOUNTS = stars.amounts()
STAR_BUTTONS_PER_ROW = 2

number_monitor = MarketMonitor(interval=MONITOR_INTERVAL, lookback=HISTORY_LOOKBACK)
ton_monitor = ton_payments.TonDonationMonitor()
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


async def send_to_chats(text: str, chat_ids: list[int]) -> None:
    if delivery is None or not chat_ids:
        return
    delivery.enqueue(text, chat_ids)
    await delivery.flush()


async def send_gram_bonus(donation: dict[str, Any]) -> None:
    chat_id = donation.get("chat_id")
    if delivery is None or chat_id is None:
        return
    try:
        await bonus.deliver(delivery.application.bot, int(chat_id), ton_payments.donation_info(donation))
    except Exception as exc:
        print(f"Bonus: gram reward failed for chat {chat_id}: {exc!r}", flush=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /start handler received an update", flush=True)
    await update.message.reply_text(
        "Привет! 🤖 Я ChatterAi.\n\n"
        "Я ищу подходящие русские пословицы и использую их смысл, чтобы ответить на твою ситуацию.\n\n"
        "Напиши вопрос обычным сообщением.\n"
        "Например: «Я постоянно откладываю важные дела». ✨"
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
        "/monitor_off — отписать этот чат от уведомлений\n"
        "/donate — реквизиты для поддержки бота в Gram\n"
        "/mydonations — проверить свои подтверждённые донаты\n"
        "/donations_status — состояние проверки донатов\n"
        "/stars — поддержать бота звёздами Telegram\n"
        "/terms — условия оплаты\n"
        "/paysupport — поддержка по платежам\n"
        "/stars_status — состояние платежей в звёздах\n\n"
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


async def donate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /donate handler received an update", flush=True)
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    pay_url = ton_web.public_pay_url(chat_id)
    text = ton_payments.format_donate(chat_id, one_tap=bool(pay_url))
    text += "\n\nМожно поддержать и звёздами Telegram — /stars."

    if not pay_url:
        await update.message.reply_text(text, parse_mode="HTML")
        return

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💛 Оплатить в один тап", url=pay_url)]])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def mydonations_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /mydonations handler received an update", flush=True)
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    if not ton_payments.enabled():
        await update.message.reply_text(ton_payments.format_donate(chat_id), parse_mode="HTML")
        return

    await update.message.reply_text("Смотрю блокчейн TON... 🔎")
    try:
        donations = await ton_monitor.donations_for_chat(chat_id)
        await update.message.reply_text(ton_payments.format_donations(donations, chat_id), parse_mode="HTML")
    except Exception as exc:
        print(f"TON /mydonations error: {exc!r}", flush=True)
        await update.message.reply_text(f"Ошибка TON API: {exc}")


async def donations_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /donations_status handler received an update", flush=True)
    text = ton_monitor.status_text()
    text += f"\n🗂 файл состояния: <code>{state_store.donations_state_file()}</code>"
    await update.message.reply_text(text, parse_mode="HTML")


async def star_balance(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    try:
        amount = await context.bot.get_my_star_balance()
    except Exception as exc:
        print(f"Stars balance request failed: {exc!r}", flush=True)
        return None
    return getattr(amount, "amount", None)


async def stars_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /stars handler received an update", flush=True)
    if not update.effective_chat or not update.message:
        return

    chat_id = update.effective_chat.id
    balance = await star_balance(context)
    rows = [
        [
            InlineKeyboardButton(f"⭐ {amount}", callback_data=f"stars:{amount}")
            for amount in STAR_AMOUNTS[start:start + STAR_BUTTONS_PER_ROW]
        ]
        for start in range(0, len(STAR_AMOUNTS), STAR_BUTTONS_PER_ROW)
    ]
    await update.message.reply_text(
        stars.format_offer(chat_id, STAR_AMOUNTS, balance),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def stars_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    print(f"Telegram callback received: {query.data if query else None}", flush=True)
    if query is None or query.message is None or not query.data:
        return

    await query.answer()

    if not query.data.startswith("stars:"):
        return
    try:
        amount = int(query.data.split(":", 1)[1])
    except ValueError:
        return
    if amount not in STAR_AMOUNTS:
        await query.message.reply_text("Не знаю такую сумму. Посмотри /stars.")
        return

    chat_id = query.message.chat_id
    try:
        await context.bot.send_invoice(
            chat_id=chat_id,
            title=stars.invoice_title(amount),
            description=stars.invoice_description(),
            payload=stars.payload_for(chat_id, amount),
            provider_token="",
            currency=stars.CURRENCY,
            prices=[LabeledPrice(label=stars.price_label(amount), amount=amount)],
            start_parameter=stars.START_PARAMETER,
        )
    except Exception as exc:
        print(f"Stars send_invoice failed: {exc!r}", flush=True)
        await query.message.reply_text(f"Не смог выставить счёт: {exc}")


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    print("Telegram pre_checkout_query received", flush=True)
    if query is None:
        return

    error = stars.check_invoice(query.invoice_payload, query.currency, query.total_amount)
    if error is None:
        await query.answer(ok=True)
        return

    print(
        f"Stars pre_checkout rejected ({error}): payload={query.invoice_payload!r} "
        f"amount={query.total_amount} currency={query.currency!r}",
        flush=True,
    )
    await query.answer(
        ok=False,
        error_message="Не удалось проверить платёж. Попробуй ещё раз или напиши /paysupport.",
    )


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    payment = message.successful_payment if message else None
    print("Telegram successful_payment received", flush=True)
    if message is None or payment is None:
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None
    stars.record_payment(payment, chat_id, user_id)

    if payment.currency != stars.CURRENCY:
        print(f"Stars payment with unexpected currency: {payment.currency!r}", flush=True)
        return

    await message.reply_text(stars.format_thanks(payment), parse_mode="HTML")

    if chat_id is not None:
        try:
            await bonus.deliver(context.bot, chat_id, stars.payment_info(payment, chat_id))
        except Exception as exc:
            print(f"Bonus: stars reward failed for chat {chat_id}: {exc!r}", flush=True)


async def terms_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /terms handler received an update", flush=True)
    if not update.message:
        return
    await update.message.reply_text(stars.format_terms(), parse_mode="HTML")


async def paysupport_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /paysupport handler received an update", flush=True)
    if not update.message:
        return
    await update.message.reply_text(stars.format_support(), parse_mode="HTML")


async def stars_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram /stars_status handler received an update", flush=True)
    if not update.message:
        return

    records = stars.saved_records()
    total = sum(int(item.get("amount") or 0) for item in records if isinstance(item, dict))
    last = records[-1] if records else None
    lines = [
        "⭐ <b>Статус платежей в звёздах</b>\n",
        f"🧾 записей: <b>{len(records)}</b>",
        f"💰 всего звёзд: <b>{total}</b>",
        f"🎛 суммы в кнопках: <code>{', '.join(str(value) for value in STAR_AMOUNTS)}</code>",
        f"🌐 валюта: <b>{stars.CURRENCY}</b>",
        "⏱ окно pre_checkout: <b>10 сек</b> (требование Telegram)",
    ]
    balance = await star_balance(context)
    if balance is not None:
        lines.append(f"⭐ баланс бота: <b>{balance}</b>")
    if last:
        lines.append(
            f"🕒 последний: <b>{last.get('amount')}</b> звёзд, charge_id "
            f"<code>{html.escape(str(last.get('telegram_payment_charge_id'))[:40])}</code>"
        )
    lines.append(f"🗂 файл состояния: <code>{state_store.stars_state_file()}</code>")
    lines.append("")
    lines.append("Charge id нужен для возврата через refundStarPayment.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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
    telegram_app.add_handler(CommandHandler("donate", donate_command))
    telegram_app.add_handler(CommandHandler("mydonations", mydonations_command))
    telegram_app.add_handler(CommandHandler("donations_status", donations_status_command))
    telegram_app.add_handler(CommandHandler("stars", stars_command))
    telegram_app.add_handler(CommandHandler("terms", terms_command))
    telegram_app.add_handler(CommandHandler("paysupport", paysupport_command))
    telegram_app.add_handler(CommandHandler("stars_status", stars_status_command))
    telegram_app.add_handler(CallbackQueryHandler(stars_callback, pattern=r"^stars:"))
    telegram_app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    telegram_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
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

        ton_monitor.load()
        ton_monitor.start(send_to_chats, delivery.flush, send_gram_bonus)
        print(
            f"TON donations: enabled={ton_payments.enabled()}, "
            f"wallet={ton_payments.WALLET_ADDRESS or 'not configured'}, "
            f"interval={ton_monitor.interval}s",
            flush=True,
        )

    async def on_cleanup(app: web.Application) -> None:
        global http_session
        print("Telegram cleanup: stopping monitor and application", flush=True)
        await number_monitor.stop()
        await ton_monitor.stop()
        if http_session is not None:
            await http_session.close()
            http_session = None
        marketapp.bind_session(None)
        sheets_sync.bind_session(None)
        await telegram_app.stop()
        await telegram_app.shutdown()

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="ChatterAi is alive! 🤖")

    async def pay_page(request: web.Request) -> web.Response:
        try:
            chat_id = int(request.query.get("chat", ""))
        except (TypeError, ValueError):
            return web.Response(status=400, text="Query parameter chat must be an integer")

        if not ton_payments.enabled():
            return web.Response(status=503, text="Donations are not configured")

        try:
            base = ton_web.base_url(str(request.url.origin()), request.headers.get("X-Forwarded-Proto"))
            page = ton_web.render_page(chat_id, base)
        except Exception as exc:
            print(f"TON pay page error: {exc!r}", flush=True)
            return web.Response(status=500, text="Cannot render the payment page")

        return web.Response(text=page, content_type="text/html", charset="utf-8")

    async def tonconnect_manifest(request: web.Request) -> web.Response:
        base = ton_web.base_url(str(request.url.origin()), request.headers.get("X-Forwarded-Proto"))
        print(f"TON Connect manifest requested, base={base}", flush=True)
        return web.json_response(
            ton_web.manifest(base),
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def tonconnect_icon(request: web.Request) -> web.Response:
        icon_path = Path(__file__).resolve().parent / "static" / "icon-180.png"
        if not icon_path.exists():
            return web.Response(status=404, text="Not found")
        return web.Response(
            body=icon_path.read_bytes(),
            content_type="image/png",
            headers={"Access-Control-Allow-Origin": "*"},
        )

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
    app.router.add_get(ton_web.PAY_PATH, pay_page)
    app.router.add_get(ton_web.MANIFEST_PATH, tonconnect_manifest)
    app.router.add_get(ton_web.ICON_PATH, tonconnect_icon)
    app.router.add_post(webhook_path, telegram_webhook)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    web.run_app(create_app(), host="0.0.0.0", port=port)
