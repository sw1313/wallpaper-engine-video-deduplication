param([string]$ReleaseVersion = '')

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw '缺少 .venv，请先创建 Python 环境并安装 requirements.txt、torch、torchvision、transformers、modelscope 和 pyinstaller。'
}
& $pythonPath -m PyInstaller --noconfirm --distpath 'build\package' WEVideoTools.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 打包失败。' }
$packagePath = Join-Path $PSScriptRoot 'dist\WEVideoTools'
New-Item -ItemType Directory -Path $packagePath -Force | Out-Null
Copy-Item -Path 'build\package\WEVideoTools\*' -Destination $packagePath -Recurse -Force
foreach ($conflictingDll in @('_internal\icuuc.dll', '_internal\icudt78.dll')) {
    $dllPath = Join-Path $packagePath $conflictingDll
    if (Test-Path -LiteralPath $dllPath) { Remove-Item -LiteralPath $dllPath }
}
Copy-Item -LiteralPath 'config.example.toml','wallpaper-engine-video-deduplication.js','we_archive_bridge.js','we_archive_bridge.ps1','LICENSE' -Destination $packagePath -Force
$localConfig = Join-Path $packagePath 'config.toml'
if (-not (Test-Path -LiteralPath $localConfig)) {
    Copy-Item -LiteralPath 'config.example.toml' -Destination $localConfig
}
New-Item -ItemType Directory -Path (Join-Path $packagePath 'output') -Force | Out-Null
Write-Host "打包完成 $packagePath"
if ($ReleaseVersion) {
    & $pythonPath package_release.py --version $ReleaseVersion
    if ($LASTEXITCODE -ne 0) { throw 'Release 打包失败。' }
}
