import asyncio
import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery
)
from aiogram.filters import CommandStart

# Настройки берутся из /etc/friendbot/bot.env (токен в коде не храним).
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_1_ID = int(os.environ.get("CHANNEL_1_ID", "-1004323004181"))  # первый канал (на него нужно подписаться)
CHANNEL_1_URL = os.environ.get("CHANNEL_1_URL", "https://t.me/aida_smotr")
CHANNEL_2_ID = int(os.environ.get("CHANNEL_2_ID", "-1004403787574"))  # закрытый канал с фильмом

PRICE_STARS = int(os.environ.get("PRICE_STARS", "15"))  # Цена входа в звездах

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 1. Подписаться на канал", url=CHANNEL_1_URL)],
        [InlineKeyboardButton(text=f"🔓 2. Проверить подписку и войти ({PRICE_STARS} ⭐️)", callback_data="check_sub")]
    ])

@dp.message(CommandStart())
async def start_cmd(message: Message):
    await message.answer(
        f"Привет! Чтобы получить доступ в закрытый канал со всеми видео:\n\n"
        f"1. Подпишись на наш первый канал по кнопке ниже.\n"
        f"2. Нажми кнопку проверки и оплати вход ({PRICE_STARS} ⭐️).\n\n"
        f"Сразу после оплаты бот выдаст тебе личную ссылку в закрытый канал!",
        reply_markup=get_keyboard()
    )

@dp.callback_query(F.data == "check_sub")
async def check_subscription(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_1_ID, user_id=user_id)
        if member.status in ["member", "administrator", "creator", "restricted"]:
            await callback.answer()
            await bot.send_invoice(
                chat_id=user_id,
                title="Доступ в закрытый канал",
                description="Одноразовая ссылка на вход в приватный канал со всеми видео.",
                payload=f"access_{user_id}",
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label="Вход в закрытый канал", amount=PRICE_STARS)]
            )
        else:
            await callback.answer("❌ Ты еще не подписался на первый канал! Подпишись и нажми снова.", show_alert=True)
    except Exception as e:
        await callback.answer("Ошибка проверки: убедись, что бот добавлен в админы первого канала.", show_alert=True)
        print(f"Ошибка проверки подписки: {e}")

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def success_payment_handler(message: Message):
    user_id = message.from_user.id
    try:
        # Генерируем одноразовую ссылку ровно на 1 человека (member_limit=1)
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_2_ID,
            member_limit=1,
            name=f"Paid {user_id}"
        )
        await message.answer(
            f"🎉 Оплата прошла успешно!\n\n"
            f"Вот твоя личная одноразовая ссылка в закрытый канал со всеми видео:\n"
            f"{invite.invite_link}\n\n"
            f"⚠️ Ссылка сработает только один раз для твоего аккаунта."
        )
    except Exception as e:
        await message.answer(
            "⚠️ Оплата прошла, но не удалось создать ссылку. Проверь, что бот — админ во втором канале с правом добавления участников."
        )
        print(f"Ошибка создания ссылки: {e}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
