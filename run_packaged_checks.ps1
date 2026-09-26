param([string]$AhkBase = 'C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe')
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$testRoot = Join-Path $projectRoot ('release-test-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot 'dist\ScreenTrans') -Destination $testRoot -Recurse
$portable = Join-Path $testRoot 'ScreenTrans'
Copy-Item -LiteralPath (Join-Path $projectRoot 'tests\release-config.json') -Destination (Join-Path $portable 'config.json')
$testAppData = Join-Path $portable 'test-appdata'
New-Item -ItemType Directory -Path $testAppData | Out-Null
function Start-Isolated([string]$file, [string]$arguments) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $file
    $info.Arguments = $arguments
    $info.WorkingDirectory = $portable
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.EnvironmentVariables['LOCALAPPDATA'] = $testAppData
    $info.EnvironmentVariables['SCREENTRANS_RELEASE_TEST'] = '1'
    return [System.Diagnostics.Process]::Start($info)
}
$report = @{ testDirectory = $portable; phases = @(); ahkStarted = $false; captureHotkey = $false; history = $false; cleanExit = $false }
$launcher = $null
$state = $null
try {
foreach ($phase in @('write', 'restart', 'cleared')) {
    $process = Start-Isolated (Join-Path $portable 'ScreenTransResident.exe') "--release-smoke --smoke-phase=$phase"
    if (!$process.WaitForExit(120000)) { $process.Kill(); throw "Smoke phase timed out: $phase" }
    $phaseReport = Join-Path $portable "smoke-report-$phase.json"
    if (!(Test-Path -LiteralPath $phaseReport)) { throw "No smoke report: $phase (exit $($process.ExitCode))" }
    $result = Get-Content -LiteralPath $phaseReport -Raw | ConvertFrom-Json
    $report.phases += $result
    if (!$result.ok -or $process.ExitCode -ne 0) { throw "Smoke failed: $phase : $($result.error)" }
}
$launcher = Start-Isolated (Join-Path $portable 'TranEasy.exe') ''
$stateFile = Join-Path $portable 'data\app_state.json'
$deadline = (Get-Date).AddSeconds(20)
while (!(Test-Path -LiteralPath $stateFile) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 100 }
if (!(Test-Path -LiteralPath $stateFile)) { throw 'AHK did not start the packaged resident' }
$state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
$report.ahkStarted = $true
$capture = Start-Process -FilePath $AhkBase -ArgumentList "/ErrorStdOut `"$projectRoot\tests\ahk_capture_smoke.ahk`" $($state.pid)" -PassThru -Wait -WindowStyle Hidden
$report.captureHotkey = $capture.ExitCode -eq 0
if (!$report.captureHotkey) {
    $report.hotkeyManualCheckRequired = "Injected key did not complete capture test (code $($capture.ExitCode)); verify physical key in interactive session."
    Write-Warning $report.hotkeyManualCheckRequired
}
$requestId = 'packaged-ahk-smoke'
$payload = @{ id = $requestId; text = 'AHK portable smoke'; source = 'selection'; action = 'translate' } | ConvertTo-Json -Compress
$request = Join-Path $portable "data\requests\request_$requestId.json"
[IO.File]::WriteAllText($request + '.tmp', $payload, [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath ($request + '.tmp') -Destination $request
$history = Join-Path $portable ('data\' + (Get-Date -Format 'yyyy-MM-dd') + '.txt')
$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    if ((Test-Path -LiteralPath $history) -and ((Get-Content -LiteralPath $history -Raw) -match 'AHK portable smoke')) { $report.history = $true; break }
    Start-Sleep -Milliseconds 100
}
if (!$report.history) { throw 'AHK resident protocol/history test failed' }
Start-Sleep -Seconds 2
$running = Get-Process -Id $state.pid -ErrorAction Stop
$report.residentWorkingSetMB = [math]::Round($running.WorkingSet64 / 1MB, 1)
$closer = Start-Process -FilePath $AhkBase -ArgumentList "/ErrorStdOut `"$projectRoot\tests\close_ahk.ahk`" $($launcher.Id)" -PassThru -Wait -WindowStyle Hidden
if ($closer.ExitCode -ne 0) { throw 'Could not request normal AHK exit' }
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Process -Id $state.pid -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 100 }
$report.cleanExit = !(Get-Process -Id $state.pid -ErrorAction SilentlyContinue) -and !(Get-Process -Id $launcher.Id -ErrorAction SilentlyContinue)
if (!$report.cleanExit) { throw 'A packaged process remained after AHK exit' }
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $testRoot 'packaged-checks.json') -Encoding UTF8
Write-Output ("Packaged checks completed (review manual checks): " + (Join-Path $testRoot 'packaged-checks.json'))
Write-Output ("Resident working set MB: " + $report.residentWorkingSetMB)
} finally {
    # Only our isolated test processes are eligible for fallback cleanup.
    if ($launcher -and !$launcher.HasExited) {
        $launcher.Kill()
        $launcher.WaitForExit(5000) | Out-Null
    }
    if ($state) {
        $leftover = Get-Process -Id $state.pid -ErrorAction SilentlyContinue
        if ($leftover -and $leftover.Path -eq (Join-Path $portable 'ScreenTransResident.exe')) {
            $leftover.Kill()
            $leftover.WaitForExit(5000) | Out-Null
        }
    }
}
