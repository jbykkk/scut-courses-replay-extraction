param([switch]$NoLaunch)
$ErrorActionPreference = 'Stop'
$scutRoot = $PSScriptRoot
$scutExe = Join-Path $scutRoot 'SCUT学习助手.exe'
if (-not (Test-Path -LiteralPath $scutExe)) {
    $scutCompiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    & $scutCompiler /nologo /target:winexe /reference:System.Windows.Forms.dll /reference:System.Drawing.dll "/win32icon:$scutRoot\extension\icons\assistant.ico" "/out:$scutExe" "$scutRoot\scripts\DesktopLauncher.cs"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot build the desktop entry.' }
}
$scutShell = New-Object -ComObject WScript.Shell
$scutPlaces = @([Environment]::GetFolderPath('Desktop'), (Join-Path ([Environment]::GetFolderPath('Programs')) 'SCUT 学习助手'))
foreach ($scutPlace in $scutPlaces) {
    New-Item -ItemType Directory -Path $scutPlace -Force | Out-Null
    $scutShortcut = $scutShell.CreateShortcut((Join-Path $scutPlace 'SCUT 学习助手.lnk'))
    $scutShortcut.TargetPath = $scutExe
    $scutShortcut.WorkingDirectory = $scutRoot
    $scutShortcut.IconLocation = "$scutRoot\extension\icons\assistant.ico"
    $scutShortcut.Description = '打开学习助手；按需启动本地服务'
    $scutShortcut.Save()
}
& (Join-Path $scutRoot 'register-launcher.ps1')
if (-not $NoLaunch) { Start-Process -FilePath $scutExe -WorkingDirectory $scutRoot -WindowStyle Hidden }
