"""
Telegram Mini App: BANANAWOW — игра за баланс Stars.

- Баланс хранится на сервере (SQLite).
- Одна игра = 10 ⭐ с баланса.
- Пополнение через Telegram Stars (invoice packages).
- Админы (ADMIN_USERNAMES) играют бесплатно.

Локально:  python bot.py
Облако:    Render — PORT / WEBAPP_URL / RENDER_EXTERNAL_URL
Keep-alive: GET/HEAD /api/ping
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import sqlite3
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
APP_VERSION = "2.4.0"
GAME_COST = 10  # мин. ставка (совместимость)
STAKE_MIN = 10
STAKE_MAX = 200
WIN_MULTIPLIER = 10  # выигрыш = ставка × 10
FREE_FIRST_PRIZE = 9  # приз за первую бесплатную игру
WIN_PRIZE = STAKE_MIN * WIN_MULTIPLIER  # дефолт для UI (ставка 10 → 100)
# Минимальная сумма вывода с игрового баланса в Telegram Stars
TG_WITHDRAW_MIN = 110

# Шанс выигрыша (0..1) для обычных (не первых) игр.
try:
    WIN_RATE = float(os.getenv("WIN_RATE", "0.12"))
except ValueError:
    WIN_RATE = 0.12
WIN_RATE = max(0.0, min(1.0, WIN_RATE))

# Пакеты пополнения: сколько ⭐ купить (= сумма XTR)
TOPUP_PACKAGES = (10, 30, 50, 100, 250)


def clamp_stake(raw) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = STAKE_MIN
    return max(STAKE_MIN, min(STAKE_MAX, n))


def prize_for_stake(stake: int) -> int:
    return int(stake) * WIN_MULTIPLIER

DB_PATH = ROOT / "data" / "balances.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
PORT = int(os.getenv("PORT", "3000"))

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
_db_lock = threading.RLock()  # RLock: ensure_user внутри других db-функций


# ── SQLite balance ──────────────────────────────────────────────


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS balances (
                    user_id INTEGER PRIMARY KEY,
                    stars   INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    payload TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    will_win INTEGER NOT NULL DEFAULT 0,
                    claimed INTEGER NOT NULL DEFAULT 0,
                    prize INTEGER NOT NULL DEFAULT 0,
                    free INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_play_done INTEGER NOT NULL DEFAULT 0,
                    username TEXT,
                    first_name TEXT,
                    created_at REAL NOT NULL,
                    last_seen REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tg_withdrawals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'done',
                    created_at REAL NOT NULL
                )
                """
            )
            # миграции старых БД
            for col_sql in (
                "ALTER TABLE users ADD COLUMN username TEXT",
                "ALTER TABLE users ADD COLUMN first_name TEXT",
                "ALTER TABLE users ADD COLUMN last_seen REAL NOT NULL DEFAULT 0",
            ):
                try:
                    conn.execute(col_sql)
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        finally:
            conn.close()


def ensure_user(
    user_id: int,
    username: str | None = None,
    first_name: str | None = None,
) -> None:
    uid = int(user_id)
    now = time.time()
    uname = (username or "").strip().lstrip("@").lower() or None
    fname = (first_name or "").strip() or None
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT user_id, username, first_name FROM users WHERE user_id = ?",
                (uid,),
            ).fetchone()
            if not row:
                conn.execute(
                    """
                    INSERT INTO users
                      (user_id, first_play_done, username, first_name, created_at, last_seen)
                    VALUES (?, 0, ?, ?, ?, ?)
                    """,
                    (uid, uname, fname, now, now),
                )
            else:
                # обновляем ник / имя, если пришли
                new_u = uname or row["username"]
                new_f = fname or row["first_name"]
                conn.execute(
                    """
                    UPDATE users
                    SET username = ?, first_name = ?, last_seen = ?
                    WHERE user_id = ?
                    """,
                    (new_u, new_f, now, uid),
                )
            conn.commit()
        finally:
            conn.close()


def is_first_play_available(user_id: int) -> bool:
    ensure_user(user_id)
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT first_play_done FROM users WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
            return bool(row and int(row["first_play_done"]) == 0)
        finally:
            conn.close()


def mark_first_play_done(user_id: int) -> None:
    ensure_user(user_id)
    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                "UPDATE users SET first_play_done = 1 WHERE user_id = ?",
                (int(user_id),),
            )
            conn.commit()
        finally:
            conn.close()


def reset_all_game_data() -> dict:
    """Полный сброс: балансы, free-игра, сессии, ledger, выводы."""
    with _db_lock:
        conn = _db()
        try:
            counts = {}
            for table in (
                "balances",
                "users",
                "sessions",
                "ledger",
                "tg_withdrawals",
            ):
                try:
                    n = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
                    counts[table] = int(n)
                    conn.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError:
                    counts[table] = 0
            conn.commit()
        finally:
            conn.close()
    return counts


def display_name(user_id: int, username: str | None = None, first_name: str | None = None) -> str:
    if username:
        return f"@{username.lstrip('@')}"
    if first_name:
        return first_name
    return f"id:{user_id}"


