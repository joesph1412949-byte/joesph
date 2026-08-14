@echo off
chcp 65001 >nul
setlocal
echo [restart] 正在停止占用 8899 端口的旧后端进程...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8899" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1 && echo    已停止 PID %%a
)
timeout /t 1 /nobreak >nul
echo [restart] 正在启动 Vibe-Trading 后端 (端口 8899)...
cd /d D:\Vibe-Trading
start "vibe-backend" "D:\Vibe-Trading\.venv\Scripts\vibe-trading.exe" serve --port 8899
echo [restart] 完成。前端 http://localhost:5899 会自动重连后端。
pause
