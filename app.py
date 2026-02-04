import os
import asyncio
import logging
from typing import Dict, Any, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================
# CONFIG (ENV VARS)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
VIP_LINK = os.getenv("VIP_LINK", "https://t.me/seuCanalVIP")  # link do canal principal

# Opcional (recomendado): URL pública do seu app no Railway, ex:
# https://seuapp.up.railway.app
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")

# Se você quiser "assinatura" no final das mensagens
BRAND_NAME = os.getenv("BRAND_NAME", "CANAL VIP")

if not BOT_TOKEN:
    raise RuntimeError("Faltou BOT_TOKEN nas variáveis de ambiente.")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("Faltou MP_ACCESS_TOKEN nas variáveis de ambiente.")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vipbot")

# =========================
# PLANOS (você pode alterar valores depois)
# =========================
PLANS = {
    "p1": {
        "title": "🏆 PLANO 1 – CANAL VIP (PRINCIPAL)",
        "price": 1.00,
        "desc_lines": [
            "💎 *Plano mais vendido / destaque no bot*",
            "",
            "<b>Acesso:</b>",
            "✅ Canal VIP (principal)",
            "",
            "<b>Como funciona:</b>",
            f"Após o pagamento, você recebe o link do <b>CANAL VIP</b>. "
            f"E na <b>bio do CANAL VIP</b> tem o link dos outros canais para você entrar.",
        ],
    },
    "p2": {
        "title": "🔥 PLANO 2 – VIP PLUS (2 CANAIS)",
        "price": 3.00,
        "desc_lines": [
            "⭐ *Mais valor por um preço melhor*",
            "",
            "<b>Acesso:</b>",
            "✅ Canal VIP (principal)",
            "✅ + acesso extra (pela bio do Canal VIP)",
            "",
            "<b>Como funciona:</b>",
            f"Você paga e recebe o link do <b>CANAL VIP</b>. "
            f"Na <b>bio</b> dele estão os links dos outros canais.",
        ],
    },
    "p3": {
        "title": "👑 PLANO 3 – VIP TOTAL (ALL IN)",
        "price": 7.00,
        "desc_lines": [
            "💰 *Plano premium / máximo valor*",
            "",
            "<b>Acesso:</b>",
            "✅ Canal VIP (principal)",
            "✅ + acesso aos outros canais via bio",
            "",
            "<b>Como funciona:</b>",
            f"Após o pagamento, você recebe o link do <b>CANAL VIP</b>. "
            f"Na <b>bio do Canal VIP</b> você encontra o link de todos os outros canais.",
        ],
    },
}

# =========================
# "BANCO" EM MEMÓRIA (simples)
# Em produção ideal: Redis/DB.
# =========================
# payment_id -> {user_id, plan_key}
PAYMENTS: Dict[str, Dict[str, Any]] = {}
# user_id -> last_payment_id
LAST_PAYMENT_BY_USER: Dict[int, str] = {}


# =========================
# MERCADO PAGO (PIX)
# =========================
MP_API = "https://api.mercadopago.com"

async def mp_create_pix_payment(amount: float, user_id: int, plan_key: str) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    notification_url = f"{PUBLIC_BASE_URL}/mp/webhook" if PUBLIC_BASE_URL else None

    payload = {
        "transaction_amount": float(amount),
        "description": f"{BRAND_NAME} - {plan_key} - user {user_id}",
        "payment_method_id": "pix",
        "payer": {"email": f"user{user_id}@example.com"},
        "metadata": {"telegram_user_id": user_id, "plan_key": plan_key},
    }
    if notification_url:
        payload["notification_url"] = notification_url

    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post("https://api.mercadopago.com/v1/payments", headers=headers, json=payload)

    if r.status_code >= 400:
        # ISSO AQUI VAI TE MOSTRAR O MOTIVO REAL NO LOG DO RAILWAY
        raise RuntimeError(f"MP ERROR {r.status_code} -> {r.text}")

    data = r.json()

    tx = (data.get("point_of_interaction", {}) or {}).get("transaction_data", {}) or {}
    qr_code = tx.get("qr_code")
    if not qr_code:
        raise RuntimeError(f"MP SEM QR_CODE -> {data}")

    return {
        "payment_id": str(data.get("id")),
        "status": str(data.get("status")),
        "qr_code": qr_code,
        "qr_code_base64": tx.get("qr_code_base64"),
    }



