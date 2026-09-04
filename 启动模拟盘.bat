@echo off
chcp 65001 >nul
cd /d D:\cc-joesph
set PYTHONIOENCODING=utf-8
title PaperDaemon - Paper Trading Daemon
echo ============================================
echo  Paper Trading Daemon
echo  Closing this window = stopping the daemon
echo  (Restart after market open to backfill missed days)
echo ============================================
python -m prism.paper_daemon
pause
