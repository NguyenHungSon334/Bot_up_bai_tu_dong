# Check + install everything the bot needs, then start it.
# Usage: run.bat   (or: powershell -ExecutionPolicy Bypass -File start.ps1 [-CheckOnly])
param([switch]$CheckOnly)

# Not 'Stop': in PS 5.1 any stderr line from a native exe would abort the script.
# Failures are checked via $LASTEXITCODE and raised with `throw`.
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'
Set-Location $PSScriptRoot

function Write-Step($msg) { Write-Host "-> $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "   OK  $msg" -ForegroundColor Green }

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Test-Command($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

# The Windows Store "python" stub exists even when Python is not installed, so run it.
function Test-Python {
    try {
        & python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Install-WithWinget($id, $label, $extra = @()) {
    if (-not (Test-Command winget)) {
        throw "Thiếu $label và máy không có winget. Cài $label thủ công rồi chạy lại."
    }
    Write-Step "Cài $label (có thể hiện hộp thoại xin quyền, bấm Yes)..."
    & winget install -e --id $id --accept-source-agreements --accept-package-agreements --silent @extra
    Update-SessionPath
}

function Find-Chrome {
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if ($root -and (Test-Path "$root\Google\Chrome\Application\chrome.exe")) { return $true }
    }
    return $false
}

# 1. Python
Write-Step 'Kiểm tra Python'
if (-not (Test-Python)) {
    Install-WithWinget 'Python.Python.3.12' 'Python 3.12' @('--override', '/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1')
    if (-not (Test-Python)) { throw 'Cài Python xong nhưng chưa nhận. Đóng cửa sổ này rồi chạy lại run.bat.' }
}
Write-Ok (& python --version)

# 2. Node.js (npx runs the Playwright MCP server)
Write-Step 'Kiểm tra Node.js'
if (-not (Test-Command npx)) {
    Install-WithWinget 'OpenJS.NodeJS.LTS' 'Node.js LTS'
    if (-not (Test-Command npx)) { throw 'Cài Node.js xong nhưng chưa nhận. Đóng cửa sổ này rồi chạy lại run.bat.' }
}
Write-Ok "Node $(& node --version)"

# 3. Google Chrome
Write-Step 'Kiểm tra Google Chrome'
if (-not (Find-Chrome)) {
    Install-WithWinget 'Google.Chrome' 'Google Chrome'
    if (-not (Find-Chrome)) { throw 'Không tìm thấy chrome.exe sau khi cài.' }
}
Write-Ok 'Chrome'

# 4. Claude Code + login
Write-Step 'Kiểm tra Claude Code'
if (-not (Test-Command claude)) {
    Write-Step 'Cài Claude Code...'
    & npm install -g @anthropic-ai/claude-code
    Update-SessionPath
    if (-not (Test-Command claude)) { throw 'Cài Claude Code xong nhưng chưa nhận. Đóng cửa sổ này rồi chạy lại run.bat.' }
}
$auth = (& claude auth status --json 2>$null | Out-String)
if (-not ($auth -match '"loggedIn":\s*true')) {
    Write-Step 'Claude Code chưa đăng nhập -> mở trang đăng nhập...'
    & claude auth login
    $auth = (& claude auth status --json 2>$null | Out-String)
    if (-not ($auth -match '"loggedIn":\s*true')) { throw 'Claude Code vẫn chưa đăng nhập.' }
}
Write-Ok 'Claude Code đã đăng nhập'

# 5. Python packages
Write-Step 'Kiểm tra thư viện Python'
& python -c "import flask, PIL" 2>$null
if ($LASTEXITCODE -ne 0) {
    & python -m pip install --disable-pip-version-check -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'pip install thất bại.' }
}
Write-Ok 'Flask, Pillow'

# 6. Playwright MCP: download once so the first post isn't slow
$mcp = (& python -c "import worker; print(worker.MCP_PACKAGE)").Trim()
$marker = Join-Path $PSScriptRoot ".mcp-$($mcp -replace '[^\w.-]', '_')"
if (-not (Test-Path $marker)) {
    Write-Step "Tải $mcp (lần đầu)..."
    & cmd /c npx -y $mcp --version
    if ($LASTEXITCODE -ne 0) { throw "Tải $mcp thất bại (kiểm tra mạng)." }
    New-Item -ItemType File $marker | Out-Null
}
Write-Ok $mcp

# 7. Facebook login: checked from the bot Chrome's real cookies every start
#    (new machine, expired session, copied profile -> all caught here).
Write-Step 'Kiểm tra đăng nhập Facebook'
& python -c "import worker, sys; sys.exit(0 if worker.ensure_chrome() else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Không mở được Chrome bot.' }
while ($true) {
    & python -c "import worker, sys; sys.exit({True: 0, False: 1}.get(worker.facebook_logged_in(), 2))"
    if ($LASTEXITCODE -eq 0) { Write-Ok 'Facebook đã đăng nhập'; break }
    if ($LASTEXITCODE -eq 2) {
        Write-Host '   !!  Không kiểm tra được đăng nhập Facebook, bỏ qua' -ForegroundColor Yellow
        break
    }
    Write-Host ''
    Write-Host '   Facebook CHƯA ĐĂNG NHẬP.' -ForegroundColor Yellow
    Write-Host '   Đăng nhập trong cửa sổ Chrome bot vừa mở (đăng nhập xong cứ để Chrome mở).'
    Read-Host '   Xong nhấn Enter để kiểm tra lại'
}

Write-Host ''
Write-Host 'Mọi thứ sẵn sàng.' -ForegroundColor Green
if ($CheckOnly) { exit 0 }

Start-Process 'http://127.0.0.1:5050'
& python app.py
exit $LASTEXITCODE
