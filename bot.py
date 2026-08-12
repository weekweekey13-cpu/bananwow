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
import math
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
APP_VERSION = "2.10.0"
GAME_COST = 10  # мин. ставка (совместимость)
STAKE_MIN = 10
STAKE_MAX = 200
WIN_MULTIPLIER = 10  # устар.
WELCOME_BONUS = 100  # приветственный бонус на баланс
FREE_FIRST_PRIZE = 0
# Множители к ставке (в 2 раза меньше): ставка 10 → банан 500, ставка 30 → банан 1500
FRUIT_MULT: dict[str, float] = {
    "banana": 50,
    "strawberry": 25,
    "cherry": 15,
    "lemon": 10,
    "grape": 7.5,
}
FRUIT_IDS = tuple(FRUIT_MULT.keys())
FRUIT_PRIZES: dict[str, int] = {
    k: int(round(v * STAKE_MIN)) for k, v in FRUIT_MULT.items()
}  # таблица при ставке 10 (для UI по умолчанию)
WIN_PRIZE = FRUIT_PRIZES["banana"]
# Минимальная сумма вывода с игрового баланса в Telegram Stars
TG_WITHDRAW_MIN = 110
# Сервисный сбор за вывод — оплачивается звёздами ЛИЧНОГО аккаунта (invoice XTR)
WITHDRAW_FEE_RATE = 0.05

# Три одинаковых — редко; чем дороже фрукт, тем реже.
try:
    WIN_RATE = float(os.getenv("WIN_RATE", "0.10"))
except ValueError:
    WIN_RATE = 0.10
WIN_RATE = max(0.0, min(1.0, WIN_RATE))
# Две из трёх — утешительный приз 25% ставки
try:
    PAIR_RATE = float(os.getenv("PAIR_RATE", "0.28"))
except ValueError:
    PAIR_RATE = 0.28
PAIR_RATE = max(0.0, min(1.0, PAIR_RATE))
PAIR_STAKE_SHARE = 0.25
PITY_AFTER = 7  # после серии поражений — пара, не джекпот
# Веса фруктов при 3 одинаковых (дороже → меньше вес)
FRUIT_WEIGHTS: dict[str, int] = {
    "grape": 40,
    "lemon": 26,
    "cherry": 18,
    "strawberry": 11,
    "banana": 5,
}

# Пакеты пополнения: сколько ⭐ купить (= сумма XTR)
TOPUP_PACKAGES = (10, 30, 50, 100, 250)


def withdraw_fee_for(amount: int) -> int:
    """5% от суммы вывода, вверх до целой ⭐, минимум 1."""
    try:
        n = int(amount)
    except (TypeError, ValueError):
        n = TG_WITHDRAW_MIN
    return max(1, int(math.ceil(n * WITHDRAW_FEE_RATE)))


def clamp_stake(raw) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = STAKE_MIN
    return max(STAKE_MIN, min(STAKE_MAX, n))


def prize_for_fruit(fruit: str, stake: int = STAKE_MIN) -> int:
    """Приз = множитель фрукта × ставка (10→500🍌, 30→1500🍌)."""
    return int(round(float(FRUIT_MULT.get(fruit, 0)) * int(stake)))


def paytable_for_stake(stake: int) -> dict[str, int]:
    return {k: prize_for_fruit(k, stake) for k in FRUIT_IDS}


def pair_prize_for_stake(stake: int) -> int:
    """25% от ставки, минимум 1 ⭐."""
    return max(1, int(round(int(stake) * PAIR_STAKE_SHARE)))


def pick_weighted_fruit() -> str:
    items = [(fid, int(FRUIT_WEIGHTS.get(fid, 1))) for fid in FRUIT_IDS]
    total = sum(w for _, w in items) or 1
    r = random.uniform(0, total)
    acc = 0.0
    for fid, w in items:
        acc += w
        if r <= acc:
            return fid
    return items[-1][0]


def pick_round(
    stake: int = STAKE_MIN,
    user_id: int | None = None,
) -> tuple[str, str, int]:
    """Исход: ('win'|'pair'|'lose', fruit_id, prize)."""
    fruit = pick_weighted_fruit()
    roll = random.random()
    kind = "lose"
    if roll < WIN_RATE:
        kind = "win"
    elif roll < WIN_RATE + PAIR_RATE:
        kind = "pair"
    if kind == "lose" and user_id is not None:
        if get_loss_streak(int(user_id)) >= PITY_AFTER:
            kind = "pair"
    if kind == "win":
        prize = prize_for_fruit(fruit, stake)
    elif kind == "pair":
        prize = pair_prize_for_stake(stake)
    else:
        prize = 0
    return kind, fruit, prize


