@echo off
chcp 65001 >nul
cd /d D:\cc-joesph
set PYTHONIOENCODING=utf-8
title TT Daemon - SIM CHANNEL (QMT simulation)
echo ============================================
echo  TT Strategy Daemon -- SIM CHANNEL
echo.
echo  Signals go to  D:/QMT_SIGNALS/sim/pending/
echo  Consumed by    qmt_signal_bridge_demo.py (inside QMT)
echo.
echo  MODE: DRY-RUN -- computes intents only,
echo        writes NO signal file, places NO order.
echo.
echo  !! SAFETY: the sim bridge does NOT check the
echo  !! account. It places orders on whatever account
echo  !! QMT is currently logged into.
echo  !! Confirm QMT is on your SIMULATION account
echo  !! BEFORE loading qmt_signal_bridge_demo.py.
echo.
echo  To actually emit signals you must:
echo    1) run with --live
echo    2) keep D:/QMT_SIGNALS/paused ABSENT
echo    3) write today's date into
echo       D:/QMT_SIGNALS/sim/armed.txt
echo.
echo  Closing this window = stopping the daemon
echo ============================================
python -m tt.daemon --interval 5 --env sim
pause
