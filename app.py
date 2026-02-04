import os
import uuid
import time
import sqlite3
import asyncio
from typing import Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# -------------------- ENV --------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "").strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
CANAL_ID_RAW = os.environ.get("CANAL_ID", "").strip()
PRICE_RAW = os.environ.get("PRICE", "29.90").strip()

if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN não definido")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("ENV MP_ACCESS_TOKEN não definido")
if not PUBLIC_URL:
    raise RuntimeError("ENV PUBLIC_URL não definido (ex: https://seuapp.up.railway.app)")
if not CANAL_ID_RAW:
    raise RuntimeError("ENV CANAL_ID não definido (ex: -1002038536945)")

try:
    CANAL_ID = int(CANAL_ID_RAW)
except ValueError:
    raise RuntimeError("CANAL_ID precisa ser número inteiro (ex: -100...)")

try:
    PRICE = float(PRICE_RAW.replace(",", "."))
except ValueError:
    raise RuntimeError("PRICE inválido. Ex: 29.90")

# -------------------- DB --------------------
db = sqlite3.connect("db.sqlite", check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  payment_id TEXT,
  status TEXT NOT NULL,
  amount REAL NOT NULL,
  created_at INTEGER NOT NULL
)
""")
db.commit()

def db_create_order(order_id: str, user_id: int, amount: float) -> None:
    db.execute(
        "INSERT OR REPLACE INTO orders(order_id,user_id,payment_id,status,amount,created_at) VALUES (?,?,?,?,?,?)",
        (order_id, user_id, None, "pending", amount, int(time.time()))
    )
    db.commit()

def db_set_payment(order_id: str, payment_id: str) -> None:
    db.execute("UPDATE orders SET payment_id=? WHERE order_id=?", (payment_id, order_id))
    db.commit()

def db_set_status(order_id: str, status: str) -> None:
    db.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
    db.commit()

def db_get_order(order_id: str) -> Optional[Tuple[str, int, Optional[str], str, float, int]]:
    row = db.execute(
        "SELECT order_id,user_id,payment_id,status,amount,created_at FROM orders WHERE order_id=?",
        (order_id,)
    ).fetchone()
    return row

def db_find_by_payment(payment_id: str) -> Optional[Tuple[str, int, Optional[str], str, float, int]]:
    row = db.execute(
        "SELECT order_id,user_id,payment_id,status,amount,created_at FROM orders WHERE payment_id=?",
        (payment_id,)
    ).fetchone()
    return row

# -------------------- Mercado Pago --------------------
MP_PAYMENTS_URL = "https://api.mercadopago.com/v1/payments"

async def mp_create_pix(order_id: str, user_id: int, amount: float) -> dict:
    """
    Cria um pagamento PIX no Mercado Pago.
    Retorna o JSON da resposta.
    """
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        # >>> isso resolve o erro: Header X-Idempotency-Key can't be null
        "X-Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }

    payload = {
        "transaction_amount": round(amount, 2),
        "description": "Acesso VIP",
        "payment_method_id": "pix",
        "external_reference": order_id,
        "notification_url": f"{PUBLIC_URL}/mp/webhook",
        "payer": {
            # e-mail “fake” só para identificação; pode trocar por algo real se quiser
            "email": f"user{user_id}@bot.local"
        }
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(MP_PAYMENTS_URL, headers=headers, json=payload)
        data = r.json()

    if r.status_code >= 400:
        # devolve erro detalhado
        raise RuntimeError(f"MP_ERROR {r.status_code}: {data}")

    return data

async def mp_get_payment(payment_id: str) -> dict:
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{MP_PAYMENTS_URL}/{payment_id}", headers=headers)
        data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"MP_GET_ERROR {r.status_code}: {data}")
    return data

# -------------------- Telegram Bot + FastAPI --------------------
fastapi_app = FastAPI()
tg_app = Application.builder().token(BOT_TOKEN).build()

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Comprar (R$ {PRICE:.2f})", callback_data="buy")],
    ])

def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Voltar", callback_data="back")],
    ])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bem-vindo!\n\nClique para gerar o PIX e comprar o acesso.",
        reply_markup=main_menu()
    )

async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "✅ Menu:",
        reply_markup=main_menu()
    )

async def on_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user_id = q.from_user.id
    order_id = str(uuid.uuid4())
    db_create_order(order_id, user_id, PRICE)

    try:
        mp_data = await mp_create_pix(order_id=order_id, user_id=user_id, amount=PRICE)
        payment_id = str(mp_data["id"])
        db_set_payment(order_id, payment_id)

        tx = mp_data.get("point_of_interaction", {}).get("transaction_data", {})
        pix_copia_cola = tx.get("qr_code")  # copia e cola
        ticket_url = tx.get("ticket_url")   # página do QR

        if not pix_copia_cola:
            raise RuntimeError(f"MercadoPago não retornou 'qr_code'. Resposta: {mp_data}")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Já paguei", callback_data=f"check:{order_id}")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="back")],
        ])

        text = (
            "✅ **PIX gerado!**\n\n"
            f"💰 Valor: **R$ {PRICE:.2f}**\n\n"
            "📌 **Copia e cola (PIX):**\n"
            f"`{pix_copia_cola}`\n\n"
        )
        if ticket_url:
            text += f"🔗 QR/Link: {ticket_url}\n\n"
        text += "Depois de pagar, clique em **✅ Já paguei**."

        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    except Exception as e:
        await q.edit_message_text(
            f"❌ Erro ao criar o PIX.\n\n{e}",
            reply_markup=back_menu()
        )

async def grant_access(user_id: int):
    """
    Entrega o acesso: cria link de convite único e manda para o usuário.
    """
    # Link único (1 uso), expira em 1 hora
    invite = await tg_app.bot.create_chat_invite_link(
        chat_id=CANAL_ID,
        member_limit=1,
        expire_date=int(time.time()) + 3600
    )

    await tg_app.bot.send_message(
        chat_id=user_id,
        text=(
            "✅ Pagamento aprovado!\n\n"
            "Aqui está seu link de acesso (1 uso, expira em 1 hora):\n"
            f"{invite.invite_link}"
        )
    )

async def on_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, order_id = q.data.split(":", 1)
    order = db_get_order(order_id)
    if not order:
        await q.edit_message_text("Pedido não encontrado.", reply_markup=back_menu())
        return

    _, user_id, payment_id, status, amount, created_at = order

    if status == "approved":
        await q.edit_message_text("✅ Esse pedido já foi aprovado. Verifique seu privado.", reply_markup=back_menu())
        return

    if not payment_id:
        await q.edit_message_text("❌ Esse pedido ainda não tem payment_id.", reply_markup=back_menu())
        return

    try:
        p = await mp_get_payment(payment_id)
        mp_status = p.get("status")

        if mp_status == "approved":
            db_set_status(order_id, "approved")
            await q.edit_message_text("✅ Pagamento aprovado! Enviando acesso no privado...", reply_markup=back_menu())
            await grant_access(user_id)
        else:
            await q.edit_message_text(
                f"⏳ Ainda não aprovado.\nStatus: **{mp_status}**\n\nSe você pagou agora, espere 1–2 minutos e tente de novo.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Verificar de novo", callback_data=f"check:{order_id}")],
                    [InlineKeyboardButton("⬅️ Voltar", callback_data="back")],
                ])
            )

    except Exception as e:
        await q.edit_message_text(f"❌ Erro ao verificar pagamento:\n\n{e}", reply_markup=back_menu())

# -------------------- Mercado Pago Webhook --------------------
@fastapi_app.post("/mp/webhook")
async def mp_webhook(req: Request):
    """
    MercadoPago manda notificações aqui.
    A gente busca o pagamento e, se aprovado, libera o acesso.
    """
    body = await req.json()

    payment_id = None
    # Formato comum: {"data":{"id":"123"}}
    if isinstance(body, dict):
        payment_id = (body.get("data") or {}).get("id")

    if not payment_id:
        return {"ok": True}

    try:
        p = await mp_get_payment(str(payment_id))
        if p.get("status") != "approved":
            return {"ok": True}

        payment_id_str = str(p.get("id"))
        order_id = p.get("external_reference")
        if not order_id:
            # sem referência não dá pra saber quem é
            return {"ok": True}

        order = db_get_order(order_id)
        if not order:
            # tenta encontrar pelo payment_id se existir
            order2 = db_find_by_payment(payment_id_str)
            if not order2:
                return {"ok": True}
            order = order2

        _, user_id, _, status, _, _ = order
        if status == "approved":
            return {"ok": True}

        db_set_status(order_id, "approved")
        await grant_access(user_id)

    except Exception:
        # não quebra o webhook
        return {"ok": True}

    return {"ok": True}

@fastapi_app.get("/")
async def health():
    return {"ok": True}

# -------------------- Start/Stop --------------------
@fastapi_app.on_event("startup")
async def on_startup():
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CallbackQueryHandler(on_buy, pattern="^buy$"))
    tg_app.add_handler(CallbackQueryHandler(on_back, pattern="^back$"))
    tg_app.add_handler(CallbackQueryHandler(on_check, pattern="^check:"))

    await tg_app.initialize()
    await tg_app.start()

    # POLLING: precisa ter só 1 instância rodando (senão dá Conflict getUpdates)
    asyncio.create_task(tg_app.updater.start_polling(drop_pending_updates=True))

@fastapi_app.on_event("shutdown")
async def on_shutdown():
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()

# Para rodar local: uvicorn app:fastapi_app --reload
app = fastapi_app
