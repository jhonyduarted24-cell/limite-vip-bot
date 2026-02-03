import os
import sqlite3
import uuid
import asyncio
from typing import Optional

import httpx
from fastapi import FastAPI, Request

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes,
)

# ========= CONFIG (Render Environment Variables) =========
BOT_TOKEN = os.environ["BOT_TOKEN"]
MP_ACCESS_TOKEN = os.environ["MP_ACCESS_TOKEN"]
CANAL_ID = int(os.environ["CANAL_ID"])  # ex: -1002038536945
VIP_LINK = os.environ["VIP_LINK"]       # ex: https://t.me/+NeBcMy0nh_Q1YzY5

# (opcional) preço do VIP em reais, ex: 29.90
PRICE = float(os.getenv("PRICE", "29.90"))
DESCRIPTION = os.getenv("DESCRIPTION", "Acesso Canal VIP")

# ========= DB (SQLite simples) =========
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

# ========= FastAPI =========
api = FastAPI()

# ========= Telegram Application =========
tg_app = Application.builder().token(BOT_TOKEN).build()


# ----------------- Mercado Pago helpers -----------------
async def mp_create_pix(order_id: str, user_id: int) -> dict:
    """
    Cria um pagamento PIX no Mercado Pago e retorna qr_code (copia e cola) + payment_id.
    """
    url = "https://api.mercadopago.com/v1/payments"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}

    payload = {
        "transaction_amount": PRICE,
        "description": DESCRIPTION,
        "payment_method_id": "pix",
        # MP pede email — aqui usamos um placeholder baseado no user_id
        "payer": {"email": f"user{user_id}@bot.local"},
        "external_reference": order_id,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    tx = data.get("point_of_interaction", {}).get("transaction_data", {})
    return {
        "mp_payment_id": str(data["id"]),
        "status": data.get("status"),
        "qr_code": tx.get("qr_code"),  # copia e cola
        "ticket_url": tx.get("ticket_url"),
    }


async def mp_get_payment(payment_id: str) -> dict:
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        return r.json()


def db_set_order(order_id: str, user_id: int, mp_payment_id: Optional[str], status: str):
    db.execute(
        "INSERT OR REPLACE INTO orders(order_id,user_id,mp_payment_id,status) VALUES (?,?,?,?)",
        (order_id, user_id, mp_payment_id, status),
    )
    db.commit()


def db_update_order_status(order_id: str, status: str):
    db.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
    db.commit()


def db_get_user_by_order(order_id: str) -> Optional[int]:
    row = db.execute("SELECT user_id FROM orders WHERE order_id=?", (order_id,)).fetchone()
    return int(row[0]) if row else None


def db_set_join_pending(user_id: int, pending: int):
    db.execute("INSERT OR REPLACE INTO join_requests(user_id,pending) VALUES (?,?)", (user_id, pending))
    db.commit()


def db_is_join_pending(user_id: int) -> bool:
    row = db.execute("SELECT pending FROM join_requests WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row[0] == 1)


# ----------------- Telegram handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(f"💳 Comprar VIP (R$ {PRICE:.2f})", callback_data="buy")]]
    await update.message.reply_text(
        "🔞 *Canal VIP*\n\nClique abaixo para gerar o PIX.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )

