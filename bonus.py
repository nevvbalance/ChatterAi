import io
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile

from data.proverb_loader import load_proverbs

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_PERSONAL_FILENAME = "chatterai-blagodarnost.txt"
DEFAULT_LINK_LABEL = "Перейти по ссылке"
SECRETS_DIR = Path("/etc/secrets")
ANIMATION_SUFFIXES = {".gif", ".mp4", ".webm"}
CAPTION = "💛 Спасибо за поддержку! Держи файл-благодарность."
READY_CAPTION = "💛 И небольшой подарок от ChatterAi."
_proverbs_cache: list[dict[str, Any]] | None = None


def link_url() -> str:
    return os.environ.get("BONUS_LINK_URL", "").strip()


def link_label() -> str:
    return os.environ.get("BONUS_LINK_LABEL", "").strip() or DEFAULT_LINK_LABEL


def link_keyboard() -> InlineKeyboardMarkup | None:
    url = link_url()
    if not url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(link_label(), url=url)]])


def personal_filename() -> str:
    return os.environ.get("BONUS_PERSONAL_FILENAME", "").strip() or DEFAULT_PERSONAL_FILENAME


def effect_id() -> str:
    """Telegram message effect: a full-screen animation, private chats only."""
    return os.environ.get("PAY_EFFECT_ID", "").strip()


def ready_file_id() -> str:
    """Telegram file_id of the ready-made bonus, uploaded to Telegram once."""
    return os.environ.get("BONUS_FILE_ID", "").strip()


def ready_file_path() -> Path | None:
    """Ready-made bonus: a Render secret file (secrets:name) or any path on disk."""
    raw = os.environ.get("BONUS_FILE_PATH", "").strip()
    if not raw:
        return None

    if raw.startswith("secrets:"):
        candidate = SECRETS_DIR / raw.split(":", 1)[1].strip()
    else:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_DIR / candidate

    return candidate if candidate.is_file() else None


def proverb_for(chat_id: int | None) -> dict[str, Any] | None:
    """A proverb that is stable for one chat and varies between chats."""
    global _proverbs_cache
    if _proverbs_cache is None:
        try:
            _proverbs_cache = [item for item in load_proverbs() if item.get("proverb")]
        except Exception as exc:
            print(f"Bonus: cannot load proverbs: {exc!r}", flush=True)
            _proverbs_cache = []
    if not _proverbs_cache:
        return None
    rng = random.Random(chat_id if chat_id is not None else 0)
    return rng.choice(_proverbs_cache)


def personal_text(info: dict[str, Any]) -> str:
    paid_at = info.get("paid_at")
    if not isinstance(paid_at, datetime):
        paid_at = datetime.now(timezone.utc)

    lines = [
        "ChatterAi — благодарность за поддержку",
        "=" * 40,
        "",
        "Спасибо! Этот файл — личный знак благодарности за донат.",
        "",
        f"Платёж:       {info.get('amount_text', '—')}",
        f"Способ:       {info.get('source_label', '—')}",
        f"Дата (UTC):   {paid_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Идентификатор: {info.get('reference', '—')}",
        f"Чат:          {info.get('chat_id', '—')}",
        "",
    ]

    item = proverb_for(info.get("chat_id"))
    if item:
        lines.append("Пословица в тему:")
        lines.append(f"«{item.get('proverb', '')}»")
        meaning = (item.get("meaning") or "").strip()
        if meaning:
            lines.append("")
            lines.append(meaning)
        lines.append("")

    url = link_url()
    if url:
        lines.append(f"Ссылка: {url}")
        lines.append("")

    lines.extend([
        "-" * 40,
        "Доступ к функциям бота остаётся бесплатным: этот файл — подарок, а не пропуск.",
        "Вопросы по платежу — команда /paysupport.",
    ])
    return "\n".join(lines)


def personal_document(info: dict[str, Any]) -> InputFile:
    payload = personal_text(info).encode("utf-8")
    return InputFile(io.BytesIO(payload), filename=personal_filename())


def ready_document(path: Path) -> InputFile:
    return InputFile(io.BytesIO(path.read_bytes()), filename=path.name)


async def _send_ready(bot: Any, chat_id: int) -> str | None:
    """Send the ready-made bonus: by Telegram file_id first, then from a local path."""
    file_id = ready_file_id()
    if file_id:
        try:
            await bot.send_animation(chat_id=chat_id, animation=file_id, caption=READY_CAPTION)
            return f"file_id:{file_id[:8]}…"
        except Exception as exc:
            print(f"Bonus: send_animation by file_id failed, trying a document: {exc!r}", flush=True)
            try:
                await bot.send_document(chat_id=chat_id, document=file_id, caption=READY_CAPTION)
                return f"file_id:{file_id[:8]}…"
            except Exception as document_exc:
                print(f"Bonus: ready file by file_id failed for chat {chat_id}: {document_exc!r}", flush=True)
                return None

    path = ready_file_path()
    if path is None:
        configured = os.environ.get("BONUS_FILE_PATH", "").strip()
        if configured:
            print(f"Bonus: BONUS_FILE_PATH={configured!r} does not point to an existing file", flush=True)
        return None

    try:
        document = ready_document(path)
        if path.suffix.lower() in ANIMATION_SUFFIXES:
            await bot.send_animation(chat_id=chat_id, animation=document, caption=READY_CAPTION)
        else:
            await bot.send_document(chat_id=chat_id, document=document, caption=READY_CAPTION)
        return path.name
    except Exception as exc:
        print(f"Bonus: ready file delivery failed for chat {chat_id}: {exc!r}", flush=True)
        return None


async def _send_personal(bot: Any, chat_id: int, info: dict[str, Any]) -> tuple[bool, str | None]:
    """Send the personal file with the payment effect, falling back without it.

    Returns (delivered, effect_actually_applied).
    """
    effect = effect_id() or None
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=personal_document(info),
            caption=CAPTION,
            reply_markup=link_keyboard(),
            message_effect_id=effect,
        )
        return True, effect
    except Exception as exc:
        if not effect:
            print(f"Bonus: personal file delivery failed for chat {chat_id}: {exc!r}", flush=True)
            return False, None

        print(f"Bonus: effect {effect!r} rejected, retrying without it: {exc!r}", flush=True)
        try:
            await bot.send_document(
                chat_id=chat_id,
                document=personal_document(info),
                caption=CAPTION,
                reply_markup=link_keyboard(),
            )
            return True, None
        except Exception as retry_exc:
            print(f"Bonus: personal file delivery failed for chat {chat_id}: {retry_exc!r}", flush=True)
            return False, None


async def deliver(bot: Any, chat_id: int, info: dict[str, Any]) -> dict[str, Any]:
    """Send the personal file, then the ready-made bonus and the link button."""
    result: dict[str, Any] = {"personal": None, "ready": None, "link": link_url() or None, "effect": None}

    delivered, applied_effect = await _send_personal(bot, chat_id, info)
    if delivered:
        result["personal"] = personal_filename()
        result["effect"] = applied_effect

    result["ready"] = await _send_ready(bot, chat_id)

    print(
        f"Bonus delivered: chat={chat_id} personal={result['personal']!r} "
        f"ready={result['ready']!r} link={bool(result['link'])}",
        flush=True,
    )
    return result
