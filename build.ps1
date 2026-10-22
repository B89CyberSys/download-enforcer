$ErrorActionPreference = 'Stop'
$originalPath = $env:PATH
$pythonExecutable = (Get-Command python).Source
# Prevent unrelated tools' native libraries from replacing Windows' own ICU library.
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$(Split-Path -Parent $pythonExecutable)"
Push-Location $PSScriptRoot
try {
    $icon = Join-Path $PSScriptRoot 'assets\enforcer.ico'
    $assets = Join-Path $PSScriptRoot 'assets'
    $version = & $pythonExecutable -c "from fetchline import __version__; print(__version__)"
    $release = Join-Path $PSScriptRoot "releases\$version"
    $licenses = Join-Path $PSScriptRoot 'licenses'
    & $pythonExecutable -m PyInstaller --noconfirm --clean --onedir --windowed --name DownloadEnforcer --icon $icon --collect-all libtorrent --add-data "$assets;assets" --add-data "$licenses;licenses" --distpath $release --workpath .build --specpath .build run.py
    if ($LASTEXITCODE -ne 0) { throw 'The Windows build failed.' }
    Write-Output "Built: $release\DownloadEnforcer\DownloadEnforcer.exe"
} finally {
    $env:PATH = $originalPath
    Pop-Location
}
