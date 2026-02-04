import os
import uuid
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
VIP_LINK = os.getenv("VIP_LINK")

PLANO_NOME = "VIP 7 dias"
PLANO_VALOR = 9.90  # valor REAL, não use centavos muito baixos
PLANO_DESCRICAO = "Acesso VIP por 7 dias"

MP_URL = "https://api.mercadopago.com/v1/payments"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💳 Comprar VIP – R$ 9,90", callback_data="buy")]
    ]
    await update.message.reply_text(
        "👑 *Plano VIP*\n\nAcesso por 7 dias.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        pix_data = {
            "transaction_amount": PLANO_VALOR,
            "description": PLANO_DESCRICAO,
            "payment_method_id": "pix",
            "payer": {
                "email": f"user_{query.from_user.id}@telegram.com"
            }
        }

        headers = {
            "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "X-Idempotency-Key": str(uuid.uuid4())
        }

        r = requests.post(MP_URL, json=pix_data, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()

        copia_cola = data["point_of_interaction"]["transaction_data"]["qr_code"]

        await query.message.reply_text(
            f"✅ *PIX gerado!*\n\n"
            f"📦 Plano: {PLANO_NOME}\n"
            f"💰 Valor: R$ {PLANO_VALOR}\n\n"
            f"🔑 *Copia e cola PIX:*\n`{copia_cola}`\n\n"
            f"Após pagar, você receberá acesso manualmente.",
            parse_mode="Markdown"
        )

    except Exception as e:
        await query.message.reply_text(f"❌ Erro ao gerar PIX.\n\n{e}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buy, pattern="buy"))
    app.run_polling()

if __name__ == "__main__":
    main()
