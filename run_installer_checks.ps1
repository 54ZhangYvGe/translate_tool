param([string]$ResumeTestRoot = '')
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$resume = $ResumeTestRoot -ne ''
$testRoot = if ($resume) { [IO.Path]::GetFullPath($ResumeTestRoot) } else { Join-Path $projectRoot ('release-test-install-' + (Get-Date -Format 'yyyyMMdd-HHmmss')) }
if (!$testRoot.StartsWith((Join-Path $projectRoot 'release-test-install-'), [StringComparison]::OrdinalIgnoreCase)) { throw 'Test path is outside the installer test workspace.' }
$installed = Join-Path $testRoot 'app'
$localData = Join-Path $installed 'test-appdata'
$userData = Join-Path $localData 'TranEasy'
$desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'TranEasy.lnk'
$startLink = Join-Path ([Environment]::GetFolderPath('Programs')) 'TranEasy\TranEasy.lnk'
if (!$resume) {
if ((Test-Path -LiteralPath $desktopLink) -or (Test-Path -LiteralPath $startLink)) { throw 'Existing TranEasy shortcut: stop to avoid overwriting user files.' }
if (Test-Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{D7C627CB-0B2B-4D88-B2AC-3AA19FD389E1}_is1') { throw 'Existing TranEasy installation: stop test.' }
New-Item -ItemType Directory -Path $testRoot | Out-Null
$setup = Start-Process -FilePath (Join-Path $projectRoot 'release\TranEasy-Setup-v0.1.0.exe') -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=`"$installed`" /TASKS=desktopicon /LOG=`"$testRoot\install.log`"" -PassThru -Wait -WindowStyle Hidden
if ($setup.ExitCode -ne 0) { throw "Install failed: $($setup.ExitCode)" }
} elseif (!(Test-Path -LiteralPath (Join-Path $installed 'installed.mode'))) { throw 'Cannot resume an absent test installation.' }
$report = @{ installed = $true; shortcuts = @(); phases = @(); cleanExit = $false; uninstalled = $false; userDataRetained = $false }
New-Item -ItemType Directory -Path $userData -Force | Out-Null
if (!$resume) { Copy-Item -LiteralPath (Join-Path $projectRoot 'tests\release-config.json') -Destination (Join-Path $userData 'config.json') }
function Start-TestProcess([string]$file, [string]$arguments) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $file
    $info.Arguments = $arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WorkingDirectory = $installed
    $info.EnvironmentVariables['LOCALAPPDATA'] = $localData
    $info.EnvironmentVariables['SCREENTRANS_RELEASE_TEST'] = '1'
    [Diagnostics.Process]::Start($info)
}
foreach ($phase in @('write','restart','cleared')) {
    if (!$resume) {
    $run = Start-TestProcess (Join-Path $installed 'ScreenTransResident.exe') "--release-smoke --smoke-phase=$phase"
    if (!$run.WaitForExit(120000)) { $run.Kill(); throw "Installed GUI test timeout: $phase" }
    }
    $result = Get-Content -LiteralPath (Join-Path $userData "smoke-report-$phase.json") -Raw | ConvertFrom-Json
    if (!$result.ok -or (!$resume -and $run.ExitCode -ne 0)) { throw "Installed GUI test failed: $($result.error)" }
    $report.phases += $result
}
$shell = New-Object -ComObject WScript.Shell
foreach ($path in @($desktopLink, $startLink)) {
    if (!(Test-Path -LiteralPath $path)) { throw 'Shortcut missing' }
    $link = $shell.CreateShortcut($path)
    if ($link.TargetPath -ne (Join-Path $installed 'TranEasy.exe')) { throw 'Wrong shortcut target' }
    if ($link.IconLocation -notlike '*transeasy-icon.ico*') { throw 'Wrong shortcut icon' }
    # Start the actual shortcut target with isolated credentials rather than
    # letting Explorer inherit the user's real API credential directory.
    $launcher = Start-TestProcess $link.TargetPath ''
    $stateFile = Join-Path $userData 'data\app_state.json'
    $deadline = (Get-Date).AddSeconds(20)
    while (!(Test-Path -LiteralPath $stateFile) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 100 }
    if (!(Test-Path -LiteralPath $stateFile)) { throw 'Shortcut target failed to start resident' }
    $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    $stop = Start-TestProcess $link.TargetPath '--exit'
    if (!$stop.WaitForExit(20000) -or $stop.ExitCode -ne 0) { throw 'Normal exit failed' }
    $exitDeadline = (Get-Date).AddSeconds(10)
    while (((Get-Process -Id $state.pid -ErrorAction SilentlyContinue) -or (Get-Process -Id $launcher.Id -ErrorAction SilentlyContinue)) -and (Get-Date) -lt $exitDeadline) { Start-Sleep -Milliseconds 100 }
    if ((Get-Process -Id $state.pid -ErrorAction SilentlyContinue) -or (Get-Process -Id $launcher.Id -ErrorAction SilentlyContinue)) { throw 'Process remained after exit' }
    $report.shortcuts += $path
}
$report.cleanExit = $true
$launcher = Start-TestProcess (Join-Path $installed 'TranEasy.exe') ''
Start-Sleep -Seconds 4
$state = Get-Content -LiteralPath (Join-Path $userData 'data\app_state.json') -Raw | ConvertFrom-Json
$uninstaller = Join-Path $installed 'unins000.exe'
$uninstallInfo = [Diagnostics.ProcessStartInfo]::new()
$uninstallInfo.FileName = $uninstaller
$uninstallInfo.Arguments = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=`"$testRoot\uninstall.log`""
$uninstallInfo.UseShellExecute = $false
$uninstallInfo.CreateNoWindow = $true
$uninstallInfo.EnvironmentVariables['LOCALAPPDATA'] = $localData
$uninstallRun = [Diagnostics.Process]::Start($uninstallInfo)
if (!$uninstallRun.WaitForExit(60000) -or $uninstallRun.ExitCode -ne 0) { throw 'Uninstall failed' }
$report.uninstalled = !(Test-Path -LiteralPath (Join-Path $installed 'TranEasy.exe')) -and !(Test-Path -LiteralPath (Join-Path $installed 'ScreenTransResident.exe'))
$report.userDataRetained = (Test-Path -LiteralPath (Join-Path $userData 'config.json')) -and (Test-Path -LiteralPath (Join-Path $userData 'data'))
$report.cleanExit = !(Get-Process -Id $state.pid -ErrorAction SilentlyContinue) -and !(Get-Process -Id $launcher.Id -ErrorAction SilentlyContinue)
if (!$report.uninstalled -or !$report.userDataRetained -or !$report.cleanExit -or (Test-Path -LiteralPath $desktopLink) -or (Test-Path -LiteralPath $startLink)) { throw 'Uninstall/data/process post-check failed' }
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $testRoot 'installer-checks.json') -Encoding utf8
Write-Output (Join-Path $testRoot 'installer-checks.json')
