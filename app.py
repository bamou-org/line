import os
import hashlib
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path

from flask import Flask, g, render_template, request, redirect, url_for, send_file, abort, flash, jsonify, session
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import secrets
import hmac

# -----------------------
# Basic config
# -----------------------
BASE_DIR = Path(__file__).resolve().parent
# Load environment from .env if present
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "videos.db"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm", "m4v"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,  # set True when using HTTPS
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,  # 12 hours
)

# Auth config from environment
AUTH_USERNAME = os.environ.get("APP_USERNAME")
AUTH_PASSWORD = os.environ.get("APP_PASSWORD")

if not AUTH_USERNAME or not AUTH_PASSWORD:
    # Fail fast so it is obvious credentials must be set
    raise RuntimeError(
        "Missing APP_USERNAME/APP_PASSWORD. Create a .env file with APP_USERNAME and APP_PASSWORD."
    )


# -----------------------
# Database helpers
# -----------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()

@app.before_request
def init_db():
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_hash TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            name TEXT,
            caption TEXT,
            taken_at TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mime_type TEXT
        );
        """
    )
    # Per-video upload results table
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS video_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            service TEXT NOT NULL,
            status TEXT NOT NULL, -- 'success' | 'failed'
            error TEXT,
            created_at TEXT NOT NULL,
            uploaded_at TEXT,
            FOREIGN KEY(video_id) REFERENCES videos(id)
        );
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_video_uploads_video ON video_uploads(video_id);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_video_uploads_status ON video_uploads(status);")
    # Server-side sessions table for stronger auth
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen TEXT
        );
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_videos_taken_at ON videos(taken_at);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_videos_file_hash ON videos(file_hash);")
    db.commit()


# -----------------------
# Auth helpers & guard
# -----------------------

def _get_session_row_by_token(token: str):
    if not token:
        return None
    th = hashlib.sha256(token.encode("utf-8")).hexdigest()
    db = get_db()
    row = db.execute("SELECT * FROM sessions WHERE token_hash = ? AND username = ?", (th, AUTH_USERNAME)).fetchone()
    return row


def is_authenticated() -> bool:
    token = session.get("sid")
    row = _get_session_row_by_token(token)
    if row:
        # Update last_seen occasionally
        try:
            get_db().execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (datetime.utcnow().isoformat(timespec="seconds"), row["id"]))
            get_db().commit()
        except Exception:
            pass
        return True
    return False


@app.before_request
def require_login():
    # Allow unauthenticated access to login, static files and health checks if any
    allowed = {"login"}
    if request.endpoint in allowed or (request.endpoint or "").startswith("static"):
        return None
    if not is_authenticated():
        # Preserve next URL to redirect back after login
        next_url = request.url
        return redirect(url_for("login", next=next_url))


# -----------------------
# Utilities
# -----------------------

def allowed_file(filename: str) -> bool:
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS


def sha256_file(file_storage) -> str:
    # Compute SHA256 as we stream the file once
    file_storage.stream.seek(0)
    h = hashlib.sha256()
    for chunk in iter(lambda: file_storage.stream.read(8192), b""):
        h.update(chunk)
    file_storage.stream.seek(0)  # reset for saving
    return h.hexdigest()


def parse_eu_datetime(date_str: str, time_str: str) -> datetime:
    # Expected formats: DD.MM.YY and HH:MM (24h)
    try:
        d = datetime.strptime(date_str.strip(), "%d.%m.%y").date()
        t = datetime.strptime(time_str.strip(), "%H:%M").time()
        return datetime.combine(d, t)
    except ValueError:
        # Try with 4-digit year if user enters DD.MM.YYYY
        try:
            d = datetime.strptime(date_str.strip(), "%d.%m.%Y").date()
            t = datetime.strptime(time_str.strip(), "%H:%M").time()
            return datetime.combine(d, t)
        except ValueError:
            raise


def ensure_not_past(dt: datetime) -> bool:
    """Return True if dt is now or in the future (local time)."""
    return dt >= datetime.now()


def month_range(year: int, month: int):
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def fetch_month_counts(year: int, month: int) -> dict:
    """Return mapping YYYY-MM-DD -> count of videos taken that day."""
    db = get_db()
    start, end = month_range(year, month)
    cur = db.execute(
        """
        SELECT substr(taken_at, 1, 10) as day, COUNT(*) as cnt
        FROM videos
        WHERE date(taken_at) BETWEEN date(?) AND date(?)
        GROUP BY day
        """,
        (start.isoformat(), end.isoformat()),
    )
    out = {row["day"]: row["cnt"] for row in cur.fetchall()}
    return out


# -----------------------
# Routes
# -----------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if hmac.compare_digest(username, AUTH_USERNAME or "") and hmac.compare_digest(password, AUTH_PASSWORD or ""):
            session.clear()
            # Create a random session token and store its hash server-side
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            db = get_db()
            db.execute(
                "INSERT INTO sessions (username, token_hash, created_at, last_seen) VALUES (?, ?, ?, ?)",
                (username, token_hash, datetime.utcnow().isoformat(timespec="seconds"), datetime.utcnow().isoformat(timespec="seconds")),
            )
            db.commit()
            # Store only the opaque token in the client session
            session["sid"] = token
            flash("Logged in.")
            dest = request.args.get("next")
            # Basic open redirect guard
            if dest and dest.startswith(("/", url_for("index"))):
                return redirect(dest)
            return redirect(url_for("index"))
        else:
            flash("Invalid credentials.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST", "GET"])  # allow GET for simplicity
def logout():
    # Best-effort server-side invalidation
    token = session.get("sid")
    if token:
        try:
            th = hashlib.sha256(token.encode("utf-8")).hexdigest()
            db = get_db()
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
            db.commit()
        except Exception:
            pass
    session.clear()
    flash("Logged out.")
    return redirect(url_for("login"))


@app.route("/")
def index():
    # Determine current month from query params or today
    try:
        year = int(request.args.get("year") or datetime.today().year)
        month = int(request.args.get("month") or datetime.today().month)
        # clamp
        if not (1 <= month <= 12):
            raise ValueError
    except ValueError:
        year = datetime.today().year
        month = datetime.today().month

    counts = fetch_month_counts(year, month)

    # List videos for this month ordered by taken_at desc
    db = get_db()
    start, end = month_range(year, month)
    videos = db.execute(
        """
        SELECT * FROM videos
        WHERE date(taken_at) BETWEEN date(?) AND date(?)
        ORDER BY taken_at ASC
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    # For intensity scale
    max_count = max(counts.values()) if counts else 0

    # prev/next month links
    prev_year, prev_month = (year, month - 1)
    next_year, next_month = (year, month + 1)
    if month == 1:
        prev_year, prev_month = (year - 1, 12)
    if month == 12:
        next_year, next_month = (year + 1, 1)

    # Flat list of all days in the month
    start, end = month_range(year, month)
    all_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    # Per-video success counts
    succ_by_video = {
        row[0]: row[1]
        for row in db.execute(
            """
            SELECT video_id, COUNT(*) AS succ
            FROM video_uploads
            WHERE status = 'success'
            GROUP BY video_id
            """
        ).fetchall()
    }
    
    return render_template(
        "index.html",
        year=year,
        month=month,
        all_days=all_days,
        counts=counts,
        max_count=max_count,
        videos=videos,
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        today=date.today(),
        upload_success_by_video=succ_by_video,
    )


