$ErrorActionPreference='Stop'
$scutStopState=Join-Path $PSScriptRoot '.local'
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'installed.json')) { $scutStopState=Join-Path $env:LOCALAPPDATA 'SCUTClassroomAssistant\data' }
if ($env:SCUT_ASSISTANT_HOME) { $scutStopState=$env:SCUT_ASSISTANT_HOME }
$scutPidFile=Join-Path $scutStopState 'server.pid'
if (-not (Test-Path -LiteralPath $scutPidFile)) { Write-Host 'No service process started by this project was found.'; exit }
$scutPid=[int](Get-Content -LiteralPath $scutPidFile -Raw)
$scutProcess=Get-CimInstance Win32_Process -Filter "ProcessId = $scutPid"
$scutExpected=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '.venv\Scripts\python.exe'))
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'runtime\python.exe')) { $scutExpected=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'runtime\python.exe')) }
if ($scutProcess -and $scutProcess.ExecutablePath -eq $scutExpected -and $scutProcess.CommandLine -match 'assistant_service') {
    $scutChildren=Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $scutPid -and $_.CommandLine -match 'assistant_service' }
    $scutChildren | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
    Stop-Process -Id $scutPid
    Wait-Process -Id $scutPid -Timeout 10 -ErrorAction SilentlyContinue
    Write-Host 'Local service stopped. Received audio and unfinished work remain saved.'
} elseif ($scutProcess) { throw 'The PID belongs to another process; nothing was stopped.' }
