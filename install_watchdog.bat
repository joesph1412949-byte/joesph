@echo off
REM 注册 watchdog 为"开机自启"任务计划(需管理员运行一次)
REM 用法: 右键"以管理员身份运行"本文件
chcp 65001 >nul
setlocal

set PY=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe
set PY312=C:\Users\28037\AppData\Local\Programs\Python\Python312\python.exe
set SCRIPT=D:\cc-joesph\ops\watchdog.py
set TASK=QMT_Quant_Watchdog

echo [1/2] 创建任务计划: %TASK%
REM 2026-09-19 修正: 原来 /TR 里写的是 cmd 的 `cd /d D:\cc-joesph`, 而目标解释器
REM 是 PowerShell → 实测报"找不到接受实际参数'D:\cc-joesph'的位置形式参数",
REM 随后 `python ops\watchdog.py` 会以计划任务默认 cwd(System32)执行 → 找不到脚本。
REM 现: 绝对路径 python + 脚本, 不切目录 —— watchdog.py 自己 sys.path.insert
REM 项目根, cwd 无关(已实测从 System32 起 rc=0)。
REM 注: 两个路径都没有空格, 所以 -Command 里的路径不加引号。若要给它们加引号必须
REM 用 PowerShell 的调用运算符 `&`, 而 cmd 的引号配对规则会把那个 & 当成命令分隔符
REM (实测: /TR 的值被截断在 `-Command "` 处, 后半段被 cmd 当成新命令执行)。
schtasks /Create /TN %TASK% /SC ONSTART /RU "%USERNAME%" /RL LIMITED /TR "\"%PY%\" -NoProfile -WindowStyle Hidden -Command \"%PY312% %SCRIPT%\"" /F
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
