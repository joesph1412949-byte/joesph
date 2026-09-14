@echo off
chcp 65001 >nul
cd /d D:\cc-joesph
set PYTHONIOENCODING=utf-8
title TT Daemon - DIRECT (dry-run, no real orders)
echo ================================================================
echo  TT 做T守护 - 直连模式 (miniQMT 外部 Python 直下)
echo.
echo  模式: DRY-RUN  ==^> 只算不发单, 不会动你的钱和股票
echo.
echo  直连模式说明:
echo    - 用外部 Python 的 xtquant 直接报单, 不经过信号文件桥
echo    - 需要 QMT 客户端已登录 (miniQMT 在运行)
echo    - 本窗口关闭 = 守护停止
echo.
echo  ---------------------------------------------------------------
echo   想真正下单? 改用: 启动做T实盘直连.bat  (需先跑 做T-今日放行.bat)
echo  ---------------------------------------------------------------
echo ================================================================
python -m tt.daemon --direct --interval 5
pause