def public_game_config() -> dict:
    return {
        "gameCost": GAME_COST,
        "stakeMin": STAKE_MIN,
        "stakeMax": STAKE_MAX,
        "winMultiplier": WIN_MULTIPLIER,
        "welcomeBonus": WELCOME_BONUS,
        "freeFirstPrize": FREE_FIRST_PRIZE,
        "winPrize": WIN_PRIZE,
        "tgWithdrawMin": TG_WITHDRAW_MIN,
        "withdrawFeeRate": WITHDRAW_FEE_RATE,
        "withdrawFee": withdraw_fee_for(TG_WITHDRAW_MIN),
        "winRate": WIN_RATE,
        "pairRate": PAIR_RATE,
        "pairStakeShare": PAIR_STAKE_SHARE,
        "pityAfter": PITY_AFTER,
        "paytable": paytable_for_stake(STAKE_MIN),
        "fruitMultipliers": dict(FRUIT_MULT),
        "packages": list(TOPUP_PACKAGES),
        "stars": GAME_COST,
    }

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
                    fruit TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_play_done INTEGER NOT NULL DEFAULT 0,
                    welcome_bonus_claimed INTEGER NOT NULL DEFAULT 0,
                    loss_streak INTEGER NOT NULL DEFAULT 0,
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS withdraw_fees (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    withdraw_amount INTEGER NOT NULL,
                    fee_amount INTEGER NOT NULL,
                    payload TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    paid_at REAL,
                    used_at REAL
                )
                """
            )
            # миграции старых БД
            for col_sql in (
                "ALTER TABLE users ADD COLUMN username TEXT",
                "ALTER TABLE users ADD COLUMN first_name TEXT",
                "ALTER TABLE users ADD COLUMN last_seen REAL NOT NULL DEFAULT 0",
                "ALTER TABLE sessions ADD COLUMN fruit TEXT",
                "ALTER TABLE users ADD COLUMN welcome_bonus_claimed INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE users ADD COLUMN loss_streak INTEGER NOT NULL DEFAULT 0",
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
    """Совместимость: теперь это «бонус ещё не забирали»."""
    return is_welcome_bonus_available(user_id)


def _user_bonus_claimed(row) -> bool:
    if row is None:
        return False
    keys = row.keys()
    if "welcome_bonus_claimed" in keys:
        return int(row["welcome_bonus_claimed"] or 0) == 1
    return int(row["first_play_done"] or 0) == 1


def get_loss_streak(user_id: int) -> int:
    uid = int(user_id)
    with _db_lock:
        conn = _db()
        try:
            try:
                row = conn.execute(
                    "SELECT loss_streak FROM users WHERE user_id = ?",
                    (uid,),
                ).fetchone()
            except sqlite3.OperationalError:
                return 0
            if not row:
                return 0
            return max(0, int(row["loss_streak"] or 0))
        finally:
            conn.close()


def note_round_result(user_id: int, won: bool) -> None:
    uid = int(user_id)
    with _db_lock:
        conn = _db()
        try:
            if won:
                sql = "UPDATE users SET loss_streak = 0 WHERE user_id = ?"
                conn.execute(sql, (uid,))
            else:
                try:
                    conn.execute(
                        "UPDATE users SET loss_streak = COALESCE(loss_streak, 0) + 1 WHERE user_id = ?",
                        (uid,),
                    )
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        except sqlite3.OperationalError:
            conn.rollback()
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


def is_welcome_bonus_available(user_id: int) -> bool:
    """Первый запуск: бонус 100⭐ ещё не забирали."""
    ensure_user(user_id)
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT first_play_done, welcome_bonus_claimed FROM users WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
            return bool(row) and not _user_bonus_claimed(row)
        except sqlite3.OperationalError:
            row = conn.execute(
                "SELECT first_play_done FROM users WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
            return bool(row) and int(row["first_play_done"] or 0) == 0
        finally:
            conn.close()


def claim_welcome_bonus(user_id: int) -> tuple[bool, int, str]:
    """
    Начислить WELCOME_BONUS один раз. Атомарно.
    → (ok, balance, error_code)
    """
    uid = int(user_id)
    now = time.time()
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT first_play_done, welcome_bonus_claimed FROM users WHERE user_id = ?",
                    (uid,),
                ).fetchone()
            except sqlite3.OperationalError:
                row = conn.execute(
                    "SELECT first_play_done FROM users WHERE user_id = ?",
                    (uid,),
                ).fetchone()
            if not row:
                conn.execute(
                    """
                    INSERT INTO users
                      (user_id, first_play_done, username, first_name, created_at, last_seen)
                    VALUES (?, 0, NULL, NULL, ?, ?)
                    """,
                    (uid, now, now),
                )
                claimed = False
            else:
                claimed = _user_bonus_claimed(row)
            if claimed:
                bal_row = conn.execute(
                    "SELECT stars FROM balances WHERE user_id = ?", (uid,)
                ).fetchone()
                bal = int(bal_row["stars"]) if bal_row else 0
                conn.rollback()
                return False, bal, "already_claimed"

            try:
                conn.execute(
                    """
                    UPDATE users
                    SET first_play_done = 1, welcome_bonus_claimed = 1, last_seen = ?
                    WHERE user_id = ?
                    """,
                    (now, uid),
                )
            except sqlite3.OperationalError:
                conn.execute(
                    "UPDATE users SET first_play_done = 1, last_seen = ? WHERE user_id = ?",
                    (now, uid),
                )
            bal_row = conn.execute(
                "SELECT stars FROM balances WHERE user_id = ?", (uid,)
            ).fetchone()
            current = int(bal_row["stars"]) if bal_row else 0
            new_bal = current + WELCOME_BONUS
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
                (uid, WELCOME_BONUS, "welcome_bonus", "welcome_100", now),
            )
            conn.commit()
            return True, new_bal, "ok"
        except Exception:
            conn.rollback()
            raise
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
                "withdraw_fees",
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
            try:
                rows = conn.execute(
                    """
                    SELECT u.user_id, u.username, u.first_name, u.first_play_done,
                           u.welcome_bonus_claimed,
                           u.last_seen, u.created_at,
                           COALESCE(b.stars, 0) AS stars
                    FROM users u
                    LEFT JOIN balances b ON b.user_id = u.user_id
                    ORDER BY u.last_seen DESC, u.created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            except sqlite3.OperationalError:
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
            try:
                free_left = conn.execute(
                    "SELECT COUNT(*) AS c FROM users WHERE COALESCE(welcome_bonus_claimed, 0) = 0"
                ).fetchone()["c"]
                played = conn.execute(
                    "SELECT COUNT(*) AS c FROM users WHERE welcome_bonus_claimed = 1"
                ).fetchone()["c"]
            except sqlite3.OperationalError:
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
            bonuses = conn.execute(
                "SELECT COALESCE(SUM(delta), 0) AS s FROM ledger WHERE reason = 'welcome_bonus'"
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
                "welcome_bonus_paid": int(bonuses),
                "wins_paid": int(wins),
                "stakes_burned": int(stakes),
            }
        finally:
            conn.close()


