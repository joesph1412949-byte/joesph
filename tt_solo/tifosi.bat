@echo off
chcp 65001 >nul
rem ================================================================
rem  tifosi  --  做T 策略统一入口, 唯一启动文件
rem
rem  做T 策略自 2026-09-16 起是自包含项目 tt_solo/
rem  原先 6 个 启动做T*.bat 与 做T-今日放行.bat 已并入本文件。
rem  桌面上的 tifosi.bat 只是薄壳, 指向本文件。
rem
rem  要点:
rem    * 长驻进程一律另开新窗口, 所以菜单不被占用,
rem      关掉菜单不会杀掉守护; 关那个新窗口才是停守护。
rem    * 工作目录与环境变量在顶部设一次, 子进程自动继承。
rem    * 真报单的 --live 放在高级子菜单, 并要求再按一次 Y 确认。
rem    * 用 choice.exe 读键, 不用 set /p: set /p 在 chcp 65001 下
rem      读键不可靠, 且非法输入会导致菜单空转。choice 天然重试。
rem    * 本文件必须保持 CRLF 行尾, 否则 cmd 会吃掉行首字符。
rem ================================================================
cd /d D:\cc-joesph\tt_solo
set PYTHONIOENCODING=utf-8

:menu
cls
echo ================================================================
echo   tifosi  --  做T 策略统一入口
echo ================================================================
echo.
echo   长驻, 各开新窗口; 关那个窗口 = 停止
echo     1  做T守护         演练 DRY-RUN, 信号文件通道
echo     2  做T直连守护     演练 DRY-RUN, miniQMT 外部直连
echo     3  做T模拟守护     演练 DRY-RUN, sim 通道
echo     4  做T仪表盘       只读看板 + 急停/放行   http://127.0.0.1:5011
echo.
echo   一次性
echo     5  今日放行         写 D:/QMT_SIGNALS/real/armed.txt
echo     6  查看闸门状态
echo.
echo     A  高级选项, 含真报单          0  退出
echo.
choice /c 123456A0 /n /m "选择并回车: "
if errorlevel 255 goto end
if errorlevel 8 goto end
if errorlevel 7 goto advanced
if errorlevel 6 goto show_status
if errorlevel 5 goto arm_today
if errorlevel 4 goto run_panel
if errorlevel 3 goto run_sim
if errorlevel 2 goto run_direct
if errorlevel 1 goto run_dry
goto menu

:run_dry
echo.
echo 启动: 做T守护, DRY-RUN 只算不发单, 不写信号文件。
echo 要真正发信号必须同时满足: 加 --live; D:/QMT_SIGNALS/paused 不存在; armed.txt 含今日日期。
timeout /t 2 /nobreak >nul
start "tifosi - 做T守护 演练" cmd /k "python -m ttcore.daemon --interval 5"
goto menu

:run_direct
echo.
echo 启动: 做T直连守护, DRY-RUN。外部 Python 经 xtquant 直接报单, 不经信号文件桥。
echo 需要 QMT 客户端已登录, 即 miniQMT 在运行。想真正下单请走 A 高级选项。
timeout /t 2 /nobreak >nul
start "tifosi - 做T直连守护 演练" cmd /k "python -m ttcore.daemon --direct --interval 5"
goto menu

:run_sim
echo.
echo !! 安全提醒: sim 桥不校验账户, 它会对 QMT 当前登录的任意账户下单。
echo !! 加载 qmt_signal_bridge_demo.py 之前, 请先确认 QMT 登录的是模拟账户。
echo.
echo 启动: 做T模拟守护, DRY-RUN。信号发往 D:/QMT_SIGNALS/sim/pending/。
timeout /t 3 /nobreak >nul
start "tifosi - 做T模拟守护 sim" cmd /k "python -m ttcore.daemon --interval 5 --env sim"
goto menu

:run_panel
echo.
echo 启动: 做T仪表盘, 只监听 127.0.0.1:5011, 只读看板加急停与放行, 绝不下单。
timeout /t 2 /nobreak >nul
start "tifosi - 做T仪表盘 只读" cmd /k "python dashboard\app.py"
goto menu

:arm_today
cls
echo ================================================================
echo   今日放行
echo ================================================================
echo.
echo   作用: 往 D:/QMT_SIGNALS/real/armed.txt 写入今天的日期。
echo   守护每天都必须有一张当日放行条才会真正下单;
echo   昨天的条今天自动失效, 相当于每天一道人工确认。
echo.
echo   注意: 若已按急停, 本工具会拒绝放行并以退出码 2 结束,
echo   它不会替你解除急停。请先在高级选项里解除, 再回来放行。
echo.
python ttcore\arm_today.py
echo.
pause
goto menu

:show_status
cls
echo ================================================================
echo   闸门状态
echo ================================================================
echo.
python ttcore\arm_today.py --status
echo.
echo   若上面显示不可以, 常见原因:
echo     armed.txt 里是昨天的日期, 用主菜单 5 写今日放行条
echo     paused 文件存在, 在高级选项 2 解除急停
echo.
pause
goto menu

:advanced
cls
echo ================================================================
echo   高级选项
echo ================================================================
echo.
echo     1  实盘直连守护   真实报单 --live, 会再要求按 Y 确认
echo     2  解除急停       删除 D:/QMT_SIGNALS/paused
echo     3  撤销今日放行   删除 armed.txt
echo.
echo     R  返回主菜单
echo.
choice /c 123R /n /m "选择并回车: "
if errorlevel 255 goto end
if errorlevel 4 goto menu
if errorlevel 3 goto disarm
if errorlevel 2 goto resume_gate
if errorlevel 1 goto run_live
goto advanced

:run_live
cls
echo ================================================================
echo   !!  警告: 这是实盘直连模式, 会真实报单  !!
echo ================================================================
echo.
echo   条件, 缺一不可:
echo     1. 本入口将显式加 --live
echo     2. D:/QMT_SIGNALS/paused 不存在, 即急停开关未按
echo     3. D:/QMT_SIGNALS/real/armed.txt 含今日日期
echo.
echo   建议先跑主菜单 2 做T直连守护演练, 确认意图无误再回来开这个。
echo   或先用主菜单 5 写今日放行条。
echo.
echo   按 Ctrl+C 或关闭窗口 = 停止
echo ================================================================
echo.
choice /c YN /n /m "确认要真报单吗? 按 Y 确认, 按 N 放弃: "
if errorlevel 255 goto live_abort
if errorlevel 2 goto live_abort
if errorlevel 1 goto live_go
goto live_abort

:live_go
echo.
echo 8 秒后启动, Ctrl+C 可中断...
timeout /t 8 /nobreak >nul
start "tifosi - 做T实盘直连 真实报单" cmd /k "python -m ttcore.daemon --direct --live --interval 5"
goto menu

:live_abort
echo.
echo 已放弃, 未启动实盘守护。
timeout /t 2 /nobreak >nul
goto advanced

:resume_gate
python ttcore\arm_today.py --resume
pause
goto advanced

:disarm
python ttcore\arm_today.py --disarm
pause
goto advanced

:end
exit /b 0
