param(
    [ValidateSet('Start', 'Status', 'Pause', 'Resume', 'PauseDownloads', 'ResumeDownloads', 'Stop')]
    [string]$Action = 'Status',
    [string]$InstallDir = ''
)

$ErrorActionPreference = 'Stop'
$workerPath = Join-Path $PSScriptRoot 'WEWorker.exe'
if (Test-Path -LiteralPath $workerPath) {
    $commandPath = $workerPath
    $commandArguments = @('--worker', 'we_workshop_guard.py')
} else {
    $commandPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $commandPath)) {
        throw 'Use the portable release, or create the project .venv first.'
    }
    $commandArguments = @('-X', 'utf8', (Join-Path $PSScriptRoot 'we_workshop_guard.py'))
}
$commandArguments += switch ($Action) {
    'PauseDownloads' { 'pause-downloads' }
    'ResumeDownloads' { 'resume-downloads' }
    default { $Action.ToLowerInvariant() }
}
if ($Action -eq 'Start') { $commandArguments += '--sequential' }
if ($InstallDir) { $commandArguments += @('--install-dir', $InstallDir) }
& $commandPath @commandArguments
if ($LASTEXITCODE -ne 0) { throw "Workshop guard command failed with exit code $LASTEXITCODE." }
