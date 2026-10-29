# Zip the forward-test record (state/, journal/, audit/, logs/) into backups/<date>.zip.
#
#   powershell -ExecutionPolicy Bypass -File backup-forward-test.ps1             # backups/<today>.zip
#   powershell -ExecutionPolicy Bypass -File backup-forward-test.ps1 -Date 2026-10-29
#
# Read-only on the record: files are opened shared, so it can run while the bot is idle. Run it
# outside the session window so the SQLite database and its -wal file are a consistent pair.
# state/bot.lock (the single-instance lock) is skipped. backups/ is gitignored.
param([string]$Date = (Get-Date -Format 'yyyy-MM-dd'))
$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$dir = Join-Path $root 'backups'
$out = Join-Path $dir "$Date.zip"
$tmp = "$out.tmp"
New-Item -ItemType Directory -Force $dir | Out-Null
if (Test-Path $tmp) { Remove-Item $tmp }

Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($tmp, 'Create')
$n = 0
try {
    foreach ($part in 'state', 'journal', 'audit', 'logs') {
        $base = Join-Path $root $part
        if (-not (Test-Path $base)) { Write-Warning "$part/ not found, skipped"; continue }
        Get-ChildItem $base -Recurse -File | Where-Object Name -ne 'bot.lock' | ForEach-Object {
            $rel = $_.FullName.Substring($root.Length + 1) -replace '\\', '/'
            $src = [System.IO.File]::Open($_.FullName, 'Open', 'Read', 'ReadWrite, Delete')
            try {
                $entry = $zip.CreateEntry($rel, [System.IO.Compression.CompressionLevel]::Optimal)
                $entry.LastWriteTime = $_.LastWriteTime
                $dst = $entry.Open()
                try { $src.CopyTo($dst) } finally { $dst.Dispose() }
            } finally { $src.Dispose() }
            $n++
        }
    }
} finally { $zip.Dispose() }

Move-Item -Force $tmp $out
"Backed up $n files to $out ($([math]::Round((Get-Item $out).Length / 1KB)) KB)"
