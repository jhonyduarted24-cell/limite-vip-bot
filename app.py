import os
import uuid
import json
import sqlite3
import asyncio
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================
# ENV (Railway Variables)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
VIP_LINK = os.getenv("VIP_LINK", "")  # link de convite do canal/grupo
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # ex: https://seuapp.up.railway.app

# CANAL_ID é opcional: só precisa se você vai aprovar pedido de entrada automaticamente
CANAL_ID_RAW = os.getenv("CANAL_ID", "")
CANAL_ID: Optional[int] = int(CANAL_ID_RAW) if CANAL_ID_RAW.strip() else None

if not BOT_TOKEN:
    raise RuntimeError("Faltou BOT_TOKEN nas variáveis da Railway.")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("Faltou MP_ACCESS_TOKEN nas variáveis da Railway.")
if not VIP_LINK:
    raise RuntimeError("Faltou VIP_LINK nas variáveis da Railway.")
if not PUBLIC_URL:
    raise RuntimeError("Faltou PUBLIC_URL nas variáveis da Railway (URL do seu app).")

MP_NOTIFICATION_URL = f"{PUBLIC_URL}/mp/webhook"

# =========================
# Planos (edite aqui)
# =========================
# Você pode mudar nome e preço aqui.
PLANS = [
    {"id": "p1", "name": "VIP 7 dias", "price": 1.10},
    {"id": "p2", "name": "VIP 30 dias", "price": 29.90},
    {"id": "p3", "name": "VIP 90 dias", "price": 69.90},
]

def get_plan(plan_id: str):
    for p in PLANS:
        if p["id"] == plan_id:
            return p
    return None

# =========================
# DB (SQLite)
# =========================
db = sqlite3.connect("db.sqlite", check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL;")
db.execute("""
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  plan_id TEXT NOT NULL,
  payment_id TEXT,
  status TEXT NOT NULL
)
""")
db.commit()

def db_create_order(order_id: str, user_id: int, plan_id: str):
    db.execute(
        "INSERT INTO orders(order_id,user_id,plan_id,payment_id,status) VALUES (?,?,?,?,?)",
        (order_id, user_id, plan_id, None, "pending"),
    )
    db.commit()

def db_set_payment(order_id: str, payment_id: str):
    db.execute("UPDATE orders SET payment_id=? WHERE order_id=?", (payment_id, order_id))
    db.commit()

def db_set_status(order_id: str, status: str):
    db.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
    db.commit()

def db_get_order_by_payment(payment_id: str):
    cur = db.execute("SELECT order_id,user_id,plan_id,status FROM orders WHERE payment_id=?", (payment_id,))
    row = cur.fetchone()
    return row  # (order_id, user_id, plan_id, status) or None

def db_get_order(order_id: str):
    cur = db.execute("SELECT order_id,user_id,plan_id,payment_id,status FROM orders WHERE order_id=?", (order_id,))
    return cur.fetchone()

# =========================
# Mercado Pago helpers
# =========================
async def mp_create_pix(amount: float, description: str, order_id: str, payer_email: str):
    """
    Cria pagamento PIX via /v1/payments.
    IMPORTANTE: usa X-Idempotency-Key (evita erro e duplicação).
    """
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": str(uuid.uuid4()),
    }

    payload = {
        "transaction_amount": float(amount),
        "description": description,
        "payment_method_id": "pix",
        "payer": {"email": payer_email},
        "external_reference": order_id,
        "notification_url": MP_NOTIFICATION_URL,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://api.mercadopago.com/v1/payments", headers=headers, json=payload)
        data = r.json()

    if r.status_code >= 400:
        # devolve erro completo para você ver no Telegram
        raise RuntimeError(f"MP_ERROR {r.status_code}: {json.dumps(data, ensure_ascii=False)}")

    # Alguns retornos têm qr_code e qr_code_base64
    tx = data.get("point_of_interaction", {}).get("transaction_data", {}) or {}
    qr_code = tx.get("qr_code")
    qr_base64 = tx.get("qr_code_base64")
    payment_id = str(data.get("id"))

    if not qr_code or not payment_id:
        raise RuntimeError(f"Resposta MP sem QR/payment_id: {json.dumps(data, ensure_ascii=False)[:900]}")

    return payment_id, qr_code, qr_base64

async def mp_get_payment(payment_id: str):
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"https://api.mercadopago.com/v1/payments/{payment_id}", headers=headers)
        data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"MP_ERROR {r.status_code}: {json.dumps(data, ensure_ascii=False)}")
    return data

# =========================
# Telegram bot
# =========================
tg = Application.builder().token(BOT_TOKEN).build()

def plans_keyboard():
    kb = []
    for p in PLANS:
        kb.append([InlineKeyboardButton(f"{p['name']} — R$ {p['price']:.2f}", callback_data=f"plan:{p['id']}")])
    return InlineKeyboardMarkup(kb)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bem-vindo!\n\nEscolha um plano para gerar o PIX:",
        reply_markup=plans_keyboard(),
    )

