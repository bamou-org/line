# line
Buffer alternative – scheduled upload of short-form content to various platforms

## Simple Flask Video Uploader

This repository now includes a small Flask app to upload videos with optional name, caption, date, and time. Uploaded videos are stored on disk using a SHA-256 content hash as the filename, and their metadata is stored in a local SQLite database.

### Features
- Optional display name (defaults to the original filename without extension if not provided)
- Caption field
- Date and time fields (combined into a `captured_at` datetime)
- Deduplicated storage by content hash (re-uploads of the same content will update metadata rather than storing a second copy)
- Simple listing page and detail page with an HTML5 video player

### Project Structure
- `app.py` – Flask application and SQLite models
- `uploader.py` – Background polling script that uploads due videos to enabled social services
- `templates/` – HTML templates (`base.html`, `index.html`, `upload.html`, `detail.html`)
- `static/style.css` – Minimal styles
- `uploads/` – Created automatically at runtime; holds hashed video files
- `requirements.txt` – Python dependencies
- `videos.db` – SQLite database (created on first run)

### Prerequisites
- Python 3.10+

### Setup
```bash
# (optional) create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# install dependencies
pip install -r requirements.txt
```

### Configuration (.env)
Create a `.env` file in the project root with your login credentials and Flask secret key. You can also add API keys for services; if a key is present, the uploader will attempt to post to that service once the scheduled time is reached.

```
APP_USERNAME=your_username
APP_PASSWORD=your_password
FLASK_SECRET_KEY=change-this-secret

# Optional: API keys to enable uploads to services
# If a key is set, that service is considered enabled by the background uploader.
TIKTOK_API_KEY=your_tiktok_key
YOUTUBE_API_KEY=your_youtube_key
INSTAGRAM_API_KEY=your_instagram_key
TWITTER_API_KEY=your_twitter_key

# Optional: polling interval for uploader (seconds, default 30)
UPLOADER_POLL_SECONDS=30
```

`APP_USERNAME` and `APP_PASSWORD` are required; the app will refuse to start if they are missing. All other values are optional.

### Run
```bash
# start the Flask app
python app.py
```

The app will start on http://localhost:5000

### Background Uploader
The uploader is a separate long-running script that checks the database periodically for videos whose scheduled `taken_at` time has passed. For each enabled service (as determined by API keys provided in `.env`), it attempts an upload and records the result.

Run it in a separate terminal:

```bash
python uploader.py
```

You can also run it in the background using tools like `nohup`, `screen`, `tmux`, or a systemd service. Example with `nohup`:

```bash
nohup python uploader.py > uploader.log 2>&1 &
```

Notes:
- The current service integrations are stubs; replace the logic in `upload_to_service()` within `uploader.py` with actual API/SDK calls.
- The uploader respects API key presence in `.env` to decide which services are enabled.
- Upload attempts and results are tracked in the `video_uploads` table; aggregate successes are also logged in `upload_success_events`.

### Authentication
- Accessing any route will redirect to `GET /login` if not authenticated.
- Log in with the credentials from your `.env` file.
- Log out via `GET /logout` (or add a small link/button in the UI if desired).

- Home/List: `GET /`
- Upload form: `GET /upload`
- Submit upload: `POST /upload`
- Video detail: `GET /videos/<id>`
- Serve uploaded file: `GET /uploads/<filename>`

### Notes
- Supported video extensions: mp4, webm, ogg, mov, avi, mkv, m4v
- The `.gitignore` excludes the `uploads/` directory, SQLite database files, and `.env` to avoid committing sensitive or large files.
- For production, set a strong `FLASK_SECRET_KEY`.
