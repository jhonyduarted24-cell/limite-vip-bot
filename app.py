import os
import json
import uuid
import time
import asyncio
import logging
import sqlite3
from typing import Optional, Dict, Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# =========================
# CONFIG (ENV)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")  # ex: https://seuapp.up.railway.app  (opcional)
VIP_LINK = os.getenv("VIP_LINK", "https://t.me/seuCanalVIP")  # link do canal principal
PAYER_EMAIL = os.getenv("PAYER_EMAIL", "pagamentos@example.com")  # precisa ser email válido
BRAND_NAME = os.getenv("BRAND_NAME", "CANAL VIP")
CURRENCY = os.getenv("CURRENCY", "BRL")  # PIX é BRL

if not BOT_TOKEN:
    raise RuntimeError("Faltou BOT_TOKEN nas variáveis de ambiente.")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("Faltou MP_ACCESS_TOKEN nas variáveis de ambiente.")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vipbot")

MP_API = "https://api.mercadopago.com"

# =========================
# PLANOS (troca valores depois)
# =========================
PLANS = {
    "p1": {"name": "🏆 PLANO 1 – CANAL VIP (PRINCIPAL)", "price": 5.00},
    "p2": {"name": "🔥 PLANO 2 – VIP PLUS (2 CANAIS)", "price": 12.00},
    "p3": {"name": "👑 PLANO 3 – VIP TOTAL (ALL IN)", "price": 25.00},
}

# Mensagem exatamente no estilo que você quer (explicando a bio)
def plan_description(plan_key: str) -> str:
    if plan_key == "p1":
        return (
            "💎 <i>Plano mais vendido / destaque no bot</i>\n\n"
            "<b>Acesso:</b>\n"
            "✅ Canal VIP (principal)\n\n"
            "<b>Como funciona:</b>\n"
            "Após o pagamento, você recebe o link do <b>CANAL VIP</b>.\n"
            "E na <b>bio do CANAL VIP</b> tem o link dos outros canais para você entrar."
        )
    if plan_key == "p2":
        return (
            "⭐ <i>Mais valor por um preço melhor</i>\n\n"
            "<b>Acesso:</b>\n"
            "✅ Canal VIP (principal)\n"
            "✅ + acesso extra (via bio do Canal VIP)\n\n"
            "<b>Como funciona:</b>\n"
            "Você paga e recebe o link do <b>CANAL VIP</b>.\n"
            "Na <b>bio</b> dele estão os links dos outros canais."
        )
    return (
        "💰 <i>Plano premium / máximo valor</i>\n\n"
        "<b>Acesso:</b>\n"
        "✅ Canal VIP (principal)\n"
        "✅ + acesso aos outros canais via bio\n\n"
        "<b>Como funciona:</b>\n"
        "Após o pagamento, você recebe o link do <b>CANAL VIP</b>.\n"
        "Na <b>bio do Canal VIP</b> você encontra o link de todos os outros canais."
    )

