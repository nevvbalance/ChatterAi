import hashlib
import os

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! 🤖 Я ChatterAi. Бот работает!\n\n"
        "Напиши /help, чтобы увидеть доступные команды."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/start - запустить бота\n"
        "/help - показать помощь"
    )


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ChatterAi is alive! 🤖")


async def create_app() -> web.Application:
    token = os.environ["BOT_TOKEN"]
    public_url = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")

    # A deterministic secret derived from the private bot token.
    webhook_secret = hashlib.sha256(token.encode()).hexdigest()
    webhook_path = f"/telegram/{webhook_secret}"
    webhook_url = f"{public_url}{webhook_path}"

    telegram_app = Application.builder().token(token).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_command))

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
    application = create_app()
    web.run_app(application, host="0.0.0.0", port=port)
