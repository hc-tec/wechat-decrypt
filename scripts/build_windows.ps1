$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Resolve-PythonCmd {
  $py = Get-Command python -ErrorAction SilentlyContinue
  if ($py) { return "python" }
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) { return "py" }
  throw "python/py not found in PATH"
}

$PY = Resolve-PythonCmd

# 避免上一次运行的 exe 占用 dist 内 DLL，导致 PyInstaller 清理失败
try {
  Stop-Process -Name "WeChatDataServiceGUI","WeChatDataServiceGUIConsole","WeChatDataService" -Force -ErrorAction SilentlyContinue
} catch {
  # ignore
}

if (!(Test-Path ".venv-build")) {
  & $PY -m venv .venv-build
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

& .venv-build\Scripts\pyinstaller "--noconfirm" "--clean" "--onedir" "--noconsole" "--name" "WeChatDataServiceGUI" "--hidden-import" "image_key_extractor" @AddBinaryArgs "gui_main.py"
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller GUI failed with exit code $LASTEXITCODE"
}

# 额外生成一个 Console 版（用于排障；安装包默认不包含）。
& .venv-build\Scripts\pyinstaller "--noconfirm" "--clean" "--onedir" "--console" "--name" "WeChatDataServiceGUIConsole" "--hidden-import" "image_key_extractor" @AddBinaryArgs "gui_main.py"
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller GUIConsole failed with exit code $LASTEXITCODE"
}

# 避免误打包来自 Anaconda 等环境的 ICU DLL（会导致 Qt6Core.dll 依赖不匹配而无法加载）。
# 这里把 dist 中的 icu*.dll disable 掉，防止冻结版启动时报 “DLL load failed”。
Write-Host ""
Write-Host "[cleanup] disable bundled ICU DLLs (best-effort)"
& .venv-build\Scripts\python -u scripts\cleanup_icu.py "dist\\WeChatDataServiceGUI\\_internal" "dist\\WeChatDataServiceGUIConsole\\_internal"
if ($LASTEXITCODE -ne 0) {
  throw "cleanup_icu.py failed with exit code $LASTEXITCODE"
}
Start-Sleep -Milliseconds 600
& .venv-build\Scripts\python -u scripts\cleanup_icu.py "dist\\WeChatDataServiceGUI\\_internal" "dist\\WeChatDataServiceGUIConsole\\_internal"
if ($LASTEXITCODE -ne 0) {
  throw "cleanup_icu.py failed with exit code $LASTEXITCODE"
}

$Left = @()
foreach ($d in @("dist\\WeChatDataServiceGUI\\_internal", "dist\\WeChatDataServiceGUIConsole\\_internal")) {
  Get-ChildItem -Path (Join-Path $d "icu*.dll") -ErrorAction SilentlyContinue | ForEach-Object {
    $Left += $_.FullName
  }
}
if ($Left.Count -gt 0) {
  Write-Host ("[cleanup] WARN: ICU DLL still present:`n  " + ($Left -join "`n  "))
} else {
  Write-Host "[cleanup] OK"
}

Write-Host ""
Write-Host "Built: dist\\WeChatDataService\\WeChatDataService.exe"
Write-Host "Built: dist\\WeChatDataServiceGUI\\WeChatDataServiceGUI.exe"
Write-Host "Built: dist\\WeChatDataServiceGUIConsole\\WeChatDataServiceGUIConsole.exe"
