$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw '请先运行 py -3.13 -m venv .venv，再用 .venv\Scripts\python.exe -m pip install -r requirements.txt 安装依赖。'
}
Start-Process -FilePath $pythonPath -ArgumentList 'we_ui.py' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
