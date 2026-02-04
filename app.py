import os
import uuid
import sqlite3
from typing import Optional, Dict, Any, List

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ==========================
# CONFIG (edite aqui)
# ==========================
# Planos (você pode mudar nomes/valores aqui quando quiser)
PLANS: List[Dict[str, Any]] = [
    {"id": "vip_7", "name": "🥉 VIP 7 dias", "price": 00.90},
    {"id": "vip_30", "name": "🥇 VIP 30 dias", "price": 49.90},
    {"id": "vip_90", "name": "💎 VIP 90 dias", "price": 99.90},
]

CURRENCY = "BRL"

# Mensagens
WELCOME_TEXT = (
    "🔞 *Limite 18 VIP*\n\n"
    "Escolha um plano abaixo para gerar o PIX.\n"
    "Depois de pagar, clique em *✅ Já paguei* para liberar o acesso."
)

# ==========================
# ENV VARS (obrigatórias)
# ==========================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "").strip()
VIP_LINK = os.environ.get("VIP_LINK", "").strip()

# Opcional (se você quiser usar depois)
CANAL_ID = os.environ.get("CANAL_ID", "").strip()  # pode deixar vazio por enquanto

# Railway/Render usa PORT
PORT = int(os.environ.get("PORT", "8000"))

if not BOT_TOKEN:
    raise RuntimeError("Faltou BOT_TOKEN nas variáveis do ambiente.")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("Faltou MP_ACCESS_TOKEN nas variáveis do ambiente.")
if not VIP_LINK:
    raise RuntimeError("Faltou VIP_LINK nas variáveis do ambiente.")


# ==========================
# DB simples (sqlite)
# ==========================
DB_PATH = "db.sqlite3"


