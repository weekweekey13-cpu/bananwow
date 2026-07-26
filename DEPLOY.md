# BANANAWOW в интернете 24/7 (как Калаграм)

Та же схема, что у мессенджера в папке «Сайт»:
**GitHub → Render (free) → UptimeRobot / Cloudflare Worker (keep-alive)**

ПК можно выключать. HTTPS постоянный, без ngrok и без предупреждений.

## За 10–15 минут

### 1. GitHub
1. https://github.com/signup  
2. https://github.com/new → имя `bananawow` → Create (Public, **без** README)

### 2. Залить код
Запустите **`ЗАГРУЗИТЬ НА GITHUB.bat`** в папке «тгбот»  
и вставьте URL: `https://github.com/ВАШ_НИК/bananawow.git`

Если спросит пароль — token:  
https://github.com/settings/tokens → classic → галочка **repo**

### 3. Render
1. https://dashboard.render.com/register (через GitHub)  
2. **New +** → **Web Service** → репозиторий `bananawow`  
3. Настройки:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
   - **Instance type:** Free  
4. Environment:
   - `BOT_TOKEN` = токен от @BotFather (**обязательно**)
   - `ADMIN_USERNAMES` = `bonamartin69` (по желанию)
   - `WEBAPP_URL` — можно не ставить (Render сам даёт `RENDER_EXTERNAL_URL`)  
5. **Create Web Service** → 3–5 минут  
6. Ссылка вида **`https://bananawow-xxxx.onrender.com`**

### 4. Чтобы НЕ засыпал (как у Калаграма)
Бесплатный Render спит ~15 мин без визитов → бот «молчит».

**Самый простой путь — UptimeRobot:**
1. Запустите **`НАСТРОЙ KEEP-ALIVE.bat`**  
   или откройте `https://ВАШ.onrender.com/keepalive-setup`
2. Monitor: HTTP(s)  
3. URL: `https://ВАШ.onrender.com/api/ping`  
4. Every **5 minutes**

**Или Cloudflare Worker** (файл `scripts/cloudflare-ping-worker.js`) — «второй сайт», который не спит.

### 5. Telegram
Бот при старте сам ставит Menu Button на Render-URL.  
Проверка: бот → `/start` → Play.

Сохраните URL в `PUBLIC-URL.txt` (удобно для keep-alive).

## Важно
- **Не коммитьте** `.env` с токеном — токен только в Render Environment.
- Без keep-alive первый заход после сна 30–60 сек.
- Локальный `START.bat` (туннель) больше не нужен для 24/7.

## Проверка
- `https://ВАШ.onrender.com/` — игра  
- `https://ВАШ.onrender.com/api/ping` — `{"ok":true}`  
- `https://ВАШ.onrender.com/api/health` — `last_external_ping_sec_ago`
