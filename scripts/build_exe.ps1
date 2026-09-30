# 将采集端打包为单文件 exe：dist\HeartRateCollector.exe
# 用法（在仓库根目录）：powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
# 界面基于 pywebview（Edge WebView2），打包时会带上 client\ui 和 web 两个目录。
# 注意：PyInstaller 在含中文的路径下容易出问题，所以在 %TEMP% 下的纯英文路径中构建，再把 exe 复制回来。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$work = Join-Path ([IO.Path]::GetTempPath()) "hr-collector-build"
$venv = Join-Path $work "venv-webview"

if (-not (Test-Path "$venv\Scripts\python.exe")) {
    python -m venv $venv
    & "$venv\Scripts\python" -m pip install --upgrade pip
    & "$venv\Scripts\python" -m pip install "bleak>=0.20.0,<2" "pywebview>=5.0,<7" "websockets>=12.0,<16" pyinstaller
}

Remove-Item -Recurse -Force "$work\src", "$work\web" -ErrorAction SilentlyContinue
Copy-Item -Recurse (Join-Path $root "client") "$work\src"
Copy-Item -Recurse (Join-Path $root "web") "$work\web"

& "$venv\Scripts\pyinstaller" --noconfirm --clean --onefile --windowed `
    --name HeartRateCollector --paths "$work\src" `
    --add-data "$work\src\ui;ui" --add-data "$work\web;web" `
    --distpath "$work\dist" --workpath "$work\build" --specpath "$work\build" `
    "$work\src\app.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }

New-Item -ItemType Directory -Force (Join-Path $root "dist") | Out-Null
Copy-Item "$work\dist\HeartRateCollector.exe" (Join-Path $root "dist") -Force
Write-Host "完成：dist\HeartRateCollector.exe"