def reason_ru(reason: str) -> str:
    r = (reason or "").lower()
    if r == "play":
        return "bet"
    if r == "win_claim":
        return "win"
    if r == "welcome_bonus":
        return "bonus"
    if r == "topup" or r.startswith("topup"):
        return "topup"
    if r == "tg_withdraw":
        return "withdraw"
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


def create_withdraw_fee(user_id: int, withdraw_amount: int, fee_amount: int, payload: str) -> None:
    now = time.time()
    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                """
                INSERT INTO withdraw_fees
                  (user_id, withdraw_amount, fee_amount, payload, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (int(user_id), int(withdraw_amount), int(fee_amount), payload, now),
            )
            conn.commit()
        finally:
            conn.close()


def mark_withdraw_fee_paid(payload: str) -> dict | None:
    """Пометить сбор оплаченным. → row dict или None."""
    now = time.time()
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM withdraw_fees WHERE payload = ?",
                (payload,),
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            status = row["status"]
            if status == "pending":
                conn.execute(
                    "UPDATE withdraw_fees SET status = 'paid', paid_at = ? WHERE payload = ?",
                    (now, payload),
                )
                conn.commit()
                data = dict(row)
                data["status"] = "paid"
                return data
            conn.commit()
            return dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def get_latest_paid_fee(user_id: int) -> dict | None:
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                """
                SELECT id, withdraw_amount, fee_amount FROM withdraw_fees
                WHERE user_id = ? AND status = 'paid'
                ORDER BY id DESC LIMIT 1
                """,
                (int(user_id),),
            ).fetchone()
            return dict(row) if row else None
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()


def has_usable_withdraw_fee(user_id: int, withdraw_amount: int) -> bool:
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                """
                SELECT id FROM withdraw_fees
                WHERE user_id = ? AND withdraw_amount = ? AND status = 'paid'
                ORDER BY id DESC LIMIT 1
                """,
                (int(user_id), int(withdraw_amount)),
            ).fetchone()
            return bool(row)
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()


def consume_paid_withdraw_fee(user_id: int, withdraw_amount: int) -> tuple[bool, int]:
    """Списать оплаченный сбор. → (ok, fee_amount)."""
    now = time.time()
    with _db_lock:
        conn = _db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, fee_amount FROM withdraw_fees
                WHERE user_id = ? AND withdraw_amount = ? AND status = 'paid'
                ORDER BY id DESC LIMIT 1
                """,
                (int(user_id), int(withdraw_amount)),
            ).fetchone()
            if not row:
                conn.rollback()
                return False, 0
            conn.execute(
                "UPDATE withdraw_fees SET status = 'used', used_at = ? WHERE id = ?",
                (now, int(row["id"])),
            )
            conn.commit()
            return True, int(row["fee_amount"])
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def restore_withdraw_fee(user_id: int, withdraw_amount: int) -> None:
    """Вернуть used → paid, если вывод не прошёл."""
    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                """
                UPDATE withdraw_fees
                SET status = 'paid', used_at = NULL
                WHERE id = (
                    SELECT id FROM withdraw_fees
                    WHERE user_id = ? AND withdraw_amount = ? AND status = 'used'
                    ORDER BY id DESC LIMIT 1
                )
                """,
                (int(user_id), int(withdraw_amount)),
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()


def parse_wfee_payload(payload: str) -> tuple[int, int, int] | None:
    """wfee_{userId}_{withdrawAmount}_{fee}_{ts} → (uid, amount, fee)."""
    if not payload or not payload.startswith("wfee_"):
        return None
    parts = payload.split("_")
    if len(parts) < 5:
        return None
    try:
        uid = int(parts[1])
        amount = int(parts[2])
        fee = int(parts[3])
    except ValueError:
        return None
    if uid < 1 or amount < 1 or fee < 1:
        return None
    return uid, amount, fee


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


