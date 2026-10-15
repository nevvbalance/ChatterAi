import html
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any

import state as state_store

CURRENCY = "XTR"
PAYLOAD_KIND = "stars-donate"
START_PARAMETER = "stars"
DEFAULT_AMOUNTS = "1,10,25,100"
MAX_AMOUNT = 100000
PAYLOAD_LIMIT = 128
RECORDS_LIMIT = 500
RECEIPT_HINT = "Чек и история платежа видны в приложении Telegram."


def amounts() -> list[int]:
    """Star amounts offered as buttons, configurable via STARS_AMOUNTS."""
    raw = os.environ.get("STARS_AMOUNTS", DEFAULT_AMOUNTS)
    parsed: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        value = chunk.strip()
        if not value:
            continue
        try:
            number = int(value)
        except ValueError:
            print(f"Stars: STARS_AMOUNTS entry {value!r} is not a number, skipping", flush=True)
            continue
        if 0 < number <= MAX_AMOUNT and number not in parsed:
            parsed.append(number)
    if not parsed:
        print("Stars: STARS_AMOUNTS is empty or invalid, falling back to defaults", flush=True)
        return [int(value) for value in DEFAULT_AMOUNTS.split(",") if value.isdigit()]
    return parsed


def payload_for(chat_id: int, amount: int) -> str:
    token = secrets.token_hex(4)
    payload = f"{PAYLOAD_KIND}:{chat_id}:{amount}:{token}"
    if len(payload.encode("utf-8")) > PAYLOAD_LIMIT:
        raise ValueError("stars payload is too long for the Bot API")
    return payload


def parse_payload(payload: Any) -> tuple[int, int] | None:
    """Return (chat_id, amount) for our own invoice payloads, otherwise None."""
    if not isinstance(payload, str):
        return None
    parts = payload.split(":")
    if len(parts) != 4 or parts[0] != PAYLOAD_KIND:
        return None
    try:
        chat_id = int(parts[1])
        amount = int(parts[2])
    except ValueError:
        return None
    if amount <= 0 or amount > MAX_AMOUNT:
        return None
    return chat_id, amount


def check_invoice(payload: Any, currency: Any, total_amount: Any) -> str | None:
    """None means the pre-checkout may be approved; otherwise a reason for the log."""
    parsed = parse_payload(payload)
    if parsed is None:
        return "unknown invoice payload"
    if currency != CURRENCY:
        return f"unexpected currency {currency!r}"
    if total_amount != parsed[1]:
        return f"amount mismatch: query={total_amount} payload={parsed[1]}"
    return None


def price_label(amount: int) -> str:
    return f"{amount} ⭐ поддержка"


def invoice_title(amount: int) -> str:
    return f"Поддержка ChatterAi — {amount} ⭐"


def invoice_description() -> str:
    return "Добровольный донат в поддержку бота. Все функции остаются бесплатными, ничего не блокируется."


def format_offer(chat_id: int, star_amounts: list[int], balance: int | None = None) -> str:
    buttons = ", ".join(f"⭐ {amount}" for amount in star_amounts)
    lines = [
        "⭐ <b>Поддержать ChatterAi звёздами Telegram</b>\n",
        "Звёзды — внутренняя валюта Telegram, это самый быстрый способ: ни кошелька, ни адреса, ни комментария не нужно.",
        "",
        f"Выбери сумму: {buttons}.",
        "Платёж придёт мне через Telegram, чек останется у тебя в приложении.",
        "После оплаты бот сразу пришлёт небольшой файл с благодарностью и ссылку.",
        "",
        "Это подарок, а не пропуск: доступ к функциям бота как был, так и остаётся бесплатным.",
    ]
    if balance is not None:
        lines.append("")
        lines.append(f"⭐ баланс бота: <b>{balance}</b> звёзд")
    lines.append("")
    lines.append("Оплата, возврат и условия — /terms и /paysupport.")
    return "\n".join(lines)


