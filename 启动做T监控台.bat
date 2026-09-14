@echo off
chcp 65001 >nul
cd /d D:\cc-joesph
set PYTHONIOENCODING=utf-8
title TT Monitor - Trading Panel (READ ONLY)
echo ============================================
echo  TT Monitor Panel  (localhost only)
echo  http://127.0.0.1:5010
echo  Read-only view. It can PAUSE, never place orders.
echo  Closing this window = stopping the panel
echo ============================================
python tt_web\app.py
pause
