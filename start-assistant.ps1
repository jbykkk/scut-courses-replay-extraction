param([switch]$Foreground, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$scutRoot = $PSScriptRoot
Set-Location -LiteralPath $scutRoot
$scutState = Join-Path $scutRoot '.local'
if (Test-Path -LiteralPath (Join-Path $scutRoot 'installed.json')) { $scutState = Join-Path $env:LOCALAPPDATA 'SCUTClassroomAssistant\data' }
if ($env:SCUT_ASSISTANT_HOME) { $scutState = $env:SCUT_ASSISTANT_HOME }
$env:SCUT_ASSISTANT_HOME = $scutState
New-Item -ItemType Directory -Force -Path $scutState | Out-Null
function Write-ScutLaunchLog([string]$Text) {
    Add-Content -LiteralPath (Join-Path $scutState 'launcher.log') -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $Text) -Encoding UTF8
}
function Get-ScutHealth {
    # Bypass system/VPN proxies for loopback, including Windows PowerShell 5.1.
    try {
        $scutRequest = [Net.HttpWebRequest]::Create('http://127.0.0.1:8765/health')
        $scutRequest.Proxy = $null
        $scutRequest.Timeout = 1200
        $scutRequest.ReadWriteTimeout = 1200
        $scutResponse = $scutRequest.GetResponse()
        try {
            $scutReader = New-Object IO.StreamReader($scutResponse.GetResponseStream())
            try { $scutReader.ReadToEnd() | ConvertFrom-Json } finally { $scutReader.Dispose() }
        } finally { $scutResponse.Dispose() }
    } catch { return $null }
}
$scutLaunchMutex = [Threading.Mutex]::new($false, 'Local\SCUTClassroomAssistantLauncher')
$scutLaunchAcquired = $false
try {
    try { $scutLaunchAcquired = $scutLaunchMutex.WaitOne(90000) }
    catch [Threading.AbandonedMutexException] { $scutLaunchAcquired = $true }
    if (-not $scutLaunchAcquired) { throw 'The previous launch is still in progress. See .local/launcher.log.' }
    Write-ScutLaunchLog 'Checking local service.'
    $scutHealth = Get-ScutHealth
    if (-not $scutHealth) {
        # A slow health response must not spawn another server on the same port.
        $scutExisting = $null
        $scutPython = Join-Path $scutRoot '.venv\Scripts\python.exe'
        if (Test-Path -LiteralPath (Join-Path $scutRoot 'runtime\python.exe')) { $scutPython = Join-Path $scutRoot 'runtime\python.exe' }
        $scutPidPath = Join-Path $scutState 'server.pid'
        if (Test-Path -LiteralPath $scutPidPath) {
            $scutSavedPid = 0
            if ([int]::TryParse((Get-Content -LiteralPath $scutPidPath -Raw).Trim(), [ref]$scutSavedPid)) {
                $scutCandidate = Get-Process -Id $scutSavedPid -ErrorAction SilentlyContinue
                if ($scutCandidate -and $scutCandidate.Path -eq $scutPython) { $scutExisting = $scutCandidate }
            }
        }
        if (-not $scutExisting) {
            if (-not (Test-Path -LiteralPath $scutPython -PathType Leaf)) {
                if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'First setup requires uv: https://docs.astral.sh/uv/.' }
                Write-ScutLaunchLog 'Installing first-time dependencies.'
                & uv sync --extra asr --extra study --python 3.12 --quiet
                if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
            }
            $env:PYTHONIOENCODING = 'utf-8'
            if ($Foreground) { & $scutPython -u -m assistant_service; exit $LASTEXITCODE }
            Write-ScutLaunchLog 'Starting service in background.'
            $scutExisting = Start-Process -FilePath $scutPython -ArgumentList @('-u','-m','assistant_service') -WorkingDirectory $scutRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $scutState 'server.log') -RedirectStandardError (Join-Path $scutState 'server-error.log')
            $scutExisting.Id | Set-Content -LiteralPath $scutPidPath
        } else { Write-ScutLaunchLog 'Existing service process found; waiting instead of launching a duplicate.' }
        $scutDeadline = [DateTime]::UtcNow.AddSeconds(90)
        while (-not $scutHealth -and [DateTime]::UtcNow -lt $scutDeadline) {
            $scutExisting.Refresh()
            if ($scutExisting.HasExited) { throw 'Service exited during startup. See .local/server-error.log.' }
            Start-Sleep -Milliseconds 400
            $scutHealth = Get-ScutHealth
        }
        if (-not $scutHealth) { throw 'Service is still not responding. See .local/launcher.log and .local/server-error.log.' }
    }
    if ($scutHealth.app -ne 'scut-local-assistant') { throw 'Port 8765 is used by another program.' }
    & (Join-Path $scutRoot 'register-launcher.ps1')
    Write-ScutLaunchLog ('Service ready, version ' + $scutHealth.version)
    Write-Host 'SCUT Classroom Assistant ready: http://127.0.0.1:8765'
    if (-not $NoBrowser) {
        $scutDesktop = Join-Path $scutRoot 'SCUT学习助手.exe'
        if (Test-Path -LiteralPath $scutDesktop) { Start-Process -FilePath $scutDesktop -WorkingDirectory $scutRoot -WindowStyle Hidden }
        else { Start-Process 'http://127.0.0.1:8765/study.html' }
    }
} catch {
    Write-ScutLaunchLog ('Launch failed: ' + $_.Exception.Message)
    throw
} finally {
    if ($scutLaunchAcquired) { $scutLaunchMutex.ReleaseMutex() }
    $scutLaunchMutex.Dispose()
}