@app.route("/upload", methods=["POST"]) 
def upload():
    file = request.files.get("video")
    name = request.form.get("name") or None
    caption = request.form.get("caption") or None
    date_str = request.form.get("date") or ""
    time_str = request.form.get("time") or ""

    if not file or file.filename == "":
        flash("Please select a video file.", "error")
        return redirect(url_for("index"))

    if not allowed_file(file.filename):
        flash("Unsupported file type.", "error")
        return redirect(url_for("index"))

    try:
        taken_at_dt = parse_eu_datetime(date_str, time_str)
        if not ensure_not_past(taken_at_dt):
            flash("Date/time cannot be before now.", "error")
            return redirect(url_for("index"))
    except Exception:
        flash("Invalid date/time. Use DD.MM.YY and HH:MM.", "error")
        return redirect(url_for("index"))

    file_hash = sha256_file(file)

    # Store by hash without extension
    out_path = UPLOAD_DIR / file_hash
    if not out_path.exists():
        # Save once
        file.save(out_path)

    db = get_db()
    db.execute(
        """
        INSERT INTO videos (file_hash, original_filename, name, caption, taken_at, uploaded_at, size_bytes, mime_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_hash,
            secure_filename(file.filename),
            name,
            caption,
            taken_at_dt.isoformat(timespec="minutes"),
            datetime.utcnow().isoformat(timespec="seconds"),
            out_path.stat().st_size,
            file.mimetype,
        ),
    )
    db.commit()

    # Redirect to same month as taken_at
    return redirect(url_for("index", year=taken_at_dt.year, month=taken_at_dt.month))


@app.route("/video/<int:video_id>")
def video_detail(video_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not row:
        abort(404)
    # Format taken date/time for display
    try:
        taken_dt = datetime.fromisoformat(row["taken_at"]).strftime("%d.%m.%Y %H:%M")
    except Exception:
        taken_dt = row["taken_at"]
    return render_template("detail.html", video=row, taken_display=taken_dt)


@app.route("/video/<int:video_id>/edit", methods=["GET", "POST"])
def edit_video(video_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not row:
        abort(404)

    if request.method == "POST":
        name = request.form.get("name") or None
        caption = request.form.get("caption") or None
        date_str = request.form.get("date") or ""
        time_str = request.form.get("time") or ""
        try:
            taken_at_dt = parse_eu_datetime(date_str, time_str)
            if not ensure_not_past(taken_at_dt):
                flash("Date/time cannot be before now.", "error")
                return redirect(url_for("edit_video", video_id=video_id))
        except Exception:
            flash("Invalid date/time. Use DD.MM.YY and HH:MM.", "error")
            return redirect(url_for("edit_video", video_id=video_id))

        db.execute(
            """
            UPDATE videos
            SET name = ?, caption = ?, taken_at = ?
            WHERE id = ?
            """,
            (
                name,
                caption,
                taken_at_dt.isoformat(timespec="minutes"),
                video_id,
            ),
        )
        db.commit()
        flash("Video updated.")
        return redirect(url_for("video_detail", video_id=video_id))

    # Prefill EU date/time
    try:
        dt = datetime.fromisoformat(row["taken_at"])
    except Exception:
        dt = datetime.utcnow()
    pre_date = dt.strftime("%d.%m.%y")
    pre_time = dt.strftime("%H:%M")
    return render_template("edit.html", video=row, pre_date=pre_date, pre_time=pre_time)


@app.route("/video/<int:video_id>/delete", methods=["POST"])
def delete_video(video_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not row:
        abort(404)

    file_hash = row["file_hash"]

    # Delete DB row
    db.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    db.commit()

    # If no other records reference this hash, remove the file
    others = db.execute("SELECT COUNT(*) as c FROM videos WHERE file_hash = ?", (file_hash,)).fetchone()
    if others and others["c"] == 0:
        path = UPLOAD_DIR / file_hash
        if path.exists():
            try:
                path.unlink()
            except Exception:
                # Non-fatal
                pass

    flash("Video removed.")
    # Redirect to the month of the deleted video's taken_at
    try:
        dt = datetime.fromisoformat(row["taken_at"])
        return redirect(url_for("index", year=dt.year, month=dt.month))
    except Exception:
        return redirect(url_for("index"))


@app.route("/stream/<int:video_id>")
def stream_video(video_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not row:
        abort(404)
    path = UPLOAD_DIR / row["file_hash"]
    if not path.exists():
        abort(404)
    # Simple send_file (not range requests)
    return send_file(path, mimetype=row["mime_type"], as_attachment=False)


# -----------------------
# App startup
# -----------------------
if __name__ == "__main__":
    # Ensure DB is initialized within an app context when running directly
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
