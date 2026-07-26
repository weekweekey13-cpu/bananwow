"""
Надёжный запуск: бот + HTTPS-туннель без страницы-предупреждения.

Провайдеры (по кругу, бесплатно):
  1) Tunnelmole  — https://*.tunnelmole.net
  2) Localtunnel — https://*.loca.lt

Cloudflare quick tunnel с этой сети часто не поднимается (timeout) —
оставлен как запасной, если вдруг заработает.

Авто-рестарт бота и туннеля, health-check, обновление WEBAPP_URL и
кнопки меню в Telegram.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
LOG_PATH = ROOT / "stable.log"
PORT = 3000
NODE_DIR = ROOT / "tools" / "node"
NPX = NODE_DIR / "npx.cmd"
CLOUDFLARED = ROOT / "cloudflared.exe"

# URL-паттерны рабочих туннелей
URL_PATTERNS = [
    re.compile(r"https://[a-z0-9.-]+\.tunnelmole\.(net|com)", re.I),
    re.compile(r"https://[a-z0-9-]+\.loca\.lt", re.I),
    re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I),
    re.compile(r"https://[a-z0-9-]+\.localtunnel\.me", re.I),
]

# Признаки «битого» ответа (предупреждения / страница туннеля, не игра)
BAD_MARKERS = (
    "no tunnel",
    "serveo browser warning",
    "ngrok",
    "visit site",
    "err_ngrok",
    "cloudflare tunnel error",
    "tunnel not found",
    "502 bad gateway",
    "503 service",
    "localtunnel error",
    "unable to connect",
    "this tunnel is offline",
)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_env() -> dict[str, str]:
    data: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
    return data


def save_env(env: dict[str, str]) -> None:
    lines = [
        f"BOT_TOKEN={env.get('BOT_TOKEN', '')}",
        f"WEBAPP_URL={env.get('WEBAPP_URL', '')}",
        f"PORT={env.get('PORT', str(PORT))}",
        f"ADMIN_USERNAMES={env.get('ADMIN_USERNAMES', 'bonamartin69')}",
        "",
    ]
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")


def api(token: str, method: str, payload: dict) -> dict:
    req = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def set_menu(token: str, url: str) -> None:
    if not url.endswith("/"):
        url += "/"
    try:
        r = api(
            token,
            "setChatMenuButton",
            {
                "menu_button": {
                    "type": "web_app",
                    "text": "Play",
                    "web_app": {"url": url},
                }
            },
        )
        log(f"[menu] {r}")
    except Exception as e:
        log(f"[menu] error: {e}")


def extract_url(text: str) -> str | None:
    for pat in URL_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0).rstrip("/") + "/"
    return None


def http_ok(url: str) -> bool:
    """Проверяем, что снаружи реально отдаётся игра, а не warning-page."""
    try:
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 TelegramBot-HealthCheck/1.0",
                "Accept": "text/html,*/*",
            },
            method="GET",
        )
        with urlopen(req, timeout=15) as resp:
            body = resp.read(8000).decode("utf-8", errors="ignore")
            low = body.lower()
            if any(m in low for m in BAD_MARKERS):
                return False
            # Игра или хотя бы наш index
            return (
                "Find 3" in body
                or "найди 3" in low
                or "index.html" in low
                or 'id="grid"' in low
                or "card" in low and "fruit" in low
            )
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def kill(proc: subprocess.Popen | None) -> None:
    if not proc or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def node_env() -> dict[str, str]:
    env = os.environ.copy()
    if NODE_DIR.exists():
        env["Path"] = str(NODE_DIR) + os.pathsep + env.get("Path", "")
        env["PATH"] = env["Path"]
    # меньше интерактива npm
    env["npm_config_yes"] = "true"
    env["CI"] = "1"
    return env


def start_bot() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(ROOT / "bot.py")],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _popen_npx(args: list[str]) -> subprocess.Popen:
    if NPX.exists():
        cmd = [str(NPX), "--yes", *args]
    else:
        cmd = ["npx", "--yes", *args]
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=node_env(),
        shell=True,  # .cmd на Windows
    )


def start_tunnelmole() -> subprocess.Popen:
    return _popen_npx(["tunnelmole@latest", str(PORT)])


def start_localtunnel() -> subprocess.Popen:
    # --print-requests не нужен; print-requests иногда шумит
    return _popen_npx(["localtunnel@2.0.2", "--port", str(PORT)])


def start_cloudflared() -> subprocess.Popen | None:
    if not CLOUDFLARED.exists():
        return None
    return subprocess.Popen(
        [
            str(CLOUDFLARED),
            "tunnel",
            "--url",
            f"http://127.0.0.1:{PORT}",
            "--no-autoupdate",
        ],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )


PROVIDERS = [
    ("tunnelmole", start_tunnelmole, 100),
    ("localtunnel", start_localtunnel, 90),
    ("cloudflared", start_cloudflared, 70),
]


def read_url(tunnel: subprocess.Popen, timeout: float) -> str | None:
    deadline = time.time() + timeout
    buf_lines: list[str] = []
    while time.time() < deadline:
        if tunnel.poll() is not None:
            # дочитать остаток
            if tunnel.stdout:
                rest = tunnel.stdout.read() or ""
                for line in rest.splitlines():
                    log(f"[tunnel] {line.rstrip()}")
                    u = extract_url(line)
                    if u:
                        return u
            return None
        line = tunnel.stdout.readline() if tunnel.stdout else ""
        if not line:
            time.sleep(0.15)
            continue
        s = line.rstrip()
        log(f"[tunnel] {s}")
        buf_lines.append(s)
        u = extract_url(s)
        if u:
            return u
        # localtunnel иногда пишет "your url is: https://..."
        joined = "\n".join(buf_lines[-15:])
        u = extract_url(joined)
        if u:
            return u
    return None


def wait_local_http(timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{PORT}/", timeout=2) as resp:
                body = resp.read(2000).decode("utf-8", errors="ignore")
                if body:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def main() -> None:
    env = load_env()
    token = env.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN missing in .env")

    global PORT
    try:
        PORT = int(env.get("PORT") or "3000")
    except ValueError:
        PORT = 3000

    bot: subprocess.Popen | None = None
    tunnel: subprocess.Popen | None = None
    current_url = ""
    provider_idx = 0
    fail_streak = 0

    def cleanup(*_a):
        log("[stop] shutting down…")
        kill(bot)
        kill(tunnel)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    log("=== BANANAWOW stable: multi-tunnel (no warning page) ===")
    log("Providers: Tunnelmole → Localtunnel → Cloudflare (fallback)")
    log("Окно не закрывать. Лог: stable.log")

    while True:
        # --- bot ---
        if bot is None or bot.poll() is not None:
            code = bot.returncode if bot else None
            log(f"[bot] starting… (prev exit={code})")
            kill(bot)
            bot = start_bot()
            if not wait_local_http(30):
                log("[bot] local HTTP not ready, retry in 5s")
                kill(bot)
                bot = None
                time.sleep(5)
                continue
            log(f"[bot] OK on port {PORT}")

        need_tunnel = (
            tunnel is None
            or tunnel.poll() is not None
            or not current_url
            or not http_ok(current_url)
        )

        if need_tunnel:
            if current_url:
                log(f"[health] dead or bad: {current_url}")
            kill(tunnel)
            tunnel = None
            current_url = ""

            name, starter, t_out = PROVIDERS[provider_idx % len(PROVIDERS)]
            provider_idx += 1
            log(f"[tunnel] trying {name}…")

            proc = starter()
            if proc is None:
                log(f"[tunnel] {name} unavailable, next…")
                fail_streak += 1
                time.sleep(2)
                continue

            tunnel = proc
            url = read_url(tunnel, float(t_out))
            if not url:
                log(f"[tunnel] {name}: no URL, next in 6s")
                kill(tunnel)
                tunnel = None
                fail_streak += 1
                # если много фейлов подряд — подольше пауза
                time.sleep(6 if fail_streak < 6 else 20)
                continue

            ok = False
            for i in range(25):
                if tunnel.poll() is not None:
                    break
                if http_ok(url):
                    ok = True
                    break
                time.sleep(1.0)

            if not ok:
                log(f"[tunnel] {name}: URL not serving game ({url}), next")
                kill(tunnel)
                tunnel = None
                fail_streak += 1
                time.sleep(4)
                continue

            fail_streak = 0
            current_url = url
            env["WEBAPP_URL"] = url
            save_env(env)
            set_menu(token, url)
            log(f"[ok] LIVE via {name}: {url}")
            log("Telegram → бот → /start → Play  (без предупреждения)")

            # Перезапуск бота, чтобы подхватить новый WEBAPP_URL
            kill(bot)
            bot = start_bot()
            wait_local_http(20)
            # provider_idx уже сдвинут — при следующем падении возьмём следующий,
            # но успешный провайдер оставим «предпочтительным»: откатим idx-1
            provider_idx = (provider_idx - 1) % len(PROVIDERS)

        # health loop
        time.sleep(18)
        if current_url and not http_ok(current_url):
            log("[health] tunnel died, reconnecting…")
            kill(tunnel)
            tunnel = None
            current_url = ""
            # следующий провайдер
            provider_idx = (provider_idx + 1) % len(PROVIDERS)


if __name__ == "__main__":
    main()
