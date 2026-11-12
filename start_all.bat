@echo off
cd /d "%~dp0"

rem Backend image contains API source (not a bind mount), so --build is required
rem to load newly added routes such as the read-only RAG assistant.
docker compose up -d --build mysql redis backend
if errorlevel 1 goto :failed

rem Worker mounts backend/app but OCR voucher evaluation also depends on image/font packages.
docker compose up -d --build --force-recreate worker
if errorlevel 1 goto :failed

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1"
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo [ERROR] Services failed to start. Keep this window open and check the message above.
pause
exit /b 1