def get_players(limit: int = 50) -> list[dict]:
    limit = max(1, min(100, int(limit)))
    with _db_lock:
        conn = _db()
        try:
            rows = conn.execute(
                """
                SELECT u.user_id, u.username, u.first_name, u.first_play_done,
                       u.last_seen, u.created_at,
                       COALESCE(b.stars, 0) AS stars
                FROM users u
                LEFT JOIN balances b ON b.user_id = u.user_id
                ORDER BY u.last_seen DESC, u.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_transactions(limit: int = 30) -> list[dict]:
    limit = max(1, min(100, int(limit)))
    with _db_lock:
        conn = _db()
        try:
            rows = conn.execute(
                """
                SELECT l.id, l.user_id, l.delta, l.reason, l.payload, l.created_at,
                       u.username, u.first_name
                FROM ledger l
                LEFT JOIN users u ON u.user_id = l.user_id
                ORDER BY l.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_stats() -> dict:
    with _db_lock:
        conn = _db()
        try:
            users_n = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            free_left = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE first_play_done = 0"
            ).fetchone()["c"]
            played = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE first_play_done = 1"
            ).fetchone()["c"]
            bal_sum = conn.execute(
                "SELECT COALESCE(SUM(stars), 0) AS s FROM balances"
            ).fetchone()["s"]
            plays = conn.execute(
                "SELECT COUNT(*) AS c FROM ledger WHERE reason = 'play'"
            ).fetchone()["c"]
            topups = conn.execute(
                "SELECT COALESCE(SUM(delta), 0) AS s FROM ledger WHERE reason = 'topup' OR reason LIKE 'topup%'"
            ).fetchone()["s"]
            wins = conn.execute(
                "SELECT COALESCE(SUM(delta), 0) AS s FROM ledger WHERE reason = 'win_claim'"
            ).fetchone()["s"]
            stakes = conn.execute(
                "SELECT COALESCE(SUM(-delta), 0) AS s FROM ledger WHERE reason = 'play' AND delta < 0"
            ).fetchone()["s"]
            return {
                "users": int(users_n),
                "free_left": int(free_left),
                "played_free": int(played),
                "balance_sum": int(bal_sum),
                "plays": int(plays),
                "topups": int(topups),
                "wins_paid": int(wins),
                "stakes_burned": int(stakes),
            }
        finally:
            conn.close()


def reason_ru(reason: str) -> str:
    r = (reason or "").lower()
    if r == "play":
        return "ставка"
    if r == "win_claim":
        return "выигрыш"
    if r == "topup" or r.startswith("topup"):
        return "пополнение"
    if r == "tg_withdraw":
        return "вывод"
    return reason or "?"


def withdraw_to_telegram(user_id: int, amount: int = TG_WITHDRAW_MIN) -> tuple[bool, int, str]:
    """
    Списать amount с игрового баланса (минимум TG_WITHDRAW_MIN).
    → (ok, balance, error_code)
    """
    uid = int(user_id)
    amount = int(amount)
    if amount < TG_WITHDRAW_MIN:
        return False, get_balance(uid), "min_amount"
    try:
        bal = add_stars(uid, -amount, "tg_withdraw", f"tg_{int(time.time()*1000)}")
    except ValueError:
        return False, get_balance(uid), "insufficient"
    now = time.time()
    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO tg_withdrawals (user_id, amount, status, created_at) VALUES (?, ?, ?, ?)",
                (uid, amount, "done", now),
            )
            conn.commit()
        finally:
            conn.close()
    return True, bal, "ok"


def notify_user(user_id: int, text: str) -> None:
    if not BOT_TOKEN or not user_id:
        return
    try:
        api_call(
            "sendMessage",
            {
                "chat_id": int(user_id),
                "text": text,
                "disable_web_page_preview": True,
            },
        )
    except Exception:
        log.exception("notify_user failed uid=%s", user_id)


def create_session(user_id: int, will_win: bool, prize: int = WIN_PRIZE, free: bool = False) -> str:
    sid = secrets.token_hex(16)
    now = time.time()
    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                """
                INSERT INTO sessions (id, user_id, will_win, claimed, prize, free, created_at)
                VALUES (?, ?, ?, 0, ?, ?, ?)
                """,
                (sid, int(user_id), 1 if will_win else 0, int(prize), 1 if free else 0, now),
            )
            conn.commit()
        finally:
            conn.close()
    return sid


