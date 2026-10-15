import asyncio
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import aiohttp

import state as state_store

API_BASE = os.environ.get("TON_API_BASE", "https://toncenter.com/api/v3").strip().rstrip("/")
WALLET_ADDRESS = os.environ.get("TON_WALLET_ADDRESS", "").strip()
COMMENT_PREFIX = os.environ.get("TON_COMMENT_PREFIX", "donate").strip() or "donate"
SUGGESTED_AMOUNT = os.environ.get("TON_DONATION_AMOUNT", "0.1").strip() or "0.1"
POLL_INTERVAL = int(os.environ.get("TON_POLL_INTERVAL", "120"))
HISTORY_LIMIT = int(os.environ.get("TON_HISTORY_LIMIT", "50"))
MAX_ATTEMPTS = int(os.environ.get("TON_MAX_ATTEMPTS", "4"))
BACKOFF_BASE = float(os.environ.get("TON_BACKOFF_BASE", "2"))
SEEN_LIMIT = 5000
NANO = 1_000_000_000
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=25)

COMMENT_RE = re.compile(rf"^{re.escape(COMMENT_PREFIX)}-(-?\d+)$")

Sender = Callable[[str, list[int]], Awaitable[None]]
Drain = Callable[[], Awaitable[None]]
Reward = Callable[[dict[str, Any]], Awaitable[None]]

_session: aiohttp.ClientSession | None = None


def bind_session(session: aiohttp.ClientSession | None) -> None:
    """Reuse the shared HTTP session instead of opening one per request."""
    global _session
    _session = session


def enabled() -> bool:
    """Donations are configured only when the receiving wallet address is set."""
    return bool(WALLET_ADDRESS)


def comment_for(chat_id: int) -> str:
    return f"{COMMENT_PREFIX}-{chat_id}"


