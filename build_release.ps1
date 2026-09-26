param(
    [string]$AhkCompiler = "C:\Program Files\AutoHotkey\Compiler\Ahk2Exe.exe",
    [string]$AhkBase = "C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe"
)
$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath $AhkCompiler) -or !(Test-Path -LiteralPath $AhkBase)) {
    throw 'Need project .venv, AutoHotkey v2, and Ahk2Exe to build.'
}
Push-Location $projectRoot
$previousUserBase = $env:PYTHONUSERBASE
$env:PYTHONUSERBASE = Join-Path $projectRoot 'build\python-userbase'
try {
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed' }
    & $python -m unittest discover -s tests -q
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
    & $python -m PyInstaller --noconfirm --clean ScreenTrans.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
    $output = Join-Path $projectRoot 'dist\ScreenTrans'
    $ahkExe = Join-Path $output 'TranEasy.exe'
    $compiler = Start-Process -FilePath $AhkCompiler -ArgumentList "/in `"$projectRoot\translator.ahk`" /out `"$ahkExe`" /base `"$AhkBase`" /icon `"$projectRoot\assets\transeasy-icon.ico`"" -PassThru -Wait -WindowStyle Hidden
    if ($compiler.ExitCode -ne 0 -or !(Test-Path -LiteralPath $ahkExe)) { throw 'AHK compilation failed' }
    Copy-Item -LiteralPath (Join-Path $projectRoot 'config.example.json') -Destination $output
    Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination $output
    Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination $output
    New-Item -ItemType Directory -Path (Join-Path $output 'assets') -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot 'assets\transeasy-icon.ico') -Destination (Join-Path $output 'assets')
    foreach ($image in @('transeasy-icon-preview.png','transeasy-main.png','transeasy-history.png','transeasy-settings.png')) {
        Copy-Item -LiteralPath (Join-Path $projectRoot ('assets\' + $image)) -Destination (Join-Path $output 'assets')
    }
    Copy-Item -LiteralPath 'C:\Program Files\AutoHotkey\license.txt' -Destination (Join-Path $output 'AutoHotkey-LICENSE.txt')
    $releaseDir = Join-Path $projectRoot 'release'
    New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
    $archive = Join-Path $releaseDir 'TranEasy-v0.1.0-win64-portable.zip'
    Compress-Archive -Path $output -DestinationPath $archive -Force
    Write-Output "Portable directory: $output"
    Write-Output "ZIP: $archive"
} finally {
    $env:PYTHONUSERBASE = $previousUserBase
    Pop-Location
}