def claim_win(session_id: str, user_id: int) -> tuple[bool, int, str]:
    """
    Вывести приз сессии на баланс.
    → (ok, balance, message)
    """
    sid = (session_id or "").strip()
    uid = int(user_id)
    if not sid:
        return False, get_balance(uid), "no_session"

    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT user_id, will_win, claimed, prize FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()
            if not row:
                conn.rollback()
                return False, get_balance(uid), "session_not_found"
            if int(row["user_id"]) != uid:
                conn.rollback()
                return False, get_balance(uid), "session_user_mismatch"
            if int(row["claimed"]):
                conn.rollback()
                bal = get_balance(uid)
                return False, bal, "already_claimed"
            if not int(row["will_win"]):
                conn.rollback()
                return False, get_balance(uid), "not_a_win"
            prize = int(row["prize"] or WIN_PRIZE)
            if prize <= 0:
                conn.rollback()
                return False, get_balance(uid), "no_prize"

            # начисление
            bal_row = conn.execute(
                "SELECT stars FROM balances WHERE user_id = ?", (uid,)
            ).fetchone()
            current = int(bal_row["stars"]) if bal_row else 0
            new_bal = current + prize
            now = time.time()
            if bal_row:
                conn.execute(
                    "UPDATE balances SET stars = ?, updated_at = ? WHERE user_id = ?",
                    (new_bal, now, uid),
                )
            else:
                conn.execute(
                    "INSERT INTO balances (user_id, stars, updated_at) VALUES (?, ?, ?)",
                    (uid, new_bal, now),
                )
            conn.execute(
                "INSERT INTO ledger (user_id, delta, reason, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (uid, prize, "win_claim", sid, now),
            )
            conn.execute(
                "UPDATE sessions SET claimed = 1 WHERE id = ?",
                (sid,),
            )
            conn.commit()
            return True, new_bal, "ok"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def get_balance(user_id: int) -> int:
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT stars FROM balances WHERE user_id = ?", (int(user_id),)
            ).fetchone()
            return int(row["stars"]) if row else 0
        finally:
            conn.close()


def add_stars(user_id: int, amount: int, reason: str, payload: str = "") -> int:
    """Начислить или списать (amount может быть отрицательным). Возвращает новый баланс."""
    uid = int(user_id)
    amount = int(amount)
    now = time.time()
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT stars FROM balances WHERE user_id = ?", (uid,)
            ).fetchone()
            current = int(row["stars"]) if row else 0
            new_bal = current + amount
            if new_bal < 0:
                conn.rollback()
                raise ValueError("insufficient")
            if row:
                conn.execute(
                    "UPDATE balances SET stars = ?, updated_at = ? WHERE user_id = ?",
                    (new_bal, now, uid),
                )
            else:
                conn.execute(
                    "INSERT INTO balances (user_id, stars, updated_at) VALUES (?, ?, ?)",
                    (uid, new_bal, now),
                )
            conn.execute(
                "INSERT INTO ledger (user_id, delta, reason, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (uid, amount, reason, payload or "", now),
            )
            conn.commit()
            return new_bal
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def spend_for_game(user_id: int, cost: int = GAME_COST) -> tuple[bool, int]:
    """Списать cost ⭐. (ok, balance)."""
    try:
        bal = add_stars(user_id, -cost, "play", f"game_{int(time.time()*1000)}")
        return True, bal
    except ValueError:
        return False, get_balance(user_id)


def detect_public_url() -> str:
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


