"""
Telegram Mini App bot: игра + оплата 10 Stars.
Админы (ADMIN_USERNAMES) играют бесплатно.

Локально:  python bot.py
Облако:    Render / Free hosting — PORT из env, WEBAPP_URL из RENDER_EXTERNAL_URL
Keep-alive: GET/HEAD /api/ping  (UptimeRobot / Cloudflare Worker каждые 5 мин)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

APP_NAME = "bananawow"
APP_VERSION = "1.2.0"
PRICE_STARS = 10

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
PORT = int(os.getenv("PORT", "3000"))

# username без @, через запятую. Только они играют без Stars.
ADMIN_USERNAMES = {
    u.strip().lstrip("@").lower()
    for u in os.getenv("ADMIN_USERNAMES", "bonamartin69").split(",")
    if u.strip()
}

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("tgbot")

app_bot: Application | None = None
_started_at = time.time()
_last_external_ping: float | None = None


def detect_public_url() -> str:
    """HTTPS URL для Mini App: env → Render → пусто (локально)."""
    for key in (
        "WEBAPP_URL",
        "PUBLIC_URL",
        "RENDER_EXTERNAL_URL",
        "RAILWAY_PUBLIC_DOMAIN",
    ):
        raw = (os.getenv(key) or "").strip()
        if not raw:
            continue
        if key == "RAILWAY_PUBLIC_DOMAIN" and not raw.startswith("http"):
            raw = "https://" + raw
        if not raw.endswith("/"):
            raw += "/"
        if raw.startswith("https://") or raw.startswith("http://"):
            return raw
    return ""


def api_call(method: str, payload: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def validate_webapp_init_data(init_data: str) -> dict | None:
    """Проверка подписи Telegram.WebApp.initData → user dict или None."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calculated = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        return None
    user_raw = parsed.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except json.JSONDecodeError:
        return None


def is_admin_user(user: dict | None) -> bool:
    if not user:
        return False
    uname = (user.get("username") or "").strip().lstrip("@").lower()
    if uname and uname in ADMIN_USERNAMES:
        return True
    return False


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        log.info("HTTP " + fmt, *args)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def end_headers(self):
        p = (self.path or "").split("?", 1)[0].lower()
        if (
            p.endswith(".html")
            or p.endswith(".png")
            or p.endswith(".jpg")
            or p.endswith(".webp")
            or p in ("/", "/index.html", "", "/keepalive-setup")
        ):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
        super().end_headers()

    def do_HEAD(self):
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        if path in ("/api/ping", "/api/health"):
            self._mark_ping()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._cors()
            self.end_headers()
            return
        self.path = path
        return super().do_HEAD()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        # ?v=... не должен попадать в путь к файлу
        self.path = path

        if path == "/api/ping":
            return self._json(200, self._ping_payload())
        if path == "/api/health":
            return self._json(200, self._health_payload())
        if path == "/api/price":
            return self._json(200, {"stars": PRICE_STARS})
        if path in ("/keepalive-setup", "/keepalive-setup.html"):
            self.path = "/keepalive-setup.html"
            return super().do_GET()
        if path in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}

        if parsed.path == "/api/me":
            return self._handle_me(body)
        if parsed.path == "/api/create-invoice":
            return self._handle_invoice(body)

        self.send_error(404)

    def _mark_ping(self) -> None:
        global _last_external_ping
        _last_external_ping = time.time()

    def _ping_payload(self) -> dict:
        """Keep-alive для UptimeRobot / Cloudflare Worker / cron-job.org."""
        self._mark_ping()
        return {
            "ok": True,
            "woke": True,
            "app": APP_NAME,
            "version": APP_VERSION,
            "ts": _last_external_ping,
        }

    def _health_payload(self) -> dict:
        age = None
        if _last_external_ping:
            age = int(time.time() - _last_external_ping)
        has_token = bool(BOT_TOKEN)
        return {
            "ok": True,
            "app": APP_NAME,
            "version": APP_VERSION,
            "uptime_sec": int(time.time() - _started_at),
            "webapp_url": WEBAPP_URL or None,
            "bot_token_set": has_token,
            "last_external_ping_sec_ago": age,
            "ping_url": "/api/ping",
            "setup_url": "/keepalive-setup",
            "hint": (
                "Нет BOT_TOKEN в Environment — добавь в Render и Redeploy."
                if not has_token
                else (
                    "Чтобы Render не засыпал: UptimeRobot → /api/ping каждые 5 мин."
                )
            ),
        }

    def _handle_me(self, body: dict):
        """Кто открыл приложение: админ или нет (проверка initData)."""
        init_data = (body.get("initData") or "").strip()
        username_hint = (body.get("username") or "").strip().lstrip("@").lower()

        user = validate_webapp_init_data(init_data) if init_data else None
        verified = user is not None

        if verified:
            admin = is_admin_user(user)
            uname = (user.get("username") or "").lower()
            uid = user.get("id")
        else:
            admin = False
            uname = username_hint or None
            uid = body.get("userId")

        soft = bool(username_hint and username_hint in ADMIN_USERNAMES)

        log.info(
            "api/me verified=%s admin=%s soft=%s user=%s id=%s",
            verified,
            admin,
            soft,
            uname,
            uid,
        )
        self._json(
            200,
            {
                "ok": True,
                "isAdmin": admin or (soft and not verified),
                "verified": verified,
                "username": uname,
                "userId": uid,
                "stars": PRICE_STARS,
            },
        )

    def _handle_invoice(self, body: dict):
        init_data = (body.get("initData") or "").strip()
        user = validate_webapp_init_data(init_data) if init_data else None
        if is_admin_user(user):
            self._json(200, {"ok": True, "free": True, "stars": 0})
            return

        user_id = body.get("userId") or "anon"
        payload = f"play_{user_id}_{int(time.time() * 1000)}"

        try:
            result = api_call(
                "createInvoiceLink",
                {
                    "title": "One game",
                    "description": (
                        f"Find 3 identical fruits — 3 moves. "
                        f"Price: {PRICE_STARS} Stars."
                    ),
                    "payload": payload,
                    "provider_token": "",
                    "currency": "XTR",
                    "prices": [{"label": "One game", "amount": PRICE_STARS}],
                },
            )
            if not result.get("ok"):
                raise RuntimeError(result.get("description") or str(result))
            link = result["result"]
            self._json(200, {"ok": True, "invoiceLink": link, "stars": PRICE_STARS})
        except (HTTPError, URLError, RuntimeError, KeyError) as e:
            log.exception("createInvoiceLink failed")
            msg = getattr(e, "reason", None) or str(e)
            if isinstance(e, HTTPError):
                try:
                    msg = e.read().decode("utf-8", errors="replace")
                except Exception:
                    msg = str(e)
            self._json(500, {"ok": False, "error": msg})

    def _json(self, code: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)


