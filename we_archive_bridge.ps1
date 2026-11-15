param(
    [ValidateSet('Install', 'Uninstall', 'Status')]
    [string]$Action = 'Install',
    [string]$InstallDir = ''
)

$ErrorActionPreference = 'Stop'
$utf8 = [System.Text.UTF8Encoding]::new($false)
$markerPattern = '(?s)<!-- WE_ARCHIVE_BRIDGE_BEGIN -->.*?<!-- WE_ARCHIVE_BRIDGE_END -->'
$scriptName = 'we-archive-bridge.js'

function Write-AtomicText([string]$Path, [string]$Text) {
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [System.IO.File]::WriteAllText($temporary, $Text, $utf8)
        if (Test-Path -LiteralPath $Path) {
            [System.IO.File]::Replace($temporary, $Path, [NullString]::Value)
        } else {
            [System.IO.File]::Move($temporary, $Path)
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
}

function Add-ArchiveFolders($Folders, $Entries, $Seen) {
    foreach ($folder in $Folders) {
        foreach ($item in $folder.items.PSObject.Properties) {
            $path = $item.Name.Replace('\', '/')
            $match = [regex]::Match($path, '(/projects/(?:myprojects|backup)/(\d+)/.+)$', 'IgnoreCase')
            if (!$match.Success) { continue }
            if ([System.IO.Path]::GetExtension($path).ToLowerInvariant() -notin
                @('.mp4', '.webm', '.mkv', '.avi', '.wmv', '.mov', '.m4v')) { continue }
            $relative = $match.Groups[1].Value
            if ($Seen.Add($relative)) { $Entries.Add(@($match.Groups[2].Value, $relative)) }
        }
        Add-ArchiveFolders $folder.subfolders $Entries $Seen
    }
}

if (!$InstallDir) {
    $configPath = Join-Path $PSScriptRoot 'config.toml'
    if (Test-Path -LiteralPath $configPath) {
        $configText = [System.IO.File]::ReadAllText($configPath)
        $match = [regex]::Match($configText, '(?m)^we_install_dir\s*=\s*("(?:\\.|[^"\\])*")')
        if ($match.Success) { $InstallDir = $match.Groups[1].Value | ConvertFrom-Json }
    }
}
if (!$InstallDir) {
    $steamPath = (Get-ItemProperty -Path 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).SteamPath
    $libraries = @($steamPath)
    if ($steamPath) {
        $libraryFile = Join-Path $steamPath 'steamapps\libraryfolders.vdf'
        if (Test-Path -LiteralPath $libraryFile) {
            foreach ($entry in [regex]::Matches([System.IO.File]::ReadAllText($libraryFile), '"path"\s+"([^"]+)"')) {
                $libraries += $entry.Groups[1].Value.Replace('\\', '\')
            }
        }
    }
    foreach ($library in $libraries) {
        if (!$library) { continue }
        $candidate = Join-Path $library 'steamapps\common\wallpaper_engine'
        if (Test-Path -LiteralPath (Join-Path $candidate 'ui\dist\index.html')) {
            $InstallDir = $candidate
            break
        }
    }
}
if (!$InstallDir) { throw 'Wallpaper Engine was not found. Supply -InstallDir.' }

$resolvedInstall = (Resolve-Path -LiteralPath $InstallDir).Path
$uiDirectory = Join-Path $resolvedInstall 'ui\dist'
$indexPath = Join-Path $uiDirectory 'index.html'
$extensionPath = Join-Path $uiDirectory ('scripts\' + $scriptName)
$indexBytes = [System.IO.File]::ReadAllBytes($indexPath)
$indexText = $utf8.GetString($indexBytes)
$installed = [regex]::IsMatch($indexText, $markerPattern)

if ($Action -eq 'Status') {
    [pscustomobject]@{
        InstallDir = $resolvedInstall
        Installed = $installed
        ScriptExists = (Test-Path -LiteralPath $extensionPath)
    } | ConvertTo-Json
    exit 0
}

$cleanIndex = [regex]::Replace($indexText, $markerPattern, '')
if ($Action -eq 'Install') {
    $nativeScript = [System.IO.File]::ReadAllText((Join-Path $uiDirectory 'scripts\scripts.js'))
    foreach ($symbol in @('window.browseWallpapersCtrl', 'updateWallpapers', 'queryWorkshop', 'getLocalFileSizes')) {
        if (!$nativeScript.Contains($symbol)) { throw "Unsupported Wallpaper Engine UI. Missing $symbol." }
    }
    if (!$cleanIndex.Contains('</head>')) { throw 'Wallpaper Engine index.html has no closing head tag.' }
    $sourcePath = Join-Path $PSScriptRoot 'we_archive_bridge.js'
    $directoryJson = ($resolvedInstall.Replace('\', '/') | ConvertTo-Json -Compress)
    $archiveEntries = [System.Collections.Generic.List[object]]::new()
    $seenArchives = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $weConfigPath = Join-Path $resolvedInstall 'config.json'
    if (Test-Path -LiteralPath $weConfigPath) {
        $weConfig = [System.IO.File]::ReadAllText($weConfigPath) | ConvertFrom-Json
        foreach ($profile in $weConfig.PSObject.Properties) {
            Add-ArchiveFolders $profile.Value.general.browser.folders $archiveEntries $seenArchives
        }
    }
    $archiveJson = ConvertTo-Json -InputObject ($archiveEntries.ToArray()) -Depth 4 -Compress
    $extensionText = 'window.weArchiveBridgeInstallDir = ' + $directoryJson + ";`n" +
        'window.weArchiveBridgeArchiveFiles = ' + $archiveJson + ";`n" +
        [System.IO.File]::ReadAllText($sourcePath)
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $scriptHash = [BitConverter]::ToString($hasher.ComputeHash($utf8.GetBytes($extensionText))).Replace('-', '').Substring(0, 12)
    } finally { $hasher.Dispose() }
    $tag = '<!-- WE_ARCHIVE_BRIDGE_BEGIN --><script src="scripts/' + $scriptName +
        '?v=1.0.10&amp;build=' + $scriptHash + '"></script><!-- WE_ARCHIVE_BRIDGE_END -->'
    $newIndex = $cleanIndex.Replace('</head>', ($tag + '</head>'))
    $sameScript = (Test-Path -LiteralPath $extensionPath) -and
        ([System.IO.File]::ReadAllText($extensionPath) -ceq $extensionText)
    if (($newIndex -ceq $indexText) -and $sameScript) {
        Write-Output 'Archive bridge is already up to date.'
        exit 0
    }
} else {
    if (!$installed) { Write-Output 'Archive bridge is already uninstalled.'; exit 0 }
    $newIndex = $cleanIndex
}

$backupDir = Join-Path $uiDirectory ('archive-bridge-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
[void][System.IO.Directory]::CreateDirectory($backupDir)
[System.IO.File]::WriteAllBytes((Join-Path $backupDir 'index.html'), $indexBytes)
if (Test-Path -LiteralPath $extensionPath) {
    Copy-Item -LiteralPath $extensionPath -Destination (Join-Path $backupDir $scriptName)
}
if ($Action -eq 'Install') {
    Write-AtomicText $extensionPath $extensionText
    Write-AtomicText $indexPath $newIndex
} else {
    Write-AtomicText $indexPath $newIndex
    if (Test-Path -LiteralPath $extensionPath) { Remove-Item -LiteralPath $extensionPath }
}
Write-Output "Archive bridge $Action completed. Reopen the Wallpaper Engine browser."
Write-Output "Backup saved to $backupDir"
