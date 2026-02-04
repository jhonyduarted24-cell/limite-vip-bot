import os
import time
import json
import uuid
import sqlite3
from dataclasses import dataclass
from typing import Optional, Dict, Any

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ChatJoinRequestHandler,
)

# =========================
# Config (edite só aqui)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
VIP_LINK = os.getenv("VIP_LINK", "").strip()

# 3 planos (mude nomes, dias e preços aqui)
PLANS = {
    "p1": {"name": "VIP 7 dias",  "days": 7,  "price": 9.90},
    "p2": {"name": "VIP 30 dias", "days": 30, "price": 24.90},
    "p3": {"name": "VIP 90 dias", "days": 90, "price": 59.90},
}

MP_API_BASE = "https://api.mercadopago.com"  # NÃO use .com.ar/.com.br aqui
DB_PATH = "db.sqlite"

# =========================
# DB
# =========================
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS payments ("
        "user_id INTEGER, plan_id TEXT, mp_payment_id INTEGER, "
        "status TEXT, created_at INTEGER, expires_at INTEGER, "
        "PRIMARY KEY(user_id, mp_payment_id)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS last_pending ("
        "user_id INTEGER PRIMARY KEY, mp_payment_id INTEGER, plan_id TEXT"
        ")"
    )
    conn.commit()
    return conn

CONN = db()

def set_last_pending(user_id: int, mp_payment_id: int, plan_id: str):
    CONN.execute(
        "INSERT OR REPLACE INTO last_pending(user_id, mp_payment_id, plan_id) VALUES (?, ?, ?)",
        (user_id, mp_payment_id, plan_id),
    )
    CONN.commit()

def get_last_pending(user_id: int) -> Optional[Dict[str, Any]]:
    row = CONN.execute(
        "SELECT mp_payment_id, plan_id FROM last_pending WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    return {"mp_payment_id": int(row[0]), "plan_id": row[1]}

def set_payment(user_id: int, plan_id: str, mp_payment_id: int, status: str, expires_at: int):
    CONN.execute(
        "INSERT OR REPLACE INTO payments(user_id, plan_id, mp_payment_id, status, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, plan_id, mp_payment_id, status, int(time.time()), expires_at),
    )
    CONN.commit()

def update_payment_status(user_id: int, mp_payment_id: int, status: str):
    CONN.execute(
        "UPDATE payments SET status=? WHERE user_id=? AND mp_payment_id=?",
        (status, user_id, mp_payment_id),
    )
    CONN.commit()

def set_subscription_active(user_id: int, plan_id: str, mp_payment_id: int, days: int):
    expires_at = int(time.time()) + days * 86400
    set_payment(user_id, plan_id, mp_payment_id, "approved", expires_at)

def has_active_subscription(user_id: int) -> bool:
    now = int(time.time())
    row = CONN.execute(
        "SELECT 1 FROM payments WHERE user_id=? AND status='approved' AND expires_at > ? LIMIT 1",
        (user_id, now),
    ).fetchone()
    return bool(row)

def subscription_expires_at(user_id: int) -> Optional[int]:
    now = int(time.time())
    row = CONN.execute(
        "SELECT MAX(expires_at) FROM payments WHERE user_id=? AND status='approved' AND expires_at > ?",
        (user_id, now),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0])

# =========================
# Mercado Pago (PIX)
# =========================
async def mp_create_pix(plan_id: str, user_id: int) -> Dict[str, Any]:
    """
    Cria um pagamento PIX no Mercado Pago via /v1/payments.
    Retorna: payment_id, qr_code, qr_base64
    """
    plan = PLANS[plan_id]
    amount = float(plan["price"])

    # Mercado Pago costuma exigir um payer com email.
    fake_email = f"user{user_id}@example.com"

    payload = {
        "transaction_amount": amount,
        "description": f"{plan['name']}",
        "payment_method_id": "pix",
        "payer": {"email": fake_email},
    }

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        # ESTE header não pode ser nulo (se for, dá erro 400 como no seu print)
        "X-Idempotency-Key": str(uuid.uuid4()),
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{MP_API_BASE}/v1/payments", headers=headers, json=payload)
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"MP_ERROR {r.status_code}: {json.dumps(data, ensure_ascii=False)}")

    mp_payment_id = int(data["id"])
    tx = data.get("point_of_interaction", {}).get("transaction_data", {}) or {}
    qr_code = tx.get("qr_code")
    qr_base64 = tx.get("qr_code_base64")

    if not qr_code:
        # Sem qr_code geralmente significa token errado (test/prod), conta sem PIX, ou resposta incompleta
        raise RuntimeError(f"MP_ERROR: PIX não retornou qr_code. Resposta: {json.dumps(data, ensure_ascii=False)}")

    return {"payment_id": mp_payment_id, "qr_code": qr_code, "qr_base64": qr_base64}

async def mp_get_payment(payment_id: int) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{MP_API_BASE}/v1/payments/{payment_id}", headers=headers)
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"MP_ERROR {r.status_code}: {json.dumps(data, ensure_ascii=False)}")
    return data

