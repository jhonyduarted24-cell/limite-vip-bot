import os
import json
import uuid
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional

import httpx
from fastapi import FastAPI, Request
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

# =========================
# CONFIG (Railway Variables)
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "").strip()  # precisa ser APP_USR... (produção)
VIP_LINK = os.environ.get("VIP_LINK", "").strip()                # link convite do seu canal/grupo VIP
TELEGRAM_WEBHOOK_URL = os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip()  # https://SEUAPP.up.railway.app

if not BOT_TOKEN:
    raise RuntimeError("Faltou BOT_TOKEN nas Variables do Railway.")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("Faltou MP_ACCESS_TOKEN nas Variables do Railway.")
if not VIP_LINK:
    raise RuntimeError("Faltou VIP_LINK nas Variables do Railway.")
if not TELEGRAM_WEBHOOK_URL:
    raise RuntimeError("Faltou TELEGRAM_WEBHOOK_URL nas Variables do Railway.")

# ==========
# PLANOS
# ==========
# Você pode mudar nome e valor aqui quando quiser
PLANS: Dict[str, Dict[str, object]] = {
    "vip7":  {"name": "VIP 7 dias",  "price": 9.90},
    "vip30": {"name": "VIP 30 dias", "price": 29.90},
    "vip90": {"name": "VIP 90 dias", "price": 69.90},
}

# ==========
# MEMÓRIA (simples)
# ==========
# (Se você reiniciar o servidor, zera. Depois a gente coloca banco se quiser.)
USER_EMAIL: Dict[int, str] = {}                 # telegram_user_id -> email
PENDING_PAYMENTS: Dict[str, Dict[str, object]] = {}  # payment_id(str) -> {user_id, plan_key}

# ==========
# MERCADO PAGO
# ==========
MP_API = "https://api.mercadopago.com"

@dataclass
class PixPayment:
    payment_id: str
    copy_paste: str
    qr_base64: Optional[str]
    status: str

async def mp_create_pix(amount: float, description: str, payer_email: str) -> PixPayment:
    """
    Cria pagamento PIX no Mercado Pago (produção).
    """
    url = f"{MP_API}/v1/payments"
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        # MUITO IMPORTANTE: idempotency sempre tem que existir e ser único
        "X-Idempotency-Key": str(uuid.uuid4()),
    }

    payload = {
        "transaction_amount": float(amount),
        "description": description,
        "payment_method_id": "pix",
        "payer": {
            "email": payer_email
        }
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json=payload)
        # Se der erro, devolve texto detalhado
        if r.status_code >= 400:
            raise RuntimeError(f"MP_ERROR {r.status_code}: {r.text}")

        data = r.json()

    tx = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
    copy_paste = tx.get("qr_code")
    qr_base64 = tx.get("qr_code_base64")
    if not copy_paste:
        raise RuntimeError(f"MP não retornou qr_code. Resposta: {json.dumps(data)[:1200]}")

    return PixPayment(
        payment_id=str(data.get("id")),
        copy_paste=copy_paste,
        qr_base64=qr_base64,
        status=str(data.get("status", "")),
    )

async def mp_get_payment(payment_id: str) -> dict:
    url = f"{MP_API}/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"MP_ERROR {r.status_code}: {r.text}")
        return r.json()

# ==========
# TELEGRAM BOT
# ==========
app = FastAPI()
tg_app: Application = Application.builder().token(BOT_TOKEN).build()

def build_plans_keyboard() -> InlineKeyboardMarkup:
    kb = []
    for key, p in PLANS.items():
        kb.append([
            InlineKeyboardButton(
                text=f"{p['name']} — R$ {float(p['price']):.2f}",
                callback_data=f"plan:{key}",
            )
        ])
    return InlineKeyboardMarkup(kb)

def build_pay_keyboard(payment_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Já paguei", callback_data=f"paid:{payment_id}")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="back:start")],
    ])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Se não tem email salvo, pede
    if user_id not in USER_EMAIL:
        await update.message.reply_text(
            "👋 Olá! Antes de gerar o PIX, me envie seu e-mail.\n\n"
            "Digite assim:\n"
            "/email seuemail@exemplo.com"
        )
        return

    await update.message.reply_text(
        "✅ Escolha um plano para gerar o PIX:",
        reply_markup=build_plans_keyboard(),
    )

