$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (!(Test-Path ".venv-build")) {
  py -m venv .venv-build
}

& .venv-build\Scripts\python -m pip install -U pip
& .venv-build\Scripts\python -m pip install -r requirements.txt pyinstaller

# 推荐 onedir：配置文件/密钥等可以和 exe 放在同一目录，便于分发与持久化
& .venv-build\Scripts\pyinstaller "--noconfirm" "--clean" "--onedir" "--name" "WeChatDataService" "main.py"
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed with exit code $LASTEXITCODE"
}

# GUI（Qt）：用于普通用户配置/启动/托盘常驻
$QtBin = (Resolve-Path ".venv-build\Lib\site-packages\PyQt6\Qt6\bin").Path
$ExtraQtDlls = @("concrt140.dll", "d3dcompiler_47.dll")
$AddBinaryArgs = @()
foreach ($dll in $ExtraQtDlls) {
  $p = Join-Path $QtBin $dll
  if (Test-Path $p) {
    $AddBinaryArgs += "--add-binary"
    $AddBinaryArgs += "$p;PyQt6\Qt6\bin"
  }
}

& .venv-build\Scripts\pyinstaller "--noconfirm" "--clean" "--onedir" "--noconsole" "--name" "WeChatDataServiceGUI" @AddBinaryArgs "gui_main.py"
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller GUI failed with exit code $LASTEXITCODE"
}

# 避免误打包来自 Anaconda 等环境的 ICU DLL（会导致 Qt6Core.dll 依赖不匹配而无法加载）
$IcuCandidates = @(
  "dist\\WeChatDataServiceGUI\\_internal\\icuuc.dll",
  "dist\\WeChatDataServiceGUI\\_internal\\icudt73.dll"
)
foreach ($p in $IcuCandidates) {
  if (Test-Path $p) {
    Remove-Item -Force $p
  }
}

Write-Host ""
Write-Host "Built: dist\\WeChatDataService\\WeChatDataService.exe"
Write-Host "Built: dist\\WeChatDataServiceGUI\\WeChatDataServiceGUI.exe"
