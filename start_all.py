"""
Поднимает HTTP+бота и Cloudflare tunnel, прописывает WEBAPP_URL в .env.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
CLOUDFLARED = ROOT / "cloudflared.exe"
PORT = 3000


def load_token() -> str:
    text = ENV_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("BOT_TOKEN not found in .env")


def write_env(token: str, webapp_url: str, port: int = PORT):
    ENV_PATH.write_text(
        f"BOT_TOKEN={token}\nWEBAPP_URL={webapp_url}\nPORT={port}\n",
        encoding="utf-8",
    )
    print("Updated .env WEBAPP_URL =", webapp_url)


def set_menu_button(token: str, url: str):
    payload = json.dumps(
        {
            "menu_button": {
                "type": "web_app",
                "text": "Играть",
                "web_app": {"url": url},
            }
        }
    ).encode("utf-8")
    req = Request(
        f"https://api.telegram.org/bot{token}/setChatMenuButton",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        print("setChatMenuButton:", resp.read().decode("utf-8", errors="replace"))


def main():
    token = load_token()
    if not CLOUDFLARED.exists():
        raise SystemExit(f"Нет {CLOUDFLARED}")

    # 1) HTTP + bot (URL пока localhost — обновим после туннеля)
    write_env(token, f"http://127.0.0.1:{PORT}/", PORT)

    bot_proc = subprocess.Popen(
        [sys.executable, str(ROOT / "bot.py")],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print("Started bot pid", bot_proc.pid)
    time.sleep(2)

    # 2) Cloudflare quick tunnel
    tunnel = subprocess.Popen(
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
    )
    print("Started cloudflared pid", tunnel.pid)

    url = None
    pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    deadline = time.time() + 60
    buf = ""
    while time.time() < deadline:
        line = tunnel.stdout.readline() if tunnel.stdout else ""
        if not line:
            if tunnel.poll() is not None:
                break
            time.sleep(0.2)
            continue
        print("[tunnel]", line.rstrip())
        buf += line
        m = pattern.search(line)
        if m:
            url = m.group(0)
            if not url.endswith("/"):
                url += "/"
            break

    if not url:
        bot_proc.terminate()
        tunnel.terminate()
        raise SystemExit("Не удалось получить URL туннеля")

    print("PUBLIC URL:", url)
    write_env(token, url, PORT)

    try:
        set_menu_button(token, url)
    except Exception as e:
        print("Menu button error:", e)

    # 3) Перезапуск бота с правильным WEBAPP_URL
    bot_proc.terminate()
    try:
        bot_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        bot_proc.kill()

    bot_proc = subprocess.Popen(
        [sys.executable, str(ROOT / "bot.py")],
        cwd=str(ROOT),
        stdout=sys.stdout,
        stderr=sys.stderr,
        text=True,
    )
    print("Bot restarted with WEBAPP_URL. Open t.me/bananwowbot → /start")
    print("Keep this window open.")

    # Держим туннель живым; бот в foreground выше — wait both
    try:
        while True:
            if bot_proc.poll() is not None:
                print("Bot exited", bot_proc.returncode)
                break
            if tunnel.poll() is not None:
                print("Tunnel exited", tunnel.returncode)
                break
            # drain tunnel logs
            if tunnel.stdout:
                line = tunnel.stdout.readline()
                if line:
                    print("[tunnel]", line.rstrip())
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        bot_proc.terminate()
        tunnel.terminate()


if __name__ == "__main__":
    main()
