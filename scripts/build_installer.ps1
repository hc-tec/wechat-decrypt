$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "[1/3] Build binaries..." -ForegroundColor Cyan
& .\scripts\build_windows.ps1

Write-Host "[2/3] Locate ISCC.exe (Inno Setup)..." -ForegroundColor Cyan
$iscc = $env:ISCC
if (!$iscc) {
  $candidates = @(
    "C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe",
    "C:\\Program Files\\Inno Setup 6\\ISCC.exe"
  )
  foreach ($c in $candidates) {
    if (Test-Path $c) { $iscc = $c; break }
  }
}
if (!$iscc -or !(Test-Path $iscc)) {
  throw "ISCC.exe not found. Please install Inno Setup 6 and/or set env:ISCC to ISCC.exe path."
}

Write-Host "[3/3] Build installer..." -ForegroundColor Cyan
& $iscc ".\\installer\\WeChatDataService.iss"
if ($LASTEXITCODE -ne 0) {
  throw "ISCC failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "Built: dist-installer\\WeChatDataServiceSetup.exe"