async def on_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, plan_id = q.data.split(":", 1)
    plan = get_plan(plan_id)
    if not plan:
        await q.edit_message_text("Plano inválido. Use /start novamente.")
        return

    user_id = q.from_user.id
    order_id = str(uuid.uuid4())
    db_create_order(order_id, user_id, plan_id)

    try:
        payment_id, qr_code, _qr_base64 = await mp_create_pix(
            amount=plan["price"],
            description=f"Plano {plan['name']}",
            order_id=order_id,
            payer_email=f"user{user_id}@example.com",
        )
        db_set_payment(order_id, payment_id)

        # MUITO importante: mandar o PIX como TEXTO SIMPLES, 1 linha, sem Markdown
        # (evita banco recusar por causa de quebra de linha)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Já paguei", callback_data=f"check:{order_id}")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="back")],
        ])

        msg = (
            "✅ PIX gerado!\n\n"
            f"📦 Plano: {plan['name']}\n"
            f"💰 Valor: R$ {plan['price']:.2f}\n\n"
            "📌 Copia e cola (PIX):\n"
            f"{qr_code}\n\n"
            "Depois de pagar, clique em ✅ Já paguei."
        )
        await q.edit_message_text(msg, reply_markup=kb)

    except Exception as e:
        await q.edit_message_text(f"❌ Erro ao criar o PIX.\n\n{e}\n\nTente /start novamente.")

async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Escolha um plano:", reply_markup=plans_keyboard())

async def on_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, order_id = q.data.split(":", 1)
    order = db_get_order(order_id)
    if not order:
        await q.edit_message_text("Pedido não encontrado. Use /start.")
        return

    _order_id, user_id, plan_id, payment_id, status = order
    plan = get_plan(plan_id) or {"name": plan_id, "price": 0}

    if not payment_id:
        await q.edit_message_text("Esse pedido ainda não tem pagamento. Use /start.")
        return

    try:
        payment = await mp_get_payment(payment_id)
        mp_status = payment.get("status", "")
        db_set_status(order_id, mp_status)

        if mp_status == "approved":
            # Se você quiser aprovar entrada automaticamente, o bot precisa ser admin do canal/grupo
            # e o usuário precisa pedir para entrar.
            if CANAL_ID is not None:
                try:
                    await tg.bot.approve_chat_join_request(chat_id=CANAL_ID, user_id=user_id)
                except Exception:
                    pass

            await q.edit_message_text(
                "✅ Pagamento aprovado!\n\nClique para entrar:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Entrar no VIP", url=VIP_LINK)]
                ])
            )
        else:
            await q.edit_message_text(
                f"⏳ Ainda não aprovado.\n\nStatus atual: {mp_status}\n\n"
                "Se você acabou de pagar, espere 1–3 minutos e clique de novo.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Verificar novamente", callback_data=f"check:{order_id}")],
                    [InlineKeyboardButton("⬅️ Voltar", callback_data="back")]
                ])
            )

    except Exception as e:
        await q.edit_message_text(f"❌ Erro ao verificar pagamento.\n\n{e}")

# Registrar handlers (ISSO É O QUE FALTAVA NO SEU CÓDIGO)
tg.add_handler(CommandHandler("start", cmd_start))
tg.add_handler(CallbackQueryHandler(on_plan, pattern=r"^plan:"))
tg.add_handler(CallbackQueryHandler(on_check, pattern=r"^check:"))
tg.add_handler(CallbackQueryHandler(on_back, pattern=r"^back$"))

# =========================
# FastAPI (webhook MP + health)
# =========================
app = FastAPI()

@app.get("/")
async def health():
    return {"ok": True}

@app.post("/mp/webhook")
async def mp_webhook(req: Request):
    """
    Mercado Pago manda:
    { "data": { "id": "123" }, "type": "payment" }
    """
    body = await req.json()
    payment_id = str((body.get("data") or {}).get("id") or "").strip()
    if not payment_id:
        return {"ok": True}

    # Busca pagamento no MP para confirmar status
    try:
        payment = await mp_get_payment(payment_id)
        mp_status = payment.get("status", "")
        order = db_get_order_by_payment(payment_id)
        if order:
            order_id, user_id, plan_id, _old_status = order
            db_set_status(order_id, mp_status)

            if mp_status == "approved":
                # envia link pro usuário
                await tg.bot.send_message(
                    chat_id=user_id,
                    text="✅ Pagamento aprovado!\n\nClique para entrar:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔗 Entrar no VIP", url=VIP_LINK)]
                    ])
                )

                if CANAL_ID is not None:
                    try:
                        await tg.bot.approve_chat_join_request(chat_id=CANAL_ID, user_id=user_id)
                    except Exception:
                        pass

    except Exception:
        # webhook não pode falhar, senão MP fica reenviando
        return {"ok": True}

    return {"ok": True}

# =========================
# Start Telegram polling dentro do FastAPI
# =========================
@app.on_event("startup")
async def on_startup():
    # sobe o bot (polling) junto com a API
    await tg.initialize()
    await tg.start()
    tg.updater_task = asyncio.create_task(tg.updater.start_polling(drop_pending_updates=True))

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await tg.updater.stop()
    except Exception:
        pass
    await tg.stop()
    await tg.shutdown()
