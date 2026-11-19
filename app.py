"""Local web UI to queue Facebook posts. Run: python app.py -> http://127.0.0.1:5050"""
import json
import logging
import os
import uuid
from datetime import datetime
from urllib.parse import urlparse

from flask import Flask, flash, redirect, render_template, request, url_for

import worker
from db import BASE, db, init

MEDIA_DIR = BASE / "media"
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov"}
SHRINK_EXT = {".jpg", ".jpeg", ".png", ".webp"}  # not gif: may be animated
# Facebook shows photos at most 2048px on the long side; anything bigger only slows the upload.
MAX_IMAGE_SIDE = 2048
JPEG_QUALITY = 88
STATUS_LABEL = {"pending": "Chờ", "running": "Đang đăng", "done": "Đã đăng", "partial": "Một phần",
                "pending_approval": "Chờ duyệt", "failed": "Lỗi"}
KIND_LABEL = {"group": "group", "page": "page", "profile": "cá nhân"}

app = Flask(__name__)
app.secret_key = os.urandom(16)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024


def is_facebook_url(url: str) -> bool:
    p = urlparse(url)
    return p.scheme == "https" and (p.netloc == "facebook.com" or p.netloc.endswith(".facebook.com"))


def kind_of(url: str) -> str:
    path = urlparse(url).path
    if "/groups/" in path:
        return "group"
    return "profile" if path.rstrip("/") == "/me" else "page"


def shrink_image(path) -> None:
    """Downscale photos larger than MAX_IMAGE_SIDE in place (phone photos: ~4-5 MB -> <1 MB)."""
    from PIL import Image, ImageOps
    with Image.open(path) as src:
        if max(src.size) <= MAX_IMAGE_SIDE:
            return
        img = ImageOps.exif_transpose(src)  # keep the photo upright once EXIF is dropped
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    if path.suffix.lower() in (".jpg", ".jpeg"):
        img.convert("RGB").save(path, quality=JPEG_QUALITY, optimize=True)
    elif path.suffix.lower() == ".webp":
        img.save(path, quality=JPEG_QUALITY)
    else:
        img.save(path, optimize=True)


def save_uploads(files) -> list[str]:
    paths = []
    for f in files:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXT:
            raise ValueError(f"File không hỗ trợ: {f.filename}")
        dest = MEDIA_DIR / f"{uuid.uuid4().hex}{ext}"
        f.save(dest)
        if ext in SHRINK_EXT:
            try:
                shrink_image(dest)
            except Exception:  # a photo we can't shrink is still postable as-is
                logging.getLogger("app").warning("Không thu nhỏ được ảnh %s", f.filename, exc_info=True)
        paths.append(str(dest))
    return paths


def load_posts(c, where: str, order: str) -> list[dict]:
    posts = [dict(p) for p in c.execute(f"SELECT * FROM posts WHERE {where} ORDER BY {order} LIMIT 100")]
    by_post = {p["id"]: p for p in posts}
    for p in posts:
        p["deliveries"] = []
        p["n_media"] = len(json.loads(p["images"]))
    if posts:
        rows = c.execute(
            f"SELECT d.*, t.name, t.kind FROM deliveries d JOIN targets t ON t.id = d.target_id "
            f"WHERE d.post_id IN ({','.join('?' * len(posts))}) ORDER BY d.position", list(by_post))
        for d in rows:
            by_post[d["post_id"]]["deliveries"].append(dict(d))
    return posts


def feed_context(c) -> dict:
    return {"queue": load_posts(c, "status IN ('pending', 'running')", "scheduled_at, id"),
            "history": load_posts(c, "status NOT IN ('pending', 'running')", "updated_at DESC"),
            "label": STATUS_LABEL}


@app.get("/")
def index():
    with db() as c:
        targets = c.execute("SELECT * FROM targets ORDER BY kind DESC, name").fetchall()
        ctx = feed_context(c)
    return render_template("index.html", targets=targets, now=worker.now_str(), kind_label=KIND_LABEL, **ctx)


@app.get("/feed")
def feed_partial():
    """Queue + history only, for the page's live refresh (doesn't consume flash messages)."""
    with db() as c:
        return render_template("_feed.html", **feed_context(c))


