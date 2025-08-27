import os
import subprocess
import tempfile
import shutil
import json
import telegram

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
import yt_dlp
import ffmpeg
from deep_translator import GoogleTranslator


# ======= НАСТРОЙКИ =======
TOKEN = "8390458001:AAFcG4B-CV8hJ9TUF1xI5cr9SL_FMsDv1Dc"  # ← Замени на свой!
MAX_DURATION = 600  # Макс. длительность видео: 10 минут (в секундах)
MAX_FILESIZE = 100 * 1024 * 1024  # 100 МБ
# =======================


def format_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_srt(segments, srt_path):
    with open(srt_path, "w", encoding="utf-8-sig") as f:  # utf-8-sig = без BOM
        for i, seg in enumerate(segments, 1):
            start = format_time(seg["start"])
            end = format_time(seg["end"])
            text = seg["text"].strip()
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")


def translate_segments(segments):
    translated = []
    for seg in segments:
        try:
            tr = GoogleTranslator(source='en', target='ru').translate(seg["text"])
        except Exception:
            tr = seg["text"]  # fallback
        translated.append({
            "start": seg["start"],
            "end": seg["end"],
            "text": tr
        })
    return translated


def run_whisper(audio_path):
    print("🔊 Запускаем Whisper...")
    print(f"📁 Аудиофайл: {audio_path}")
    
    json_path = audio_path.replace(".wav", ".json")
    print(f"📄 Ожидаемый JSON: {json_path}")

    # Проверим, существует ли аудио
    if not os.path.exists(audio_path):
        raise Exception("❌ Аудиофайл не найден для Whisper")

    # Проверка прав на запись
    print("📁 Проверка прав: можно ли записать в папку?")
    test_file = os.path.join(os.path.dirname(audio_path), "test_write.txt")
    try:
        with open(test_file, 'w') as f:
            f.write("test")
        print("✅ Запись возможна")
        os.remove(test_file)
    except Exception as e:
        print(f"❌ Нет прав на запись: {e}")
        raise

    # Запускаем Whisper
    result = subprocess.run([
        "whisper", audio_path,
        "--model", "tiny",
        "--language", "en",
        "--task", "translate",
        "--output_format", "json",
        "--output_dir", os.path.dirname(audio_path)
    ], capture_output=True, text=True)

    print("✅ Whisper завершил работу.")
    print("STDOUT:", result.stdout)
    print("STDERR:", result.stderr)

    if result.returncode != 0:
        raise Exception(f"❌ Whisper завершился с ошибкой: {result.stderr}")

    if not os.path.exists(json_path):
        raise Exception(f"❌ Whisper не создал файл: {json_path}")

    print("📄 JSON найден, читаем...")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return data["segments"]


