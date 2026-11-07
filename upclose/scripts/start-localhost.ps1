[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent $PSScriptRoot
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if ($dockerCommand) {
    $dockerExecutable = $dockerCommand.Source
} else {
    $dockerExecutable = Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe'
    if (-not (Test-Path -LiteralPath $dockerExecutable)) {
        throw 'Docker CLI not found. Start Docker Desktop and add its CLI directory to PATH.'
    }
}
$composeArguments = @('compose', '-f', 'infra/compose.yaml', '-f', 'infra/compose.hosts.yaml', '-f', 'infra/compose.localhost.yaml')
Push-Location $workspaceRoot
try {
    & $dockerExecutable @composeArguments config --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Local hostname configuration failed validation.' }
    & $dockerExecutable @composeArguments up --build -d api worker app landing local-edge
    if ($LASTEXITCODE -ne 0) { throw 'Local hostname startup failed. Check Docker output and port 80 availability.' }
    & $dockerExecutable @composeArguments exec -T api alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
    # App nginx resolves API at startup; refresh after a recreated API container.
    & $dockerExecutable @composeArguments restart app
    if ($LASTEXITCODE -ne 0) { throw 'App proxy restart failed.' }
    Write-Host 'Landing: http://localhost'
    Write-Host 'Product: http://app.localhost'
    Write-Host 'Existing sessions remain at their original origin; start a new tab/session on app.localhost.'
} finally {
    Pop-Location
}
