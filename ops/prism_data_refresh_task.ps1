# 盘后数据刷新 —— Windows 计划任务入口
#
# 职责: 调用同目录的 prism_data_refresh.ps1, 把完整输出**边跑边**留存到 runtime/log/,
#       并按退出码分级留痕(告警旗 / 降级软旗)。
# 退出码(透传自 prism.data_refresh): 0=全部段成功 / 1=存在失败段 / 2=存在空数据段
# 分级: 0 = 正常; 2 = 提醒(周末/节假日/数据源封禁时属正常, 不升级为告警); 1 = 告警, 额外写告警旗文件
# 安全性: 本脚本只刷新本地行情缓存, 不产生信号、不读写 D:/QMT_SIGNALS、不做任何下单动作。
#
# ⚠️ 本文件必须保存为 **UTF-8 带 BOM**。Windows PowerShell 5.1 读取无 BOM 的 .ps1 时按 GBK 解码,
#    中文若出现在字符串字面量里, 字节对齐可能吞掉引号 → 直接语法错误(2026-09-19 实测踩过)。
#    另: PS 5.1 的 Tee-Object 没有 -Encoding 参数(那是 PS 6+); 且本脚本**刻意不缓冲**输出 ——
#    2026-09-19 实测 --build-sectors 在东财被封时单次尝试就要 >16 分钟, 缓冲式实现会让整个运行期
#    不可观测(卡住了连卡在哪一段都看不到), 故改为逐行实时落盘。
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
$degFt  = Join-Path $logDir 'data_refresh_DEGRADED.txt'
$utf8   = New-Object Text.UTF8Encoding($true)
$nl     = [Environment]::NewLine

# 只保留最近 60 份日志, 防止无限增长
Get-ChildItem $logDir -Filter 'data_refresh_*.log' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -Skip 60 |
    Remove-Item -Force -ErrorAction SilentlyContinue

$passthru = @()
if ($DryRun) { $passthru += '--dry-run' }

[IO.File]::WriteAllText($log, ('== 盘后数据刷新开始 {0} =={1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $nl), $utf8)

# 逐行流式落盘: 长任务(实测单段 >16 分钟)运行期间必须可观测。
$stream = New-Object Collections.Generic.List[string]
& (Join-Path $PSScriptRoot 'prism_data_refresh.ps1') @passthru 2>&1 | ForEach-Object {
    $line = [string]$_
    $stream.Add($line)
    Write-Host $line
    [IO.File]::AppendAllText($log, $line + $nl, $utf8)
}
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 0 }

$verdict = switch ($code) {
    0       { 'OK    全段成功' }
    1       { 'FAIL  存在失败段, 需人工处理' }
    2       { 'WARN  存在空数据段(休市/数据源封禁时属正常)' }
    default { '未知   退出码 ' + $code }
}

# 降级检测: data_refresh 降级成功时聚合码仍为 0(源降级是有意的容错, 不是故障), 故必须单独留痕,
# 否则"天天降级"会静默成常态。软旗与告警旗分开: 软旗不惊动人, 但巡检可见。
# 注意: 这里匹配的是 data_refresh 的输出用词, 与它的文案耦合 —— 改文案要同步改这里。
$degraded = [bool](($stream -join $nl) -match '降级')

@('',
  ('[task] 退出码={0}  {1}  结束于 {2}' -f $code, $verdict, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')),
  ('[task] 日志: {0}' -f $log)) | ForEach-Object {
    Write-Host $_
    [IO.File]::AppendAllText($log, $_ + $nl, $utf8)
}

if ($code -eq 1) {
    # 告警旗: 盘后刷新真失败时落一个可被巡检看见的文件
    [IO.File]::WriteAllText($failFt,
        ('盘后数据刷新失败 {0}  退出码=1  详见 {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $log),
        $utf8)
} elseif ($code -eq 0 -and (Test-Path $failFt)) {
    # 恢复"无失败"才清告警旗; 空数据(2)不清 —— "未推进"本身不是"已恢复"的证据
    Remove-Item $failFt -Force -ErrorAction SilentlyContinue
}

if ($degraded) {
    # 软旗: 本次走到了降级源。只有"既无失败又无降级"的成功才清它。
    [IO.File]::WriteAllText($degFt,
        ('盘后数据刷新走了降级源(数据已刷到, 但主源不可用) {0}  退出码={1}  详见 {2}' -f
            (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $code, $log),
        $utf8)
} elseif ($code -eq 0 -and (Test-Path $degFt)) {
    Remove-Item $degFt -Force -ErrorAction SilentlyContinue
}

exit $code
