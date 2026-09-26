param([string]$Compiler = (Join-Path $PSScriptRoot 'build\inno-tool\compiler\ISCC.exe'))
$ErrorActionPreference = 'Stop'
if (!(Test-Path -LiteralPath $Compiler)) { throw 'Provide official Inno Setup ISCC.exe via -Compiler.' }
if (!(Test-Path -LiteralPath (Join-Path $PSScriptRoot 'dist\ScreenTrans\TranEasy.exe'))) { throw 'Run build_release.ps1 first.' }
& $Compiler (Join-Path $PSScriptRoot 'installer\TranEasy.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed' }