async def cmd_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Use assim: /email seuemail@exemplo.com")
        return

    email = context.args[0].strip()
    if "@" not in email or "." not in email:
        await update.message.reply_text("Esse e-mail parece inválido. Tente novamente.")
        return

    USER_EMAIL[user_id] = email
    await update.message.reply_text("✅ Email salvo! Agora digite /start para escolher o plano.")

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user_id = q.from_user.id
    data = q.data or ""

    if data == "back:start":
        await q.edit_message_text("✅ Escolha um plano para gerar o PIX:", reply_markup=build_plans_keyboard())
        return

    # Escolheu plano
    if data.startswith("plan:"):
        if user_id not in USER_EMAIL:
            await q.edit_message_text(
                "Antes de gerar o PIX, envie seu e-mail:\n/email seuemail@exemplo.com"
            )
            return

        plan_key = data.split(":", 1)[1]
        plan = PLANS.get(plan_key)
        if not plan:
            await q.edit_message_text("Plano inválido. Digite /start novamente.")
            return

        try:
            pix = await mp_create_pix(
                amount=float(plan["price"]),
                description=str(plan["name"]),
                payer_email=USER_EMAIL[user_id],
            )
        except Exception as e:
            await q.edit_message_text(f"❌ Erro ao criar o PIX.\n\n{e}")
            return

        # guarda pendência
        PENDING_PAYMENTS[pix.payment_id] = {"user_id": user_id, "plan_key": plan_key}

        msg = (
            "✅ *PIX gerado!*\n\n"
            f"📦 *Plano:* {plan['name']}\n"
            f"💰 *Valor:* R$ {float(plan['price']):.2f}\n\n"
            "🔑 *Copia e cola (PIX):*\n"
            f"`{pix.copy_paste}`\n\n"
            "Depois de pagar, clique em *✅ Já paguei*."
        )

        await q.edit_message_text(msg, reply_markup=build_pay_keyboard(pix.payment_id), parse_mode="Markdown")
        return

    # Clicou "já paguei"
    if data.startswith("paid:"):
        payment_id = data.split(":", 1)[1]
        pending = PENDING_PAYMENTS.get(payment_id)
        if not pending:
            await q.edit_message_text("Não achei esse pagamento. Gere um novo com /start.")
            return

        try:
            info = await mp_get_payment(payment_id)
        except Exception as e:
            await q.edit_message_text(f"❌ Erro ao consultar pagamento.\n\n{e}")
            return

        status = str(info.get("status", "")).lower()
        if status == "approved":
            # aprovado -> libera link
            await q.edit_message_text(
                "✅ Pagamento aprovado!\n\n"
                f"🎉 Aqui está seu acesso VIP:\n{VIP_LINK}"
            )
            # limpa pendência
            PENDING_PAYMENTS.pop(payment_id, None)
            return

        await q.edit_message_text(
            f"⏳ Ainda não consta como aprovado.\n\nStatus atual: *{status}*\n\n"
            "Se você acabou de pagar, espere 1-2 min e clique de novo em ✅ Já paguei.",
            parse_mode="Markdown",
            reply_markup=build_pay_keyboard(payment_id),
        )

# Registra handlers
tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("email", cmd_email))
tg_app.add_handler(CallbackQueryHandler(on_callback))

# ==========
# WEBHOOK (Railway)
# ==========
@app.on_event("startup")
async def on_startup():
    # Sobe o bot e registra webhook
    await tg_app.initialize()
    await tg_app.start()

    webhook_url = TELEGRAM_WEBHOOK_URL.rstrip("/") + "/telegram"
    await tg_app.bot.set_webhook(url=webhook_url)

@app.on_event("shutdown")
async def on_shutdown():
    await tg_app.stop()
    await tg_app.shutdown()

@app.post("/telegram")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "ok"}
