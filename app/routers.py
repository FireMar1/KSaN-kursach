import os
import aiofiles
import shutil
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from .database import get_db
from .models import Video, User, AuditLog
from .services import convert_to_hls, UPLOAD_DIR, HLS_DIR
from .auth import ensure_default_admin, verify_password, create_session, require_admin, revoke_session

router = APIRouter()

MAX_FILE_SIZE = 500 * 1024 * 1024
ALLOWED_EXTENSIONS = {".mp4", ".avi", ".mkv", ".mov", ".webm"}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def _client_device_info(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if forwarded_for:
        ip = forwarded_for.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    return f"ip={ip}; ua={user_agent}"

@router.get("/", response_class=HTMLResponse)
async def read_root(request: Request, db: Session = Depends(get_db)):
    ensure_default_admin(db)
    videos = db.query(Video).order_by(Video.upload_time.desc()).all()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"videos": videos}
    )

@router.get("/videos")
async def list_videos(db: Session = Depends(get_db)):
    videos = db.query(Video).order_by(Video.upload_time.desc()).all()
    def get_source_url(video_id: int) -> str | None:
        for ext in ALLOWED_EXTENSIONS:
            candidate = os.path.join(UPLOAD_DIR, f"{video_id}{ext}")
            if os.path.exists(candidate):
                return f"/uploads/{video_id}{ext}"
        return None

    return [
        {
            "id": video.id,
            "filename": video.filename,
            "status": video.status,
            "stream_url": video.stream_url,
            "source_url": get_source_url(video.id),
        }
        for video in videos
    ]

@router.post("/admin/login")
async def admin_login(request: Request, db: Session = Depends(get_db)):
    ensure_default_admin(db)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Ожидается JSON тело запроса")
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", "")).strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="Введите логин и пароль")
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    token = create_session(user.id, user.role)
    db.add(AuditLog(user_id=user.id, action="admin_login", details="Admin login success"))
    db.commit()
    return {"access_token": token, "token_type": "bearer", "username": user.username}


@router.post("/admin/check")
async def check_admin(session_data: dict = Depends(require_admin)):
    return {"ok": True, "user_id": session_data["user_id"]}


@router.post("/admin/logout")
async def admin_logout(request: Request):
    revoke_session(request.headers.get("authorization"))
    return {"ok": True}


@router.get("/admin/logs")
async def get_logs(session_data: dict = Depends(require_admin), db: Session = Depends(get_db)):
    logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(50).all()
    return [
        {
            "id": log.id,
            "user_id": log.user_id,
            "action": log.action,
            "details": log.details,
            "created_at": log.created_at.isoformat(),
        }
        for log in logs
    ]

@router.post("/upload/")
async def upload_video(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    filename = os.path.basename(file.filename or "")
    if not filename:
        raise HTTPException(status_code=400, detail="Пустое имя файла")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Неверный формат файла")

    new_video = Video(filename=filename, status="processing")
    db.add(new_video)
    db.commit()
    db.refresh(new_video)

    save_path = os.path.join(UPLOAD_DIR, f"{new_video.id}{ext}")
    stream_folder = os.path.join(HLS_DIR, str(new_video.id))
    os.makedirs(stream_folder, exist_ok=True)

    total_bytes = 0
    async with aiofiles.open(save_path, 'wb') as out_file:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_FILE_SIZE:
                await file.close()
                if os.path.exists(save_path):
                    os.remove(save_path)
                if os.path.exists(stream_folder):
                    shutil.rmtree(stream_folder)
                db.delete(new_video)
                db.commit()
                raise HTTPException(status_code=413, detail="Файл слишком большой")
            await out_file.write(chunk)
    await file.close()

    device_info = _client_device_info(request)
    db.add(
        AuditLog(
            action="video_uploaded",
            details=f"Video {new_video.id}: {filename}; {device_info}",
        )
    )
    db.commit()
    db.refresh(new_video)

    background_tasks.add_task(convert_to_hls, save_path, stream_folder, new_video.id)

    return {"message": "Файл загружен и обрабатывается", "video_id": new_video.id}

@router.post("/delete/{video_id}")
async def delete_video(video_id: int, session_data: dict = Depends(require_admin), db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Видео не найдено")
    try:
        if video.stream_url:
            parts = video.stream_url.strip("/").split("/")
            if len(parts) >= 3 and parts[0] == "hls":
                folder_id = parts[1]
                hls_path = os.path.join(HLS_DIR, folder_id)
                if os.path.exists(hls_path):
                    shutil.rmtree(hls_path)
        for ext in ALLOWED_EXTENSIONS:
            source_path = os.path.join(UPLOAD_DIR, f"{video.id}{ext}")
            if os.path.exists(source_path):
                os.remove(source_path)
                break
        db.add(
            AuditLog(
                user_id=session_data["user_id"],
                action="video_deleted",
                details=f"Video {video.id}: {video.filename}",
            )
        )
        db.delete(video)
        db.commit()
        return {"message": "Видео успешно удалено"}
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Ошибка при удалении: {exc}")