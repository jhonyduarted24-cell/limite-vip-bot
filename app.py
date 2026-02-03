import os, sqlite3, uuid
import httpx
from fastapi import FastAPI, Request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ChatJoinRequestHandler

BOT_TOKEN = os.environ["BOT_TOKEN"]
CANAL_ID = int(os.environ["CANAL_ID"])
MP_ACCESS_TOKEN = os.environ["MP_ACCESS_TOKEN"]
VIP_LINK = os.environ["VIP_LINK"]

db = sqlite3.connect("db.sqlite", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT,
    user_id INTEGER,
    payment_id TEXT,
    status TEXT
)""")
db.execute("""CREATE TABLE IF NOT EXISTS join_requests (
    user_id INTEGER,
    pending INTEGER
)""")
db.commit()

app = FastAPI()
bot = Application.builder().token(BOT_TOKEN).build()

async def start(update, context):
    kb = [[InlineKeyboardButton("💳 Comprar acesso VIP", callback_data="buy")]]
    await update.message.reply_text(
        "🔞 Acesso ao Canal VIP\n\nPagamento via PIX.",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def buy(update, context):
    q = update.callback_query
    await q.answer()

    user_id = q.from_user.id
    order_id = str(uuid.uuid4())

    db.execute("INSERT INTO orders VALUES (?,?,?,?)",
               (order_id, user_id, None, "pending"))
    db.commit()

    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    payload = {
        "transaction_amount": 29.90,
        "description": "Acesso Canal VIP",
        "payment_method_id": "pix",
        "payer": {"email": f"user{user_id}@bot.com"},
        "external_reference": order_id
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.mercadopago.com/v1/payments",
            headers=headers,
            json=payload
        )
        data = r.json()

    pix = data["point_of_interaction"]["transaction_data"]["qr_code"]
    payment_id = data["id"]

    db.execute(
        "UPDATE orders SET payment_id=? WHERE order_id=?",
        (payment_id, order_id)
    )
    db.commit()

    await q.edit_message_text(
        f"🧾 Pedido criado\n\n"
        f"📌 Copie o PIX abaixo e pague:\n\n"
        f"`{pix}`\n\n"
        f"Após pagar, aguarde a aprovação automática.",
        parse_mode="Markdown"
    )

async def on_join(update, context):
    user_id = update.chat_join_request.from_user.id
    db.execute("INSERT OR REPLACE INTO join_requests VALUES (?,1)", (user_id,))
    db.commit()

@bot.post_init
async def init(app_):
    app_.add_handler(CommandHandler("start", start))
    app_.add_handler(CallbackQueryHandler(buy))
    app_.add_handler(ChatJoinRequestHandler(on_join))

@app.post("/mp/webhook")
async def mp_webhook(req: Request):
    data = await req.json()
    payment_id = data.get("data", {}).get("id")

    if not payment_id:
        return {"ok": True}

    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://api.mercadopago.com/v1/payments/{payment_id}",
            headers=headers
        )
        payment = r.json()

    if payment["status"] == "approved":
        order_id = payment["external_reference"]
        user = db.execute(
            "SELECT user_id FROM orders WHERE order_id=?",
            (order_id,)
        ).fetchone()

        if user:
            user_id = user[0]
            await bot.bot.send_message(
                user_id,
                "✅ Pagamento aprovado!\nClique abaixo para entrar no VIP.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔞 Entrar no VIP", url=VIP_LINK)]
                ])
            )
            await bot.bot.approve_chat_join_request(CANAL_ID, user_id)

    return {"ok": True}