# =========================
# SQLITE (persistência)
# =========================
DB_PATH = os.getenv("DB_PATH", "vipbot.sqlite3")

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS payments (
            payment_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            plan_key TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )"""
    )
    return conn

def db_upsert_payment(payment_id: str, user_id: int, plan_key: str, status: str):
    conn = db_conn()
    with conn:
        conn.execute(
            """INSERT INTO payments(payment_id, user_id, plan_key, status, created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(payment_id) DO UPDATE SET status=excluded.status""",
            (payment_id, user_id, plan_key, status, int(time.time())),
        )
    conn.close()

def db_get_payment(payment_id: str) -> Optional[Dict[str, Any]]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT payment_id, user_id, plan_key, status, created_at FROM payments WHERE payment_id=?", (payment_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "payment_id": row[0],
        "user_id": row[1],
        "plan_key": row[2],
        "status": row[3],
        "created_at": row[4],
    }

def db_update_status(payment_id: str, status: str):
    conn = db_conn()
    with conn:
        conn.execute("UPDATE payments SET status=? WHERE payment_id=?", (status, payment_id))
    conn.close()

# =========================
# MERCADO PAGO (PIX)
# =========================
def mp_headers(idempotency_key: Optional[str] = None) -> Dict[str, str]:
    h = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        h["X-Idempotency-Key"] = idempotency_key
    return h

async def mp_create_pix_payment(amount: float, user_id: int, plan_key: str) -> Dict[str, Any]:
    notification_url = f"{PUBLIC_BASE_URL}/mp/webhook" if PUBLIC_BASE_URL else None

    payload = {
        "transaction_amount": float(amount),
        "description": f"{BRAND_NAME} - {plan_key} - user {user_id}",
        "payment_method_id": "pix",
        "currency_id": CURRENCY,
        "payer": {"email": PAYER_EMAIL},  # precisa ser válido
        "metadata": {"telegram_user_id": user_id, "plan_key": plan_key},
    }
    if notification_url:
        payload["notification_url"] = notification_url

    idem = str(uuid.uuid4())
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{MP_API}/v1/payments",
            headers=mp_headers(idem),
            json=payload,
        )

    if r.status_code >= 400:
        # log real do MP (pra você nunca mais ficar no escuro)
        log.error("MP CREATE ERROR %s -> %s", r.status_code, r.text)
        raise RuntimeError(f"MP ERROR {r.status_code} -> {r.text}")

    data = r.json()
    tx = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
    qr_code = tx.get("qr_code")

    if not qr_code:
        log.error("MP SEM QR_CODE -> %s", json.dumps(data, ensure_ascii=False))
        raise RuntimeError(f"MP SEM QR_CODE -> {data}")

    return {
        "payment_id": str(data.get("id")),
        "status": str(data.get("status", "")).lower(),
        "qr_code": qr_code,
        "qr_code_base64": tx.get("qr_code_base64"),
    }

async def mp_get_payment_status(payment_id: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{MP_API}/v1/payments/{payment_id}", headers=mp_headers())
    if r.status_code >= 400:
        log.error("MP STATUS ERROR %s -> %s", r.status_code, r.text)
        raise RuntimeError(f"MP STATUS ERROR {r.status_code} -> {r.text}")
    data = r.json()
    return str(data.get("status", "")).lower()

# =========================
# TELEGRAM UI
# =========================
def menu_text() -> str:
    return (
        "✅ <b>Escolha um plano abaixo</b>\n\n"
        "1️⃣ <b>Canal VIP</b> – acesso ao canal principal\n"
        "2️⃣ <b>VIP Plus</b> – canal principal + 1 extra (via bio)\n"
        "3️⃣ <b>VIP Total</b> – acesso total (via bio)\n\n"
        "💡 <i>Depois do pagamento, você recebe o link do Canal VIP.\n"
        "Na bio dele tem os links dos outros canais.</i>"
    )

def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 Plano 1 – Canal VIP", callback_data="buy:p1")],
        [InlineKeyboardButton("🔥 Plano 2 – VIP Plus", callback_data="buy:p2")],
        [InlineKeyboardButton("👑 Plano 3 – VIP Total", callback_data="buy:p3")],
    ])

def pay_kb(payment_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Já paguei (verificar)", callback_data=f"check:{payment_id}")],
        [InlineKeyboardButton("⬅️ Voltar aos planos", callback_data="back:plans")],
    ])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        menu_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=menu_kb(),
        disable_web_page_preview=True,
    )

async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "back:plans":
        await q.edit_message_text(
            menu_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=menu_kb(),
            disable_web_page_preview=True,
        )
        return

    if data.startswith("buy:"):
        plan_key = data.split("buy:", 1)[1]
        plan = PLANS.get(plan_key)
        if not plan:
            await q.edit_message_text("Plano inválido. Use /start.")
            return

        try:
            pay = await mp_create_pix_payment(plan["price"], q.from_user.id, plan_key)
        except Exception:
            await q.edit_message_text(
                "❌ Não consegui gerar o PIX agora.\n\n"
                "➡️ Confira estas variáveis no Railway:\n"
                "• MP_ACCESS_TOKEN (Access Token mesmo)\n"
                "• PAYER_EMAIL (email válido)\n\n"
                "Depois tente novamente.",
                parse_mode=ParseMode.HTML,
            )
            return

        payment_id = pay["payment_id"]
        db_upsert_payment(payment_id, q.from_user.id, plan_key, pay["status"])

        msg = (
            f"{plan['name']}\n"
            f"<b>Valor:</b> R$ {plan['price']:.2f}\n\n"
            f"{plan_description(plan_key)}\n\n"
            "💳 <b>PIX (copia e cola):</b>\n"
            f"<code>{pay['qr_code']}</code>\n\n"
            "✅ Depois que você pagar, clique em <b>“Já paguei (verificar)”</b>.\n"
            "📩 Quando confirmar, eu te envio o link do <b>CANAL VIP</b> automaticamente."
        )

        await q.edit_message_text(
            msg,
            parse_mode=ParseMode.HTML,
            reply_markup=pay_kb(payment_id),
            disable_web_page_preview=True,
        )
        return

    if data.startswith("check:"):
        payment_id = data.split("check:", 1)[1]
        info = db_get_payment(payment_id)
        if not info:
            await q.edit_message_text("Pagamento não encontrado. Gere um novo pelo /start.")
            return

        if info["user_id"] != q.from_user.id:
            await q.edit_message_text("Esse pagamento não é seu.")
            return

        try:
            status = await mp_get_payment_status(payment_id)
        except Exception:
            await q.edit_message_text(
                "❌ Não consegui verificar agora. Tente novamente.",
                parse_mode=ParseMode.HTML,
                reply_markup=pay_kb(payment_id),
            )
            return

        db_update_status(payment_id, status)

        if status == "approved":
            await q.edit_message_text(
                "✅ <b>Pagamento confirmado!</b>\n\n"
                f"🔗 Aqui está o link do <b>CANAL VIP</b>:\n{VIP_LINK}\n\n"
                "📌 <i>Importante:</i> na <b>bio do Canal VIP</b> você encontra os links dos outros canais.",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        elif status in ("pending", "in_process"):
            await q.edit_message_text(
                "⏳ <b>Ainda não confirmou</b>.\n\n"
                "Se você acabou de pagar, pode levar alguns instantes.\n"
                "Clique em <b>“Já paguei (verificar)”</b> novamente.",
                parse_mode=ParseMode.HTML,
                reply_markup=pay_kb(payment_id),
            )
        else:
            await q.edit_message_text(
                f"⚠️ Status do pagamento: <b>{status}</b>\n\n"
                "Se deu erro no pagamento, gere um novo PIX pelo /start.",
                parse_mode=ParseMode.HTML,
                reply_markup=menu_kb(),
            )

# =========================
# FASTAPI (webhook MP) - opcional
# =========================
app = FastAPI()
tg_app: Optional[Application] = None

@app.post("/mp/webhook")
async def mp_webhook(request: Request):
    body = await request.json()
    log.info("Webhook recebido: %s", body)

    payment_id = None
    if isinstance(body, dict):
        payment_id = (body.get("data") or {}).get("id") or body.get("id")
    if not payment_id:
        return JSONResponse({"ok": True}, status_code=200)

    payment_id = str(payment_id)
    info = db_get_payment(payment_id)
    if not info:
        return JSONResponse({"ok": True, "msg": "unknown payment"}, status_code=200)

    try:
        status = await mp_get_payment_status(payment_id)
        db_update_status(payment_id, status)
    except Exception:
        return JSONResponse({"ok": True}, status_code=200)

    if status == "approved" and tg_app:
        try:
            await tg_app.bot.send_message(
                chat_id=info["user_id"],
                text=(
                    "✅ <b>Pagamento confirmado!</b>\n\n"
                    f"🔗 Aqui está o link do <b>CANAL VIP</b>:\n{VIP_LINK}\n\n"
                    "📌 <i>Importante:</i> na <b>bio do Canal VIP</b> você encontra os links dos outros canais."
                ),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Falha ao notificar usuário no Telegram")

    return JSONResponse({"ok": True}, status_code=200)

# =========================
# RUN BOTH (Polling + API)
# =========================
async def run_bot():
    global tg_app
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CallbackQueryHandler(on_cb))

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    log.info("Bot rodando (polling).")

async def run_api():
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    await asyncio.gather(run_bot(), run_api())

if __name__ == "__main__":
    asyncio.run(main())