def format_thanks(payment: Any) -> str:
    amount = getattr(payment, "total_amount", 0)
    return (
        "⭐ <b>Спасибо за поддержку звёздами!</b>\n\n"
        f"Пришло: <b>{amount} ⭐</b>\n"
        f"{html.escape(RECEIPT_HINT)}\n\n"
        "Пословица в тему: «Долг платежом красен» 🪶"
    )


def format_terms() -> str:
    return (
        "📄 <b>Условия оплаты</b>\n\n"
        "Оплата звёздами Telegram — добровольный донат в поддержку бота ChatterAi.\n"
        "Что приходит после оплаты: персональный текстовый файл с благодарностью — "
        "сумма, дата, идентификатор платежа и пословица — и ссылка. "
        "Функции бота при этом остаются бесплатными и не блокируются: файл это подарок, "
        "а не пропуск к доступу.\n\n"
        "Как это работает:\n"
        "• платёж проходит внутри Telegram в валюте XTR (звёзды), операцию обеспечивает Telegram;\n"
        "• все платежи необратимы на стороне Telegram, возврат делает владелец бота через "
        "refundStarPayment;\n"
        "• чек и история покупок доступны в приложении Telegram;\n"
        "• поддержка Telegram не помогает с покупками внутри этого бота.\n\n"
        "Запрос на возврат или вопрос по платежу — команда /paysupport.\n\n"
        "Отправляя платёж, ты подтверждаешь, что прочитал эти условия и согласен с ними."
    )


def format_support() -> str:
    contact = os.environ.get("SUPPORT_CONTACT", "").strip()
    lines = [
        "🛟 <b>Поддержка по платежам</b>\n",
        "Если платёж не прошёл, списался дважды или нужен возврат — напиши мне, "
        "поддержка Telegram такими покупками не занимается.",
        "",
        "В сообщении укажи: сумму в звёздах, дату и id платежа из чека в приложении Telegram.",
    ]
    if contact:
        lines.append("")
        lines.append(f"Контакт: {html.escape(contact)}")
    else:
        lines.append("")
        lines.append("Контакт для связи пока не задан владельцем бота.")
    lines.append("")
    lines.append("Владелец бота рассматривает такие запросы и может вернуть звёзды через refundStarPayment.")
    return "\n".join(lines)


def payment_info(payment: Any, chat_id: int | None) -> dict[str, Any]:
    """Payment details for the personal bonus file."""
    return {
        "source": "stars",
        "source_label": "Telegram Stars (XTR)",
        "amount_text": f"{getattr(payment, 'total_amount', 0)} ⭐",
        "paid_at": datetime.now(timezone.utc),
        "reference": getattr(payment, "telegram_payment_charge_id", "") or "—",
        "chat_id": chat_id,
    }


def record_payment(payment: Any, chat_id: int | None, user_id: int | None) -> dict[str, Any]:
    """Append the payment to the state file and log it: the charge id is needed for refunds."""
    record = {
        "telegram_payment_charge_id": getattr(payment, "telegram_payment_charge_id", "") or "",
        "provider_payment_charge_id": getattr(payment, "provider_payment_charge_id", "") or "",
        "currency": getattr(payment, "currency", "") or "",
        "amount": getattr(payment, "total_amount", 0),
        "invoice_payload": getattr(payment, "invoice_payload", "") or "",
        "chat_id": chat_id,
        "user_id": user_id,
        "at": int(time.time()),
    }

    data = state_store.load_state(state_store.stars_state_file())
    records = data.get("payments")
    if not isinstance(records, list):
        records = []
    records.append(record)
    data["payments"] = records[-RECORDS_LIMIT:]
    data["total_stars"] = sum(int(item.get("amount") or 0) for item in data["payments"] if isinstance(item, dict))
    state_store.save_state(data, state_store.stars_state_file())

    print(
        "Stars payment recorded: "
        f"charge_id={record['telegram_payment_charge_id']} "
        f"amount={record['amount']} {record['currency']} "
        f"chat={chat_id} user={user_id} payload={record['invoice_payload']}",
        flush=True,
    )
    return record


def saved_records() -> list[dict[str, Any]]:
    data = state_store.load_state(state_store.stars_state_file())
    records = data.get("payments")
    return records if isinstance(records, list) else []
