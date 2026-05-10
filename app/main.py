import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.database import engine, Base, SessionLocal
from app.auth import ensure_default_admin
from app.routers import router
from app.services import HLS_DIR, UPLOAD_DIR
import mimetypes

# Добавляем поддержку HLS типов (нужно сделать до инициализации FastAPI)
mimetypes.add_type('application/vnd.apple.mpegurl', '.m3u8')
mimetypes.add_type('video/mp2t', '.ts')

app = FastAPI(title="HLS Streaming Service")
# Создаем таблицы
Base.metadata.create_all(bind=engine)

# Определяем путь к папке app
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(HLS_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Подключаем статику (она внутри app)
app.mount("/static", StaticFiles(directory=os.path.join(CURRENT_DIR, "static")), name="static")

# Подключаем медиа (она в корне проекта)
app.mount("/hls", StaticFiles(directory=HLS_DIR), name="hls")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

app.include_router(router)


@app.on_event("startup")
def init_admin_user() -> None:
    db = SessionLocal()
    try:
        ensure_default_admin(db)
    finally:
        db.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="localhost", port=8001, reload=True)