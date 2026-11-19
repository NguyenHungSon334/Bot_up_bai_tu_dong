@echo off
rem Open the bot's Chrome (same profile + control port the bot uses) to log in to Facebook.
rem If it is already open, just use that window. You can leave it open.
cd /d "%~dp0"
python -c "import worker, sys; sys.exit(0 if worker.ensure_chrome() else 1)" || (echo Khong mo duoc Chrome bot & pause)
