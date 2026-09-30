# 打包 Windows 单文件 exe，输出到 dist\：
#   HeartRateCollector.exe  采集端（pywebview 窗口，带上 client\ui 和 web）
#   HeartRateServer.exe     服务端（控制台窗口，带上 web；配置和数据库放在 exe 旁边）
# 用法（在仓库根目录）：
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1                  # 两个都打包
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -Target client   # 只打包采集端
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -Target server   # 只打包服务端
# 注意：PyInstaller 在含中文的路径下容易出问题，所以在 %TEMP% 下的纯英文路径中构建，再把 exe 复制回来。
param([ValidateSet("all", "client", "server")] [string]$Target = "all")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$work = Join-Path ([IO.Path]::GetTempPath()) "hr-collector-build"
New-Item -ItemType Directory -Force (Join-Path $root "dist") | Out-Null

function New-BuildVenv([string]$Path, [string[]]$Packages) {
    if (-not (Test-Path "$Path\Scripts\python.exe")) {
        python -m venv $Path
        & "$Path\Scripts\python" -m pip install --upgrade pip
        & "$Path\Scripts\python" -m pip install @Packages pyinstaller
    }
}

if ($Target -in "all", "client") {
    $venv = Join-Path $work "venv-webview"
    New-BuildVenv $venv @("bleak>=0.20.0,<2", "pywebview>=5.0,<7", "websockets>=12.0,<16")

    Remove-Item -Recurse -Force "$work\src", "$work\web" -ErrorAction SilentlyContinue
    Copy-Item -Recurse (Join-Path $root "client") "$work\src"
    Copy-Item -Recurse (Join-Path $root "web") "$work\web"

    & "$venv\Scripts\pyinstaller" --noconfirm --clean --onefile --windowed `
        --name HeartRateCollector --paths "$work\src" `
        --add-data "$work\src\ui;ui" --add-data "$work\web;web" `
        --distpath "$work\dist" --workpath "$work\build" --specpath "$work\build" `
        "$work\src\app.py"
    if ($LASTEXITCODE -ne 0) { throw "采集端打包失败" }
    Copy-Item "$work\dist\HeartRateCollector.exe" (Join-Path $root "dist") -Force
    Write-Host "完成：dist\HeartRateCollector.exe"
}

if ($Target -in "all", "server") {
    $venv = Join-Path $work "venv-server"
    New-BuildVenv $venv @("fastapi>=0.115.0,<1", "uvicorn[standard]>=0.30.0,<1")

    # Bundled Cloudflare client for the built-in tunnel. Pinned version + SHA-256 from the official
    # release page (https://github.com/cloudflare/cloudflared/releases); bump both together.
    $cloudflaredVersion = "2026.9.3"
    $cloudflaredSha256 = "f096265ec2fcbe9bb6e2d64268db167ced3fcbb83d894bdb9e2fcdb26f2ea7e2"
    $cloudflared = Join-Path $work "cloudflared-$cloudflaredVersion.exe"
    if (-not (Test-Path $cloudflared) -or (Get-FileHash $cloudflared -Algorithm SHA256).Hash -ne $cloudflaredSha256) {
        Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/download/$cloudflaredVersion/cloudflared-windows-amd64.exe" -OutFile $cloudflared -UseBasicParsing
        if ((Get-FileHash $cloudflared -Algorithm SHA256).Hash -ne $cloudflaredSha256) {
            Remove-Item $cloudflared -Force
            throw "cloudflared.exe 校验失败，已中止打包"
        }
    }

    # server.app finds the dashboard at <package parent>\web, so keep server\ and web\ side by side.
    $srv = Join-Path $work "srv"
    Remove-Item -Recurse -Force $srv -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force $srv | Out-Null
    Copy-Item -Recurse (Join-Path $root "server") "$srv\server"
    Copy-Item -Recurse (Join-Path $root "web") "$srv\web"
    Copy-Item $cloudflared "$srv\cloudflared.exe"

    & "$venv\Scripts\pyinstaller" --noconfirm --clean --onefile --console `
        --name HeartRateServer --paths $srv `
        --collect-submodules uvicorn --hidden-import server.app `
        --add-data "$srv\web;web" --add-binary "$srv\cloudflared.exe;." `
        --distpath "$work\dist-server" --workpath "$work\build-server" --specpath "$work\build-server" `
        "$srv\server\launcher.py"
    if ($LASTEXITCODE -ne 0) { throw "服务端打包失败" }
    Copy-Item "$work\dist-server\HeartRateServer.exe" (Join-Path $root "dist") -Force
    Write-Host "完成：dist\HeartRateServer.exe"
}
