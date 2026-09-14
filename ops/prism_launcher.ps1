# prism_launcher.ps1 — PRISM 桌面启动器实际逻辑(守护+网页, 双防重复)。
# 桌面 PRISM.bat 只是 4 行 ASCII 薄壳, 指到本文件; 解析健壮性由 PowerShell 保证
# (2026-09-04: 纯 bat 版因 if 块内引号含括号导致 cmd 解析闪退, 弃用)。
$ErrorActionPreference = "Continue"
$repo = "D:\cc-joesph"
Set-Location $repo
$env:PYTHONIOENCODING = "utf-8"

Write-Host "=== PRISM 模拟盘启动器 ==="

# 1) 守护: 已运行则跳过(paper_daemon 命令行匹配)
$daemon = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
          Where-Object { $_.CommandLine -match "paper_daemon" }
if ($daemon) {
    Write-Host ("[i] 模拟盘守护已在运行 (PID {0}), 跳过" -f ($daemon.ProcessId -join ","))
} else {
    Write-Host "[1/2] 启动模拟盘守护..."
    Start-Process cmd -WorkingDirectory $repo -ArgumentList '/k', 'title PRISM-daemon & chcp 65001 >nul & python -m prism.paper_daemon'
}

# 2) 网页: 5000 已监听则跳过
$listen = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    Write-Host ("[i] 网页 GUI 已在运行 (PID {0}), 跳过" -f ($listen.OwningProcess | Select-Object -First 1))
} else {
    Write-Host "[2/2] 启动网页 GUI..."
    Start-Process cmd -WorkingDirectory $repo -ArgumentList '/k', 'title PRISM-web & chcp 65001 >nul & python prism_web\app.py'
    Start-Sleep -Seconds 2
}

Start-Process "http://127.0.0.1:5000"
Write-Host "完成: 守护窗口=模拟盘(关闭=停止), 网页= http://127.0.0.1:5000 (本窗口可关闭)"
Start-Sleep -Seconds 2
