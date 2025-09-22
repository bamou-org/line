import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List
from dotenv import load_dotenv
from instagrapi import Client

# -----------------------
# Paths and environment
# -----------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "videos.db"
UPLOAD_DIR = BASE_DIR / "uploads"

# Map of service -> env var that enables it
SERVICE_ENV_MAP: Dict[str, str] = {
    "tiktok": "TIKTOK_API_KEY",
    "instagram": "INSTAGRAM_USERNAME",
}


# -----------------------
# DB helpers
# -----------------------

def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def ensure_schema(db: sqlite3.Connection) -> None:
    # Minimal schema to allow recording per-video upload results and aggregate events
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS video_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            service TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            uploaded_at TEXT,
            FOREIGN KEY(video_id) REFERENCES videos(id)
        );
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_video_uploads_video ON video_uploads(video_id);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_video_uploads_status ON video_uploads(status);")
    db.commit()

# -----------------------
# Service helpers
# -----------------------

def enabled_services() -> List[str]:
    services = []
    for svc, env_name in SERVICE_ENV_MAP.items():
        if os.environ.get(env_name):
            services.append(svc)
    return services


def has_success(db: sqlite3.Connection, video_id: int, service: str) -> bool:
    cur = db.execute(
        """
        SELECT 1 FROM video_uploads
        WHERE video_id = ? AND service = ? AND status = 'success'
        LIMIT 1
        """,
        (video_id, service),
    )
    return cur.fetchone() is not None


def mark_result(
    db: sqlite3.Connection,
    video_id: int,
    service: str,
    ok: bool,
    error: str | None = None,
) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    db.execute(
        """
        INSERT INTO video_uploads (video_id, service, status, error, created_at, uploaded_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            video_id,
            service,
            "success" if ok else "failed",
            error,
            now,
            now if ok else None,
        ),
    )
    db.commit()


# -----------------------
# Upload implementations (stubs)
# -----------------------

def upload_to_service(service: str, video_path: Path, caption: str | None) -> None:
    if service == "tiktok":
        return None
    elif service == "instagram":
        print("[uploader] uploading to instagram")
        username = os.environ.get("INSTAGRAM_USERNAME")
        password = os.environ.get("INSTAGRAM_PASSWORD")

        if not username or not password:
            raise RuntimeError("Missing INSTAGRAM_USERNAME/INSTAGRAM_PASSWORD in environment")

        cl = Client()

        # Persist session to reduce login challenges across runs
        session_path = BASE_DIR / ".instagrapi.json"
        try:
            if session_path.exists():
                cl.load_settings(session_path)
        except Exception:
            # If loading fails, continue with a fresh login
            pass

        # Perform login (this will refresh cookies if settings were loaded)
        cl.login(username, password)

        # Best-effort session save
        try:
            cl.dump_settings(session_path)
        except Exception:
            pass

        # Upload as a Reel (recommended for most video uploads). Fallback to feed video if needed.
        cap = caption or ""
        cl.clip_upload(str(video_path), cap)
        
        return None

# -----------------------
# Main loop
# -----------------------

def main_loop() -> None:
    print("[uploader] starting background loop; poll interval:", 30, "s")
    while True:
        try:
            db = get_db()
            ensure_schema(db)

            svcs = enabled_services()
            if not svcs:
                print("[uploader] no services enabled via API keys; sleeping...")
                db.close()
                time.sleep(30)
                continue

            # Find due videos (taken_at <= now)
            cur = db.execute(
                """
                SELECT * FROM videos
                WHERE taken_at <= strftime('%Y-%m-%dT%H:%M', 'now','localtime')
                ORDER BY taken_at ASC
                """
            )
            rows = cur.fetchall()
            print(f"[uploader] {len(rows)} due videos")



            for row in rows:
                video_id = row["id"]
                caption = row["caption"]
                file_hash = row["file_hash"]
                video_path = UPLOAD_DIR / file_hash
                if not video_path.exists():
                    # Skip and mark as failed for all services to avoid repeated attempts
                    for svc in svcs:
                        if not has_success(db, video_id, svc):
                            mark_result(db, video_id, svc, ok=False, error="file missing on disk")
                    continue

                for svc in svcs:
                    if has_success(db, video_id, svc):
                        continue
                    try:
                        upload_to_service(svc, video_path, caption)
                        mark_result(db, video_id, svc, ok=True)
                        print(f"[uploader] uploaded video {video_id} to {svc}")
                    except Exception as e:
                        mark_result(db, video_id, svc, ok=False, error=str(e))
                        print(f"[uploader] failed to upload video {video_id} to {svc}: {e}")

            db.close()
        except Exception as outer:
            print("[uploader] loop error:", outer)
        finally:
            time.sleep(30)


if __name__ == "__main__":
    main_loop()
