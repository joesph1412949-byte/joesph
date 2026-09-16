@echo off
chcp 65001 >nul
title TT - Arm Today
set PYTHONIOENCODING=utf-8
echo ================================================================
echo  做T - 今日放行 (arm)
echo.
echo  作用: 往 D:/QMT_SIGNALS/real/armed.txt 写入今天的日期。
echo        守护进程每天都必须有一张"当日放行条"才会真正下单,
echo        昨天的条今天自动失效 -- 相当于每天一道人工确认。
echo.
echo  同时确保急停开关 D:/QMT_SIGNALS/paused 不存在。
echo ================================================================
python "D:\cc-joesph\tt_solo\ttcore\arm_today.py" %*
echo.
pause
