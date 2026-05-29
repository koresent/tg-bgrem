import asyncio
import hashlib
import logging
import os
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message, TelegramObject
from aiohttp import ClientSession, FormData
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
REMBG_API_URL = os.getenv("REMBG_API_URL", "http://rembg:7000/")
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set")

ALLOWED_USERS = {
    int(uid.strip()) for uid in ALLOWED_USERS_RAW.split(",") if uid.strip().isdigit()
}


class WhitelistMiddleware(BaseMiddleware):
    """
    Middleware to restrict bot access to explicitly allowed users only.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
            if user_id not in ALLOWED_USERS:
                logger.warning(f"Unauthorized access attempt from user_id: {user_id}")
                return
        return await handler(event, data)


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.message.middleware(WhitelistMiddleware())


async def request_background_removal(
    session: ClientSession, image_bytes: bytes
) -> bytes:
    """
    Sends image bytes to the rembg HTTP API via POST multipart/form-data
    and returns the processed image bytes.
    """
    logger.info(f"Sending POST request to rembg API: {REMBG_API_URL}")

    data = FormData()
    data.add_field("file", image_bytes, filename="input.png", content_type="image/png")

    async with session.post(REMBG_API_URL, data=data) as response:
        if response.status == 200:
            return await response.read()

        error_msg = await response.text()
        logger.error(f"Rembg API error: Status {response.status}, Body: {error_msg}")
        raise RuntimeError(f"Rembg service returned status {response.status}")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Hello! I am a background removal bot.\n"
        "Send me a photo (as an image or document), and I will return the result."
    )


@dp.message()
async def process_image_message(message: Message, http_session: ClientSession):
    if message.photo:
        file_id = message.photo[-1].file_id
        is_document = False
    elif (
        message.document
        and message.document.mime_type
        and message.document.mime_type.startswith("image/")
    ):
        file_id = message.document.file_id
        is_document = True
    else:
        await message.answer("Please send an image in photo or document format.")
        return

    processing_msg = None

    try:
        processing_msg = await message.answer("Processing image, please wait...")

        file_info = await bot.get_file(file_id)
        if not file_info.file_path:
            raise ValueError("Failed to retrieve file path from Telegram")

        image_buffer = await bot.download_file(file_info.file_path)
        if not image_buffer:
            raise ValueError("Failed to download file bytes from Telegram")

        file_data = image_buffer.getvalue()

        if is_document:
            orig_name = message.document.file_name or "image.png"
            name_part, _ = os.path.splitext(orig_name)
            out_filename = f"{name_part}_no_bg.png"
        else:
            file_hash = hashlib.md5(file_data).hexdigest()
            short_id = file_hash[:8]
            out_filename = f"{short_id}_no_bg.png"

        await bot.send_chat_action(chat_id=message.chat.id, action="upload_document")

        processed_bytes = await request_background_removal(http_session, file_data)

        result_file = BufferedInputFile(processed_bytes, filename=out_filename)
        await message.answer_document(document=result_file)

    except Exception as e:
        logger.error(f"Error while processing image: {e}", exc_info=True)
        await message.answer(
            "An error occurred while processing the image. Please try again later."
        )

    finally:
        if processing_msg:
            try:
                await processing_msg.delete()
            except Exception:
                pass


async def main():
    async with ClientSession() as session:
        dp.workflow_data.update(http_session=session)
        await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
