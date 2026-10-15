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
FONT_DIR = PROJECT_DIR / "assets" / "fonts"
FONT_REGULAR = FONT_DIR / "PT_Sans-Web-Regular.ttf"
FONT_BOLD = FONT_DIR / "PT_Sans-Web-Bold.ttf"
CARD_FILENAME = "chatterai-blagodarnost.png"
CARD_WIDTH = 1080
CARD_HEIGHT = 1350
CARD_FRAME = 60
CARD_PAD = 60
COLOR_BG = (15, 19, 25)
COLOR_PANEL = (26, 32, 43)
COLOR_BORDER = (39, 48, 64)
COLOR_TEXT = (232, 237, 246)
COLOR_MUTED = (150, 163, 184)
COLOR_ACCENT = (76, 194, 255)
COLOR_AMBER = (255, 212, 121)
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


def card_enabled() -> bool:
    if os.environ.get("BONUS_PERSONAL_FORMAT", "png").strip().lower() == "txt":
        return False
    return FONT_REGULAR.is_file() and FONT_BOLD.is_file()


def _font(bold: bool, size: int):
    from PIL import ImageFont

    return ImageFont.truetype(str(FONT_BOLD if bold else FONT_REGULAR), size)


def _wrap(draw: Any, text: str, font: Any, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if not current or draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _format_paid_at(info: dict[str, Any]) -> str:
    paid_at = info.get("paid_at")
    if not isinstance(paid_at, datetime):
        paid_at = datetime.now(timezone.utc)
    return paid_at.strftime("%Y-%m-%d %H:%M UTC")


def _card_amount(text: str) -> str:
    """PT Sans has no emoji, and a missing glyph renders as tofu — keep only drawable characters."""
    cleaned = "".join(ch for ch in text if ord(ch) < 0x2000 or ch in "—–…")
    return " ".join(cleaned.split()) or text


def card_bytes(info: dict[str, Any]) -> bytes:
    """Render the personal reward as a PNG card. No emoji: the bundled font has none."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), COLOR_BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        [CARD_FRAME, CARD_FRAME, CARD_WIDTH - CARD_FRAME, CARD_HEIGHT - CARD_FRAME],
        radius=48,
        fill=COLOR_PANEL,
        outline=COLOR_BORDER,
        width=2,
    )

    left = CARD_FRAME + CARD_PAD
    right = CARD_WIDTH - CARD_FRAME - CARD_PAD
    usable = right - left

    url = link_url()
    footer: list[tuple[str, tuple[int, int, int]]] = []
    if url:
        footer.append((url, COLOR_ACCENT))
    footer.append(("Доступ к функциям бота остаётся бесплатным.", COLOR_MUTED))
    footer.append(("Вопросы по платежу — команда /paysupport", COLOR_MUTED))
    footer_y = CARD_HEIGHT - CARD_FRAME - CARD_PAD - 42 * (len(footer) - 1)

    y = CARD_FRAME + CARD_PAD
    draw.text((left, y), "ChatterAi", font=_font(True, 44), fill=COLOR_ACCENT)
    y += 78
    draw.text((left, y), "Спасибо за поддержку", font=_font(True, 58), fill=COLOR_TEXT)
    y += 104
    draw.line([(left, y), (right, y)], fill=COLOR_BORDER, width=2)
    y += 44

    draw.text((left, y), "СУММА", font=_font(False, 28), fill=COLOR_MUTED)
    y += 42
    draw.text((left, y), _card_amount(str(info.get("amount_text") or "—")), font=_font(True, 88), fill=COLOR_AMBER)
    y += 132

    reference = str(info.get("reference") or "—")
    if len(reference) > 34:
        reference = reference[:31] + "…"

    for label, value in (
        ("Способ", str(info.get("source_label") or "—")),
        ("Дата", _format_paid_at(info)),
        ("Идентификатор", reference),
        ("Чат", str(info.get("chat_id") or "—")),
    ):
        draw.text((left, y), label, font=_font(False, 30), fill=COLOR_MUTED)
        draw.text((left + 320, y), value, font=_font(False, 32), fill=COLOR_TEXT)
        y += 56

    y += 20
    draw.line([(left, y), (right, y)], fill=COLOR_BORDER, width=2)
    y += 44

    text_limit = footer_y - 40
    item = proverb_for(info.get("chat_id"))
    if item and y < text_limit:
        for line in _wrap(draw, f"«{item.get('proverb', '')}»", _font(True, 40), usable):
            if y >= text_limit:
                break
            draw.text((left, y), line, font=_font(True, 40), fill=COLOR_TEXT)
            y += 54

        meaning = (item.get("meaning") or "").strip()
        if meaning:
            y += 8
            for line in _wrap(draw, meaning, _font(False, 30), usable):
                if y >= text_limit:
                    break
                draw.text((left, y), line, font=_font(False, 30), fill=COLOR_MUTED)
                y += 44

    for line, color in footer:
        draw.text((left, footer_y), line, font=_font(False, 30), fill=color)
        footer_y += 42

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


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


async def _send_card(bot: Any, chat_id: int, info: dict[str, Any], effect: str | None) -> None:
    await bot.send_photo(
        chat_id=chat_id,
        photo=InputFile(io.BytesIO(card_bytes(info)), filename=CARD_FILENAME),
        caption=CAPTION,
        reply_markup=link_keyboard(),
        message_effect_id=effect,
    )


async def _send_text_file(bot: Any, chat_id: int, info: dict[str, Any], effect: str | None) -> None:
    await bot.send_document(
        chat_id=chat_id,
        document=personal_document(info),
        caption=CAPTION,
        reply_markup=link_keyboard(),
        message_effect_id=effect,
    )


async def _send_personal(
    bot: Any, chat_id: int, info: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """Send the reward as a card when possible, otherwise as a text file.

    Returns (delivered, effect_actually_applied, what_was_sent).
    """
    senders: list[tuple[str, Any]] = []
    if card_enabled():
        senders.append((CARD_FILENAME, _send_card))
    senders.append((personal_filename(), _send_text_file))

    effect = effect_id() or None
    for name, send in senders:
        try:
            await send(bot, chat_id, info, effect)
            return True, effect, name
        except Exception as exc:
            if effect:
                print(f"Bonus: {name} rejected effect {effect!r}, retrying without it: {exc!r}", flush=True)
                try:
                    await send(bot, chat_id, info, None)
                    return True, None, name
                except Exception as retry_exc:
                    print(f"Bonus: {name} delivery failed for chat {chat_id}: {retry_exc!r}", flush=True)
                    continue
            print(f"Bonus: {name} delivery failed for chat {chat_id}: {exc!r}", flush=True)
    return False, None, None


async def deliver(bot: Any, chat_id: int, info: dict[str, Any]) -> dict[str, Any]:
    """Send the personal file, then the ready-made bonus and the link button."""
    result: dict[str, Any] = {"personal": None, "ready": None, "link": link_url() or None, "effect": None}

    delivered, applied_effect, sent = await _send_personal(bot, chat_id, info)
    if delivered:
        result["personal"] = sent
        result["effect"] = applied_effect

    result["ready"] = await _send_ready(bot, chat_id)

    print(
        f"Bonus delivered: chat={chat_id} personal={result['personal']!r} "
        f"ready={result['ready']!r} link={bool(result['link'])}",
        flush=True,
    )
    return result
