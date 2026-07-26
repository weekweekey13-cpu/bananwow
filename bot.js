/**
 * Telegram-бот + API для оплаты 10 Stars за одну игру.
 *
 * npm install
 * скопируйте .env.example → .env
 * npm start
 */

require("dotenv").config();
const path = require("path");
const express = require("express");
const { Telegraf, Markup } = require("telegraf");

const token = process.env.BOT_TOKEN;
const webAppUrl = process.env.WEBAPP_URL;
const port = Number(process.env.PORT || 3000);
const PRICE_STARS = 10;

if (!token) {
  console.error("Укажите BOT_TOKEN в файле .env");
  process.exit(1);
}
if (!webAppUrl) {
  console.error("Укажите WEBAPP_URL в файле .env (HTTPS-ссылка на игру)");
  process.exit(1);
}

const bot = new Telegraf(token);
const app = express();
app.use(express.json());

// Раздаём игру (index.html и статику) с того же сервера
app.use(express.static(path.join(__dirname)));

// --- Telegram bot ---

bot.start(async (ctx) => {
  await ctx.reply(
    "🎰 Добро пожаловать!\n\n" +
      "Найди 3 одинаковых фрукта за 3 попытки.\n" +
      `Одна игра — ${PRICE_STARS} ⭐`,
    Markup.keyboard([
      Markup.button.webApp("🎮 Играть", webAppUrl),
    ]).resize()
  );
});

bot.command("play", async (ctx) => {
  await ctx.reply(
    `Одна игра — ${PRICE_STARS} ⭐`,
    Markup.inlineKeyboard([
      Markup.button.webApp("🎮 Играть", webAppUrl),
    ])
  );
});

// Обязательно: подтверждение платежа до списания Stars
bot.on("pre_checkout_query", async (ctx) => {
  try {
    await ctx.answerPreCheckoutQuery(true);
  } catch (err) {
    console.error("pre_checkout_query error:", err.message);
  }
});

bot.on("successful_payment", async (ctx) => {
  const sp = ctx.message.successful_payment;
  console.log(
    "Оплата OK:",
    "user=",
    ctx.from?.id,
    "stars=",
    sp.total_amount,
    "payload=",
    sp.invoice_payload
  );
  try {
    await ctx.reply("✅ Оплата прошла! Можно играть — откройте мини-приложение.");
  } catch (_) {}
});

// --- API для Mini App ---

/**
 * Создаёт ссылку на счёт (Telegram Stars).
 * Клиент открывает её через Telegram.WebApp.openInvoice(link, cb).
 */
app.post("/api/create-invoice", async (req, res) => {
  try {
    const userId = req.body?.userId || "anon";
    const payload = `play_${userId}_${Date.now()}`;

    // createInvoiceLink — способ для Mini App (не сообщение в чат)
    const invoiceLink = await bot.telegram.callApi("createInvoiceLink", {
      title: "Одна игра",
      description: `Найди 3 одинаковых — 3 попытки. Стоимость: ${PRICE_STARS} Stars.`,
      payload,
      provider_token: "", // пусто для Telegram Stars
      currency: "XTR",
      prices: [{ label: "Одна игра", amount: PRICE_STARS }],
    });

    res.json({ ok: true, invoiceLink, stars: PRICE_STARS });
  } catch (err) {
    console.error("createInvoiceLink error:", err);
    res.status(500).json({
      ok: false,
      error: err.description || err.message || "invoice_failed",
    });
  }
});

app.get("/api/price", (_req, res) => {
  res.json({ stars: PRICE_STARS });
});

// --- start ---

app.listen(port, () => {
  console.log(`HTTP: http://localhost:${port}`);
  console.log(`Mini App URL (должен быть HTTPS снаружи): ${webAppUrl}`);
});

bot.launch().then(() => {
  console.log("Бот запущен. Цена игры:", PRICE_STARS, "⭐");
});

process.once("SIGINT", () => bot.stop("SIGINT"));
process.once("SIGTERM", () => bot.stop("SIGTERM"));
