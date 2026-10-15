import base64
import json
import os
from decimal import Decimal, InvalidOperation
from string import Template
from typing import Any
from urllib.parse import urljoin

import ton_payments

PAY_PATH = "/pay"
MANIFEST_PATH = "/tonconnect-manifest.json"
ICON_PATH = "/static/icon-180.png"
APP_NAME = "ChatterAi"
NETWORK_MAINNET = "-239"
VALID_FOR_SECONDS = 600
NANO = Decimal(10) ** 9
COMMENT_MAX_BYTES = 123

CRC32C_POLY = 0x82F63B78


def _crc32c_table() -> list[int]:
    table: list[int] = []
    for index in range(256):
        value = index
        for _ in range(8):
            value = (value >> 1) ^ (CRC32C_POLY if value & 1 else 0)
        table.append(value)
    return table


_CRC32C_TABLE = _crc32c_table()


def crc32c(data: bytes) -> int:
    value = 0xFFFFFFFF
    for byte in data:
        value = _CRC32C_TABLE[(value ^ byte) & 0xFF] ^ (value >> 8)
    return value ^ 0xFFFFFFFF


def text_comment_payload(text: str) -> str:
    """Base64 BoC of a single cell carrying a text comment.

    Layout follows the documented standard cell representation: d1 = 0 (no refs,
    ordinary cell, level 0), d2 = floor(bits/8) + ceil(bits/8), body = 32 zero
    bits (text-comment op) followed by the UTF-8 bytes. Comments longer than 123
    bytes need a snake chain and are rejected here on purpose.
    """
    payload = text.encode("utf-8")
    if len(payload) > COMMENT_MAX_BYTES:
        raise ValueError(f"comment is {len(payload)} bytes, which does not fit into a single cell")

    bits = 32 + 8 * len(payload)
    cell = bytes([0, bits // 8 + (bits + 7) // 8]) + b"\x00" * 4 + payload
    body = bytes.fromhex("b5ee9c72") + bytes([0b0100_0001, 0x01, 0x01, 0x01, 0x00, len(cell), 0x00]) + cell
    return base64.b64encode(body + crc32c(body).to_bytes(4, "big")[::-1]).decode("ascii")


def amount_nano(amount: str) -> str:
    try:
        value = Decimal(amount)
    except (InvalidOperation, ValueError):
        value = Decimal("0.1")
    return str(int(value * NANO))


def pay_url(chat_id: int, base_url: str) -> str:
    return f"{base_url.rstrip('/')}{PAY_PATH}?chat={chat_id}"


def public_pay_url(chat_id: int) -> str:
    """Absolute payment-page URL for the bot button, empty when no public URL is configured."""
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    return pay_url(chat_id, base_url) if base_url else ""


def manifest(base_url: str) -> dict[str, Any]:
    root = base_url.rstrip("/")
    return {
        "url": root,
        "name": APP_NAME,
        "iconUrl": f"{root}{ICON_PATH}",
    }


PAGE_TEMPLATE = Template("""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Поддержать ChatterAi</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px 18px 40px; background: #11151c; color: #e8edf6;
         font: 16px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; }
  .wrap { max-width: 460px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 6px; }
  p.sub { margin: 0 0 22px; color: #96a3b8; }
  .card { background: #1a202b; border: 1px solid #273040; border-radius: 14px; padding: 18px; margin-bottom: 16px; }
  .amount { font-size: 30px; font-weight: 700; letter-spacing: -0.5px; }
  .amount span { font-size: 17px; font-weight: 500; color: #96a3b8; }
  .comment { margin-top: 10px; font-family: ui-monospace, Consolas, monospace; font-size: 14px;
             color: #ffd479; word-break: break-all; }
  #ton-connect { margin: 4px 0 14px; }
  button#pay { width: 100%; padding: 15px; font-size: 17px; font-weight: 600; color: #08131f;
               background: #4cc2ff; border: 0; border-radius: 12px; cursor: pointer; }
  button#pay:disabled { background: #2b3644; color: #7d8b9e; cursor: not-allowed; }
  #status { margin-top: 12px; min-height: 22px; color: #96a3b8; font-size: 14px; }
  details { margin-top: 4px; }
  summary { cursor: pointer; color: #96a3b8; font-size: 14px; }
  .mono { font-family: ui-monospace, Consolas, monospace; font-size: 13px; word-break: break-all;
          background: #11151c; border: 1px solid #273040; border-radius: 8px; padding: 10px; margin-top: 10px; }
  .hint { color: #96a3b8; font-size: 13px; margin-top: 18px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Поддержать ChatterAi</h1>
  <p class="sub">Донат в Gram, сеть mainnet. Бот не берёт комиссию.</p>

  <div class="card">
    <div class="amount">$AMOUNT_TEXT <span>Gram</span></div>
    <div class="comment">комментарий: $COMMENT</div>
  </div>

  <div class="card">
    <div id="ton-connect"></div>
    <button id="pay" disabled>Оплатить $AMOUNT_TEXT Gram</button>
    <div id="status">Подключи кошелёк, чтобы оплатить в один тап.</div>
  </div>

  <div class="card">
    <details>
      <summary>Перевести вручную из любого кошелька</summary>
      <div class="mono">Адрес:<br>$ADDRESS</div>
      <div class="mono">Комментарий:<br>$COMMENT</div>
      <div class="hint">Комментарий обязателен: по нему бот понимает, кто платил. Без него платёж не зачтётся.
      С биржи переводить нельзя — комментарий часто теряется.</div>
    </details>
  </div>

  <p class="hint">После перевода бот сам проверит блокчейн и напишет спасибо в чат. Это занимает до пары минут.</p>
</div>

<script src="https://unpkg.com/@tonconnect/ui@latest/dist/tonconnect-ui.min.js"></script>
<script>
(function () {
  var payButton = document.getElementById('pay');
  var status = document.getElementById('status');

  if (!window.TON_CONNECT_UI) {
    document.getElementById('ton-connect').textContent = 'Не удалось загрузить TON Connect.';
    status.textContent = 'Переведи вручную по реквизитам ниже — они те же самые.';
    return;
  }

  var ui = new TON_CONNECT_UI.TonConnectUI({
    manifestUrl: $MANIFEST_URL,
    buttonRootId: 'ton-connect'
  });

  ui.onStatusChange(function (wallet) {
    payButton.disabled = !wallet;
    status.textContent = wallet ? 'Кошелёк подключён. Можно оплачивать.' : 'Подключи кошелёк, чтобы оплатить в один тап.';
  });

  payButton.addEventListener('click', async function () {
    payButton.disabled = true;
    status.textContent = 'Открываю кошелёк…';
    try {
      await ui.sendTransaction({
        validUntil: Math.floor(Date.now() / 1000) + $VALID_FOR,
        network: $NETWORK,
        messages: [
          {
            address: $ADDRESS_JSON,
            amount: $AMOUNT_NANO_JSON,
            payload: $PAYLOAD_JSON
          }
        ]
      });
      status.textContent = 'Перевод отправлен. Бот проверит его и подтвердит в чате.';
    } catch (error) {
      status.textContent = 'Перевод не отправлен: ' + ((error && error.message) ? error.message : String(error));
      payButton.disabled = false;
    }
  });
})();
</script>
</body>
</html>
""")


def render_page(chat_id: int, base_url: str) -> str:
    comment = ton_payments.comment_for(chat_id)
    amount = ton_payments.SUGGESTED_AMOUNT
    manifest_url = urljoin(base_url.rstrip("/") + "/", MANIFEST_PATH.lstrip("/"))
    return PAGE_TEMPLATE.substitute(
        AMOUNT_TEXT=amount,
        AMOUNT_NANO_JSON=json.dumps(amount_nano(amount)),
        COMMENT=comment,
        ADDRESS=ton_payments.WALLET_ADDRESS,
        ADDRESS_JSON=json.dumps(ton_payments.WALLET_ADDRESS),
        PAYLOAD_JSON=json.dumps(text_comment_payload(comment)),
        MANIFEST_URL=json.dumps(manifest_url),
        VALID_FOR=VALID_FOR_SECONDS,
        NETWORK=json.dumps(NETWORK_MAINNET),
    )