def download_video(url, temp_dir):
    ydl_opts = {
        "format": "best",
        "outtmpl": os.path.join(temp_dir, "video.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "Referer": "https://rutube.ru/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        },
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return filename, info.get("duration", 0)
        except Exception as e:
            raise Exception(f"Не удалось скачать видео: {str(e)}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [{"text": "🎥 Отправить видео"}, {"text": "🌐 Вставить ссылку"}],
        [{"text": "🛑 Отмена"}]
    ]
    reply_markup = {"keyboard": keyboard, "resize_keyboard": True, "one_time_keyboard": False}

    await update.message.reply_text(
        "Привет! 👋 Я — бот для добавления субтитров.\n"
        "Отправь мне:\n"
        "1. Видео (до 20 МБ) или\n"
        "2. Ссылку на YouTube / Rutube\n\n"
        "Я распознаю речь, переведу на русский и верну видео со встроенными субтитрами! 🎞️🇷🇺",
        reply_markup=reply_markup
    )


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🎥 Отправить видео":
        await update.message.reply_text("Отлично! Пришли видео в формате MP4 или MKV.")
    elif text == "🌐 Вставить ссылку":
        await update.message.reply_text("Хорошо! Вставь ссылку на YouTube или Rutube.")
    elif text == "🛑 Отмена":
        context.user_data['cancel_requested'] = True
        await update.message.reply_text("🛑 Процесс отменён.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # --- Ссылка ---
    if update.message.text and ("http" in update.message.text or "youtu" in update.message.text or "rutube" in update.message.text):
        url = update.message.text.strip()
        await update.message.reply_text("🔗 Скачиваю видео... Подожди немного.")
        
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                video_path, duration = download_video(url, temp_dir)
            except Exception as e:
                await update.message.reply_text(f"❌ Ошибка загрузки: {str(e)}")
                return

            if duration > MAX_DURATION:
                await update.message.reply_text(f"⏳ Видео слишком длинное (>{MAX_DURATION} сек).")
                return

            await _process_video(update, context, video_path, temp_dir)

    # --- Видео или документ ---
    elif update.message.video or update.message.document:
        await update.message.reply_text("📥 Получаю файл...")

        file = update.message.document or update.message.video
        if file.file_size > MAX_FILESIZE:
            await update.message.reply_text(
                f"❌ Файл слишком большой (>{MAX_FILESIZE / 1024 / 1024:.0f} МБ).\n"
                "📤 Отправь видео как **файл** (не как видео),\n"
                "или используй **ссылку с YouTube / Rutube** — так можно обработать видео любого размера."
            )
            return

        file_name = file.file_name or "video.mp4"
        if not (file_name.lower().endswith(".mp4") or file_name.lower().endswith(".mkv")):
            await update.message.reply_text("❌ Поддерживается только .mp4 или .mkv")
            return

        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = os.path.join(temp_dir, file_name)
            try:
                actual_file = await file.get_file()
                await actual_file.download_to_drive(file_path)
            except telegram.error.BadRequest as e:
                if "File is too big" in str(e):
                    await update.message.reply_text(
                        f"❌ Не удалось загрузить видео: файл слишком большой.\n"
                        "📤 Отправляй видео как **документ** (не как видео),\n"
                        "или используй **ссылку с YouTube / Rutube** — так можно обработать видео любого размера."
                    )
                    return
                else:
                    await update.message.reply_text(f"❌ Ошибка загрузки: {str(e)}")
                    return
            except Exception as e:
                await update.message.reply_text(f"❌ Ошибка при получении файла: {str(e)}")
                return

    await _process_video(update, context, file_path, temp_dir)

async def _process_video(update: Update, context: ContextTypes.DEFAULT_TYPE, video_path: str, temp_dir: str):
    context.user_data['cancel_requested'] = False

    try:
        # 1. Извлечение аудио
        await update.message.reply_text("🎧 Извлечение аудио...")
        audio_path = os.path.join(temp_dir, "audio.wav")
        result = subprocess.run([
            "ffmpeg", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", audio_path
        ], capture_output=True)

        if result.returncode != 0:
            if context.user_data.get('cancel_requested'):
                await update.message.reply_text("🛑 Процесс отменён.")
            else:
                await update.message.reply_text("❌ Ошибка извлечения аудио.")
            return

        if not os.path.exists(audio_path):
            await update.message.reply_text("❌ Не удалось извлечь аудио.")
            return

        # 2. Распознавание через Whisper
        await update.message.reply_text("🧠 Распознаём речь и переводим...")
        segments = run_whisper(audio_path)
        
        if context.user_data.get('cancel_requested'):
            await update.message.reply_text("🛑 Процесс отменён.")
            return

        translated_segments = translate_segments(segments)

        # 3. Создание SRT
        srt_path = os.path.join(temp_dir, "subs.srt")
        generate_srt(translated_segments, srt_path)

        if not os.path.exists(srt_path):
            await update.message.reply_text("❌ Не удалось создать субтитры.")
            return

        if os.path.getsize(srt_path) == 0:
            await update.message.reply_text("❌ Субтитры пусты.")
            return

        # 4. Наложение субтитров: вызываем ffmpeg из папки C:\temp
        await update.message.reply_text("🎨 Накладываем субтитры на видео...")
        output_path = os.path.join(temp_dir, "final.mp4")

        # Копируем файлы в C:\temp
        short_dir = "C:\\temp"
        os.makedirs(short_dir, exist_ok=True)

        video_short = os.path.join(short_dir, "input.mp4")
        srt_short = os.path.join(short_dir, "subs.srt")
        output_short = os.path.join(short_dir, "output.mp4")

        shutil.copy2(video_path, video_short)
        shutil.copy2(srt_path, srt_short)

        print(f"📁 Подготовлено в C:\\temp: {os.listdir(short_dir)}")

        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-i", "subs.srt",
                "-i", "input.mp4",
                "-c:a", "copy",
                "-c:v", "libx264",
                "-crf", "23",
                "-preset", "fast",
                "-vf", "subtitles=subs.srt:charenc=utf-8",
                "output.mp4"
            ]

            result = subprocess.run(
                cmd,
                cwd=short_dir,
                capture_output=True,
                text=True,
                check=False
            )

            if result.returncode != 0:
                print("FFMPEG STDERR:", result.stderr)
                print("FFMPEG STDOUT:", result.stdout)
                await update.message.reply_text("❌ Ошибка ffmpeg при наложении субтитров.")
                return

            shutil.copy2(output_short, output_path)

        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка при обработке видео: {str(e)}")
            return

        # 5. Отправка
        await update.message.reply_text("✅ Готово! Отправляю видео...")
        try:
            with open(output_path, "rb") as f:
                await update.message.reply_video(video=f, supports_streaming=True)
        except Exception as e:
            if "TimedOut" in str(type(e).__name__):
                await update.message.reply_text("📹 Видео, скорее всего, успешно отправлено. Проверьте чат.")
            else:
                await update.message.reply_text(f"❌ Не удалось отправить видео: {str(e)}")

    except Exception as e:
        if context.user_data.get('cancel_requested'):
            await update.message.reply_text("🛑 Процесс отменён.")
        else:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
            import traceback
            print(traceback.format_exc())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['cancel_requested'] = True
    await update.message.reply_text("🛑 Отмена... Подождите, идёт завершение текущего шага.")


# ============ ЗАПУСК БОТА ============
if __name__ == "__main__":
    app = Application.builder() \
        .token(TOKEN) \
        .connect_timeout(30.0) \
        .read_timeout(120.0) \
        .write_timeout(120.0) \
        .pool_timeout(30.0) \
        .build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"https?://"), handle_message))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

    print("🤖 Бот запущен. Готов к работе!")

    app.run_polling()