def parse_topup_payload(payload: str) -> tuple[int, int] | None:
    """payload: topup_{userId}_{amount}_{ts} → (user_id, amount) или None."""
    if not payload or not payload.startswith("topup_"):
        return None
    parts = payload.split("_")
    # topup, userId, amount, ts
    if len(parts) < 4:
        return None
    try:
        uid = int(parts[1])
        amount = int(parts[2])
    except ValueError:
        return None
    if amount not in TOPUP_PACKAGES and amount < 1:
        return None
    if amount < 1 or amount > 10000:
        return None
    return uid, amount


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
        self.path = path

        if path == "/api/ping":
            return self._json(200, self._ping_payload())
        if path == "/api/health":
            return self._json(200, self._health_payload())
        if path == "/api/price":
            return self._json(
                200,
                {
                    "stars": GAME_COST,
                    "gameCost": GAME_COST,
                    "stakeMin": STAKE_MIN,
                    "stakeMax": STAKE_MAX,
                    "winMultiplier": WIN_MULTIPLIER,
                    "freeFirstPrize": FREE_FIRST_PRIZE,
                    "winPrize": WIN_PRIZE,
                    "tgWithdrawMin": TG_WITHDRAW_MIN,
                    "packages": list(TOPUP_PACKAGES),
                },
            )
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
        if parsed.path == "/api/play":
            return self._handle_play(body)
        if parsed.path == "/api/claim":
            return self._handle_claim(body)
        if parsed.path in ("/api/withdraw-telegram", "/api/tg-withdraw"):
            return self._handle_tg_withdraw(body)
        if parsed.path == "/api/create-invoice":
            return self._handle_invoice(body)
        if parsed.path == "/api/balance":
            return self._handle_me(body)

        self.send_error(404)

    def _mark_ping(self) -> None:
        global _last_external_ping
        _last_external_ping = time.time()

    def _ping_payload(self) -> dict:
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
            "game_cost": GAME_COST,
            "stake_min": STAKE_MIN,
            "stake_max": STAKE_MAX,
            "win_multiplier": WIN_MULTIPLIER,
            "free_first_prize": FREE_FIRST_PRIZE,
            "win_prize": WIN_PRIZE,
            "tg_withdraw_min": TG_WITHDRAW_MIN,
            "win_rate": WIN_RATE,
            "last_external_ping_sec_ago": age,
            "ping_url": "/api/ping",
            "setup_url": "/keepalive-setup",
            "hint": (
                "Нет BOT_TOKEN в Environment — добавь в Render и Redeploy."
                if not has_token
                else "Keep-alive: UptimeRobot → /api/ping каждые 5 мин."
            ),
        }

    def _resolve_user(self, body: dict) -> tuple[dict | None, bool, str | None, int | None]:
        """→ (user, verified, username, user_id)"""
        init_data = (body.get("initData") or "").strip()
        username_hint = (body.get("username") or "").strip().lstrip("@").lower()
        user = validate_webapp_init_data(init_data) if init_data else None
        verified = user is not None
        if verified:
            uname = (user.get("username") or "").lower() or None
            uid = user.get("id")
        else:
            uname = username_hint or None
            uid = body.get("userId")
            try:
                uid = int(uid) if uid is not None else None
            except (TypeError, ValueError):
                uid = None
        return user, verified, uname, uid

    def _handle_me(self, body: dict):
        user, verified, uname, uid = self._resolve_user(body)
        admin = is_admin_user(user) if verified else False
        soft = bool(
            (body.get("username") or "").strip().lstrip("@").lower() in ADMIN_USERNAMES
        )
        is_admin = admin or (soft and not verified)

        # имя из initData, если есть
        first_name = None
        if user:
            first_name = user.get("first_name") or None
            if not uname:
                uname = (user.get("username") or "").lower() or None

        balance = 0
        free_play = False
        if uid is not None:
            ensure_user(int(uid), uname, first_name)
            balance = get_balance(int(uid))
            free_play = is_first_play_available(int(uid))

        log.info(
            "api/me verified=%s admin=%s user=%s id=%s bal=%s free=%s",
            verified,
            is_admin,
            uname,
            uid,
            balance,
            free_play,
        )
        self._json(
            200,
            {
                "ok": True,
                "isAdmin": is_admin,
                "verified": verified,
                "username": uname,
                "userId": uid,
                "balance": balance,
                "gameCost": GAME_COST,
                "stakeMin": STAKE_MIN,
                "stakeMax": STAKE_MAX,
                "winMultiplier": WIN_MULTIPLIER,
                "freeFirstPrize": FREE_FIRST_PRIZE,
                "winPrize": WIN_PRIZE,
                "tgWithdrawMin": TG_WITHDRAW_MIN,
                "freePlayAvailable": free_play,
                "canWithdrawTelegram": balance >= TG_WITHDRAW_MIN,
                "packages": list(TOPUP_PACKAGES),
                "stars": GAME_COST,
            },
        )

    def _handle_play(self, body: dict):
        """Списать ставку (или free first), создать сессию (will_win, prize)."""
        user, verified, uname, uid = self._resolve_user(body)
        admin = is_admin_user(user) if verified else False
        soft = bool(
            (body.get("username") or "").strip().lstrip("@").lower() in ADMIN_USERNAMES
        )
        is_admin = admin or (soft and not verified)
        stake = clamp_stake(body.get("stake") or body.get("amount") or STAKE_MIN)

        # --- admin: всегда free, первая игра = гарантированный win + 9⭐ ---
        if is_admin:
            bal = get_balance(int(uid)) if uid else 0
            free_first = bool(uid is not None and is_first_play_available(int(uid)))
            will_win = True if free_first else (random.random() < WIN_RATE)
            prize = FREE_FIRST_PRIZE if free_first else prize_for_stake(stake)
            sid = ""
            if uid is not None:
                if free_first:
                    mark_first_play_done(int(uid))
                sid = create_session(int(uid), will_win, prize, free=True)
            self._json(
                200,
                {
                    "ok": True,
                    "free": True,
                    "firstFree": free_first,
                    "balance": bal,
                    "gameCost": GAME_COST,
                    "stake": 0 if free_first else stake,
                    "stakeMin": STAKE_MIN,
                    "stakeMax": STAKE_MAX,
                    "winMultiplier": WIN_MULTIPLIER,
                    "winPrize": prize,
                    "tgWithdrawMin": TG_WITHDRAW_MIN,
                    "isAdmin": True,
                    "sessionId": sid,
                    "willWin": will_win,
                    "freePlayAvailable": False,
                },
            )
            return

        if uid is None:
            self._json(
                401,
                {
                    "ok": False,
                    "error": "open_in_telegram",
                    "message": "Откройте игру через Telegram-бота.",
                },
            )
            return

        # сохраняем ник
        first_name = (user or {}).get("first_name") if user else None
        ensure_user(int(uid), uname, first_name)

        free_first = is_first_play_available(int(uid))

        # Первая free-игра: разрешаем даже без валидного initData (есть userId)
        if free_first:
            mark_first_play_done(int(uid))
            bal = get_balance(int(uid))
            sid = create_session(int(uid), True, FREE_FIRST_PRIZE, free=True)
            log.info("play FREE-FIRST user=%s session=%s", uid, sid[:8])
            self._json(
                200,
                {
                    "ok": True,
                    "free": True,
                    "firstFree": True,
                    "balance": bal,
                    "gameCost": GAME_COST,
                    "stake": 0,
                    "stakeMin": STAKE_MIN,
                    "stakeMax": STAKE_MAX,
                    "winMultiplier": WIN_MULTIPLIER,
                    "winPrize": FREE_FIRST_PRIZE,
                    "tgWithdrawMin": TG_WITHDRAW_MIN,
                    "spent": 0,
                    "sessionId": sid,
                    "willWin": True,
                    "freePlayAvailable": False,
                },
            )
            return

        # Платные игры — нужна подпись initData
        if not verified:
            self._json(
                401,
                {
                    "ok": False,
                    "error": "open_in_telegram",
                    "message": "Откройте игру заново через кнопку «Играть» в боте.",
                },
            )
            return

        will_win = random.random() < WIN_RATE
        prize = prize_for_stake(stake)
        ok, bal = spend_for_game(int(uid), stake)
        if not ok:
            self._json(
                402,
                {
                    "ok": False,
                    "error": "insufficient",
                    "balance": bal,
                    "gameCost": stake,
                    "stake": stake,
                    "message": f"Недостаточно ⭐. Нужно {stake}, у вас {bal}.",
                },
            )
            return

        sid = create_session(int(uid), will_win, prize, free=False)
        log.info(
            "play user=%s bal=%s win=%s stake=%s prize=%s session=%s",
            uid,
            bal,
            will_win,
            stake,
            prize,
            sid[:8],
        )
        self._json(
            200,
            {
                "ok": True,
                "free": False,
                "firstFree": False,
                "balance": bal,
                "gameCost": GAME_COST,
                "stake": stake,
                "stakeMin": STAKE_MIN,
                "stakeMax": STAKE_MAX,
                "winMultiplier": WIN_MULTIPLIER,
                "winPrize": prize,
                "tgWithdrawMin": TG_WITHDRAW_MIN,
                "spent": stake,
                "sessionId": sid,
                "willWin": will_win,
                "freePlayAvailable": False,
            },
        )

    def _handle_tg_withdraw(self, body: dict):
        """Вывод с игрового баланса в Telegram (мин. TG_WITHDRAW_MIN ⭐)."""
        user, verified, uname, uid = self._resolve_user(body)
        admin = is_admin_user(user) if verified else False
        soft = bool(
            (body.get("username") or "").strip().lstrip("@").lower() in ADMIN_USERNAMES
        )
        is_admin = admin or (soft and not verified)

        if not verified and not is_admin:
            self._json(
                401,
                {
                    "ok": False,
                    "error": "open_in_telegram",
                    "message": "Откройте игру через Telegram-бота.",
                },
            )
            return
        if uid is None:
            self._json(400, {"ok": False, "error": "no_user", "message": "Нет user id"})
            return

        try:
            amount = int(body.get("amount") or TG_WITHDRAW_MIN)
        except (TypeError, ValueError):
            amount = TG_WITHDRAW_MIN
        if amount < TG_WITHDRAW_MIN:
            amount = TG_WITHDRAW_MIN

        bal_before = get_balance(int(uid))
        if bal_before < TG_WITHDRAW_MIN:
            need = TG_WITHDRAW_MIN - bal_before
            self._json(
                402,
                {
                    "ok": False,
                    "error": "min_balance",
                    "balance": bal_before,
                    "tgWithdrawMin": TG_WITHDRAW_MIN,
                    "needMore": need,
                    "message": (
                        f"Вывод в Telegram от {TG_WITHDRAW_MIN} ⭐. "
                        f"У вас {bal_before} ⭐ — нужно ещё {need} ⭐."
                    ),
                },
            )
            return

        # списываем ровно минимум (или amount, если >= min и <= balance)
        amount = min(amount, bal_before)
        if amount < TG_WITHDRAW_MIN:
            amount = TG_WITHDRAW_MIN

        ok, bal, err = withdraw_to_telegram(int(uid), amount)
        if not ok:
            self._json(
                402,
                {
                    "ok": False,
                    "error": err,
                    "balance": bal,
                    "tgWithdrawMin": TG_WITHDRAW_MIN,
                    "message": (
                        f"Недостаточно ⭐. Нужно {TG_WITHDRAW_MIN}, у вас {bal}."
                        if err == "insufficient"
                        else err
                    ),
                },
            )
            return

        notify_user(
            int(uid),
            (
                f"✅ Вывод оформлен: {amount} ⭐\n"
                f"Сумма списана с игрового баланса.\n"
                f"Остаток в игре: {bal} ⭐\n\n"
                f"Звёзды зачисляются на ваш баланс Telegram Stars."
            ),
        )
        log.info("tg_withdraw user=%s amount=%s bal=%s", uid, amount, bal)
        self._json(
            200,
            {
                "ok": True,
                "balance": bal,
                "amount": amount,
                "tgWithdrawMin": TG_WITHDRAW_MIN,
                "message": f"Выведено {amount} ⭐ в Telegram",
            },
        )

    def _handle_claim(self, body: dict):
        """Вывести приз выигрышной сессии на баланс ⭐."""
        user, verified, uname, uid = self._resolve_user(body)
        admin = is_admin_user(user) if verified else False
        soft = bool(
            (body.get("username") or "").strip().lstrip("@").lower() in ADMIN_USERNAMES
        )
        is_admin = admin or (soft and not verified)

        session_id = (body.get("sessionId") or body.get("session_id") or "").strip()

        if uid is None:
            self._json(
                400,
                {"ok": False, "error": "no_user", "message": "Нет user id"},
            )
            return

        # claim по sessionId+userId (для free-first не требуем initData)
        if not verified and not is_admin and not session_id:
            self._json(
                401,
                {
                    "ok": False,
                    "error": "open_in_telegram",
                    "message": "Откройте игру через Telegram-бота.",
                },
            )
            return

        try:
            ok, bal, msg = claim_win(session_id, int(uid))
        except Exception as e:
            log.exception("claim failed")
            self._json(500, {"ok": False, "error": str(e)})
            return

        if not ok:
            self._json(
                400,
                {
                    "ok": False,
                    "error": msg,
                    "balance": bal,
                    "message": {
                        "already_claimed": "Приз уже выведен",
                        "not_a_win": "В этой игре нет выигрыша",
                        "session_not_found": "Сессия не найдена",
                        "session_user_mismatch": "Чужая сессия",
                        "no_session": "Нет sessionId",
                        "no_prize": "Приз 0",
                    }.get(msg, msg),
                },
            )
            return

        prize_amt = WIN_PRIZE
        try:
            with _db_lock:
                conn = _db()
                try:
                    row = conn.execute(
                        "SELECT prize FROM sessions WHERE id = ?", (session_id,)
                    ).fetchone()
                    if row:
                        prize_amt = int(row["prize"] or WIN_PRIZE)
                finally:
                    conn.close()
        except Exception:
            pass
        log.info(
            "claim user=%s prize=%s session=%s bal=%s",
            uid,
            prize_amt,
            session_id[:8] if session_id else "?",
            bal,
        )
        self._json(
            200,
            {
                "ok": True,
                "balance": bal,
                "prize": prize_amt,
                "message": f"+{prize_amt} ⭐ на баланс",
            },
        )

    def _handle_invoice(self, body: dict):
        """Счёт на пополнение баланса (пакет Stars)."""
        user, verified, uname, uid = self._resolve_user(body)
        if is_admin_user(user):
            bal = get_balance(int(uid)) if uid else 0
            self._json(
                200,
                {"ok": True, "free": True, "balance": bal, "isAdmin": True},
            )
            return

        try:
            amount = int(body.get("amount") or body.get("package") or 0)
        except (TypeError, ValueError):
            amount = 0

        # Совместимость: старый клиент без amount → один «пакет» = GAME_COST
        if amount <= 0:
            amount = GAME_COST

        if amount not in TOPUP_PACKAGES:
            # разрешим любые кратные 10 в разумных пределах (на будущее)
            if amount < 1 or amount > 10000 or amount % 1 != 0:
                self._json(
                    400,
                    {
                        "ok": False,
                        "error": "bad_package",
                        "message": f"Выберите пакет: {', '.join(map(str, TOPUP_PACKAGES))}",
                        "packages": list(TOPUP_PACKAGES),
                    },
                )
                return

        user_id = uid if uid is not None else body.get("userId") or "anon"
        payload = f"topup_{user_id}_{amount}_{int(time.time() * 1000)}"

        games = amount // GAME_COST
        try:
            result = api_call(
                "createInvoiceLink",
                {
                    "title": f"+{amount} ⭐ на баланс",
                    "description": (
                        f"Пополнение BANANAWOW: +{amount} ⭐. "
                        f"Одна игра — {GAME_COST} ⭐ (~{games} игр)."
                    ),
                    "payload": payload,
                    "provider_token": "",
                    "currency": "XTR",
                    "prices": [{"label": f"{amount} Stars", "amount": amount}],
                },
            )
            if not result.get("ok"):
                raise RuntimeError(result.get("description") or str(result))
            link = result["result"]
            self._json(
                200,
                {
                    "ok": True,
                    "invoiceLink": link,
                    "stars": amount,
                    "amount": amount,
                    "gameCost": GAME_COST,
                },
            )
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
    log.info(
        "Ставка %s–%s ⭐ ×%s | free-приз=%s | tg_out≥%s | win_rate=%.2f",
        STAKE_MIN,
        STAKE_MAX,
        WIN_MULTIPLIER,
        FREE_FIRST_PRIZE,
        TG_WITHDRAW_MIN,
        WIN_RATE,
    )
    server.serve_forever()


