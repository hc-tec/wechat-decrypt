param(
  [string]$Name = "",
  [string]$OutDir = "release"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function _TryGit([string[]]$GitArgs) {
  try {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (!$git) { return "" }
    & git @("rev-parse", "--is-inside-work-tree") 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { return "" }
    $out = & git @GitArgs 2>$null
    if ($LASTEXITCODE -ne 0) { return "" }
    return ($out | Out-String).Trim()
  } catch {
    return ""
  }
}

$date = Get-Date -Format "yyyyMMdd"
$sha = _TryGit @("rev-parse", "--short", "HEAD")

if (!$Name) {
  $Name = "WeChatDataService_Windows_${date}"
  if ($sha) { $Name = "${Name}_${sha}" }
}

$outBase = Join-Path $Root $OutDir
$stageDir = Join-Path $outBase $Name
$zipPath = Join-Path $outBase ("{0}.zip" -f $Name)
$shaPath = Join-Path $outBase ("{0}.sha256.txt" -f $Name)

New-Item -ItemType Directory -Force -Path $outBase | Out-Null
if (Test-Path $stageDir) { Remove-Item -Recurse -Force $stageDir }
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
if (Test-Path $shaPath) { Remove-Item -Force $shaPath }

Write-Host "[1/3] Build binaries..." -ForegroundColor Cyan
& .\scripts\build_windows.ps1

Write-Host "[2/3] Stage release files..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $stageDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $stageDir "Service") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $stageDir "GUI") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $stageDir "docs") | Out-Null

Copy-Item -Recurse -Force ".\\dist\\WeChatDataService\\*" (Join-Path $stageDir "Service")
Copy-Item -Recurse -Force ".\\dist\\WeChatDataServiceGUI\\*" (Join-Path $stageDir "GUI")
Copy-Item -Force ".\\config.example.json" (Join-Path $stageDir "config.example.json")
Copy-Item -Force ".\\docs\\API.md" (Join-Path $stageDir "docs\\API.md")
Copy-Item -Force ".\\docs\\DISTRIBUTION_WINDOWS.md" (Join-Path $stageDir "docs\\DISTRIBUTION_WINDOWS.md")

$quick = @"
WeChat Data Service (Windows) - Portable Release

How to start:
1) Keep WeChat running and logged in.
2) Run: .\\GUI\\WeChatDataServiceGUI.exe
3) Click: Start Service

Default API base:
  http://127.0.0.1:5678

Docs:
  .\\docs\\API.md

Notes:
- For first run, you may need "Run as Administrator" to read WeChat process memory.
- This is a local offline data service; do NOT expose it to the public internet unless you know what you're doing.
"@
$quick | Out-File -Encoding UTF8 (Join-Path $stageDir "QUICKSTART.txt")

Write-Host "[3/3] Create ZIP + SHA256..." -ForegroundColor Cyan
for ($i = 1; $i -le 8; $i++) {
  try {
    Compress-Archive -Path $stageDir -DestinationPath $zipPath -Force
    break
  } catch {
    if ($i -ge 8) { throw }
    Start-Sleep -Milliseconds (500 * $i)
  }
}
$hash = (Get-FileHash -Algorithm SHA256 -Path $zipPath).Hash.ToLower()
("{0}  {1}" -f $hash, (Split-Path -Leaf $zipPath)) | Out-File -Encoding ASCII -FilePath $shaPath

Write-Host ""
Write-Host "Release dir: $stageDir"
Write-Host "Release zip: $zipPath"
Write-Host "SHA256:      $shaPath"
