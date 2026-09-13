$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if ($env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
  throw "此脚本的首版交付目标是 Windows x64。"
}

npm --prefix frontend ci
npm --prefix frontend run build
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.venv\Scripts\python.exe scripts\package_smoke.py
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean ScoreFlow.spec
if (Test-Path dist\ScoreFlow-Windows-x64.zip) { Remove-Item dist\ScoreFlow-Windows-x64.zip }
Compress-Archive -Path dist\ScoreFlow -DestinationPath dist\ScoreFlow-Windows-x64.zip
Write-Host "完成：dist\ScoreFlow\ScoreFlow.exe"
Write-Host "完成：dist\ScoreFlow-Windows-x64.zip"