def create_session(
    user_id: int,
    will_win: bool,
    prize: int = 0,
    free: bool = False,
    fruit: str = "",
) -> str:
    sid = secrets.token_hex(16)
    now = time.time()
    fruit_id = (fruit or "").strip().lower()
    if fruit_id not in FRUIT_PRIZES:
        fruit_id = random.choice(FRUIT_IDS)
    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                """
                INSERT INTO sessions
                  (id, user_id, will_win, claimed, prize, free, fruit, created_at)
                VALUES (?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    sid,
                    int(user_id),
                    1 if will_win else 0,
                    int(prize),
                    1 if free else 0,
                    fruit_id,
                    now,
                ),
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
            or p.endswith(".mp3")
            or p.endswith(".wav")
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
            return self._json(200, public_game_config())
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
        if parsed.path in ("/api/claim-bonus", "/api/welcome-bonus"):
            return self._handle_claim_bonus(body)
        if parsed.path in ("/api/withdraw-telegram", "/api/tg-withdraw"):
            return self._handle_tg_withdraw(body)
        if parsed.path in (
            "/api/create-withdraw-fee",
            "/api/withdraw-fee-invoice",
        ):
            return self._handle_withdraw_fee_invoice(body)
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
            "welcome_bonus": WELCOME_BONUS,
            "free_first_prize": FREE_FIRST_PRIZE,
            "win_prize": WIN_PRIZE,
            "tg_withdraw_min": TG_WITHDRAW_MIN,
            "win_rate": WIN_RATE,
            "pity_after": PITY_AFTER,
            "paytable": paytable_for_stake(STAKE_MIN),
            "fruit_multipliers": dict(FRUIT_MULT),
            "last_external_ping_sec_ago": age,
            "ping_url": "/api/ping",
            "setup_url": "/keepalive-setup",
            "hint": (
                "BOT_TOKEN missing in Environment — add it in Render and Redeploy."
                if not has_token
                else "Keep-alive: UptimeRobot → /api/ping every 5 min."
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
        welcome_bonus = False
        paid_fee = None
        if uid is not None:
            ensure_user(int(uid), uname, first_name)
            balance = get_balance(int(uid))
            welcome_bonus = is_welcome_bonus_available(int(uid))
            paid_fee = get_latest_paid_fee(int(uid))

        log.info(
            "api/me verified=%s admin=%s user=%s id=%s bal=%s bonus=%s",
            verified,
            is_admin,
            uname,
            uid,
            balance,
            welcome_bonus,
        )
        payload = public_game_config()
        payload.update(
            {
                "ok": True,
                "isAdmin": is_admin,
                "verified": verified,
                "username": uname,
                "userId": uid,
                "balance": balance,
                "welcomeBonusAvailable": welcome_bonus,
                "freePlayAvailable": False,
                "canWithdrawTelegram": balance >= TG_WITHDRAW_MIN,
                "withdrawFeePaid": bool(paid_fee),
                "withdrawPaidAmount": (
                    int(paid_fee["withdraw_amount"]) if paid_fee else 0
                ),
            }
        )
        self._json(200, payload)

    def _play_ok(
        self,
        *,
        balance: int,
        stake: int,
        spent: int,
        prize: int,
        sid: str,
        will_win: bool,
        fruit: str,
        is_admin: bool = False,
        free: bool = False,
        pair_win: bool = False,
    ) -> None:
        payload = public_game_config()
        payload.update(
            {
                "ok": True,
                "free": free,
                "firstFree": False,
                "balance": balance,
                "stake": stake,
                "winPrize": prize,
                "spent": spent,
                "sessionId": sid,
                "willWin": will_win,
                "pairWin": pair_win,
                "matchFruit": fruit,
                "welcomeBonusAvailable": False,
                "freePlayAvailable": False,
                "isAdmin": is_admin,
            }
        )
        self._json(200, payload)

    def _handle_claim_bonus(self, body: dict):
        """Забрать приветственный бонус 100⭐ (один раз)."""
        user, verified, uname, uid = self._resolve_user(body)
        if uid is None:
            self._json(
                401,
                {
                    "ok": False,
                    "error": "open_in_telegram",
                    "message": "Open the game via the Telegram bot.",
                },
            )
            return

        first_name = (user or {}).get("first_name") if user else None
        ensure_user(int(uid), uname, first_name)

        try:
            ok, bal, err = claim_welcome_bonus(int(uid))
        except Exception as e:
            log.exception("claim-bonus failed")
            self._json(500, {"ok": False, "error": str(e)})
            return

        if not ok:
            self._json(
                400,
                {
                    "ok": False,
                    "error": err,
                    "balance": bal,
                    "welcomeBonusAvailable": False,
                    "message": "Bonus already claimed" if err == "already_claimed" else err,
                },
            )
            return

        log.info("welcome-bonus user=%s bal=%s", uid, bal)
        self._json(
            200,
            {
                "ok": True,
                "balance": bal,
                "bonus": WELCOME_BONUS,
                "welcomeBonusAvailable": False,
                "message": f"+{WELCOME_BONUS} ⭐ welcome bonus",
            },
        )

    def _handle_play(self, body: dict):
        """Списать ставку, создать сессию (will_win 1/5, приз по фрукту)."""
        user, verified, uname, uid = self._resolve_user(body)
        admin = is_admin_user(user) if verified else False
        soft = bool(
            (body.get("username") or "").strip().lstrip("@").lower() in ADMIN_USERNAMES
        )
        is_admin = admin or (soft and not verified)
        stake = clamp_stake(body.get("stake") or body.get("amount") or STAKE_MIN)
        kind, fruit, prize = pick_round(stake, uid)
        will_win = kind == "win"
        pair_win = kind == "pair"
        claimable = kind in ("win", "pair")

        # --- admin: играет бесплатно, тот же шанс ---
        if is_admin:
            bal = get_balance(int(uid)) if uid else 0
            sid = ""
            if uid is not None:
                note_round_result(int(uid), claimable)
                sid = create_session(
                    int(uid), claimable, prize, free=True, fruit=fruit
                )
            self._play_ok(
                balance=bal,
                stake=stake,
                spent=0,
                prize=prize,
                sid=sid,
                will_win=will_win,
                fruit=fruit,
                is_admin=True,
                free=True,
                pair_win=pair_win,
            )
            return

        if uid is None:
            self._json(
                401,
                {
                    "ok": False,
                    "error": "open_in_telegram",
                    "message": "Open the game via the Telegram bot.",
                },
            )
            return

        first_name = (user or {}).get("first_name") if user else None
        ensure_user(int(uid), uname, first_name)

        if not verified:
            self._json(
                401,
                {
                    "ok": False,
                    "error": "open_in_telegram",
                    "message": "Reopen the game via the Play button in the bot.",
                },
            )
            return

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
                    "welcomeBonusAvailable": is_welcome_bonus_available(int(uid)),
                    "message": f"Not enough ⭐. Need {stake}, you have {bal}.",
                },
            )
            return

        note_round_result(int(uid), claimable)
        sid = create_session(int(uid), claimable, prize, free=False, fruit=fruit)
        log.info(
            "play user=%s bal=%s kind=%s fruit=%s stake=%s prize=%s session=%s",
            uid,
            bal,
            kind,
            fruit,
            stake,
            prize,
            sid[:8],
        )
        self._play_ok(
            balance=bal,
            stake=stake,
            spent=stake,
            prize=prize,
            sid=sid,
            will_win=will_win,
            fruit=fruit,
            pair_win=pair_win,
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
                    "message": "Open the game via the Telegram bot.",
                },
            )
            return
        if uid is None:
            self._json(400, {"ok": False, "error": "no_user", "message": "No user id"})
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
                        f"Telegram withdraw from {TG_WITHDRAW_MIN} ⭐. "
                        f"You have {bal_before} ⭐ — need {need} more ⭐."
                    ),
                },
            )
            return

        # списываем ровно минимум (или amount, если >= min и <= balance)
        amount = min(amount, bal_before)
        if amount < TG_WITHDRAW_MIN:
            amount = TG_WITHDRAW_MIN

        fee = withdraw_fee_for(amount)
        fee_ok = is_admin or has_usable_withdraw_fee(int(uid), amount)
        if not fee_ok:
            self._json(
                402,
                {
                    "ok": False,
                    "error": "fee_required",
                    "balance": bal_before,
                    "amount": amount,
                    "fee": fee,
                    "feeRate": WITHDRAW_FEE_RATE,
                    "tgWithdrawMin": TG_WITHDRAW_MIN,
                    "message": (
                        "To withdraw stars to your personal account, pay a 5% service fee"
                    ),
                },
            )
            return

        if not is_admin:
            consumed, _fee_amt = consume_paid_withdraw_fee(int(uid), amount)
            if not consumed:
                self._json(
                    402,
                    {
                        "ok": False,
                        "error": "fee_required",
                        "balance": bal_before,
                        "amount": amount,
                        "fee": fee,
                        "feeRate": WITHDRAW_FEE_RATE,
                        "message": (
                            "To withdraw stars to your personal account, pay a 5% service fee"
                        ),
                    },
                )
                return

        ok, bal, err = withdraw_to_telegram(int(uid), amount)
        if not ok and not is_admin:
            restore_withdraw_fee(int(uid), amount)
        if not ok:
            self._json(
                402,
                {
                    "ok": False,
                    "error": err,
                    "balance": bal,
                    "tgWithdrawMin": TG_WITHDRAW_MIN,
                    "message": (
                        f"Not enough ⭐. Need {TG_WITHDRAW_MIN}, you have {bal}."
                        if err == "insufficient"
                        else err
                    ),
                },
            )
            return

        notify_user(
            int(uid),
            (
                f"✅ Withdrawal processed: {amount} ⭐\n"
                f"Amount deducted from game balance.\n"
                f"Game balance left: {bal} ⭐\n\n"
                f"Stars are credited to your Telegram Stars balance."
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
                "message": f"Withdrew {amount} ⭐ to Telegram",
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
                {"ok": False, "error": "no_user", "message": "No user id"},
            )
            return

        # claim по sessionId+userId (для free-first не требуем initData)
        if not verified and not is_admin and not session_id:
            self._json(
                401,
                {
                    "ok": False,
                    "error": "open_in_telegram",
                    "message": "Open the game via the Telegram bot.",
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
                        "already_claimed": "Prize already claimed",
                        "not_a_win": "This round was not a win",
                        "session_not_found": "Session not found",
                        "session_user_mismatch": "Session belongs to another user",
                        "no_session": "No sessionId",
                        "no_prize": "Prize is 0",
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
                "message": f"+{prize_amt} ⭐ to balance",
            },
        )

    def _handle_withdraw_fee_invoice(self, body: dict):
        """Счёт на сервисный сбор 5% — оплата звёздами личного аккаунта Telegram."""
        user, verified, uname, uid = self._resolve_user(body)
        admin = is_admin_user(user) if verified else False
        soft = bool(
            (body.get("username") or "").strip().lstrip("@").lower() in ADMIN_USERNAMES
        )
        is_admin = admin or (soft and not verified)

        if uid is None:
            self._json(
                401,
                {
                    "ok": False,
                    "error": "open_in_telegram",
                    "message": "Open the game via the Telegram bot.",
                },
            )
            return

        if not verified and not is_admin:
            self._json(
                401,
                {
                    "ok": False,
                    "error": "open_in_telegram",
                    "message": "Reopen the game via the Play button in the bot.",
                },
            )
            return

        try:
            amount = int(body.get("amount") or TG_WITHDRAW_MIN)
        except (TypeError, ValueError):
            amount = TG_WITHDRAW_MIN
        if amount < TG_WITHDRAW_MIN:
            amount = TG_WITHDRAW_MIN

        bal = get_balance(int(uid))
        if bal < TG_WITHDRAW_MIN:
            self._json(
                402,
                {
                    "ok": False,
                    "error": "min_balance",
                    "balance": bal,
                    "tgWithdrawMin": TG_WITHDRAW_MIN,
                    "message": f"Withdraw from {TG_WITHDRAW_MIN} ⭐. You have {bal} ⭐.",
                },
            )
            return

        amount = min(amount, bal)
        if amount < TG_WITHDRAW_MIN:
            amount = TG_WITHDRAW_MIN
        fee = withdraw_fee_for(amount)

        if is_admin:
            self._json(
                200,
                {
                    "ok": True,
                    "free": True,
                    "isAdmin": True,
                    "amount": amount,
                    "fee": 0,
                    "balance": bal,
                },
            )
            return

        if has_usable_withdraw_fee(int(uid), amount):
            self._json(
                200,
                {
                    "ok": True,
                    "alreadyPaid": True,
                    "amount": amount,
                    "fee": fee,
                    "balance": bal,
                    "message": "Fee already paid — you can withdraw.",
                },
            )
            return

        payload = f"wfee_{int(uid)}_{amount}_{fee}_{int(time.time() * 1000)}"
        try:
            result = api_call(
                "createInvoiceLink",
                {
                    "title": "5% service fee",
                    "description": (
                        f"5% service fee to withdraw {amount} ⭐ to your Telegram account. "
                        f"Pay {fee} ⭐ from your personal Stars. "
                        "The game balance is not charged."
                    ),
                    "payload": payload,
                    "provider_token": "",
                    "currency": "XTR",
                    "prices": [{"label": f"5% fee · {fee} ⭐", "amount": fee}],
                },
            )
            if not result.get("ok"):
                raise RuntimeError(result.get("description") or str(result))
            link = result["result"]
            create_withdraw_fee(int(uid), amount, fee, payload)
            self._json(
                200,
                {
                    "ok": True,
                    "invoiceLink": link,
                    "amount": amount,
                    "fee": fee,
                    "feeRate": WITHDRAW_FEE_RATE,
                    "balance": bal,
                    "fromPersonalAccount": True,
                },
            )
        except (HTTPError, URLError, RuntimeError, KeyError) as e:
            log.exception("withdraw-fee invoice failed")
            msg = getattr(e, "reason", None) or str(e)
            if isinstance(e, HTTPError):
                try:
                    msg = e.read().decode("utf-8", errors="replace")
                except Exception:
                    msg = str(e)
            self._json(500, {"ok": False, "error": msg})

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
                        "message": f"Choose a pack: {', '.join(map(str, TOPUP_PACKAGES))}",
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
                    "title": f"+{amount} ⭐ to balance",
                    "description": (
                        f"BANANAWOW top-up: +{amount} ⭐. "
                        f"One game — {GAME_COST} ⭐ (~{games} games)."
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
        "Ставка %s–%s ⭐ | bonus=%s | paytable=%s | tg_out≥%s | win_rate=%.2f",
        STAKE_MIN,
        STAKE_MAX,
        WELCOME_BONUS,
        FRUIT_PRIZES,
        TG_WITHDRAW_MIN,
        WIN_RATE,
    )
    server.serve_forever()


def _webapp_url() -> str:
    global WEBAPP_URL
    fresh = detect_public_url() or WEBAPP_URL
    if fresh:
        WEBAPP_URL = fresh
    url = WEBAPP_URL or f"http://127.0.0.1:{PORT}/"
    if not url.endswith("/"):
        url += "/"
    return url


def play_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text="🎮 Play", web_app=WebAppInfo(url=_webapp_url()))]],
        resize_keyboard=True,
    )


# Тексты кнопок админ-клавиатуры (только для ADMIN_USERNAMES)
BTN_PLAY = "🎮 Play"
BTN_PLAYERS = "👥 Players"
BTN_TX = "📜 Transactions"
BTN_STATS = "📊 Stats"
BTN_RESET = "♻️ Reset"
BTN_RESET_OK = "✅ Confirm reset"
BTN_ADMIN = "👑 Menu"


def admin_keyboard() -> ReplyKeyboardMarkup:
    """Готовые кнопки — только админу, чтобы не вводить команды руками."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(text=BTN_PLAY, web_app=WebAppInfo(url=_webapp_url()))],
            [
                KeyboardButton(text=BTN_PLAYERS),
                KeyboardButton(text=BTN_TX),
            ],
            [
                KeyboardButton(text=BTN_STATS),
                KeyboardButton(text=BTN_ADMIN),
            ],
            [
                KeyboardButton(text=BTN_RESET),
                KeyboardButton(text=BTN_RESET_OK),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def keyboard_for(user) -> ReplyKeyboardMarkup:
    if is_admin_message(user):
        return admin_keyboard()
    return play_keyboard()


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
    bonus = is_welcome_bonus_available(user.id) if user else False
    if uname in ADMIN_USERNAMES:
        text = (
            "👑 Admin mode\n\n"
            "Use the buttons below to manage the bot.\n"
            "Find 3 identical · prize = fruit × bet\n"
            "Or tap Play 👇"
        )
    else:
        bonus_line = f"🎁 Welcome bonus: claim {WELCOME_BONUS} ⭐\n" if bonus else ""
        text = (
            "🎰 BANANAWOW\n\n"
            f"{bonus_line}"
            "Find 3 identical fruits in 3 moves.\n"
            f"3 bananas = bet × {FRUIT_MULT['banana']} "
            f"(10⭐ → {prize_for_fruit('banana', 10)}, "
            f"30⭐ → {prize_for_fruit('banana', 30)})\n"
            f"Balance: {bal} ⭐\n\n"
            "Tap Play 👇"
        )
    await update.message.reply_text(text, reply_markup=keyboard_for(user))


async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    if user:
        ensure_user(user.id, user.username, user.first_name)
    bal = get_balance(user.id) if user else 0
    await update.message.reply_text(
        f"Balance: {bal} ⭐ · bet {STAKE_MIN}–{STAKE_MAX} ⭐",
        reply_markup=keyboard_for(user),
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    u = update.effective_user
    ensure_user(u.id, u.username, u.first_name)
    bal = get_balance(u.id)
    bonus = is_welcome_bonus_available(u.id)
    bonus_txt = f"claim {WELCOME_BONUS} ⭐" if bonus else "claimed"
    await update.message.reply_text(
        f"⭐ Balance: {bal}\n"
        f"Welcome bonus: {bonus_txt}\n"
        f"Bet {STAKE_MIN}–{STAKE_MAX} ⭐ · prize scales with bet\n"
        f"At 10⭐: 3🍌 {prize_for_fruit('banana', 10)} · "
        f"3🍓 {prize_for_fruit('strawberry', 10)} · "
        f"3🍒 {prize_for_fruit('cherry', 10)}\n"
        f"At 30⭐: 3🍌 {prize_for_fruit('banana', 30)} · "
        f"3🍓 {prize_for_fruit('strawberry', 30)}\n\n"
        "Top up inside the mini app.",
        reply_markup=keyboard_for(u),
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_admin_message(update.effective_user):
        await update.message.reply_text("⛔ Admin only.")
        return
    await update.message.reply_text(
        "👑 Admin panel BANANAWOW\n\n"
        "Buttons below:\n"
        f"• {BTN_PLAYERS} — who played\n"
        f"• {BTN_TX} — transactions\n"
        f"• {BTN_STATS} — summary\n"
        f"• {BTN_RESET} → then {BTN_RESET_OK}\n"
        f"• {BTN_PLAY} — open game\n\n"
        "Commands also work: /players /tx /stats /reset\n\n"
        "Admins: " + ", ".join("@" + a for a in sorted(ADMIN_USERNAMES)),
        reply_markup=admin_keyboard(),
    )


async def on_admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий reply-кнопок (только админ)."""
    if not update.message or not update.message.text:
        return
    if not is_admin_message(update.effective_user):
        return
    text = update.message.text.strip()
    if text == BTN_PLAYERS:
        await cmd_players(update, context)
    elif text == BTN_TX:
        # как /tx без аргументов
        context.args = []
        await cmd_tx(update, context)
    elif text == BTN_STATS:
        await cmd_stats(update, context)
    elif text == BTN_RESET:
        await cmd_reset(update, context)
    elif text == BTN_RESET_OK:
        await cmd_reset_confirm(update, context)
    elif text == BTN_ADMIN:
        await cmd_admin(update, context)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_admin_message(update.effective_user):
        await update.message.reply_text("⛔ Admin only.")
        return
    await update.message.reply_text(
        "⚠️ This will reset ALL players:\n"
        "• balances\n"
        "• welcome bonus (everyone can claim 100 ⭐ again)\n"
        "• sessions and transaction history\n"
        "• withdraw requests\n\n"
        f"To confirm, tap:\n«{BTN_RESET_OK}»\n"
        "or /reset_confirm",
        reply_markup=admin_keyboard(),
    )


async def cmd_reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_admin_message(update.effective_user):
        await update.message.reply_text("⛔ Admin only.")
        return
    counts = reset_all_game_data()
    log.warning(
        "ADMIN RESET by @%s counts=%s",
        update.effective_user.username,
        counts,
    )
    lines = [f"• {k}: {v}" for k, v in counts.items()]
    await update.message.reply_text(
        "✅ Reset complete.\n"
        "Everyone is treated as a new player — welcome bonus is available again.\n\n"
        "Removed:\n" + "\n".join(lines) + "\n\n"
        "⚠️ Players may still have cached UI — "
        "ask them to close and reopen the mini app.",
        reply_markup=admin_keyboard(),
    )


async def cmd_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_admin_message(update.effective_user):
        await update.message.reply_text("⛔ Admin only.")
        return
    rows = get_players(60)
    if not rows:
        await update.message.reply_text(
            "No players yet.", reply_markup=admin_keyboard()
        )
        return
    lines = [f"👥 Players ({len(rows)}):\n"]
    for r in rows:
        name = display_name(r["user_id"], r.get("username"), r.get("first_name"))
        bonus_done = r.get("welcome_bonus_claimed")
        if bonus_done is None:
            bonus_done = r.get("first_play_done")
        free = "bonus✓" if int(bonus_done or 0) == 0 else "bonus✗"
        stars = int(r.get("stars") or 0)
        seen = _fmt_ts(r.get("last_seen") or r.get("created_at") or 0)
        lines.append(f"• {name} · {stars}⭐ · {free} · {seen}")
    text = "\n".join(lines)
    # Telegram limit ~4096
    if len(text) > 4000:
        text = text[:3900] + "\n…"
    await update.message.reply_text(text, reply_markup=admin_keyboard())


async def cmd_tx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_admin_message(update.effective_user):
        await update.message.reply_text("⛔ Admin only.")
        return
    n = 25
    if context.args:
        try:
            n = int(context.args[0])
        except ValueError:
            n = 25
    rows = get_transactions(n)
    if not rows:
        await update.message.reply_text(
            "No transactions yet.", reply_markup=admin_keyboard()
        )
        return
    lines = [f"📜 Transactions (last {len(rows)}):\n"]
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
    await update.message.reply_text(text, reply_markup=admin_keyboard())


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_admin_message(update.effective_user):
        await update.message.reply_text("⛔ Admin only.")
        return
    s = get_stats()
    await update.message.reply_text(
        "📊 BANANAWOW stats\n\n"
        f"Players: {s['users']}\n"
        f"Bonus left: {s['free_left']}\n"
        f"Bonus claimed: {s['played_free']}\n"
        f"Welcome paid: {s.get('welcome_bonus_paid', 0)} ⭐\n"
        f"Total balances: {s['balance_sum']} ⭐\n"
        f"Bets (games): {s['plays']}\n"
        f"Burned in bets: {s['stakes_burned']} ⭐\n"
        f"Top-ups (ledger): {s['topups']} ⭐\n"
        f"Wins paid: {s['wins_paid']} ⭐\n"
        f"Version: {APP_VERSION}",
        reply_markup=admin_keyboard(),
    )


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if not query:
        return
    # Принимаем topup_*, wfee_* (сбор 5%) и старые play_*
    payload = query.invoice_payload or ""
    if (
        payload.startswith("topup_")
        or payload.startswith("play_")
        or payload.startswith("wfee_")
    ):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Unknown payment")


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
    wfee = parse_wfee_payload(payload)
    if wfee:
        fee_uid, w_amount, w_fee = wfee
        pay_uid = fee_uid or uid
        rec = mark_withdraw_fee_paid(payload)
        log.info(
            "withdraw-fee paid user=%s amount=%s fee=%s rec=%s",
            pay_uid,
            w_amount,
            w_fee,
            bool(rec),
        )
        if pay_uid:
            consumed, _ = consume_paid_withdraw_fee(int(pay_uid), int(w_amount))
            if consumed:
                ok, bal, err = withdraw_to_telegram(int(pay_uid), int(w_amount))
                if not ok:
                    restore_withdraw_fee(int(pay_uid), int(w_amount))
                    notify_user(
                        int(pay_uid),
                        (
                            f"✅ Сервисный сбор {w_fee} ⭐ оплачен с вашего аккаунта Telegram.\n"
                            f"Вывод пока не прошёл ({err}). "
                            "Откройте игру и нажмите «Вывести» ещё раз."
                        ),
                    )
                else:
                    notify_user(
                        int(pay_uid),
                        (
                            f"✅ Сервисный сбор {w_fee} ⭐ оплачен с личного аккаунта.\n"
                            f"Вывод {w_amount} ⭐ с игрового баланса выполнен.\n"
                            f"Игровой баланс: {bal} ⭐"
                        ),
                    )
            else:
                notify_user(
                    int(pay_uid),
                    (
                        f"✅ Сервисный сбор {w_fee} ⭐ оплачен с личного аккаунта.\n"
                        "Откройте игру и нажмите «Вывести»."
                    ),
                )
        return

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
            f"✅ +{credited} ⭐ added to balance!\n"
            f"Now: {new_bal} ⭐\n\n"
            f"One game — {GAME_COST} ⭐. Open the mini app and play 👇",
            reply_markup=play_keyboard(),
        )
    else:
        await update.message.reply_text(
            "✅ Payment successful! Open the mini app.",
            reply_markup=play_keyboard(),
        )


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
            # кнопки админ-клавиатуры (только reply text)
            app_bot.add_handler(
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND
                    & filters.Regex(
                        f"^({BTN_PLAYERS}|{BTN_TX}|{BTN_STATS}|{BTN_RESET}|{BTN_RESET_OK}|{BTN_ADMIN})$"
                    ),
                    on_admin_buttons,
                )
            )
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