def chat_id_from_comment(text: str | None) -> int | None:
    if not text:
        return None
    match = COMMENT_RE.match(text.strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def format_amount(value_nano: int) -> str:
    value = value_nano / NANO
    text = f"{value:.9f}".rstrip("0").rstrip(".")
    return text or "0"


def decoded_text(message_content: Any) -> str | None:
    """Pull the plain text comment out of API v3 message content.

    The indexer puts it under `decoded.comment` for ordinary transfers and
    under `decoded.payload` for bounce messages, so both shapes are checked.
    """
    if not isinstance(message_content, dict):
        return None
    decoded = message_content.get("decoded")
    if not isinstance(decoded, dict):
        return None
    payload = decoded.get("payload")
    for candidate in (decoded, payload if isinstance(payload, dict) else None):
        if not isinstance(candidate, dict):
            continue
        for field in ("comment", "text"):
            value = candidate.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def extract_donation(tx: Any) -> dict[str, Any] | None:
    """Keep only finalized, non-bounced Gram transfers whose comment is our invoice id."""
    if not isinstance(tx, dict):
        return None
    if tx.get("finality") != "finalized":
        return None

    in_msg = tx.get("in_msg")
    if not isinstance(in_msg, dict) or in_msg.get("bounced"):
        return None
    if in_msg.get("decoded_opcode") != "text_comment":
        return None

    comment = decoded_text(in_msg.get("message_content"))
    chat_id = chat_id_from_comment(comment)
    if chat_id is None:
        return None

    try:
        value_nano = int(in_msg.get("value") or 0)
    except (TypeError, ValueError):
        value_nano = 0

    try:
        created_at = int(tx.get("now") or 0)
    except (TypeError, ValueError):
        created_at = 0

    return {
        "chat_id": chat_id,
        "comment": comment,
        "lt": str(tx.get("lt") or ""),
        "hash": str(tx.get("hash") or ""),
        "source": str(in_msg.get("source") or ""),
        "value_nano": value_nano,
        "now": created_at,
        "mc_block_seqno": tx.get("mc_block_seqno"),
    }


async def _request(params: dict[str, str]) -> dict[str, Any]:
    url = f"{API_BASE}/transactions"
    headers: dict[str, str] = {}
    api_key = os.environ.get("TON_API_KEY", "").strip()
    if api_key:
        headers["X-API-Key"] = api_key

    session = _session
    own_session = session is None or session.closed
    if own_session:
        session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
    try:
        async with session.get(url, params=params, headers=headers) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"TON API HTTP {response.status}: {text[:300]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"TON API returned non-JSON body: {text[:300]}") from exc
    finally:
        if own_session and session is not None:
            await session.close()


async def fetch_transactions(limit: int | None = None) -> list[dict[str, Any]]:
    """Read the receiving wallet history with exponential backoff on failures."""
    if not enabled():
        return []

    params = {
        "account": WALLET_ADDRESS,
        "limit": str(limit or HISTORY_LIMIT),
        "sort": "desc",
    }

    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            data = await _request(params)
            transactions = data.get("transactions")
            return transactions if isinstance(transactions, list) else []
        except Exception as exc:
            last_error = exc
            print(f"TON API request failed (attempt {attempt + 1}/{MAX_ATTEMPTS}): {exc!r}", flush=True)
            if attempt + 1 < MAX_ATTEMPTS:
                await asyncio.sleep(BACKOFF_BASE**attempt)

    raise RuntimeError(f"TON API unavailable after {MAX_ATTEMPTS} attempt(s): {last_error!r}")


async def collect_donations(limit: int | None = None) -> list[dict[str, Any]]:
    """All finalized donations in the current window, oldest first."""
    transactions = await fetch_transactions(limit)
    donations = [donation for donation in (extract_donation(tx) for tx in transactions) if donation]
    donations.sort(key=lambda donation: _lt_value(donation.get("lt")))
    return donations


def _lt_value(lt: Any) -> int:
    try:
        return int(lt)
    except (TypeError, ValueError):
        return 0


def short_address(address: str) -> str:
    if len(address) <= 12:
        return address
    return f"{address[:6]}…{address[-6:]}"


def format_donate(chat_id: int, one_tap: bool = False) -> str:
    if not enabled():
        return (
            "💛 <b>Донаты пока не настроены</b>\n\n"
            "Владельцу бота нужно задать переменную окружения <code>TON_WALLET_ADDRESS</code> — "
            "адрес кошелька, на который будут приходить переводы."
        )

    amount = SUGGESTED_AMOUNT
    comment = comment_for(chat_id)
    one_tap_hint = (
        "\n\nКнопка ниже откроет страницу оплаты: кошелёк подставит сумму и комментарий сам."
        if one_tap
        else ""
    )
    return (
        "💛 <b>Поддержать ChatterAi</b>\n\n"
        "Бот бесплатный, но если хочется отблагодарить — вот кошелёк:\n\n"
        f"<b>Адрес:</b>\n<code>{html.escape(WALLET_ADDRESS)}</code>\n\n"
        f"<b>Сумма:</b> от {html.escape(amount)} Gram (можно любую)\n"
        f"<b>Комментарий:</b> <code>{html.escape(comment)}</code>\n\n"
        "Комментарий обязателен — по нему я понимаю, от кого перевод.\n\n"
        "⚠️ Важные мелочи:\n"
        "• отправляй с личного кошелька (Tonkeeper, Tonhub, Telegram Wallet), "
        "а не с биржи: биржи часто не передают комментарий, и платёж потеряется;\n"
        "• добавь к сумме чуть-чуть сверху на комиссию сети;\n"
        "• проверить, дошло ли, можно командой /mydonations.\n\n"
        f"Я проверяю новые переводы каждые {POLL_INTERVAL} сек и говорю спасибо в этот чат.{one_tap_hint}"
        "\n\nПосле зачисления бот пришлёт файл с благодарностью и ссылку на закрытый канал."
    )


def format_thanks(donation: dict[str, Any]) -> str:
    return (
        "💛 <b>Спасибо за поддержку!</b>\n\n"
        f"Пришло: <b>{format_amount(donation['value_nano'])} Gram</b>\n"
        f"Комментарий: <code>{html.escape(donation.get('comment') or '')}</code>\n"
        f"От: <code>{html.escape(short_address(donation.get('source') or ''))}</code>\n"
        f"Транзакция: <code>{html.escape(donation.get('hash') or '')[:44]}…</code>\n\n"
        "Пословица в тему: «Мир не без добрых людей» 🪶"
    )


def donation_info(donation: dict[str, Any]) -> dict[str, Any]:
    """Payment details for the personal bonus file."""
    created_at = donation.get("now")
    paid_at = (
        datetime.fromtimestamp(int(created_at), tz=timezone.utc)
        if created_at
        else datetime.now(timezone.utc)
    )
    return {
        "source": "gram",
        "source_label": "Gram (TON mainnet)",
        "amount_text": f"{format_amount(int(donation.get('value_nano') or 0))} Gram",
        "paid_at": paid_at,
        "reference": donation.get("hash") or "—",
        "chat_id": donation.get("chat_id"),
    }


def format_donations(donations: list[dict[str, Any]], chat_id: int) -> str:
    if not donations:
        return (
            "Пока не вижу подтверждённых переводов с твоим комментарием. 🧐\n\n"
            f"Посмотреть реквизиты — /donate.\n"
            f"Помни: комментарий должен быть <code>{html.escape(comment_for(chat_id))}</code>."
        )

    total = sum(donation["value_nano"] for donation in donations)
    lines = [
        "💛 <b>Твои подтверждённые донаты</b>\n",
        f"Всего: <b>{format_amount(total)} Gram</b> за {len(donations)} перевод(ов)\n",
    ]
    for donation in donations[-10:]:
        when = "дата неизвестна"
        if donation.get("now"):
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(donation["now"]))
        lines.append(
            f"• <b>{format_amount(donation['value_nano'])} Gram</b> — {when}, "
            f"<code>{html.escape(short_address(donation.get('source') or ''))}</code>"
        )
    lines.append("")
    lines.append("Подтверждаю только переводы, попавшие в masterchain: это необратимо и уже точно настоящее.")
    return "\n".join(lines)


