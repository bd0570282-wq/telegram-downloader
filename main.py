import re
import os
import asyncio
import logging
import tempfile
import shutil
import time
from pathlib import Path
from threading import Thread
from flask import Flask
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ParseMode

# --- FLASK BACKGROUND SERVER ---
web_app = Flask('main')

@web_app.route('/')
def home():
    return "OK"

def run_web_server():
    # Replit automatically routes port 8080 to your public web URL
    web_app.run(host='0.0.0.0', port=8080)

# --- BOT CONFIGURATION ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "bot_downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# --- HELPERS ---
def is_valid_url(url: str) -> bool:
    regex = re.compile(
        r'^(?:http|ftp)s?://'
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'
        r'localhost|'
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        r'(?::\d+)?'
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return re.match(regex, url) is not None

# --- TELEGRAM COMMANDS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Welcome to the Media Downloader Bot!**\n\n"
        "Send me any valid video link from YouTube, TikTok, Instagram, or Twitter, "
        "and I will fetch the available formats for you to download.",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    
    if not is_valid_url(url):
        await update.message.reply_text("❌ Please send a valid video link.")
        return

    status_message = await update.message.reply_text("🔍 Analyzing video link... Please wait.")

    # Run yt-dlp extraction in a separate thread to prevent blocking asyncio loop
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, lambda: extract_video_info(url))
    except Exception as e:
        logger.error(f"Extraction error: {e}")
        await status_message.edit_text("❌ Failed to parse link or extraction timed out.")
        return

    if not info:
        await status_message.edit_text("❌ Could not extract video information.")
        return

    # Filter formats (separate Video+Audio vs Audio Only)
    formats = info.get('formats', [])
    video_options = []
    audio_options = []

    for f in formats:
        # We look for formats that contain combined video/audio, or clean formats
        if f.get('vcodec') != 'none' and f.get('acodec') != 'none' and f.get('ext') == 'mp4':
            res = f.get('resolution') or f.get('format_note') or f.get('format_id')
            video_options.append((f"{res} (MP4)", f"vid_{f['format_id']}"))
        elif f.get('vcodec') == 'none' and f.get('acodec') != 'none':
            ext = f.get('ext') or 'mp3'
            audio_options.append((f"Audio ({ext.upper()})", f"aud_{f['format_id']}"))

    # Select top 3 resolutions if there are too many
    video_options = video_options[:3]
    audio_options = audio_options[:1]

    keyboard = []
    for label, callback_data in video_options + audio_options:
        keyboard.append([InlineKeyboardButton(label, callback_data=callback_data)])

    if not keyboard:
        # Fallback if no specific clean formats detected
        keyboard.append([InlineKeyboardButton("Best Quality (Combined)", "best")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    context.user_data['current_video_url'] = url
    
    title = info.get('title', 'Video File')
    await status_message.delete()
    await update.message.reply_text(
        f"🎬 **Title:** {title}\n\nSelect your preferred download option below:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

def extract_video_info(url: str):
    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

# --- CALLBACK BUTTON HANDLER ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    url = context.user_data.get('current_video_url')
    if not url:
        await query.edit_message_text("❌ Error: Session expired. Please send the link again.")
        return

    format_choice = query.data
    await query.edit_message_text("📥 Downloading asset to server layers... Please hold on.")

    loop = asyncio.get_running_loop()
    try:
        file_path, is_audio = await loop.run_in_executor(None, lambda: download_media(url, format_choice))
        if not file_path or not os.path.exists(file_path):
            raise Exception("File empty or missing path structure.")
            
        await query.edit_message_text("📤 Uploading media payload to Telegram...")
        
        with open(file_path, 'rb') as media_file:
            if is_audio:
                await query.message.reply_audio(audio=media_file, filename=os.path.basename(file_path))
            else:
                await query.message.reply_video(video=media_file, filename=os.path.basename(file_path), supports_streaming=True)
                
        await query.message.delete()
    except Exception as e:
        logger.error(f"Download/Upload failure: {e}")
        await query.edit_message_text("❌ Critical failure downloading or sending the media payload.")
    finally:
        # Cleanup temporary file space
        if 'file_path' in locals() and file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

def download_media(url: str, format_choice: str):
    unique_id = str(int(time.time()))
    outtmpl = str(DOWNLOAD_DIR / f"file_{unique_id}.%(ext)s")
    
    is_audio = format_choice.startswith("aud_")
    
    if format_choice.startswith("vid_") or is_audio:
        fmt_id = format_choice.split("_")[1]
        ydl_format = fmt_id
    elif format_choice == "best":
        ydl_format = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    else:
        ydl_format = "best"

    ydl_opts = {
        'format': ydl_format,
        'outtmpl': outtmpl,
        'quiet': True,
        'no_warnings': True,
    }

    if is_audio:
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if is_audio:
            # When converting to mp3, the expected filename extension changes
            filename = os.path.splitext(filename)[0] + ".mp3"
        return filename, is_audio

# --- MAIN INITIALIZATION ---
def main():
    if not BOT_TOKEN:
        logger.error("No TELEGRAM_BOT_TOKEN environment variable found!")
        return

    # Start Flask Webserver in background thread
    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    # Build and initialize the Telegram App
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot infrastructure running successfully on Replit...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
