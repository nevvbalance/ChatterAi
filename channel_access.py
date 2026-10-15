import os
from typing import Any

import stars
import ton_payments

DEFAULT_SCAN_LIMIT = 200


def target_chat() -> int | str | None:
    """Channel the bot may auto-approve in: numeric id or @username, None means any chat."""
    raw = os.environ.get("JOIN_APPROVE_CHAT", "").strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw.lstrip("@")


def scan_limit() -> int:
    try:
        return max(1, int(os.environ.get("JOIN_SCAN_LIMIT", str(DEFAULT_SCAN_LIMIT))))
    except ValueError:
        return DEFAULT_SCAN_LIMIT


def _stars_evidence(user_chat_id: int, user_id: int) -> dict[str, Any] | None:
    for record in reversed(stars.saved_records()):
        if not isinstance(record, dict):
            continue
        identifiers = {record.get("chat_id"), record.get("user_id")}
        if user_chat_id in identifiers or user_id in identifiers:
            return {
                "source": "stars",
                "source_label": "Telegram Stars",
                "amount": f"{record.get('amount')} ⭐",
                "reference": record.get("telegram_payment_charge_id") or "",
                "at": record.get("at"),
                "durable": False,
            }
    return None


async def payment_evidence(user_chat_id: int, user_id: int) -> dict[str, Any] | None:
    """Find a confirmed payment behind a join request: stars record first, then the chain."""
    evidence = _stars_evidence(user_chat_id, user_id)
    if evidence is not None:
        return evidence

    try:
        donations = await ton_payments.collect_donations(scan_limit())
    except Exception as exc:
        print(f"Join guard: cannot read the TON wallet history: {exc!r}", flush=True)
        return None

    for donation in donations:
        if donation.get("chat_id") in (user_chat_id, user_id):
            return {
                "source": "gram",
                "source_label": "донат в Gram",
                "amount": f"{ton_payments.format_amount(int(donation.get('value_nano') or 0))} Gram",
                "reference": donation.get("hash") or "",
                "at": donation.get("now"),
                "durable": True,
            }
    return None


def format_approval_note(evidence: dict[str, Any]) -> str:
    return (
        "✅ <b>Оплата найдена — доступ в канал открыт</b>\n\n"
        f"Основание: <b>{evidence.get('source_label') or evidence.get('source')}</b>, "
        f"{evidence.get('amount')}\n"
        "Если это ошибка — напиши /paysupport."
    )
