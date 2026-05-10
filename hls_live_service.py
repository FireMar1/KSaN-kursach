import asyncio
import logging
import os
import re
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiohttp import web
from dotenv import load_dotenv
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


@dataclass
class Config:
    watch_dir: Path
    hls_dir: Path
    hls_list_size: int
    segment_duration: int
    port: int
    playlist_name: str = "output.m3u8"

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        watch_dir = Path(os.getenv("WATCH_DIR", "./incoming")).resolve()
        hls_dir = Path(os.getenv("HLS_DIR", "./hls_output")).resolve()
        list_size = int(os.getenv("HLS_LIST_SIZE", "60"))
        # Too small windows (1-2 segments) often cause short-loop playback in live players.
        list_size = max(6, list_size)
        segment_duration = int(os.getenv("SEGMENT_DURATION", "5"))
        port = int(os.getenv("PORT", "8080"))
        return cls(watch_dir, hls_dir, list_size, segment_duration, port)


class IncomingFileHandler(FileSystemEventHandler):
    def __init__(self, service: "HLSStreamService"):
        self.service = service

    def on_created(self, event):
        if event.is_directory:
            return
        self.service.enqueue_from_watcher(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        self.service.enqueue_from_watcher(event.dest_path)


class HLSStreamService:
    SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}

    def __init__(self, config: Config):
        self.config = config
        self.config.watch_dir.mkdir(parents=True, exist_ok=True)
        self.config.hls_dir.mkdir(parents=True, exist_ok=True)
        self.playlist_path = self.config.hls_dir / self.config.playlist_name
        self.queue: asyncio.Queue[Path] = asyncio.Queue()
        self.stop_event = asyncio.Event()
        self.observer = Observer()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.current_ffmpeg: Optional[asyncio.subprocess.Process] = None
        self.seen_files: set[Path] = set()
        self.segment_index = self._detect_next_segment_index()
        self.http_runner: Optional[web.AppRunner] = None

    def _detect_next_segment_index(self) -> int:
        pattern = re.compile(r"segment_(\d+)\.ts$")
        highest = -1
        for file in self.config.hls_dir.glob("segment_*.ts"):
            match = pattern.search(file.name)
            if match:
                highest = max(highest, int(match.group(1)))
        return highest + 1

    def enqueue_from_watcher(self, raw_path: str) -> None:
        if self.loop is None:
            return
        path = Path(raw_path).resolve()
        if path.suffix.lower() not in self.SUPPORTED_EXTENSIONS:
            return
        self.loop.call_soon_threadsafe(self._enqueue_sync, path)

    def _enqueue_sync(self, path: Path) -> None:
        if path in self.seen_files:
            return
        self.seen_files.add(path)
        self.queue.put_nowait(path)
        logging.info("Queued file: %s (queue size=%s)", path.name, self.queue.qsize())

    async def enqueue_existing_files(self) -> None:
        files = sorted(
            [
                f
                for f in self.config.watch_dir.iterdir()
                if f.is_file() and f.suffix.lower() in self.SUPPORTED_EXTENSIONS
            ],
            key=lambda p: p.stat().st_mtime,
        )
        for file in files:
            self._enqueue_sync(file.resolve())

    async def wait_until_stable(self, path: Path, checks: int = 3, delay: float = 1.0) -> bool:
        if not path.exists():
            return False
        previous_size = -1
        stable = 0
        for _ in range(20):
            if not path.exists():
                return False
            current_size = path.stat().st_size
            if current_size > 0 and current_size == previous_size:
                stable += 1
                if stable >= checks:
                    return True
            else:
                stable = 0
            previous_size = current_size
            await asyncio.sleep(delay)
        return False

    async def process_file(self, path: Path) -> None:
        is_stable = await self.wait_until_stable(path)
        if not is_stable:
            logging.error("File not stable or missing, skipped: %s", path)
            return

        start_number = self.segment_index
        segment_pattern = str(self.config.hls_dir / "segment_%09d.ts")

        # `segment_%09d.ts` + `-start_number` keeps global monotonic numbering.
        # This prevents name collisions and preserves timeline order while appending.
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-fflags",
            "+genpts",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-force_key_frames",
            f"expr:gte(t,n_forced*{self.config.segment_duration})",
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-af",
            "aresample=async=1:first_pts=0",
            "-avoid_negative_ts",
            "make_zero",
            "-f",
            "hls",
            "-hls_time",
            str(self.config.segment_duration),
            "-hls_list_size",
            str(self.config.hls_list_size),
            "-hls_flags",
            "append_list+delete_segments+omit_endlist+program_date_time",
            "-hls_segment_filename",
            segment_pattern,
            "-start_number",
            str(start_number),
            str(self.playlist_path),
        ]

        logging.info("Processing %s with start_number=%s", path.name, start_number)
        try:
            self.current_ffmpeg = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await self.current_ffmpeg.communicate()
            if self.current_ffmpeg.returncode != 0:
                logging.error("FFmpeg failed for %s: %s", path.name, stderr.decode(errors="ignore"))
                return
            next_index = self._detect_next_segment_index()
            created = max(0, next_index - start_number)
            self.segment_index = next_index
            logging.info("Done %s: created %s segments, next_index=%s", path.name, created, self.segment_index)
        except Exception:
            logging.exception("Unexpected processing error for %s", path.name)
        finally:
            self.current_ffmpeg = None

    async def worker(self) -> None:
        logging.info("Worker started")
        while not self.stop_event.is_set():
            try:
                path = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self.process_file(path)
            finally:
                self.queue.task_done()
        logging.info("Worker stopped")

    def build_web_app(self) -> web.Application:
        app = web.Application(middlewares=[self.cors_middleware])

        async def player(request: web.Request) -> web.Response:
            return web.FileResponse(Path(__file__).parent / "player.html")

        async def playlist(request: web.Request) -> web.Response:
            if not self.playlist_path.exists():
                return web.Response(status=404, text="# Playlist is not ready yet\n")
            return web.FileResponse(self.playlist_path)

        async def segment(request: web.Request) -> web.Response:
            name = request.match_info["name"]
            file_path = self.config.hls_dir / name
            if not file_path.exists():
                return web.Response(status=404, text="Segment not found")
            return web.FileResponse(file_path)

        async def health(request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "status": "ok",
                    "queue_size": self.queue.qsize(),
                    "segment_index": self.segment_index,
                    "playlist": str(self.playlist_path),
                }
            )

        app.router.add_get("/", player)
        app.router.add_get("/player.html", player)
        app.router.add_get(f"/{self.config.playlist_name}", playlist)
        app.router.add_get(r"/{name:segment_\d+\.ts}", segment)
        app.router.add_get("/health", health)
        return app

    @web.middleware
    async def cors_middleware(self, request: web.Request, handler):
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        return response

    async def start_http_server(self) -> None:
        app = self.build_web_app()
        self.http_runner = web.AppRunner(app)
        await self.http_runner.setup()
        site = web.TCPSite(self.http_runner, "0.0.0.0", self.config.port)
        await site.start()
        logging.info("HTTP server started on http://0.0.0.0:%s", self.config.port)

    async def stop_http_server(self) -> None:
        if self.http_runner:
            await self.http_runner.cleanup()
            logging.info("HTTP server stopped")

    def start_watcher(self) -> None:
        handler = IncomingFileHandler(self)
        self.observer.schedule(handler, str(self.config.watch_dir), recursive=False)
        self.observer.start()
        logging.info("Watching directory: %s", self.config.watch_dir)

    def stop_watcher(self) -> None:
        self.observer.stop()
        self.observer.join(timeout=5)
        logging.info("Watcher stopped")

    async def shutdown(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        if self.current_ffmpeg and self.current_ffmpeg.returncode is None:
            logging.info("Stopping active ffmpeg process")
            self.current_ffmpeg.terminate()
            try:
                await asyncio.wait_for(self.current_ffmpeg.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.current_ffmpeg.kill()
        self.stop_watcher()
        await self.stop_http_server()

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.start_watcher()
        await self.enqueue_existing_files()
        await self.start_http_server()

        worker_task = asyncio.create_task(self.worker())
        await self.stop_event.wait()
        await worker_task


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


async def main_async() -> None:
    setup_logging()
    config = Config.from_env()
    service = HLSStreamService(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(service.shutdown()))
        except NotImplementedError:
            signal.signal(sig, lambda *_: asyncio.create_task(service.shutdown()))

    logging.info(
        "Config: WATCH_DIR=%s HLS_DIR=%s HLS_LIST_SIZE=%s SEGMENT_DURATION=%s PORT=%s",
        config.watch_dir,
        config.hls_dir,
        config.hls_list_size,
        config.segment_duration,
        config.port,
    )

    try:
        await service.run()
    finally:
        await service.shutdown()


if __name__ == "__main__":
    asyncio.run(main_async())
