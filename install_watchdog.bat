@echo off
REM 注册 watchdog 为"开机自启"任务计划(需管理员运行一次)
REM 用法: 右键"以管理员身份运行"本文件
chcp 65001 >nul
setlocal

set PY=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe
set SCRIPT=D:\cc-joesph\watchdog.py
set TASK=QMT_Quant_Watchdog

echo [1/2] 创建任务计划: %TASK%
schtasks /Create /TN %TASK% /SC ONSTART /RU "%USERNAME%" /RL LIMITED /TR "\"%PY%\" -NoProfile -WindowStyle Hidden -Command \"cd /d D:\cc-joesph; python watchdog.py\"" /F
if errorlevel 1 (
    echo    创建失败, 请确认已用管理员身份运行。
    pause
    exit /b 1
)

echo [2/2] 立即运行一次(验证)
schtasks /Run /TN %TASK%
echo.
echo 完成。watchdog 将在每次开机后自动运行, 监控 5000/8899/5899 端口。
echo 如需卸载: 管理员运行  schtasks /Delete /TN %TASK% /F
pause
