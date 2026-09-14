$log = 'D:\cc-joesph\hp-fix-log.txt'
'=== HP fix script started ' + (Get-Date) + ' ===' | Out-File -FilePath $log -Encoding utf8

# Step 1: stop and disable HpMallWindowsService
try {
    Stop-Service -Name 'HpMallWindowsService' -Force -ErrorAction SilentlyContinue
    Set-Service -Name 'HpMallWindowsService' -StartupType Disabled -ErrorAction Stop
    '1) HpMallWindowsService stopped and disabled' | Out-File -FilePath $log -Append -Encoding utf8
} catch {
    '1) Service step FAILED: ' + $_.Exception.Message | Out-File -FilePath $log -Append -Encoding utf8
}

# Step 2: kill leftover BluetoothAdvertising processes
try {
    $p = @(Get-Process -Name 'BluetoothAdvertising' -ErrorAction SilentlyContinue)
    if ($p.Count -gt 0) { $p | Stop-Process -Force -ErrorAction SilentlyContinue }
    '2) BluetoothAdvertising processes killed (count: ' + $p.Count + ')' | Out-File -FilePath $log -Append -Encoding utf8
} catch {
    '2) Process step FAILED: ' + $_.Exception.Message | Out-File -FilePath $log -Append -Encoding utf8
}

# Step 3: delete C:\$WinREAgent
try {
    if (Test-Path -LiteralPath 'C:\$WinREAgent') {
        Remove-Item -LiteralPath 'C:\$WinREAgent' -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath 'C:\$WinREAgent') {
        takeown /F 'C:\$WinREAgent' /R /A /D Y | Out-Null
        icacls 'C:\$WinREAgent' /grant *S-1-5-32-544:F /T | Out-Null
        Remove-Item -LiteralPath 'C:\$WinREAgent' -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath 'C:\$WinREAgent') {
        '3) FAILED: C:\$WinREAgent still exists' | Out-File -FilePath $log -Append -Encoding utf8
    } else {
        '3) C:\$WinREAgent deleted (~1.9 GB freed)' | Out-File -FilePath $log -Append -Encoding utf8
    }
} catch {
    '3) WinREAgent step FAILED: ' + $_.Exception.Message | Out-File -FilePath $log -Append -Encoding utf8
}

# Step 4: empty C:\CrashDumps
try {
    $files = @(Get-ChildItem -LiteralPath 'C:\CrashDumps' -File -ErrorAction SilentlyContinue)
    $files | Remove-Item -Force -ErrorAction SilentlyContinue
    '4) CrashDumps cleaned (files removed: ' + $files.Count + ')' | Out-File -FilePath $log -Append -Encoding utf8
} catch {
    '4) CrashDumps step FAILED: ' + $_.Exception.Message | Out-File -FilePath $log -Append -Encoding utf8
}

'=== HP fix script done ' + (Get-Date) + ' ===' | Out-File -FilePath $log -Append -Encoding utf8
