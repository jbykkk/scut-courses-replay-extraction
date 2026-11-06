"""Build an offline, per-user Windows installer from an explicit public file allowlist.

Run on Windows with Python 3.12, uv, Pillow, and the .NET Framework compiler.
No browser profiles, classroom records, local settings or model weights are packaged.
"""
from pathlib import Path
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / 'extension/manifest.json').read_text('utf-8'))['version']
PYTHON_URL = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'
# Published on python.org's release page; HTTPS is also required.
PYTHON_MD5 = 'fe8ef205f2e9c3ba44d0cf9954e1abd3'


def public_files(root=ROOT):
    for folder in ('assistant_service', 'extension', 'manual', 'workbuddy'):
        for p in (root/folder).rglob('*'):
            rel=p.relative_to(root)
            if p.is_file() and not any(part.startswith('.') or part=='__pycache__' for part in rel.parts) and not p.name.endswith('.test.js') and p.name!='local-connection.js':
                yield rel
    for name in ('config.py','subtitle.py','LICENSE','README.md','start-assistant.ps1','stop-assistant.ps1','停止课堂助手.cmd',
                 'register-launcher.ps1','register-native-launcher.ps1','install-desktop.ps1',
                 'scripts/NativeLauncher.cs','scripts/DesktopLauncher.cs','scripts/study_mcp.py','scripts/prepare_workbuddy.py'):
        yield Path(name)


def main():
    if os.name!='nt' or sys.version_info[:2]!=(3,12):
        raise SystemExit('Build with Windows x64 / Python 3.12')
    dist=ROOT/'dist';dist.mkdir(exist_ok=True)
    build=ROOT/'build'/('windows-'+VERSION)
    # Repeatable builds reuse downloaded runtime/dependencies but never copy untracked state.
    package=build/'app';package.mkdir(parents=True,exist_ok=True)
    runtime=package/'runtime';runtime.mkdir(exist_ok=True)
    archive=build/'python.zip'
    if not archive.exists():
        with urllib.request.urlopen(PYTHON_URL,timeout=60) as r:archive.write_bytes(r.read())
    if hashlib.md5(archive.read_bytes()).hexdigest()!=PYTHON_MD5:raise ValueError('Python download checksum mismatch')
    with zipfile.ZipFile(archive) as z:z.extractall(runtime)
    (runtime/'python312._pth').write_text('python312.zip\n.\n..\nLib/site-packages\nimport site\n','utf-8')
    uv=shutil.which('uv')
    if not uv:raise SystemExit('Install uv on the build machine')
    subprocess.run([uv,'pip','install','--python',str(runtime/'python.exe'),'--target',str(runtime/'Lib/site-packages'),
                    '--only-binary',':all:','-r',str(ROOT/'requirements-windows.lock')],check=True)
    for rel in public_files():
        dest=package/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/rel,dest)
    (package/'installed.json').write_text(json.dumps({'version':VERSION,'data':'%LOCALAPPDATA%/SCUTClassroomAssistant/data'}),'utf-8')
    # Preserve all wheels' .dist-info/license files and emit an inventory for redistribution.
    deps=[{'name':d.metadata['Name'],'version':d.version,'license':d.metadata.get('License-Expression') or d.metadata.get('License',''),'license_files':[str(p) for p in (d.files or []) if 'license' in str(p).lower() or 'copying' in str(p).lower()]} for d in importlib.metadata.distributions(path=[str(runtime/'Lib/site-packages')])]
    (package/'THIRD_PARTY.json').write_text(json.dumps({'python':{'url':PYTHON_URL,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()},'packages':deps},ensure_ascii=False,indent=2),'utf-8')
    compiler=Path(os.environ['SystemRoot'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    common=[str(compiler),'/nologo','/target:winexe','/reference:System.Windows.Forms.dll','/reference:System.Drawing.dll','/win32icon:'+str(ROOT/'extension/icons/assistant.ico')]
    subprocess.run(common+['/out:'+str(package/'SCUT学习助手.exe'),str(ROOT/'scripts/DesktopLauncher.cs')],check=True)
    # Import using the included interpreter, isolated from builder site-packages.
    check=subprocess.run([str(runtime/'python.exe'),'-I','-c','import fastapi,uvicorn,cryptography,pdfplumber,reportlab,faster_whisper;from assistant_service.app import create_app;print("Bundled runtime OK")'],cwd=package,check=True)
    portable=dist/f'SCUT-Study-{VERSION}-windows-x64.zip'
    with zipfile.ZipFile(portable,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(package.rglob('*')):
            if p.is_file() and '__pycache__' not in p.parts and not p.name.endswith('.pyc'):
                rel=p.relative_to(package)
                if any(part in {'.local','.git','data','output'} for part in rel.parts[:1]) or p.name in {'credentials.json','settings.json','local-connection.js'}:raise ValueError('Private file in payload: '+str(rel))
                z.write(p,rel.as_posix())
    installer=dist/f'SCUT-Study-Setup-{VERSION}.exe'
    subprocess.run(common+['/reference:System.IO.Compression.dll','/reference:System.IO.Compression.FileSystem.dll',
        '/resource:'+str(portable)+',payload.zip','/out:'+str(installer),str(ROOT/'scripts/Installer.cs')],check=True)
    print(json.dumps({'installer':str(installer),'bytes':installer.stat().st_size,'portable':str(portable)},ensure_ascii=False))


if __name__=='__main__':main()
