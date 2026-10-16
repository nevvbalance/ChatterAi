from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CALLBACK_PREFIX = "pick"
ACTION_SHOW = "show"
ACTION_CHOOSE = "choose"
DEFAULT_PLACEHOLDER = "🟣"
CALLBACK_LIMIT = 64
MODE_ALL = "all"
MODE_ANIMATED = "anim"
MODES = (MODE_ALL, MODE_ANIMATED)
ANIMATED_WORDS = ("animated", "anim", "анимированные", "анимация")


def encode(pack: str, index: int, action: str, mode: str = MODE_ALL) -> str:
    payload = f"{CALLBACK_PREFIX}:{action}:{mode}:{pack}:{index}"
    if len(payload.encode("utf-8")) > CALLBACK_LIMIT:
        raise ValueError("pack name is too long for callback data")
    return payload


def decode(data: str) -> tuple[str, int, str, str] | None:
    parts = (data or "").split(":")
    if len(parts) != 5 or parts[0] != CALLBACK_PREFIX:
        return None
    action, mode, pack, raw_index = parts[1], parts[2], parts[3], parts[4]
    if action not in (ACTION_SHOW, ACTION_CHOOSE) or mode not in MODES or not pack:
        return None
    try:
        index = int(raw_index)
    except ValueError:
        return None
    if index < 0:
        return None
    return pack, index, action, mode


def parse_mode(value: str | None) -> str:
    text = (value or "").strip().lower()
    return MODE_ANIMATED if text in ANIMATED_WORDS else MODE_ALL


def cache_key(pack: str, mode: str) -> str:
    return f"{pack}|{mode}"


def normalize(sticker: Any) -> dict[str, Any]:
    custom_id = getattr(sticker, "custom_emoji_id", None)
    return {
        "kind": "custom_emoji" if custom_id else "sticker",
        "id": custom_id or getattr(sticker, "file_id", ""),
        "file_id": getattr(sticker, "file_id", ""),
        "emoji": getattr(sticker, "emoji", "") or "",
        "is_animated": bool(getattr(sticker, "is_animated", False)),
        "is_video": bool(getattr(sticker, "is_video", False)),
    }


def filter_items(items: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    if mode == MODE_ANIMATED:
        return [item for item in items if item["is_animated"] or item["is_video"]]
    return list(items)


def summarize(title: str, pack: str, all_items: list[dict[str, Any]], shown: list[dict[str, Any]], mode: str) -> str:
    animated = sum(1 for item in all_items if item["is_animated"])
    video = sum(1 for item in all_items if item["is_video"])
    custom = sum(1 for item in all_items if item["kind"] == "custom_emoji")

    lines = [
        f"Набор: {title}",
        f"Имя: {pack}",
        f"Всего элементов: {len(all_items)}",
        f"Анимированных: {animated} · видео: {video} · кастомных эмодзи: {custom}",
    ]
    if mode == MODE_ANIMATED:
        lines.append(f"Показываю только анимированные: {len(shown)} из {len(all_items)}")
    lines.append("")
    lines.append("Листай кнопками ниже. Когда найдёшь нужный — нажми «Этот», и я запишу его id.")
    return "\n".join(lines)


def describe(item: dict[str, Any], position: int, total: int) -> str:
    kind = "кастомный эмодзи" if item["kind"] == "custom_emoji" else "стикер"
    marker = "анимированный" if item["is_animated"] else ("видео" if item["is_video"] else "статичный")
    return f"№{position} из {total} · {kind} · {marker}"


def keyboard(pack: str, index: int, total: int, mode: str = MODE_ALL) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬅️ Назад", callback_data=encode(pack, (index - 1) % total, ACTION_SHOW, mode)),
                InlineKeyboardButton(
                    "➡️ Прислать ещё", callback_data=encode(pack, (index + 1) % total, ACTION_SHOW, mode)
                ),
            ],
            [InlineKeyboardButton("✅ Этот", callback_data=encode(pack, index, ACTION_CHOOSE, mode))],
        ]
    )


def emoji_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def emoji_message(item: dict[str, Any], position: int, total: int) -> tuple[str, list[dict[str, Any]]]:
    """Message text with the custom emoji on the first line and its entity."""
    placeholder = item.get("emoji") or DEFAULT_PLACEHOLDER
    text = f"{placeholder}\n\n{describe(item, position, total)}"
    entities = [
        {
            "type": "custom_emoji",
            "offset": 0,
            "length": emoji_units(placeholder),
            "custom_emoji_id": item["id"],
        }
    ]
    return text, entities
