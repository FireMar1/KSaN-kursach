import os
import asyncio
from datetime import datetime
from .database import SessionLocal
from .models import Video, AuditLog

APP_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_ROOT = os.path.join(APP_DIR, "media")
UPLOAD_DIR = os.path.join(MEDIA_ROOT, "uploads")
HLS_DIR = os.path.join(MEDIA_ROOT, "hls_streams")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(HLS_DIR, exist_ok=True)


async def convert_to_hls(input_path: str, output_dir: str, video_id: int):
    # Уникальное имя для плейлиста
    playlist_name = "index.m3u8"
    output_path = os.path.join(output_dir, playlist_name)

    # Команда FFmpeg для нарезки видео (кусочки по 10 секунд)
    command = [
        "ffmpeg", "-y", "-i", input_path,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-preset", "veryfast",
        "-start_number", "0",
        "-hls_time", "10",  # Длина чанка
        "-hls_list_size", "0",  # Сохранять все чанки
        "-f", "hls",
        output_path
    ]

    try:
        # Запускаем процесс асинхронно, чтобы не блокировать сервер
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        db_local = SessionLocal()
        try:
            video = db_local.query(Video).filter(Video.id == video_id).first()
            if not video:
                return
            if process.returncode == 0:
                video.status = "ready"
                video.stream_url = f"/hls/{os.path.basename(output_dir)}/{playlist_name}"
                db_local.add(
                    AuditLog(
                        action="video_processed",
                        details=f"Video {video.id} converted to HLS at {datetime.utcnow().isoformat()}",
                    )
                )
            else:
                video.status = "error"
                error_text = stderr.decode(errors="ignore")
                print(f"FFmpeg error for video {video_id}: {error_text}")
            db_local.commit()
        finally:
            db_local.close()
    except Exception as e:
        db_local = SessionLocal()
        try:
            video = db_local.query(Video).filter(Video.id == video_id).first()
            if video:
                video.status = "error"
                db_local.commit()
        finally:
            db_local.close()
        print(f"Error processing video: {e}")