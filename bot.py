import hashlib
import os

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from data.proverb_loader import search_proverbs
from marketapp import format_numbers, format_result, get_collections, get_rent_numbers


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! 🤖 Я ChatterAi.\n\n"
        "Я ищу подходящие русские пословицы и использую их смысл, чтобы ответить на твою ситуацию.\n\n"
        "Напиши вопрос обычным сообщением.\n"
        "Например: «Я постоянно откладываю важные дела»."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/start — запустить бота\n"
        "/help — показать помощь\n"
        "/marketapp — проверить запрос к Marketapp API\n"
        "/numbers — показать доступные для аренды номера\n\n"
        "Просто напиши ситуацию или вопрос, и я подберу подходящую народную мудрость."
    )


async def marketapp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Проверяю Marketapp API... 🔎")

    try:
        data = await get_collections()
        await update.message.reply_text(format_result(data))
    except Exception as exc:
        await update.message.reply_text(f"Ошибка Marketapp API: {exc}")


async def numbers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Смотрю доступные номера в Marketapp... 📱🔎")

    try:
        data = await get_rent_numbers()
        await update.message.reply_text(format_numbers(data))
    except Exception as exc:
        await update.message.reply_text(f"Ошибка Marketapp API: {exc}")


def build_answer(query: str, matches: list[dict]) -> str:
    """Build a useful response from the retrieved proverb evidence without inventing facts."""
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
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    async def on_startup(app: web.Application) -> None:
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.bot.set_webhook(
            url=webhook_url,
            secret_token=webhook_secret,
            allowed_updates=Update.ALL_TYPES,
        )

    async def on_cleanup(app: web.Application) -> None:
        await telegram_app.bot.delete_webhook()
        await telegram_app.stop()
        await telegram_app.shutdown()

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="ChatterAi is alive! 🤖")

    async def telegram_webhook(request: web.Request) -> web.Response:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != webhook_secret:
            return web.Response(status=403, text="Forbidden")

        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.update_queue.put(update)
        return web.Response(text="OK")

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
