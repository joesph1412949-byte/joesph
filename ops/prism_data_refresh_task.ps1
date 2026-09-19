# 盘后数据刷新 —— Windows 计划任务入口
#
# 职责: 调用同目录的 prism_data_refresh.ps1, 把完整输出留存到 runtime/log/, 并按退出码分级留痕。
# 退出码(透传自 prism.data_refresh): 0=全部段成功 / 1=存在失败段 / 2=存在空数据段
# 分级: 0 = 正常; 2 = 提醒(周末/节假日/数据源封禁时属正常, 不升级为告警); 1 = 告警, 额外写告警旗文件
# 安全性: 本脚本只刷新本地行情缓存, 不产生信号、不读写 D:/QMT_SIGNALS、不做任何下单动作。
#
# ⚠️ 本文件必须保存为 **UTF-8 带 BOM**。Windows PowerShell 5.1 读取无 BOM 的 .ps1 时按 GBK 解码,
#    中文若出现在字符串字面量里, 字节对齐可能吞掉引号 → 直接语法错误(2026-09-19 实测踩过)。
#    另: PS 5.1 的 Tee-Object 没有 -Encoding 参数(那是 PS 6+), 故此处用 .NET 显式写 UTF-8。
#
# 用法:  .\ops\prism_data_refresh_task.ps1 [-DryRun]

param([switch]$DryRun)

$ErrorActionPreference = 'Continue'

$root   = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root 'runtime\log'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

$stamp  = Get-Date -Format 'yyyyMMdd_HHmmss'
$log    = Join-Path $logDir ('data_refresh_{0}.log' -f $stamp)
$failFt = Join-Path $logDir 'data_refresh_FAILED.txt'

# 只保留最近 60 份日志, 防止无限增长
Get-ChildItem $logDir -Filter 'data_refresh_*.log' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -Skip 60 |
    Remove-Item -Force -ErrorAction SilentlyContinue

$passthru = @()
if ($DryRun) { $passthru += '--dry-run' }

$out  = & (Join-Path $PSScriptRoot 'prism_data_refresh.ps1') @passthru 2>&1 | Out-String -Stream
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 0 }

$verdict = switch ($code) {
    0       { 'OK    全段成功' }
    1       { 'FAIL  存在失败段, 需人工处理' }
    2       { 'WARN  存在空数据段(休市/数据源封禁时属正常)' }
    default { '未知   退出码 ' + $code }
}

$lines = @($out) + @('', ('[task] 退出码={0}  {1}  日志={2}' -f $code, $verdict, $log))
$utf8  = New-Object Text.UTF8Encoding($true)
[IO.File]::WriteAllLines($log, [string[]]$lines, $utf8)
$lines | ForEach-Object { Write-Host $_ }

if ($code -eq 1) {
    # 告警旗: 盘后刷新真失败时落一个可被巡检看见的文件
    [IO.File]::WriteAllText($failFt,
        ('盘后数据刷新失败 {0}  退出码=1  详见 {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $log),
        $utf8)
} elseif ($code -eq 0 -and (Test-Path $failFt)) {
    # 恢复全绿才清旗; 空数据(2)不清 —— "未推进"本身不是"已恢复"的证据
    Remove-Item $failFt -Force -ErrorAction SilentlyContinue
}

exit $code
