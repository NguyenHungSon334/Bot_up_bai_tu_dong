"""Background worker: picks due posts one at a time; one Claude run posts to all its targets."""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from db import BASE, db

PROMPT_FILE = BASE / "prompts" / "facebook.md"
LOG_DIR = BASE / "logs"
PROFILE_DIR = BASE / "chrome-profile"
MCP_CONFIG = BASE / "mcp.json"
CHROME_CANDIDATES = [
    Path(os.environ.get(var, "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData")
]
MCP_PACKAGE = "@playwright/mcp@0.0.86"
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
CHROME_BOOT_S = 30

MODEL = "sonnet"
TURNS_PER_TARGET = 40
TIMEOUT_PER_TARGET_S = 10 * 60
POLL_S = 20
TIME_FMT = "%Y-%m-%dT%H:%M"

# Lean Claude runs: every turn re-reads the whole context, so it's kept tiny.
# Measured on a login check: ~59k -> ~6k tokens/turn, $0.26 -> $0.03.
SYSTEM_PROMPT = "Bạn là bot điều khiển trình duyệt bằng tool Playwright. Làm đúng yêu cầu, ngắn gọn."
MCP_TOOLS_NEEDED = {"navigate", "snapshot", "click", "type", "press_key", "file_upload", "wait_for",
                    "take_screenshot", "tabs", "handle_dialog", "hover"}
# Full tool list of the pinned MCP version. Everything not needed is denied: smaller context, and
# no evaluate/run_code_unsafe (arbitrary code) for a model that reads untrusted Facebook pages.
# ponytail: list is per MCP_PACKAGE version; re-check it when bumping the version.
MCP_TOOLS_ALL = (
    "annotate check click close console_clear console_messages cookie_clear cookie_delete cookie_get "
    "cookie_list cookie_set drag drop emulate_media evaluate file_upload fill_form find generate_locator "
    "get_config handle_dialog hide_highlight highlight hover keydown keyup localstorage_clear "
    "localstorage_delete localstorage_get localstorage_list localstorage_set mouse_click_xy mouse_down "
    "mouse_drag_xy mouse_move_xy mouse_up mouse_wheel navigate navigate_back navigate_forward network_clear "
    "network_request network_requests network_state_set pdf_save press_key press_sequentially reload resize "
    "resume route route_list run_code_unsafe select_option sessionstorage_clear sessionstorage_delete "
    "sessionstorage_get sessionstorage_list sessionstorage_set set_storage_state snapshot start_recording "
    "start_tracing start_video stop_recording stop_tracing stop_video storage_state tabs take_screenshot type "
    "uncheck unroute verify_element_visible verify_list_visible verify_text_visible verify_value video_chapter "
    "video_hide_actions video_show_actions wait_for webmcp_call webmcp_list"
).split()
MCP_TOOLS_DENIED = ",".join(f"mcp__playwright__browser_{t}" for t in MCP_TOOLS_ALL if t not in MCP_TOOLS_NEEDED)

RESULT_RE = re.compile(r"RESULT:\s*(\[.*\])")
VALID_STATUS = {"done", "pending_approval", "failed"}
OK_STATUS = {"done", "pending_approval"}

log = logging.getLogger("worker")
_wake = threading.Event()


def wake():
    """Called by the UI when a post becomes due now: skip the poll wait."""
    _wake.set()


def now_str() -> str:
    return datetime.now().strftime(TIME_FMT)


def parse_results(text: str, delivery_ids: list[int]) -> dict[int, dict]:
    """Map each delivery id -> {status, post_url, error}. Anything missing counts as failed."""
    missing = {"status": "failed", "post_url": None, "error": "Claude không báo kết quả cho đích này"}
    results = {i: dict(missing) for i in delivery_ids}
    matches = RESULT_RE.findall(text or "")
    if not matches:
        return results
    try:
        items = json.loads(matches[-1])
    except json.JSONDecodeError:
        return results
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("id") not in results:
            continue
        status = item.get("status")
        results[item["id"]] = {
            "status": status if status in VALID_STATUS else "failed",
            "post_url": item.get("post_url"),
            "error": item.get("error") if status in VALID_STATUS else f"Status lạ: {status}",
        }
    return results


def overall_status(statuses: list[str]) -> str:
    ok = sum(s in OK_STATUS for s in statuses)
    if ok == len(statuses):
        return "done"
    return "failed" if ok == 0 else "partial"


KIND_LABEL = {"group": "Group", "page": "Page", "profile": "Trang cá nhân"}


def build_prompt(post, deliveries) -> str:
    targets = "\n".join(
        f"{n}. id={d['id']} | {KIND_LABEL[d['kind']]} | {d['name']} | {d['url']}"
        for n, d in enumerate(deliveries, 1))
    images = "\n".join(f"- {p}" for p in json.loads(post["images"])) or "(không có)"
    privacy = ("CHỈ MÌNH TÔI — bắt buộc đặt đối tượng \"Chỉ mình tôi\" / \"Only me\" trước khi đăng"
               if post["only_me"] else "mặc định (không đổi)")
    return (
        PROMPT_FILE.read_text(encoding="utf-8")
        .replace("{{TARGETS}}", targets)
        .replace("{{IMAGES}}", images)
        .replace("{{PRIVACY}}", privacy)
        .replace("{{POST_ID}}", str(post["id"]))
        # content last: placeholders typed by the user inside the post stay literal
        .replace("{{CONTENT}}", post["content"])
    )


def chrome_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2):
            return True
    except OSError:
        return False


