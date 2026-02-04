import os
import time
import json
import uuid
import sqlite3
from typing import Optional, Dict, Any

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes,
)

# =========================
# ENV (Railway Variables)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
VIP_LINK = os.getenv("VIP_LINK", "").strip()

# Opcional: se você for usar aprovação automática de solicitação de entrada
CANAL_ID_RAW = os.getenv("CANAL_ID", "").strip()
CANAL_ID = int(CANAL_ID_RAW) if CANAL_ID_RAW else None

if not BOT_TOKEN:
    raise RuntimeError("Faltou BOT_TOKEN nas Variables do Railway.")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("Faltou MP_ACCESS_TOKEN nas Variables do Railway.")
if not VIP_LINK:
    raise RuntimeError("Faltou VIP_LINK nas Variables do Railway.")

MP_API_BASE = "https://api.mercadopago.com"
DB_PATH = "db.sqlite"

# =========================
# 3 PLANOS (edite aqui)
# =========================
PLANS = {
    "p1": {"name": "VIP 7 dias", "days": 7, "price": 9.90},
    "p2": {"name": "VIP 30 dias", "days": 30, "price": 24.90},
    "p3": {"name": "VIP 90 dias", "days": 90, "price": 59.90},
}

# =========================
# DB (SQLite)
# =========================
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS payments ("
        "user_id INTEGER,"
        "plan_id TEXT,"
        "mp_payment_id INTEGER,"
        "status TEXT,"
        "created_at INTEGER,"
        "expires_at INTEGER,"
        "PRIMARY KEY(user_id, mp_payment_id)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS last_pending ("
        "user_id INTEGER PRIMARY KEY,"
        "mp_payment_id INTEGER,"
        "plan_id TEXT"
        ")"
    )
    conn.commit()
    return conn

CONN = init_db()

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

def set_subscription_active(user_id: int, plan_id: str, mp_payment_id: int, days: int):
    expires_at = int(time.time()) + days * 86400
    CONN.execute(
        "INSERT OR REPLACE INTO payments(user_id, plan_id, mp_payment_id, status, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, plan_id, mp_payment_id, "approved", int(time.time()), expires_at),
    )
    CONN.commit()

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
# Mercado Pago
# =========================
async def mp_create_pix(plan_id: str, user_id: int) -> Dict[str, Any]:
    plan = PLANS[plan_id]
    amount = float(plan["price"])

    payload = {
        "transaction_amount": round(amount, 2),
        "description": plan["name"],
        "payment_method_id": "pix",
        "payer": {"email": f"user{user_id}@example.com"},
    }

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        # ✅ nunca pode ser nulo
        "X-Idempotency-Key": str(uuid.uuid4()),
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{MP_API_BASE}/v1/payments", headers=headers, json=payload)
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"MP_ERROR {r.status_code}: {json.dumps(data, ensure_ascii=False)}")

    mp_payment_id = int(data["id"])
    tx = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
    qr_code = tx.get("qr_code")

    if not qr_code:
        raise RuntimeError(f"MP_ERROR: PIX não retornou qr_code. Resposta: {json.dumps(data, ensure_ascii=False)}")

    return {"payment_id": mp_payment_id, "qr_code": qr_code}

async def mp_get_payment(payment_id: int) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{MP_API_BASE}/v1/payments/{payment_id}", headers=headers)
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"MP_ERROR {r.status_code}: {json.dumps(data, ensure_ascii=False)}")
    return data

# =========================
# Teclados
# =========================
def plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🟩 {PLANS['p1']['name']} — R$ {PLANS['p1']['price']:.2f}", callback_data="buy:p1")],
        [InlineKeyboardButton(f"🟨 {PLANS['p2']['name']} — R$ {PLANS['p2']['price']:.2f}", callback_data="buy:p2")],
        [InlineKeyboardButton(f"🟥 {PLANS['p3']['name']} — R$ {PLANS['p3']['price']:.2f}", callback_data="buy:p3")],
    ])

def after_pix_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Já paguei", callback_data="check:last")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="back:plans")],
    ])

# =========================
# Handlers
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Olá! Escolha um plano VIP:",
        reply_markup=plans_keyboard()
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    exp = subscription_expires_at(uid)
    if not exp:
        await update.message.reply_text("❌ Você não tem assinatura ativa.")
        return
    days_left = max(0, int((exp - int(time.time())) / 86400))
    await update.message.reply_text(f"✅ Assinatura ativa. Expira em ~{days_left} dia(s).")

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data or ""

    if data == "back:plans":
        await q.edit_message_text("Escolha um plano VIP:", reply_markup=plans_keyboard())
        return

    if data.startswith("buy:"):
        plan_id = data.split(":", 1)[1]
        if plan_id not in PLANS:
            await q.edit_message_text("Plano inválido.")
            return

        plan = PLANS[plan_id]
        await q.edit_message_text("⏳ Gerando PIX, aguarde...")

        try:
            pix = await mp_create_pix(plan_id=plan_id, user_id=uid)
            set_last_pending(uid, pix["payment_id"], plan_id)

            text = (
                f"✅ PIX gerado!\n\n"
                f"📦 Plano: {plan['name']}\n"
                f"💰 Valor: R$ {plan['price']:.2f}\n\n"
                f"🔑 Copia e cola (PIX):\n"
                f"`{pix['qr_code']}`\n\n"
                f"Depois de pagar, clique em ✅ Já paguei."
            )
            await q.edit_message_text(text, reply_markup=after_pix_keyboard(), parse_mode="Markdown")

        except Exception as e:
            await q.edit_message_text(f"❌ Erro ao criar o PIX.\n\n{e}")
        return

    if data == "check:last":
        pending = get_last_pending(uid)
        if not pending:
            await q.edit_message_text("❌ Não encontrei PIX pendente. Gere um novo plano.", reply_markup=plans_keyboard())
            return

        payment_id = pending["mp_payment_id"]
        plan_id = pending["plan_id"]
        plan = PLANS.get(plan_id)

        await q.edit_message_text("⏳ Conferindo pagamento...")

        try:
            pay = await mp_get_payment(payment_id)
            status = (pay.get("status") or "").lower()

            if status == "approved":
                set_subscription_active(uid, plan_id, payment_id, PLANS[plan_id]["days"])
                await q.edit_message_text(
                    "✅ Pagamento aprovado!\n\n"
                    f"🎟 Aqui está seu acesso VIP:\n{VIP_LINK}\n\n"
                    "Se o canal estiver com solicitação para entrar, peça para entrar que eu aprovo automaticamente (se configurado)."
                )
                return

            if status in ("pending", "in_process"):
                await q.edit_message_text(
                    f"⏳ Ainda não aprovado.\nStatus: {status}\n\n"
                    "Espere 1–3 minutos e clique em ✅ Já paguei novamente.",
                    reply_markup=after_pix_keyboard(),
                )
                return

            await q.edit_message_text(
                f"❌ Pagamento não aprovado.\nStatus: {status}\n\nGere um novo PIX e tente novamente.",
                reply_markup=plans_keyboard(),
            )

        except Exception as e:
            await q.edit_message_text(f"❌ Erro ao consultar pagamento.\n\n{e}", reply_markup=after_pix_keyboard())
        return

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Só funciona se CANAL_ID estiver setado e o canal estiver com "Solicitar para entrar"
    if CANAL_ID is None:
        return

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
# MAIN (corrigido)
# =========================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatJoinRequestHandler(on_join_request))

    # ✅ Correção definitiva: sem updater.idle()
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