def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            plan_id TEXT NOT NULL,
            mp_payment_id TEXT,
            status TEXT NOT NULL
        )
        """
    )
    con.commit()
    con.close()


def db_set_order(order_id: str, user_id: int, plan_id: str, mp_payment_id: Optional[str], status: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO orders(order_id, user_id, plan_id, mp_payment_id, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (order_id, user_id, plan_id, mp_payment_id, status),
    )
    con.commit()
    con.close()


def db_get_order(order_id: str) -> Optional[Dict[str, Any]]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    row = cur.execute(
        "SELECT order_id, user_id, plan_id, mp_payment_id, status FROM orders WHERE order_id=?",
        (order_id,),
    ).fetchone()
    con.close()
    if not row:
        return None
    return {
        "order_id": row[0],
        "user_id": row[1],
        "plan_id": row[2],
        "mp_payment_id": row[3],
        "status": row[4],
    }


def db_update_status(order_id: str, status: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
    con.commit()
    con.close()


# ==========================
# Mercado Pago helpers
# ==========================
def get_plan(plan_id: str) -> Optional[Dict[str, Any]]:
    for p in PLANS:
        if p["id"] == plan_id:
            return p
    return None


import uuid
import httpx

MP_ACCESS_TOKEN = os.environ["MP_ACCESS_TOKEN"]

async def mp_create_pix(plan_name: str, price: float, order_id: str) -> dict:
    url = "https://api.mercadopago.com/instore/orders/qr/seller/collectors/272720107/pos/TelegramPOS/qrs"

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        # nunca pode ser vazio/nulo:
        "X-Idempotency-Key": str(uuid.uuid4()),
    }

    payload = {
        "external_reference": order_id,
        "title": plan_name,
        "description": f"{plan_name} - Telegram",
        "total_amount": float(price),
        "items": [
            {
                "title": plan_name,
                "quantity": 1,
                "unit_price": float(price),
            }
        ],
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.put(url, headers=headers, json=payload)

    if r.status_code not in (200, 201):
        raise RuntimeError(f"MP_ERROR {r.status_code}: {r.text}")

    return r.json()




async def mp_get_payment(payment_id: str) -> Dict[str, Any]:
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)

    if r.status_code != 200:
        raise RuntimeError(f"MP_GET_ERROR {r.status_code}: {r.text}")

    return r.json()


def mp_extract_pix_info(mp_json: Dict[str, Any]) -> Dict[str, str]:
    # Aqui o MP devolve o "copia e cola" EMV (QR válido)
    qr_data = mp_json.get("qr_data")
    if not qr_data:
        raise RuntimeError(f"MP_RETURN_NO_QR_DATA: {mp_json}")
    return {"qr_code": qr_data, "qr_code_base64": ""}



# ==========================
# Telegram bot
# ==========================
application = Application.builder().token(BOT_TOKEN).build()


def build_start_keyboard() -> InlineKeyboardMarkup:
    kb = []
    for p in PLANS:
        kb.append([InlineKeyboardButton(f"{p['name']} — R$ {p['price']:.2f}", callback_data=f"buy:{p['id']}")])
    return InlineKeyboardMarkup(kb)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        WELCOME_TEXT,
        reply_markup=build_start_keyboard(),
        parse_mode="Markdown",
    )


async def cb_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # callback_data = buy:vip_7
    try:
        _, plan_id = (q.data or "").split(":", 1)
    except Exception:
        await q.edit_message_text("❌ Erro interno (callback inválido).")
        return

    plan = get_plan(plan_id)
    if not plan:
        await q.edit_message_text("❌ Plano não encontrado.")
        return

    user_id = q.from_user.id
    order_id = str(uuid.uuid4())

    await q.edit_message_text("⏳ Gerando PIX...")

    try:
        mp_json = await mp_create_pix(order_id=order_id, user_id=user_id, plan=plan)
        payment_id = str(mp_json.get("id"))
        pix = mp_extract_pix_info(mp_json)

        db_set_order(order_id, user_id, plan_id, payment_id, status="pending")

        msg = (
            f"✅ *PIX gerado!*\n\n"
            f"📦 Plano: *{plan['name']}*\n"
            f"💰 Valor: *R$ {plan['price']:.2f}*\n\n"
            f"🔑 *Copia e cola (PIX):*\n"
            f"`{pix['qr_code']}`\n\n"
            f"Depois de pagar, clique em *✅ Já paguei*."
        )

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Já paguei", callback_data=f"check:{order_id}")],
                [InlineKeyboardButton("⬅️ Voltar", callback_data="back:start")],
            ]
        )

        await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)

    except Exception as e:
        # Mostra o erro real (pra você ajustar rápido)
        await q.edit_message_text(f"❌ Erro ao criar o PIX.\n\n{str(e)}")


async def cb_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # callback_data = check:<order_id>
    try:
        _, order_id = (q.data or "").split(":", 1)
    except Exception:
        await q.edit_message_text("❌ Erro interno (check inválido).")
        return

    order = db_get_order(order_id)
    if not order:
        await q.edit_message_text("❌ Pedido não encontrado. Gere um PIX novamente com /start.")
        return

    payment_id = order.get("mp_payment_id")
    if not payment_id:
        await q.edit_message_text("❌ Este pedido não tem pagamento associado. Gere novamente com /start.")
        return

    await q.edit_message_text("🔎 Verificando pagamento...")

    try:
        mp_json = await mp_get_payment(payment_id)
        status = (mp_json.get("status") or "").lower()

        if status == "approved":
            db_update_status(order_id, "approved")
            # Entrega o link VIP
            msg = (
                "✅ *Pagamento aprovado!*\n\n"
                "Aqui está seu acesso VIP:\n"
                f"{VIP_LINK}\n\n"
                "⚠️ Se o Telegram pedir, solicite entrada e aguarde aprovação (se seu canal exigir)."
            )
            await q.edit_message_text(msg, parse_mode="Markdown")
            return

        if status in ("pending", "in_process"):
            await q.edit_message_text(
                "⏳ Ainda não aprovado.\n\n"
                "Se você já pagou, aguarde 1-3 minutos e clique novamente em *✅ Já paguei*.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✅ Já paguei", callback_data=f"check:{order_id}")]]
                ),
            )
            return

        # Rejected / cancelled / etc
        await q.edit_message_text(f"❌ Pagamento não aprovado.\nStatus: {status}\n\nGere um novo PIX com /start.")

    except Exception as e:
        await q.edit_message_text(f"❌ Erro ao verificar pagamento.\n\n{str(e)}")


async def cb_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(WELCOME_TEXT, parse_mode="Markdown", reply_markup=build_start_keyboard())


# Handlers
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CallbackQueryHandler(cb_buy, pattern=r"^buy:"))
application.add_handler(CallbackQueryHandler(cb_check, pattern=r"^check:"))
application.add_handler(CallbackQueryHandler(cb_back, pattern=r"^back:start$"))


# ==========================
# FastAPI (para Railway Web Service)
# ==========================
app = FastAPI()


@app.get("/")
async def root():
    return {"ok": True, "service": "limite-vip-bot", "status": "running"}


@app.get("/health")
async def health():
    return {"ok": True}


# (Opcional) Se você quiser usar webhooks do Mercado Pago depois, dá pra criar endpoint aqui.
# Por enquanto, não é necessário porque a verificação é pelo botão "Já paguei".


@app.on_event("startup")
async def on_startup():
    db_init()

    # ⚠️ Evita erro de "getUpdates conflict" se tiver 2 instâncias:
    # garanta 1 replica no Railway.
    await application.initialize()
    await application.start()

    # polling do bot
    if application.updater is None:
        raise RuntimeError("Updater não disponível. Verifique a versão do python-telegram-bot.")

    await application.updater.start_polling(drop_pending_updates=True)


@app.on_event("shutdown")
async def on_shutdown():
    if application.updater:
        await application.updater.stop()
    await application.stop()
    await application.shutdown()