# =========================
# Telegram UI
# =========================
def plans_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(f"🟩 {PLANS['p1']['name']} — R$ {PLANS['p1']['price']:.2f}", callback_data="buy:p1")],
        [InlineKeyboardButton(f"🟨 {PLANS['p2']['name']} — R$ {PLANS['p2']['price']:.2f}", callback_data="buy:p2")],
        [InlineKeyboardButton(f"🟥 {PLANS['p3']['name']} — R$ {PLANS['p3']['price']:.2f}", callback_data="buy:p3")],
    ]
    return InlineKeyboardMarkup(kb)

def after_pix_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("✅ Já paguei", callback_data="check:last")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="back:plans")],
    ]
    return InlineKeyboardMarkup(kb)

# =========================
# Handlers
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BOT_TOKEN or not MP_ACCESS_TOKEN or not VIP_LINK:
        await update.message.reply_text(
            "⚠️ O bot não está configurado.\n"
            "Verifique as variáveis: BOT_TOKEN, MP_ACCESS_TOKEN, VIP_LINK."
        )
        return

    await update.message.reply_text(
        "👋 Olá! Escolha um plano VIP:",
        reply_markup=plans_keyboard(),
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    exp = subscription_expires_at(uid)
    if not exp:
        await update.message.reply_text("❌ Você não tem assinatura ativa.")
        return
    days_left = int((exp - int(time.time())) / 86400)
    await update.message.reply_text(f"✅ Assinatura ativa. Expira em ~{days_left} dia(s).")

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id
    data = query.data or ""

    if data == "back:plans":
        await query.edit_message_text("Escolha um plano VIP:", reply_markup=plans_keyboard())
        return

    if data.startswith("buy:"):
        plan_id = data.split(":", 1)[1]
        if plan_id not in PLANS:
            await query.edit_message_text("Plano inválido.")
            return

        plan = PLANS[plan_id]
        await query.edit_message_text("⏳ Gerando PIX, aguarde...")

        try:
            pix = await mp_create_pix(plan_id=plan_id, user_id=uid)
            set_last_pending(uid, pix["payment_id"], plan_id)

            # mensagem com copia e cola (QR code base64 não dá pra enviar como imagem sem converter)
            text = (
                f"✅ PIX gerado!\n\n"
                f"📦 Plano: {plan['name']}\n"
                f"💰 Valor: R$ {plan['price']:.2f}\n\n"
                f"🔑 Copia e cola (PIX):\n"
                f"`{pix['qr_code']}`\n\n"
                f"Depois de pagar, clique em ✅ Já paguei."
            )
            await query.edit_message_text(text, reply_markup=after_pix_keyboard(), parse_mode="Markdown")
        except Exception as e:
            await query.edit_message_text(f"❌ Erro ao criar o PIX.\n\n{e}")
        return

    if data == "check:last":
        pending = get_last_pending(uid)
        if not pending:
            await query.edit_message_text("❌ Não encontrei um PIX pendente. Gere um novo plano.")
            return

        payment_id = pending["mp_payment_id"]
        plan_id = pending["plan_id"]
        plan = PLANS.get(plan_id)

        await query.edit_message_text("⏳ Conferindo pagamento no Mercado Pago...")

        try:
            pay = await mp_get_payment(payment_id)
            status = (pay.get("status") or "").lower()

            if status == "approved":
                # ativa assinatura
                set_subscription_active(uid, plan_id, payment_id, PLANS[plan_id]["days"])
                await query.edit_message_text(
                    "✅ Pagamento aprovado!\n\n"
                    f"🎟 Aqui está seu acesso VIP:\n{VIP_LINK}\n\n"
                    "Se o canal estiver com solicitação para entrar, peça para entrar que eu aprovo automaticamente."
                )
                return

            if status in ("pending", "in_process"):
                await query.edit_message_text(
                    f"⏳ Ainda não aprovado.\nStatus: {status}\n\n"
                    "Espere 1–3 minutos e clique em ✅ Já paguei novamente.",
                    reply_markup=after_pix_keyboard(),
                )
                return

            # rejeitado/cancelado/expired etc.
            await query.edit_message_text(
                f"❌ Pagamento não aprovado.\nStatus: {status}\n\n"
                "Gere um novo PIX e tente novamente.",
                reply_markup=plans_keyboard(),
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Erro ao consultar o pagamento.\n\n{e}", reply_markup=after_pix_keyboard())
        return

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Se seu canal estiver como "solicitar para entrar",
    o bot aprova automaticamente quem tiver assinatura ativa.
    """
    jr = update.chat_join_request
    uid = jr.from_user.id

    if has_active_subscription(uid):
        try:
            await jr.approve()
        except Exception:
            pass
    else:
        try:
            await jr.decline()
        except Exception:
            pass

# =========================
# Main
# =========================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN vazio. Configure nas Variables do Railway.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatJoinRequestHandler(on_join_request))

    # POLLING (não use webhook do Telegram aqui)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await app.updater.idle()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
