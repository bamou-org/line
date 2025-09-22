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
Create a `.env` file in the project root with your app login credentials and service settings. The background uploader considers a service "enabled" if the corresponding environment variables are present.

```
# App auth
APP_USERNAME=your_username
APP_PASSWORD=your_password
FLASK_SECRET_KEY=change-this-secret

# TikTok via cookies file (Netscape cookies.txt format)
# If set, TikTok uploads are enabled
TIKTOK_COOKIES_FILE=TikTok_cookies.txt

# Instagram login (instagrapi)
# If both are set, Instagram uploads are enabled
INSTAGRAM_USERNAME=your_instagram_username
INSTAGRAM_PASSWORD=your_instagram_password

# Optional: polling interval for uploader (seconds, default 30)
UPLOADER_POLL_SECONDS=30
```

`APP_USERNAME` and `APP_PASSWORD` are required for logging into the web UI. The service settings are optional; add only the services you want to enable.

### Run
```bash
# start the Flask app
python app.py
```

The app will start on http://localhost:5000

### Background Uploader
The uploader is a separate long-running script that checks the database periodically for videos whose scheduled `taken_at` time is due. For each enabled service (as determined by environment variables in `.env`), it attempts an upload and records the result.

Run it in a separate terminal:

```bash
python uploader.py
```

You can also run it in the background using tools like `nohup`, `screen`, `tmux`, or a systemd service. Example with `nohup`:

```bash
nohup python uploader.py > uploader.log 2>&1 &
```

Notes:
- TikTok uploads use a cookies file to authenticate the browser automation.
- Instagram uploads use `instagrapi` and will persist a session file in `.instagrapi.json` to reduce challenges.
- Upload attempts and results are tracked in the `video_uploads` table.

### TikTok cookies: how to generate
TikTok authentication is provided via a cookies file. Create this once and refresh it when your session expires.

Steps (Chrome/Chromium/Edge):

1. Log in to https://www.tiktok.com/ in your browser.
2. Install a cookies export extension such as "Get cookies.txt LOCALLY" or "cookies.txt".
3. With tiktok.com open and logged in, use the extension to export cookies as a `cookies.txt` file (Netscape format).
4. Save the file into the project directory, e.g. `TikTok_cookies.txt`.
5. Set the path in `.env`:

   ```bash
   TIKTOK_COOKIES_FILE=TikTok_cookies.txt
   ```

Tips:
- Keep the file private; it grants access to your TikTok account.
- If uploads start failing with auth issues, regenerate the cookies file (sessions can expire).
- Headless uploading is supported; the uploader passes `headless=True` to the TikTok uploader.

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

## Known issues: TikTok upload hangs on post confirmation

In some environments, the TikTok Selenium uploader can hang while waiting for the final post confirmation. In logs, you may see output like:

```
[uploader] uploading to tiktok
... Clicking the post button
```

and then the process appears stuck until a KeyboardInterrupt. The stack trace typically shows Selenium waiting on `config.selectors.upload.post_confirmation` inside `tiktok_uploader/upload.py`:

```
WebDriverWait(driver, config.explicit_wait).until(post_confirmation)
```

### Root cause
TikTok’s UI sometimes surfaces a secondary confirmation button ("Post now") after the initial "Post" click (for example, when certain settings or scheduling states are present). The library waits for a post confirmation element that may not appear until the extra "Post now" button is clicked, causing the wait to hang.

### Fix / workaround
Patch the TikTok uploader’s `_post_video()` function to optionally click the "Post now" button if it appears. This change is safe: if the button is not present, the code simply continues.

Where to change it:
- File path: `.venv/lib/python3.11/site-packages/tiktok_uploader/upload.py`
- Function: `_post_video(driver: WebDriver) -> None`

If you prefer not to modify files inside your virtual environment, copy the package into your repo (vendor it) and adjust imports to use your forked module. Otherwise, edit in place.

Replace the `_post_video()` function with the following implementation:

```python
def _post_video(driver: WebDriver) -> None:
    """
    Posts the video by clicking the post button

    Parameters
    ----------
    driver : selenium.webdriver
    """
    logger.debug(green("Clicking the post button"))

    try:
        post = WebDriverWait(driver, config.uploading_wait).until(
            lambda d: (el := d.find_element(By.XPATH, config.selectors.upload.post))
            and el.get_attribute("data-disabled") == "false"
            and el
        )
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", post
        )
        post.click()
    except ElementClickInterceptedException:
        logger.debug(green("Trying to click on the button again"))
        driver.execute_script('document.querySelector(".TUXButton--primary").click()')

    # Optionally click "Post now" if the UI shows it (e.g., after scheduling)
    try:
        logger.debug(green("Looking for 'Post now' button"))
        post_now = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    # Tries common button text patterns, including nested text nodes
                    "//button[normalize-space(.)='Post now' or normalize-space(.)='Post Now' "
                    "or .//div[normalize-space(.)='Post now' or normalize-space(.)='Post Now']]",
                )
            )
        )
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
            post_now,
        )
        post_now.click()
        logger.debug(green("'Post now' clicked"))
    except TimeoutException:
        logger.debug(green("'Post now' not shown; continuing"))
    except ElementClickInterceptedException:
        logger.debug(green("Retrying 'Post now' via JS click"))
        driver.execute_script(
            "document.evaluate("
            "\"//button[normalize-space(text())='Post now' or normalize-space(text())='Post Now']\", "
            "document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null"
            ").singleNodeValue?.click()"
        )

    # waits for the video to upload (be flexible about what indicates success)
    try:
        wait = WebDriverWait(driver, min(getattr(config, "explicit_wait", 30), 15))
        wait.until(
            EC.any_of(
                # Original success condition
                EC.presence_of_element_located(
                    (By.XPATH, config.selectors.upload.post_confirmation)
                ),
                # The post button disappears or the DOM is refreshed
                EC.invisibility_of_element_located((By.XPATH, config.selectors.upload.post)),
                # Or the element we clicked becomes stale (navigated/updated)
                EC.staleness_of(post),
            )
        )
        logger.debug(green("Post confirmation detected (or post button disappeared)"))
    except TimeoutException:
        # Don't block forever; proceed even if the specific confirmation isn't seen
        logger.debug(green("No explicit confirmation within timeout; proceeding"))
        # Optional: small grace period if the page is transitioning
        # time.sleep(2)

    logger.debug(green("Video posted successfully"))

```