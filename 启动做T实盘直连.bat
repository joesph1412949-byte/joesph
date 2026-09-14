@echo off
chcp 65001 >nul
cd /d D:\cc-joesph
set PYTHONIOENCODING=utf-8
title TT Daemon - DIRECT LIVE (REAL ORDERS!)
echo ================================================================
echo   !! 警告: 这是实盘直连模式, 会真实报单 !!
echo.
echo   条件 (缺一不可):
echo     1) 本脚本已显式 --live
echo     2) D:/QMT_SIGNALS/paused 不存在 (急停开关未按)
echo     3) D:/QMT_SIGNALS/real/armed.txt 含今日日期
echo.
echo   建议先跑 "启动做T直连守护.bat" (dry-run) 确认意图无误,
echo   再运行本脚本。或先双击 "做T-今日放行.bat" 写今日放行条。
echo.
echo   按 Ctrl+C 或关闭窗口 = 停止
echo ================================================================
timeout /t 8 /nobreak >nul
python -m tt.daemon --direct --live --interval 5
pause
