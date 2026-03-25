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

Write-Host ""
Write-Host "Built: dist\\WeChatDataService\\WeChatDataService.exe"
