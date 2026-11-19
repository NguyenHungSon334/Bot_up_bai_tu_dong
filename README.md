# Bot đăng bài Facebook tự động

Giao diện web xếp bài → đến giờ, Claude Code điều khiển Chrome (đã đăng nhập sẵn) để đăng lên Page/Group.

## Cài trên máy mới (Windows)

1. Copy thư mục dự án sang, **trừ**: `chrome-profile/`, `logs/`, `media/`, `mcp.json`, `bot.log`, `.mcp-*`.
   (`bot.db` copy nếu muốn giữ danh sách đích + lịch sử, không thì bỏ.)
2. Chạy `run.bat`. Lần đầu nó tự kiểm tra và cài cái còn thiếu (qua winget/npm/pip):
   Python, Node.js, Google Chrome, Claude Code, Flask, Playwright MCP.
   - Claude Code chưa đăng nhập → tự mở trang đăng nhập.
   - Chưa có Chrome bot → tự mở Chrome để bạn đăng nhập Facebook, xong nhấn Enter.
   - Cài xong mà báo "chưa nhận" → đóng cửa sổ, chạy lại `run.bat`.
3. Giao diện mở ở http://127.0.0.1:5050. Để cửa sổ `run.bat` chạy suốt.

Chỉ kiểm tra, không chạy bot: `powershell -ExecutionPolicy Bypass -File start.ps1 -CheckOnly`

## Dùng hằng ngày
- Bật máy → chạy `run.bat` (Chrome bot tự mở nếu chưa mở).
- Chrome bot lỡ đóng → không sao, tới giờ đăng bot tự mở lại.
- Facebook bị đăng xuất / checkpoint → bài báo lỗi `login_required`/`checkpoint` → chạy `login.bat` đăng nhập lại.

## Lưu ý
- `chrome-profile/` chứa phiên đăng nhập Facebook: không copy, không chia sẻ. Cookie mã hoá theo user Windows nên copy sang máy khác cũng không dùng được.
- Cổng 9222 (điều khiển Chrome) chỉ mở trên máy này, nhưng mọi chương trình trên máy đều dùng được nó.
- Sửa cách AI đăng bài: `prompts/facebook.md`. Đổi model: `MODEL` trong `worker.py`.
- Log từng bài: `logs/post_<id>.log`, ảnh chụp: `logs/shots/`.