@app.post("/targets")
def add_target():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    if not name or not is_facebook_url(url):
        flash("Cần tên và URL https://www.facebook.com/...")
    else:
        with db() as c:
            c.execute("INSERT INTO targets(name, kind, url) VALUES (?, ?, ?)", (name, kind_of(url), url))
    return redirect(url_for("index"))


@app.post("/targets/<int:tid>/delete")
def delete_target(tid: int):
    with db() as c:
        if c.execute("SELECT 1 FROM deliveries d JOIN posts p ON p.id = d.post_id "
                     "WHERE d.target_id = ? AND p.status IN ('pending','running')", (tid,)).fetchone():
            flash("Đích này còn bài đang chờ — xoá bài trước.")
        else:
            c.execute("DELETE FROM targets WHERE id = ?", (tid,))
    return redirect(url_for("index"))


@app.post("/posts")
def add_post():
    content = request.form.get("content", "").strip()
    target_ids = [int(t) for t in request.form.getlist("target_ids") if t.isdigit()]
    when = request.form.get("when") or worker.now_str()
    only_me = request.form.get("only_me") == "1"
    try:
        datetime.strptime(when, worker.TIME_FMT)
    except ValueError:
        flash("Giờ đăng không hợp lệ.")
        return redirect(url_for("index"))
    if not content or not target_ids:
        flash("Cần nội dung và ít nhất 1 đích đăng.")
        return redirect(url_for("index"))
    try:
        images = save_uploads(request.files.getlist("images"))
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("index"))

    with db() as c:
        kinds = {r["id"]: r["kind"] for r in c.execute("SELECT id, kind FROM targets")}
        ordered = [t for t in target_ids if t in kinds]
        if not ordered:
            flash("Đích đăng không tồn tại.")
            return redirect(url_for("index"))
        if only_me and any(kinds[t] != "profile" for t in ordered):
            flash("“Chỉ mình tôi” chỉ dùng được cho Trang cá nhân — Page/Group luôn hiển thị theo quyền của Page/Group.")
            return redirect(url_for("index"))
        pid = c.execute("INSERT INTO posts(content, images, scheduled_at, only_me, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (content, json.dumps(images), when, int(only_me), worker.now_str())).lastrowid
        c.executemany("INSERT INTO deliveries(post_id, target_id, position) VALUES (?, ?, ?)",
                      [(pid, t, i) for i, t in enumerate(ordered)])
    worker.wake()
    flash(f"Đã xếp bài lên {len(ordered)} đích, chạy lúc {when.replace('T', ' ')}.")
    return redirect(url_for("index"))


def _update_post(pid: int, sqls: list[str], allowed: tuple[str, ...], msg: str):
    with db() as c:
        row = c.execute("SELECT status FROM posts WHERE id = ?", (pid,)).fetchone()
        if row and row["status"] in allowed:
            for sql in sqls:
                c.execute(sql, {"id": pid, "now": worker.now_str()})
        else:
            flash(msg)
    worker.wake()
    return redirect(url_for("index"))


@app.post("/posts/<int:pid>/delete")
def delete_post(pid: int):
    return _update_post(pid, ["DELETE FROM posts WHERE id = :id"],
                        ("pending", "failed", "partial", "done"), "Không xoá được bài đang chạy.")


@app.post("/posts/<int:pid>/now")
def run_now(pid: int):
    return _update_post(pid, ["UPDATE posts SET scheduled_at = :now WHERE id = :id"],
                        ("pending",), "Chỉ chạy ngay được bài đang chờ.")


@app.post("/posts/<int:pid>/retry")
def retry_post(pid: int):
    # Only failed targets re-run; already-posted ones are left alone (no duplicates).
    return _update_post(pid, [
        "UPDATE deliveries SET status = 'pending', error = NULL WHERE post_id = :id AND status = 'failed'",
        "UPDATE posts SET status = 'pending', error = NULL, scheduled_at = :now, updated_at = :now WHERE id = :id",
    ], ("failed", "partial"), "Chỉ thử lại được bài lỗi.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(BASE / "bot.log", encoding="utf-8")])
    init()
    MEDIA_DIR.mkdir(exist_ok=True)
    worker.start()
    app.run(host="127.0.0.1", port=5050)