class TonDonationMonitor:
    """Polls the receiving wallet and thanks payers. The blockchain is the source of truth."""

    def __init__(self, interval: int = POLL_INTERVAL, history_limit: int = HISTORY_LIMIT) -> None:
        self.interval = interval
        self.history_limit = history_limit
        self.seen: set[str] = set()
        self.last_lt = ""
        self.baseline_done = False
        self.state_restored = False
        self.sender: Sender | None = None
        self.drain: Drain | None = None
        self.reward: Reward | None = None
        self.task: asyncio.Task | None = None
        self.running = False
        self.cycle = 0
        self.last_poll_at: float | None = None
        self.last_error: str | None = None
        self.started_at: float | None = None
        self.persistence_ok = True
        self.thanks_sent = 0
        self.last_new = 0
        self.last_seen_in_window = 0

    def load(self) -> None:
        data = state_store.load_state(state_store.donations_state_file())
        hashes = data.get("seen")
        if isinstance(hashes, list):
            self.seen = {str(value) for value in hashes}
        self.last_lt = str(data.get("last_lt") or "")
        self.baseline_done = bool(data.get("baseline_done"))
        self.state_restored = bool(self.seen) or self.baseline_done
        print(
            f"TON donations state loaded: seen={len(self.seen)}, baseline_done={self.baseline_done}",
            flush=True,
        )

    def save(self) -> None:
        payload = {
            "seen": sorted(self.seen)[-SEEN_LIMIT:],
            "last_lt": self.last_lt,
            "baseline_done": self.baseline_done,
            "cycle": self.cycle,
            "thanks_sent": self.thanks_sent,
        }
        self.persistence_ok = state_store.save_state(payload, state_store.donations_state_file())

    def start(self, sender: Sender, drain: Drain, reward: Reward | None = None) -> None:
        if not enabled():
            print("TON donations monitor not started: TON_WALLET_ADDRESS is not configured", flush=True)
            return
        self.sender = sender
        self.drain = drain
        self.reward = reward
        self.started_at = time.time()
        if self.task and not self.task.done():
            print("TON donations monitor start requested, but task is already running", flush=True)
            return
        self.task = asyncio.create_task(self.loop())
        print(f"TON donations monitor task created: {self.task!r}", flush=True)

    async def stop(self) -> None:
        self.running = False
        task = self.task
        self.task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.save()

    async def loop(self) -> None:
        self.running = True
        print(f"TON donations monitor started, interval={self.interval}s", flush=True)
        try:
            while self.running:
                await self.poll()
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            print("TON donations monitor cancelled", flush=True)
            raise
        except Exception as exc:
            self.last_error = repr(exc)
            print(f"TON donations monitor loop CRASHED: {exc!r}", flush=True)
        finally:
            self.running = False
            print("TON donations monitor stopped", flush=True)

    async def poll(self) -> list[dict[str, Any]]:
        self.cycle += 1
        self.last_poll_at = time.time()
        self.last_new = 0

        try:
            donations = await collect_donations(self.history_limit)
            self.last_error = None
        except Exception as exc:
            self.last_error = repr(exc)
            print(f"TON donations poll #{self.cycle} ERROR: {exc!r}", flush=True)
            return []

        self.last_seen_in_window = len(donations)

        if not self.baseline_done:
            self.baseline_done = True
            self.seen.update(donation["hash"] for donation in donations)
            self.last_lt = _max_lt(self.last_lt, donations)
            print(
                f"TON donations baseline: {len(donations)} finalized payment(s) treated as already processed",
                flush=True,
            )
            self.save()
            return []

        fresh = [donation for donation in donations if donation["hash"] not in self.seen]
        self.seen.update(donation["hash"] for donation in fresh)
        self.last_lt = _max_lt(self.last_lt, donations)
        self.last_new = len(fresh)

        print(
            f"TON donations poll #{self.cycle}: {len(donations)} finalized payment(s) in window, new={len(fresh)}",
            flush=True,
        )

        for donation in fresh:
            print(
                "TON donation confirmed: "
                f"chat={donation['chat_id']} lt={donation['lt']} hash={donation['hash']} "
                f"value_nano={donation['value_nano']} mc_block={donation['mc_block_seqno']}",
                flush=True,
            )
            if self.sender is not None:
                await self.sender(format_thanks(donation), [donation["chat_id"]])
                self.thanks_sent += 1
            if self.reward is not None:
                await self.reward(donation)

        if self.drain is not None:
            await self.drain()
        self.save()
        return fresh

    async def donations_for_chat(self, chat_id: int) -> list[dict[str, Any]]:
        donations = await collect_donations(self.history_limit)
        return [donation for donation in donations if donation["chat_id"] == chat_id]

    def status_text(self) -> str:
        task_state = "нет задачи"
        if self.task:
            task_state = "завершена" if self.task.done() else "жива"

        last_poll = "ещё не было"
        if self.last_poll_at:
            last_poll = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self.last_poll_at))

        uptime = "не запущен"
        if self.started_at:
            uptime = str(time.strftime("%H:%M:%S", time.gmtime(time.time() - self.started_at)))

        wallet = html.escape(WALLET_ADDRESS) if enabled() else "не задан (TON_WALLET_ADDRESS пуст)"
        return (
            "📊 <b>Статус донатов TON</b>\n\n"
            f"🌐 сеть: <b>mainnet</b> (API <code>{html.escape(API_BASE)}</code>)\n"
            f"👛 кошелёк: <code>{wallet}</code>\n"
            f"🏷 префикс комментария: <code>{html.escape(COMMENT_PREFIX)}</code>\n"
            f"🟢 running: <b>{self.running}</b>\n"
            f"⚙️ task: <b>{task_state}</b>\n"
            f"🔄 цикл: <b>{self.cycle}</b>\n"
            f"⏱ работает: <b>{uptime}</b>\n"
            f"🕒 последний опрос: <b>{last_poll}</b>\n"
            f"🧾 платежей в окне: <b>{self.last_seen_in_window}</b>\n"
            f"🆕 новых в последнем цикле: <b>{self.last_new}</b>\n"
            f"💛 благодарностей отправлено: <b>{self.thanks_sent}</b>\n"
            f"🎯 baseline собран: <b>{self.baseline_done}</b>\n"
            f"🧷 состояние сохранено: <b>{self.persistence_ok}</b>\n"
            f"⚠️ последняя ошибка: <code>{html.escape(self.last_error or 'нет')}</code>"
        )


def _max_lt(current: str, donations: list[dict[str, Any]]) -> str:
    best = _lt_value(current)
    for donation in donations:
        best = max(best, _lt_value(donation.get("lt")))
    return str(best) if best else current