def start_http():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("HTTP http://0.0.0.0:%s  (игра + API + /api/ping)", PORT)
    log.info("Админы (free play): %s", ", ".join(sorted(ADMIN_USERNAMES)) or "(нет)")
    server.serve_forever()


def play_keyboard() -> ReplyKeyboardMarkup:
    url = WEBAPP_URL or f"http://127.0.0.1:{PORT}/"
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text="🎮 Play", web_app=WebAppInfo(url=url))]],
        resize_keyboard=True,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    uname = (user.username or "").lower() if user else ""
    if uname in ADMIN_USERNAMES:
        text = (
            "👑 Admin mode\n\n"
            "Find 3 identical fruits in 3 moves.\n"
            "You play for free."
        )
    else:
        text = (
            "🎰 Welcome!\n\n"
            "Find 3 identical fruits in 3 moves.\n"
            f"One game — {PRICE_STARS} ⭐"
        )
    await update.message.reply_text(text, reply_markup=play_keyboard())


async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        f"One game — {PRICE_STARS} ⭐",
        reply_markup=play_keyboard(),
    )


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if query:
        await query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.successful_payment:
        return
    sp = update.message.successful_payment
    log.info(
        "Оплата OK user=%s stars=%s payload=%s",
        update.effective_user.id if update.effective_user else "?",
        sp.total_amount,
        sp.invoice_payload,
    )
    await update.message.reply_text("✅ Payment successful! You can play now.")


def set_menu_button(url: str):
    try:
        result = api_call(
            "setChatMenuButton",
            {
                "menu_button": {
                    "type": "web_app",
                    "text": "Play",
                    "web_app": {"url": url},
                }
            },
        )
        log.info("Menu button: %s", result)
    except Exception:
        log.exception("setChatMenuButton failed")


def main():
    global WEBAPP_URL, app_bot, BOT_TOKEN, ADMIN_USERNAMES, PORT

    load_dotenv(ROOT / ".env", override=True)
    BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

    try:
        PORT = int(os.getenv("PORT", "3000"))
    except ValueError:
        PORT = 3000

    WEBAPP_URL = detect_public_url()
    ADMIN_USERNAMES = {
        u.strip().lstrip("@").lower()
        for u in os.getenv("ADMIN_USERNAMES", "bonamartin69").split(",")
        if u.strip()
    }

    # HTTP сразу — иначе Render health-check (/api/ping) → Failed
    http_thread = threading.Thread(target=start_http, daemon=True)
    http_thread.start()
    time.sleep(0.4)
    log.info(
        "Boot: port=%s webapp=%s token=%s",
        PORT,
        WEBAPP_URL or "(empty)",
        "yes" if BOT_TOKEN else "MISSING",
    )

    if not WEBAPP_URL:
        log.warning(
            "WEBAPP_URL пуст — на Render обычно есть RENDER_EXTERNAL_URL. "
            "Кнопка Play может не работать, пока URL не определится."
        )
    elif BOT_TOKEN:
        # не валим процесс, если Telegram API временно недоступен
        try:
            set_menu_button(WEBAPP_URL if WEBAPP_URL.endswith("/") else WEBAPP_URL + "/")
        except Exception:
            log.exception("set_menu_button (non-fatal)")

    if not BOT_TOKEN:
        # Не SystemExit: иначе Render = Failed. Держим HTTP и пишем в логи.
        log.error(
            "Нет BOT_TOKEN! Render → Environment → Add: BOT_TOKEN = токен @BotFather → Save → Manual Deploy"
        )
        while True:
            time.sleep(300)
            log.error("Всё ещё нет BOT_TOKEN — бот не отвечает в Telegram, HTTP жив")

    # polling с авто-рестартом (сеть / conflict)
    while True:
        try:
            app_bot = Application.builder().token(BOT_TOKEN).build()
            app_bot.add_handler(CommandHandler("start", cmd_start))
            app_bot.add_handler(CommandHandler("play", cmd_play))
            app_bot.add_handler(PreCheckoutQueryHandler(pre_checkout))
            app_bot.add_handler(
                MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment)
            )
            log.info(
                "Бот polling… WEBAPP_URL=%s admins=%s port=%s",
                WEBAPP_URL,
                ADMIN_USERNAMES,
                PORT,
            )
            app_bot.run_polling(drop_pending_updates=True)
        except Exception:
            log.exception("polling crashed, restart in 8s")
            time.sleep(8)


if __name__ == "__main__":
    main()
