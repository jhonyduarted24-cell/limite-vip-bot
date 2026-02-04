import os
import uuid
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
VIP_LINK = os.getenv("VIP_LINK")

VALOR = 1.00  # R$ 1,00 FIXO

# ---------- START ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💳 Comprar VIP por R$ 1,00", callback_data="pagar")]
    ]
    await update.message.reply_text(
        "👋 Bem-vindo!\n\n🎟️ Acesso VIP disponível por apenas *R$ 1,00*.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ---------- CRIAR PIX ----------
def criar_pix():
    url = "https://api.mercadopago.com/v1/payments"

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "X-Idempotency-Key": str(uuid.uuid4())
    }

    data = {
        "transaction_amount": VALOR,
        "description": "Acesso VIP Telegram",
        "payment_method_id": "pix",
        "payer": {
            "email": f"cliente_{uuid.uuid4()}@botvip.com"
        }
    }

    response = requests.post(url, json=data, headers=headers, timeout=20)

    if response.status_code != 201:
        raise Exception(response.text)

    pix_data = response.json()
    copia_e_cola = pix_data["point_of_interaction"]["transaction_data"]["qr_code"]

    return copia_e_cola

# ---------- BOTÃO PAGAR ----------
async def pagar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        codigo_pix = criar_pix()

        keyboard = [
            [InlineKeyboardButton("✅ Já paguei", callback_data="confirmar")]
        ]

        await query.message.reply_text(
            f"💰 *PIX gerado com sucesso!*\n\n"
            f"💵 Valor: *R$ 1,00*\n\n"
            f"📋 *Copia e Cola (PIX):*\n"
            f"`{codigo_pix}`\n\n"
            f"Após o pagamento, clique em *Já paguei*.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except Exception as e:
        await query.message.reply_text(f"❌ Erro ao gerar PIX:\n{e}")

# ---------- CONFIRMAR ----------
async def confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.message.reply_text(
        f"✅ Pagamento confirmado!\n\n"
        f"🔓 Aqui está seu acesso VIP:\n{VIP_LINK}"
    )

# ---------- MAIN ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(pagar, pattern="pagar"))
    app.add_handler(CallbackQueryHandler(confirmar, pattern="confirmar"))

    app.run_polling()

if __name__ == "__main__":
    main()