async def mp_get_payment_status(payment_id: str) -> str:
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(f"{MP_API}/v1/payments/{payment_id}", headers=headers)
        r.raise_for_status()
        data = r.json()
    return str(data.get("status", "")).lower()


# =========================
# TELEGRAM BOT
# =========================
def plans_menu_text() -> str:
    return (
        "✅ <b>Escolha um plano abaixo</b>\n\n"
        "1️⃣ <b>Canal VIP</b> – acesso ao canal principal\n"
        "2️⃣ <b>VIP Plus</b> – canal principal + 1 extra (via bio)\n"
        "3️⃣ <b>VIP Total</b> – acesso total (via bio)\n\n"
        "💡 <i>Depois do pagamento, você recebe o link do Canal VIP. "
        "Na bio dele tem os links dos outros canais.</i>"
    )

def plans_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("🏆 Plano 1 – Canal VIP", callback_data="buy:p1")],
        [InlineKeyboardButton("🔥 Plano 2 – VIP Plus", callback_data="buy:p2")],
        [InlineKeyboardButton("👑 Plano 3 – VIP Total", callback_data="buy:p3")],
    ]
    return InlineKeyboardMarkup(kb)

def pay_keyboard(payment_id: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("✅ Já paguei (verificar)", callback_data=f"check:{payment_id}")],
        [InlineKeyboardButton("⬅️ Voltar aos planos", callback_data="back:plans")],
    ]
    return InlineKeyboardMarkup(kb)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        plans_menu_text(),
        reply_markup=plans_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "back:plans":
        await q.edit_message_text(
            plans_menu_text(),
            reply_markup=plans_keyboard(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    if data.startswith("buy:"):
        plan_key = data.split("buy:", 1)[1]
        plan = PLANS.get(plan_key)
        if not plan:
            await q.edit_message_text("Plano inválido. Use /start.")
            return

        # Monta mensagem "exatamente desse jeito" (bem parecido com o que você pediu)
        title = plan["title"]
        price = plan["price"]
        desc = "\n".join(plan["desc_lines"])

        try:
            pay = await mp_create_pix_payment(price, q.from_user.id, plan_key)
        except Exception as e:
            log.exception("Erro criando PIX")
            await q.edit_message_text(
                "❌ Não consegui gerar o PIX agora.\n"
                "Verifique se seu MP_ACCESS_TOKEN está correto e tente novamente.",
                parse_mode=ParseMode.HTML,
            )
            return

        payment_id = pay["payment_id"]
        PAYMENTS[payment_id] = {"user_id": q.from_user.id, "plan_key": plan_key}
        LAST_PAYMENT_BY_USER[q.from_user.id] = payment_id

        pix_copia_cola = pay["qr_code"]

        msg = (
            f"{title}\n"
            f"<b>Valor:</b> R$ {price:.2f}\n\n"
            f"{desc}\n\n"
            f"💳 <b>PIX (copia e cola):</b>\n"
            f"<code>{pix_copia_cola}</code>\n\n"
            f"✅ Depois que você pagar, clique em <b>“Já paguei (verificar)”</b>.\n"
            f"📩 Quando confirmar, eu te envio o link do <b>CANAL VIP</b> automaticamente."
        )

        await q.edit_message_text(
            msg,
            reply_markup=pay_keyboard(payment_id),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    if data.startswith("check:"):
        payment_id = data.split("check:", 1)[1]
        info = PAYMENTS.get(payment_id)

        if not info:
            await q.edit_message_text(
                "Não encontrei esse pagamento. Use /start e gere um PIX novo.",
                parse_mode=ParseMode.HTML,
            )
            return

        # Segurança simples: só quem gerou pode checar
        if info["user_id"] != q.from_user.id:
            await q.edit_message_text("Esse pagamento não é seu.")
            return

        try:
            status = await mp_get_payment_status(payment_id)
        except Exception:
            log.exception("Erro checando status MP")
            await q.edit_message_text(
                "❌ Não consegui verificar agora. Tente de novo em alguns segundos.",
                reply_markup=pay_keyboard(payment_id),
                parse_mode=ParseMode.HTML,
            )
            return

        if status == "approved":
            await q.edit_message_text(
                "✅ <b>Pagamento confirmado!</b>\n\n"
                f"🔗 Aqui está o link do <b>CANAL VIP</b>:\n{VIP_LINK}\n\n"
                "📌 <i>Importante:</i> na <b>bio do Canal VIP</b> você encontra o link dos outros canais.",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        elif status in ("pending", "in_process"):
            await q.edit_message_text(
                "⏳ <b>Ainda não confirmou</b>.\n\n"
                "Se você acabou de pagar, pode levar alguns instantes.\n"
                "Clique em <b>“Já paguei (verificar)”</b> novamente.",
                reply_markup=pay_keyboard(payment_id),
                parse_mode=ParseMode.HTML,
            )
        else:
            await q.edit_message_text(
                f"⚠️ Status do pagamento: <b>{status}</b>\n\n"
                "Se deu erro no pagamento, gere um novo PIX pelo /start.",
                reply_markup=plans_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        return


# =========================
# FASTAPI (WEBHOOK MP)
# Se PUBLIC_BASE_URL estiver setado e você cadastrar no MP,
# ele confirma sozinho e manda o link pro usuário.
# =========================
app = FastAPI()
tg_app: Optional[Application] = None

@app.post("/mp/webhook")
async def mp_webhook(request: Request):
    """
    Mercado Pago manda notificações aqui.
    O formato varia, então a gente pega o id e checa o status no endpoint de payments.
    """
    body = await request.json()
    log.info(f"Webhook recebido: {body}")

    # Tentativas comuns de capturar ID:
    payment_id = None
    if isinstance(body, dict):
        payment_id = (
            body.get("data", {}) or {}
        ).get("id") or body.get("id")

        # Alguns casos mandam topic/type e você precisa buscar pelo "id" da querystring,
        # mas no Railway isso depende de como configurou no MP.
    if not payment_id:
        return JSONResponse({"ok": True, "msg": "Sem payment id"}, status_code=200)

    payment_id = str(payment_id)
    info = PAYMENTS.get(payment_id)
    if not info:
        # pode ser de outro fluxo, ok
        return JSONResponse({"ok": True, "msg": "Payment desconhecido"}, status_code=200)

    try:
        status = await mp_get_payment_status(payment_id)
    except Exception:
        log.exception("Falha ao checar status no webhook")
        return JSONResponse({"ok": True}, status_code=200)

    if status == "approved" and tg_app:
        user_id = info["user_id"]
        try:
            await tg_app.bot.send_message(
                chat_id=user_id,
                text=(
                    "✅ <b>Pagamento confirmado!</b>\n\n"
                    f"🔗 Aqui está o link do <b>CANAL VIP</b>:\n{VIP_LINK}\n\n"
                    "📌 <i>Importante:</i> na <b>bio do Canal VIP</b> você encontra o link dos outros canais."
                ),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Erro enviando mensagem ao usuário pelo webhook")

    return JSONResponse({"ok": True}, status_code=200)


async def run_bot():
    global tg_app
    tg_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CallbackQueryHandler(on_callback))

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()  # polling padrão (funciona em qualquer host)
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
