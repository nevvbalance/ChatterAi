from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CALLBACK_PREFIX = "pick"
ACTION_SHOW = "show"
ACTION_CHOOSE = "choose"
DEFAULT_PLACEHOLDER = "🟣"
CALLBACK_LIMIT = 64


def encode(pack: str, index: int, action: str) -> str:
    payload = f"{CALLBACK_PREFIX}:{action}:{pack}:{index}"
    if len(payload.encode("utf-8")) > CALLBACK_LIMIT:
        raise ValueError("pack name is too long for callback data")
    return payload


def decode(data: str) -> tuple[str, int, str] | None:
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] != CALLBACK_PREFIX:
        return None
    action = parts[1]
    if action not in (ACTION_SHOW, ACTION_CHOOSE):
        return None
    try:
        index = int(parts[3])
    except ValueError:
        return None
    if not parts[2] or index < 0:
        return None
    return parts[2], index, action


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


def summarize(title: str, pack: str, items: list[dict[str, Any]]) -> str:
    animated = sum(1 for item in items if item["is_animated"])
    video = sum(1 for item in items if item["is_video"])
    custom = sum(1 for item in items if item["kind"] == "custom_emoji")
    lines = [
        f"Набор: {title}",
        f"Имя: {pack}",
        f"Всего элементов: {len(items)}",
        f"Анимированных: {animated} · видео: {video} · кастомных эмодзи: {custom}",
        "",
        "Решай глазами: анимированный стикер сам проиграется, а у кастомных эмодзи "
        "API анимацию не сообщает — её видно только в чате.",
        "",
        "Листай кнопками ниже. Когда найдёшь нужный — нажми «Этот», и я запишу его id.",
    ]
    return "\n".join(lines)


def describe(item: dict[str, Any], position: int, total: int) -> str:
    kind = "кастомный эмодзи" if item["kind"] == "custom_emoji" else "стикер"
    return f"№{position} из {total} · {kind}"


def keyboard(pack: str, index: int, total: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬅️ Назад", callback_data=encode(pack, (index - 1) % total, ACTION_SHOW)),
                InlineKeyboardButton("➡️ Прислать ещё", callback_data=encode(pack, (index + 1) % total, ACTION_SHOW)),
            ],
            [InlineKeyboardButton("✅ Этот", callback_data=encode(pack, index, ACTION_CHOOSE))],
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