# Reads the bot Chrome's (decrypted) cookies over CDP. Node 22+ has a global WebSocket.
FB_LOGIN_CHECK_JS = """
const v = await (await fetch(process.argv[1])).json();
const ws = new WebSocket(v.webSocketDebuggerUrl);
ws.onopen = () => ws.send(JSON.stringify({ id: 1, method: 'Storage.getCookies' }));
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.id !== 1) return;
  const cookies = msg.result?.cookies || [];
  const ok = cookies.some((c) => c.name === 'c_user' && c.value && c.domain.endsWith('facebook.com'));
  process.exit(ok ? 0 : 3);
};
ws.onerror = () => process.exit(4);
setTimeout(() => process.exit(5), 10000);
"""


def facebook_logged_in() -> bool | None:
    """True/False from the bot Chrome's cookies; None when it can't be checked."""
    node = shutil.which("node")
    if not node or not chrome_alive():
        return None
    try:
        r = subprocess.run([node, "--input-type=module", "-e", FB_LOGIN_CHECK_JS, f"{CDP_URL}/json/version"],
                           capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None
    return {0: True, 3: False}.get(r.returncode)


def find_chrome() -> Path | None:
    return next((p for p in CHROME_CANDIDATES if p.is_file()), None)


def ensure_chrome() -> bool:
    """Bot Chrome stays open between runs; relaunch it if it was closed."""
    if chrome_alive():
        return True
    chrome = find_chrome()
    if chrome is None:
        log.error("Không tìm thấy chrome.exe")
        return False
    log.info("Chrome bot chưa mở -> mở lại")
    # `cmd /c start` orphans Chrome: it survives the bot being stopped/killed (taskkill /T).
    subprocess.Popen(["cmd", "/c", "start", "", str(chrome), f"--remote-debugging-port={CDP_PORT}",
                      f"--user-data-dir={PROFILE_DIR}", "--no-first-run",
                      "https://www.facebook.com"],
                     creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(CHROME_BOOT_S):
        time.sleep(1)
        if chrome_alive():
            return True
    return False


def write_mcp_config():
    # Windows needs `cmd /c` to launch npx-based MCP servers.
    # Pinned: an unattended bot shouldn't pick up breaking MCP updates on its own.
    # --cdp-endpoint attaches to the always-open bot Chrome instead of launching a new one.
    config = {"mcpServers": {"playwright": {
        "command": "cmd",
        "args": ["/c", "npx", "-y", MCP_PACKAGE,
                 "--cdp-endpoint", CDP_URL,
                 # Big uploads (45 MB took > 30s) must not time out: a timeout made Claude
                 # re-upload -> 11 files in the composer instead of 5.
                 "--timeout-action", "180000",
                 # Screenshots are for our logs only; don't feed images back to the model.
                 "--image-responses", "omit",
                 "--output-dir", str(LOG_DIR / "shots")],
    }}}
    MCP_CONFIG.write_text(json.dumps(config, indent=2), encoding="utf-8")


def run_claude(prompt: str, run_id: int, n_targets: int = 1) -> dict:
    """Returns {"text", "cost", "error"}; error set means the run itself broke."""
    exe = shutil.which("claude")
    if not exe:
        return {"text": "", "cost": None, "error": "Không tìm thấy lệnh claude trong PATH"}
    if not ensure_chrome():
        return {"text": "", "cost": None, "error": "Không mở được Chrome bot. Nếu cửa sổ Chrome bot đang "
                                                   "mở không qua bot/login.bat, đóng hết rồi thử lại."}
    cmd = [exe, "-p",
           "--mcp-config", str(MCP_CONFIG), "--strict-mcp-config",
           "--tools", "",  # no built-in tools (Bash/Read/Edit...): ~26k tokens of definitions gone
           "--allowedTools", "mcp__playwright",
           "--disallowedTools", MCP_TOOLS_DENIED,
           "--setting-sources", "project",  # skip the user's global plugins/hooks
           "--disable-slash-commands", "--no-session-persistence",
           "--system-prompt", SYSTEM_PROMPT,
           "--model", MODEL, "--max-turns", str(TURNS_PER_TARGET * n_targets),
           # stream-json: one line per message, so the log gets a timestamp per tool call/result.
           "--output-format", "stream-json", "--verbose"]
    timeout_s = TIMEOUT_PER_TARGET_S * n_targets
    killed = threading.Event()

    def kill_tree():
        # Kill claude + MCP tree. Chrome is not in this tree, so it stays open.
        killed.set()
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)

    payload = None
    with open(LOG_DIR / f"post_{run_id}.log", "w", encoding="utf-8") as logf:
        # Prompt goes via stdin: avoids cmd.exe mangling newlines/quotes in post content.
        proc = subprocess.Popen(cmd, cwd=BASE, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=logf,
                                text=True, encoding="utf-8", errors="replace")
        timer = threading.Timer(timeout_s, kill_tree)
        timer.start()
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
            for line in proc.stdout:
                logf.write(f"{datetime.now():%H:%M:%S} {line}")
                logf.flush()
                if '"type":"result"' in line:
                    payload = json.loads(line)
            proc.wait()
        finally:
            timer.cancel()

    if killed.is_set():
        return {"text": "", "cost": None, "error": "Quá thời gian — kiểm tra FB xem đã đăng chưa trước khi thử lại"}
    if payload is None:
        return {"text": "", "cost": None, "error": f"claude lỗi (exit {proc.returncode}), xem logs/post_{run_id}.log"}
    return {"text": payload.get("result", ""), "cost": payload.get("total_cost_usd"), "error": None}


def claim_due_post():
    """Returns (post, deliveries) or None. Only pending deliveries run (retry skips posted ones)."""
    with db() as c:
        post = c.execute("SELECT * FROM posts WHERE status = 'pending' AND scheduled_at <= ? "
                         "ORDER BY scheduled_at, id LIMIT 1", (now_str(),)).fetchone()
        if post is None:
            return None
        c.execute("UPDATE posts SET status = 'running', error = NULL, updated_at = ? WHERE id = ?",
                  (now_str(), post["id"]))
        deliveries = c.execute(
            "SELECT d.*, t.kind, t.url, t.name FROM deliveries d JOIN targets t ON t.id = d.target_id "
            "WHERE d.post_id = ? AND d.status = 'pending' ORDER BY d.position", (post["id"],)).fetchall()
        return post, deliveries


def finish(post_id: int, results: dict[int, dict], cost: float | None, error: str | None):
    with db() as c:
        for did, r in results.items():
            c.execute("UPDATE deliveries SET status = ?, post_url = ?, error = ? WHERE id = ?",
                      (r["status"], r.get("post_url"), error or r.get("error"), did))
        statuses = [r["status"] for r in c.execute(
            "SELECT status FROM deliveries WHERE post_id = ?", (post_id,))]
        c.execute("UPDATE posts SET status = ?, error = ?, cost = COALESCE(cost, 0) + COALESCE(?, 0), "
                  "updated_at = ? WHERE id = ?",
                  (overall_status(statuses), error, cost, now_str(), post_id))


def process(post, deliveries):
    ids = [d["id"] for d in deliveries]
    if not ids:
        finish(post["id"], {}, None, "Không có đích nào cần đăng")
        return
    log.info("post %s -> %d đích", post["id"], len(ids))
    if ensure_chrome() and facebook_logged_in() is False:
        # Fail fast: no point spending a Claude run on a logged-out browser.
        finish(post["id"], parse_results("", ids), None,
               "login_required — Facebook chưa đăng nhập, chạy login.bat rồi bấm Thử lại")
        return
    run = run_claude(build_prompt(post, deliveries), post["id"], len(ids))
    results = parse_results(run["text"], ids)
    finish(post["id"], results, run["cost"], run["error"])
    log.info("post %s: %s", post["id"], {k: v["status"] for k, v in results.items()})


def recover_interrupted():
    # Never auto-requeue: the post may already be live -> duplicate post.
    msg = "Bị ngắt giữa chừng — kiểm tra FB trước khi thử lại"
    with db() as c:
        running = [r["id"] for r in c.execute("SELECT id FROM posts WHERE status = 'running'")]
        for pid in running:
            c.execute("UPDATE deliveries SET status = 'failed', error = ? "
                      "WHERE post_id = ? AND status = 'pending'", (msg, pid))
            statuses = [r["status"] for r in c.execute(
                "SELECT status FROM deliveries WHERE post_id = ?", (pid,))]
            c.execute("UPDATE posts SET status = ?, error = ? WHERE id = ?",
                      (overall_status(statuses), msg, pid))


def loop():
    recover_interrupted()
    while True:
        claimed = None
        try:
            claimed = claim_due_post()
            if claimed is None:
                _wake.wait(POLL_S)
                _wake.clear()
                continue
            process(*claimed)
        except Exception as e:  # keep the worker alive no matter what
            log.exception("worker error")
            if claimed is not None:
                post, deliveries = claimed
                finish(post["id"], parse_results("", [d["id"] for d in deliveries]), None, f"Lỗi worker: {e}")
            time.sleep(POLL_S)


def start():
    LOG_DIR.mkdir(exist_ok=True)
    write_mcp_config()
    ensure_chrome()
    threading.Thread(target=loop, daemon=True, name="worker").start()