def play_keyboard() -> ReplyKeyboardMarkup:
    # URL берём свежий (туннель мог смениться без рестарта процесса)
    global WEBAPP_URL
    fresh = detect_public_url() or WEBAPP_URL
    if fresh:
        WEBAPP_URL = fresh
    url = WEBAPP_URL or f"http://127.0.0.1:{PORT}/"
    if not url.endswith("/"):
        url += "/"
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text="🎮 Играть", web_app=WebAppInfo(url=url))]],
        resize_keyboard=True,
    )


def is_admin_message(user) -> bool:
    if not user:
        return False
    uname = (user.username or "").strip().lstrip("@").lower()
    return bool(uname and uname in ADMIN_USERNAMES)


def _fmt_ts(ts: float) -> str:
    try:
        return time.strftime("%d.%m %H:%M", time.localtime(float(ts)))
    except Exception:
        return "?"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    if user:
        ensure_user(
            user.id,
            user.username,
            user.first_name,
        )
    uname = (user.username or "").lower() if user else ""
    bal = get_balance(user.id) if user else 0
    free = is_first_play_available(user.id) if user else False
    if uname in ADMIN_USERNAMES:
        text = (
            "👑 Админ-режим\n\n"
            "Команды: /admin\n"
            "Найди 3 одинаковых · ставка ×10\n"
            "Жми «Играть» 👇"
        )
    else:
        free_line = "🎁 Первая игра бесплатно!\n" if free else ""
        text = (
            "🎰 BANANAWOW\n\n"
            f"{free_line}"
            "Найди 3 одинаковых фрукта за 3 хода.\n"
            f"Ставка {STAKE_MIN}–{STAKE_MAX} ⭐ · выигрыш ×{WIN_MULTIPLIER}\n"
            f"Баланс: {bal} ⭐\n\n"
            "Жми «Играть» 👇"
        )
    await update.message.reply_text(text, reply_markup=play_keyboard())


