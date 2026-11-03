param([switch]$SkipDependencies)
$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    & python -m venv (Join-Path $projectDir '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python virtual environment.' }
}
if (-not $SkipDependencies) {
    & $pythonExe -m pip install -r (Join-Path $projectDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
& $pythonExe -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency validation failed.' }
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$parts = @($userPath -split ';' | Where-Object { $_ })
if ($parts.TrimEnd('\') -notcontains $projectDir.TrimEnd('\')) {
    $newPath = (($parts + $projectDir) -join ';')
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
}
if (($env:Path -split ';').TrimEnd('\') -notcontains $projectDir.TrimEnd('\')) {
    $env:Path += ';' + $projectDir
}
Write-Host 'Installed. Open a NEW PowerShell in your invoice folder and run: fapiao'
Write-Host 'For this PowerShell session, run: fapiao'
