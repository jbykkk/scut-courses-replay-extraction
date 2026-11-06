$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -eq 'Core') {
    & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $PSCommandPath
    if ($LASTEXITCODE -ne 0) { throw 'Native launcher registration failed.' }
    return
}
$scutNativeRoot = $PSScriptRoot
$scutNativeState = Join-Path $scutNativeRoot '.local'
New-Item -ItemType Directory -Force -Path $scutNativeState | Out-Null
$scutNativePython = Join-Path $scutNativeRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath (Join-Path $scutNativeRoot 'runtime\python.exe')) { $scutNativePython = Join-Path $scutNativeRoot 'runtime\python.exe' }
if (-not (Test-Path -LiteralPath $scutNativePython)) { return }
$scutDiscovery = @'
import json,os,re,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
origins=set()
for suffix in ('Microsoft/Edge/User Data','Google/Chrome/User Data'):
    base=Path(os.environ['LOCALAPPDATA'])/suffix
    for name in ('Preferences','Secure Preferences'):
        for path in base.glob('*/'+name):
            try: data=json.loads(path.read_text(encoding='utf-8'))
            except (ValueError,OSError): continue
            for eid,entry in data.get('extensions',{}).get('settings',{}).items():
                try: matches=Path(entry.get('path','')).resolve()==root/'extension'
                except OSError: matches=False
                if matches and re.fullmatch('[a-p]{32}',eid):origins.add('chrome-extension://'+eid+'/')
print(json.dumps(sorted(origins)))
'@
$scutOriginsJson = $scutDiscovery | & $scutNativePython - $scutNativeRoot
if ($LASTEXITCODE -ne 0) { throw 'Cannot discover installed extension IDs.' }
[string[]]$scutOrigins = $scutOriginsJson | ConvertFrom-Json
if (-not $scutOrigins.Count) { return }
$scutSource = Join-Path $scutNativeRoot 'scripts\NativeLauncher.cs'
$scutHasher = [Security.Cryptography.SHA256]::Create()
try { $scutHash = ([BitConverter]::ToString($scutHasher.ComputeHash([IO.File]::ReadAllBytes($scutSource)))).Replace('-','').Substring(0,16).ToLowerInvariant() }
finally { $scutHasher.Dispose() }
$scutHost = Join-Path $scutNativeState ('native-launcher-' + $scutHash + '.exe')
if (-not (Test-Path -LiteralPath $scutHost)) {
    Add-Type -Path $scutSource -ReferencedAssemblies @('System.dll','System.Web.Extensions.dll') -OutputAssembly $scutHost -OutputType ConsoleApplication
}
$scutUtf8 = New-Object System.Text.UTF8Encoding($false)
$scutConfig = @{root=$scutNativeRoot;origins=$scutOrigins} | ConvertTo-Json -Depth 4
[IO.File]::WriteAllText((Join-Path $scutNativeState 'native-launcher-config.json'), $scutConfig, $scutUtf8)
$scutManifest = Join-Path $scutNativeState 'com.poesis.scut_classroom.json'
$scutManifestJson = @{name='com.poesis.scut_classroom';description='Start the local SCUT classroom service on demand';path=$scutHost;type='stdio';allowed_origins=$scutOrigins} | ConvertTo-Json -Depth 4
[IO.File]::WriteAllText($scutManifest,$scutManifestJson,$scutUtf8)
foreach ($scutBrowser in @('Microsoft\Edge','Google\Chrome')) {
    $scutKey = 'HKCU:\Software\' + $scutBrowser + '\NativeMessagingHosts\com.poesis.scut_classroom'
    New-Item -Path $scutKey -Force | Out-Null
    Set-Item -LiteralPath $scutKey -Value $scutManifest
}
Write-Host 'Browser on-demand launcher registered. No login/startup task created.'
