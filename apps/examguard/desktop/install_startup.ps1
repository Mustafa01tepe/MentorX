param(
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = 'Stop'

$desktopDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$agentPath = Join-Path $desktopDir 'desktop_agent.py'
$requirementsPath = Join-Path $desktopDir 'requirements.txt'
$venvDir = Join-Path $desktopDir '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$venvPythonw = Join-Path $venvDir 'Scripts\pythonw.exe'

if (-not (Test-Path -LiteralPath $agentPath)) {
    throw "desktop_agent.py bulunamadi: $agentPath"
}

function Find-PythonCommand {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        return @{
            FilePath = $py.Source
            Arguments = @('-3')
        }
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        return @{
            FilePath = $python.Source
            Arguments = @()
        }
    }

    throw 'Python bulunamadi. Once Python 3.12 veya daha yeni bir surum kurun.'
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    $pythonCommand = Find-PythonCommand
    & $pythonCommand.FilePath @($pythonCommand.Arguments) -m venv $venvDir
}

if (-not $SkipDependencyInstall) {
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r $requirementsPath
}

if (-not (Test-Path -LiteralPath $venvPythonw)) {
    throw "pythonw.exe bulunamadi: $venvPythonw"
}

$startupDir = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupDir 'ExamGuard Desktop Agent.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $venvPythonw
$shortcut.Arguments = "`"$agentPath`""
$shortcut.WorkingDirectory = $desktopDir
$shortcut.Description = 'ExamGuard masaustu izleme ajani'
$shortcut.Save()

$existing = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -in @('python.exe', 'pythonw.exe') -and
        $_.CommandLine -like "*desktop_agent.py*"
    }

if (-not $existing) {
    Start-Process `
        -FilePath $venvPythonw `
        -ArgumentList "`"$agentPath`"" `
        -WorkingDirectory $desktopDir `
        -WindowStyle Hidden
}

Write-Host 'ExamGuard Desktop Agent kuruldu ve baslatildi.'
Write-Host "Baslangic kisayolu: $shortcutPath"
Write-Host 'Ajan sinav baslayana kadar sistem tepsisinde bekler.'
