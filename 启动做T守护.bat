@echo off
chcp 65001 >nul
cd /d D:\cc-joesph\tt_solo
set PYTHONIOENCODING=utf-8
title TT Daemon - DRY RUN (no orders)
echo ============================================
echo  TT Strategy Daemon
echo.
echo  MODE: DRY-RUN  -- computes intents only,
echo        writes NO signal file, places NO order.
echo.
echo  To actually emit signals you must:
echo    1) run with --live
echo    2) keep D:/QMT_SIGNALS/paused ABSENT
echo    3) write today's date into
echo       D:/QMT_SIGNALS/real/armed.txt
echo.
echo  Closing this window = stopping the daemon
echo ============================================
python -m ttcore.daemon --interval 5
pause
