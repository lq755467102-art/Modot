param([Parameter(Mandatory)][string]$Stage)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.IO.Compression.FileSystem
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$assets = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime-assets.json') -Raw | ConvertFrom-Json
$stagePath = [IO.Directory]::CreateDirectory($Stage).FullName
foreach ($asset in $assets) {
    $archive = Join-Path $stagePath $asset.file
    if ($env:MODOT_DOWNLOAD_CACHE -and (Test-Path -LiteralPath (Join-Path $env:MODOT_DOWNLOAD_CACHE $asset.file) -PathType Leaf)) {
        Copy-Item -LiteralPath (Join-Path $env:MODOT_DOWNLOAD_CACHE $asset.file) -Destination $archive
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri $asset.url -OutFile $archive -TimeoutSec 180
    }
    $stream = [IO.File]::OpenRead($archive)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = [BitConverter]::ToString($sha256.ComputeHash($stream)).Replace('-', '')
    } finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
    if ($hash -ne $asset.sha256) {
        throw "Checksum mismatch for $($asset.name)"
    }
    $target = [IO.Directory]::CreateDirectory((Join-Path $stagePath 'git')).FullName
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $target)
}
foreach ($file in @('git/cmd/git.exe','git/LICENSE.txt')) {
    if (-not (Test-Path -LiteralPath (Join-Path $stagePath $file))) { throw "Incomplete upstream component: $file" }
}