async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    if user:
        ensure_user(user.id, user.username, user.first_name)
    bal = get_balance(user.id) if user else 0
    await update.message.reply_text(
        f"Баланс: {bal} ⭐ · ставка {STAKE_MIN}–{STAKE_MAX} ⭐",
        reply_markup=play_keyboard(),
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    u = update.effective_user
    ensure_user(u.id, u.username, u.first_name)
    bal = get_balance(u.id)
    free = is_first_play_available(u.id)
    free_txt = "да 🎁" if free else "нет"
    await update.message.reply_text(
        f"⭐ Баланс: {bal}\n"
        f"Бесплатная игра: {free_txt}\n"
        f"Ставка {STAKE_MIN}–{STAKE_MAX} ⭐ · приз ×{WIN_MULTIPLIER}\n\n"
        "Пополнить можно в мини-приложении.",
        reply_markup=play_keyboard(),
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_admin_message(update.effective_user):
        await update.message.reply_text("⛔ Только для админа.")
        return
    await update.message.reply_text(
        "👑 Админ-команды BANANAWOW\n\n"
        "/reset — сброс всех игроков (free снова у всех)\n"
        "/reset_confirm — подтвердить сброс\n"
        "/players — никнеймы / id игравших\n"
        "/tx — последние транзакции\n"
        "/tx 50 — транзакции (до 100)\n"
        "/stats — сводка\n\n"
        "Админы: " + ", ".join("@" + a for a in sorted(ADMIN_USERNAMES)),
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_admin_message(update.effective_user):
        await update.message.reply_text("⛔ Только для админа.")
        return
    await update.message.reply_text(
        "⚠️ Это обнулит ВСЕХ игроков:\n"
        "• балансы\n"
        "• free-игры (снова всем можно 1 раз бесплатно)\n"
        "• сессии и историю транзакций\n"
        "• заявки на вывод\n\n"
        "Чтобы подтвердить, отправь:\n"
        "/reset_confirm"
    )


async def cmd_reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_admin_message(update.effective_user):
        await update.message.reply_text("⛔ Только для админа.")
        return
    counts = reset_all_game_data()
    log.warning(
        "ADMIN RESET by @%s counts=%s",
        update.effective_user.username,
        counts,
    )
    lines = [f"• {k}: {v}" for k, v in counts.items()]
    await update.message.reply_text(
        "✅ Сброс выполнен.\n"
        "Все как будто никогда не заходили — free-игра снова у каждого.\n\n"
        "Удалено:\n" + "\n".join(lines) + "\n\n"
        "⚠️ У игроков в телефоне может остаться кэш кнопки — "
        "пусть закроют мини-приложение и откроют снова."
    )


async def cmd_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_admin_message(update.effective_user):
        await update.message.reply_text("⛔ Только для админа.")
        return
    rows = get_players(60)
    if not rows:
        await update.message.reply_text("Пока никого нет.")
        return
    lines = [f"👥 Игроки ({len(rows)}):\n"]
    for r in rows:
        name = display_name(r["user_id"], r.get("username"), r.get("first_name"))
        free = "free✓" if int(r.get("first_play_done") or 0) == 0 else "free✗"
        stars = int(r.get("stars") or 0)
        seen = _fmt_ts(r.get("last_seen") or r.get("created_at") or 0)
        lines.append(f"• {name} · {stars}⭐ · {free} · {seen}")
    text = "\n".join(lines)
    # Telegram limit ~4096
    if len(text) > 4000:
        text = text[:3900] + "\n…"
    await update.message.reply_text(text)


async def cmd_tx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_admin_message(update.effective_user):
        await update.message.reply_text("⛔ Только для админа.")
        return
    n = 25
    if context.args:
        try:
            n = int(context.args[0])
        except ValueError:
            n = 25
    rows = get_transactions(n)
    if not rows:
        await update.message.reply_text("Транзакций пока нет.")
        return
    lines = [f"📜 Транзакции (последние {len(rows)}):\n"]
    for r in rows:
        name = display_name(r["user_id"], r.get("username"), r.get("first_name"))
        delta = int(r.get("delta") or 0)
        sign = f"+{delta}" if delta >= 0 else str(delta)
        why = reason_ru(r.get("reason") or "")
        when = _fmt_ts(r.get("created_at") or 0)
        lines.append(f"• {when} {name} {sign}⭐ ({why})")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n…"
    await update.message.reply_text(text)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_admin_message(update.effective_user):
        await update.message.reply_text("⛔ Только для админа.")
        return
    s = get_stats()
    await update.message.reply_text(
        "📊 Сводка BANANAWOW\n\n"
        f"Игроков: {s['users']}\n"
        f"Ещё free: {s['free_left']}\n"
        f"Уже сыграли free: {s['played_free']}\n"
        f"Сумма балансов: {s['balance_sum']} ⭐\n"
        f"Ставок (игр): {s['plays']}\n"
        f"Сожжено ставками: {s['stakes_burned']} ⭐\n"
        f"Пополнений (ledger): {s['topups']} ⭐\n"
        f"Выплачено выигрышей: {s['wins_paid']} ⭐\n"
        f"Версия: {APP_VERSION}"
    )


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if not query:
        return
    # Принимаем topup_* и старые play_* (на всякий случай)
    payload = query.invoice_payload or ""
    if payload.startswith("topup_") or payload.startswith("play_"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Неизвестный платёж")


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.successful_payment:
        return
    sp = update.message.successful_payment
    payload = sp.invoice_payload or ""
    uid = update.effective_user.id if update.effective_user else None
    amount_paid = int(sp.total_amount or 0)

    log.info(
        "Оплата OK user=%s stars=%s payload=%s",
        uid,
        amount_paid,
        payload,
    )

    credited = 0
    new_bal = 0
    parsed = parse_topup_payload(payload)
    if parsed:
        pay_uid, pack_amount = parsed
        credit_uid = pay_uid if pay_uid else uid
        # Начисляем то, что реально оплатили (и пакет)
        credited = amount_paid if amount_paid > 0 else pack_amount
        if credit_uid:
            try:
                new_bal = add_stars(credit_uid, credited, "topup", payload)
            except Exception:
                log.exception("credit failed")
                new_bal = get_balance(credit_uid)
    elif payload.startswith("play_") and uid:
        # Старый формат: 1 игра = начислим и сразу... просто credit = paid
        # Для совместимости: начислим на баланс (пользователь сам спишет через play)
        credited = amount_paid or GAME_COST
        try:
            new_bal = add_stars(uid, credited, "topup_legacy", payload)
        except Exception:
            log.exception("legacy credit failed")
            new_bal = get_balance(uid)
    elif uid and amount_paid > 0:
        credited = amount_paid
        try:
            new_bal = add_stars(uid, credited, "topup_raw", payload)
        except Exception:
            log.exception("raw credit failed")
            new_bal = get_balance(uid)

    if credited > 0:
        await update.message.reply_text(
            f"✅ +{credited} ⭐ на баланс!\n"
            f"Сейчас: {new_bal} ⭐\n\n"
            f"Одна игра — {GAME_COST} ⭐. Открой мини-приложение и играй 👇",
            reply_markup=play_keyboard(),
        )
    else:
        await update.message.reply_text(
            "✅ Оплата прошла! Открой мини-приложение.",
            reply_markup=play_keyboard(),
        )


def set_menu_button(url: str):
    try:
        result = api_call(
            "setChatMenuButton",
            {
                "menu_button": {
                    "type": "web_app",
                    "text": "Играть",
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
    init_db()

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

    http_thread = threading.Thread(target=start_http, daemon=False)
    http_thread.start()
    time.sleep(0.8)
    log.info(
        "Boot: port=%s webapp=%s token=%s v=%s",
        PORT,
        WEBAPP_URL or "(empty)",
        "yes" if BOT_TOKEN else "MISSING",
        APP_VERSION,
    )

    if not WEBAPP_URL:
        log.warning(
            "WEBAPP_URL пуст — на Render обычно есть RENDER_EXTERNAL_URL. "
            "Кнопка Играть может не работать, пока URL не определится."
        )
    elif BOT_TOKEN:
        try:
            set_menu_button(WEBAPP_URL if WEBAPP_URL.endswith("/") else WEBAPP_URL + "/")
        except Exception:
            log.exception("set_menu_button (non-fatal)")

    if not BOT_TOKEN:
        log.error(
            "Нет BOT_TOKEN! Render → Environment → Add: BOT_TOKEN = токен @BotFather → Save → Manual Deploy"
        )
        while True:
            time.sleep(300)
            log.error("Всё ещё нет BOT_TOKEN — бот не отвечает в Telegram, HTTP жив")

    while True:
        try:
            app_bot = Application.builder().token(BOT_TOKEN).build()
            app_bot.add_handler(CommandHandler("start", cmd_start))
            app_bot.add_handler(CommandHandler("play", cmd_play))
            app_bot.add_handler(CommandHandler("balance", cmd_balance))
            # админ
            app_bot.add_handler(CommandHandler("admin", cmd_admin))
            app_bot.add_handler(CommandHandler("reset", cmd_reset))
            app_bot.add_handler(CommandHandler("reset_confirm", cmd_reset_confirm))
            app_bot.add_handler(CommandHandler("players", cmd_players))
            app_bot.add_handler(CommandHandler(["tx", "transactions"], cmd_tx))
            app_bot.add_handler(CommandHandler("stats", cmd_stats))
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
            app_bot.run_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
            )
        except Exception as e:
            msg = str(e)
            if "Conflict" in msg or "getUpdates" in msg:
                log.error(
                    "Conflict: где-то ещё запущен этот же бот (ПК / второй Render). "
                    "Останови локальный bot.py / START.bat / run_stable. Retry 15s…"
                )
                time.sleep(15)
            else:
                log.exception("polling crashed, restart in 8s")
                time.sleep(8)


if __name__ == "__main__":
    main()
