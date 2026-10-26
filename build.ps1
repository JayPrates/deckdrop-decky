# Builds the frontend and packages the plugin as out\DeckDrop.zip for Decky's "Install Plugin from ZIP File".
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $here
try {
    if (-not (Test-Path node_modules)) { npm install --no-audit --no-fund; if ($LASTEXITCODE -ne 0) { throw "npm install failed." } }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
} finally { Pop-Location }

Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$outDir = Join-Path $here 'out'
New-Item -ItemType Directory -Force $outDir | Out-Null
$zipPath = Join-Path $outDir 'DeckDrop.zip'
if (Test-Path $zipPath) { Remove-Item $zipPath }

# Entry names use forward slashes (Compress-Archive on Windows PowerShell writes backslashes, which Linux keeps literally).
$zip = [System.IO.Compression.ZipFile]::Open($zipPath, 'Create')
try {
    foreach ($f in 'plugin.json', 'package.json', 'main.py', 'README.md', 'dist/index.js') {
        $src = Join-Path $here ($f -replace '/', '\')
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $src, "DeckDrop/$f") | Out-Null
    }
} finally { $zip.Dispose() }
Write-Host "OK -> $zipPath"
