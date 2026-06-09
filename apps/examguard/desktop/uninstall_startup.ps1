$ErrorActionPreference = 'Stop'

$startupDir = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupDir 'ExamGuard Desktop Agent.lnk'

if (Test-Path -LiteralPath $shortcutPath) {
    Remove-Item -LiteralPath $shortcutPath -Force
}

$processes = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -in @('python.exe', 'pythonw.exe') -and
        $_.CommandLine -like "*desktop_agent.py*"
    }

foreach ($process in $processes) {
    Stop-Process -Id $process.ProcessId -Force
}

Write-Host 'ExamGuard Desktop Agent otomatik baslatmadan kaldirildi.'
