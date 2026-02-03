import os
import sqlite3
import uuid
import httpx

from fastapi import FastAPI, Request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes,
)

# ======= ENV VARS (Render) =======
BOT_TOKEN = os.environ["BOT_TOKEN"]
MP_ACCESS_TOKEN = os.environ["MP_ACCESS_TOKEN"]
CANAL_ID = int(os.environ["CANAL_ID"])     # ex: -1002038536945
VIP_LINK = os.environ["VIP_LINK"]          # ex: https://t.me/+NeBcMy0nh_Q1YzY5

PRICE = float(os.getenv("PRICE", "29.90"))
DESCRIPTION = os.getenv("DESCRIPTION", "Acesso Canal VIP")

# ======= DATABASE =======
db = sqlite3.connect("db.sqlite", check_same_thread=False)
db.execute(
    """CREATE TABLE IF NOT EXISTS orders(
        order_id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        mp_payment_id TEXT,
        status TEXT NOT NULL
    )"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS join_requests(
        user_id INTEGER PRIMARY KEY,
        pending INTEGER NOT NULL
    )"""
)
db.commit()

def db_set_order(order_id: str, user_id: int, mp_payment_id: str | None, status: str):
    db.execute(
        "INSERT OR REPLACE INTO orders(order_id,user_id,mp_payment_id,status) VALUES (?,?,?,?)",
        (order_id, user_id, mp_payment_id, status),
    )
    db.commit()

def db_update_order_status(order_id: str, status: str):
    db.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
    db.commit()

def db_get_user_by_order(order_id: str) -> int | None:
    row = db.execute("SELECT user_id FROM orders WHERE order_id=?", (order_id,)).fetchone()
    return int(row[0]) if row else None

def db_set_join_pending(user_id: int, pending: int):
    db.execute(
        "INSERT OR REPLACE INTO join_requests(user_id,pending) VALUES (?,?)",
        (user_id, pending),
    )
    db.commit()

def db_is_join_pending(user_id: int) -> bool:
    row = db.execute("SELECT pending FROM join_requests WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row[0] == 1)

# ======= FASTAPI APP (IMPORTANT: name must be `app`) =======
app = FastAPI()

# ======= TELEGRAM APP =======
tg_app = Application.builder().token(BOT_TOKEN).build()

# -------- Mercado Pago helpers --------
async def mp_create_pix(order_id: str, user_id: int, amount: float, desc: str) -> dict:
    url = "https://api.mercadopago.com/v1/payments"
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "transaction_amount": round(float(amount), 2),
        "description": desc,
        "payment_method_id": "pix",
        "payer": {"email": f"user{user_id}@bot.local"},
        "external_reference": order_id,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json=payload)

        # ✅ Mostra o erro real no Render Logs
        if r.status_code >= 400:
            print("MP_CREATE_PIX_ERROR_STATUS:", r.status_code)
            print("MP_CREATE_PIX_ERROR_BODY:", r.text)
            raise Exception(f"MercadoPago error {r.status_code}")

        data = r.json()

    tx = data.get("point_of_interaction", {}).get("transaction_data", {})
    return {
        "mp_payment_id": str(data["id"]),
        "status": data.get("status", "pending"),
        "qr_code": tx.get("qr_code"),
    }


# -------- Telegram handlers --------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(f"💳 Comprar VIP (R$ {PRICE:.2f})", callback_data="buy")]]
    await update.message.reply_text(
        "🔞 *Canal VIP*\n\nClique no botão abaixo para gerar o PIX.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user_id = q.from_user.id
    order_id = str(uuid.uuid4())
    db_set_order(order_id, user_id, None, "pending")

    try:
        pix = await mp_create_pix(order_id, user_id)
    except Exception:
        await q.edit_message_text("❌ Erro ao criar o PIX. Tente novamente.")
        return

    db_set_order(order_id, user_id, pix["mp_payment_id"], pix["status"])

    if not pix.get("qr_code"):
        await q.edit_message_text("❌ Não consegui gerar o PIX (qr_code vazio). Tente novamente.")
        return

    await q.edit_message_text(
        "🧾 *Pedido criado*\n\n"
        "✅ Copie e cole o PIX abaixo e pague:\n\n"
        f"`{pix['qr_code']}`\n\n"
        "Depois que o pagamento for aprovado, vou te mandar o botão para solicitar entrada no VIP.",
        parse_mode="Markdown",
    )

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Quando a pessoa clica no link e pede entrada
    jr = update.chat_join_request
    db_set_join_pending(jr.from_user.id, 1)

# ✅ Register handlers (ESSENTIAL)
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CallbackQueryHandler(buy, pattern="^buy$"))
tg_app.add_handler(ChatJoinRequestHandler(on_join_request))

# -------- FastAPI routes --------
@app.get("/")
async def health():
    return {"ok": True}

@app.post("/mp/webhook")
async def mp_webhook(req: Request):
    payload = await req.json()

    payment_id = None
    if isinstance(payload, dict):
        payment_id = (payload.get("data") or {}).get("id") or payload.get("id")

    if not payment_id:
        return {"ok": True}

    payment = await mp_get_payment(str(payment_id))
    status = payment.get("status")
    order_id = payment.get("external_reference")

    if not order_id:
        return {"ok": True}

    db_update_order_status(order_id, status or "unknown")

    if status == "approved":
        user_id = db_get_user_by_order(order_id)
        if not user_id:
            return {"ok": True}

        # manda botão do VIP
        await tg_app.bot.send_message(
            chat_id=user_id,
            text="✅ *Pagamento aprovado!*\n\nClique no botão abaixo para solicitar entrada no Canal VIP.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔞 Solicitar entrada no VIP", url=VIP_LINK)]]
            ),
            parse_mode="Markdown",
        )

        # tenta aprovar a solicitação se existir
        if db_is_join_pending(user_id):
            try:
                await tg_app.bot.approve_chat_join_request(chat_id=CANAL_ID, user_id=user_id)
                db_set_join_pending(user_id, 0)
                await tg_app.bot.send_message(chat_id=user_id, text="✅ Entrada aprovada. Bem-vindo!")
            except Exception:
                # se falhar, deixa pendente pra aprovar manualmente
                pass

    return {"ok": True}

# -------- Start Telegram polling with FastAPI --------
@app.on_event("startup")
async def on_startup():
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)

@app.on_event("shutdown")
async def on_shutdown():
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()
