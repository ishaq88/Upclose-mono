# Run from any directory. Requires the local Docker Desktop Linux engine.
[CmdletBinding()]
param([switch]$BackendOnly)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$composeFile = Join-Path $repoRoot 'infra/compose.yaml'
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if ($dockerCommand) {
    $script:DockerExecutable = $dockerCommand.Source
} else {
    $dockerDirectory = Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin'
    $script:DockerExecutable = Join-Path $dockerDirectory 'docker.exe'
    if (-not (Test-Path -LiteralPath $script:DockerExecutable)) {
        throw 'Docker CLI was not found. Install/start Docker Desktop and put docker on PATH.'
    }
    # Docker also locates its credential helper through PATH.
    $env:PATH = "$dockerDirectory;$env:PATH"
}

function Invoke-CheckedDocker([string[]]$Arguments) {
    & $script:DockerExecutable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker verification command failed (exit $LASTEXITCODE)."
    }
}

$compose = @('compose', '-f', $composeFile)
Invoke-CheckedDocker ($compose + @('config', '--quiet'))
Invoke-CheckedDocker ($compose + @('up', '-d', '--wait', 'postgres', 'floci'))
# Bucket/CORS setup is idempotent; storage tests use the emulator through its in-network name.
Invoke-CheckedDocker ($compose + @('run', '--rm', 'storage-init'))
$buildServices = if ($BackendOnly) { @('api') } else { @('api', 'web') }
Invoke-CheckedDocker ($compose + @('build') + $buildServices)

# Every invocation owns exactly this randomly named disposable database.
# Never migrate, truncate or drop the shared development database for tests.
$testDatabase = 'upclose_test_verify_' + [Guid]::NewGuid().ToString('N')
$created = $false
try {
    Invoke-CheckedDocker ($compose + @('exec', '-T', 'postgres', 'createdb', '-U', 'upclose', $testDatabase))
    $created = $true
    $testUrl = "postgresql+psycopg://upclose:local-development-only@postgres:5432/$testDatabase"
    $sourcePath = (Join-Path $repoRoot 'services/api').Replace('\', '/')
    Invoke-CheckedDocker ($compose + @(
        'run', '--rm', '--no-deps', '--volume', "${sourcePath}:/workspace", '--workdir', '/workspace',
        'api', 'python', 'scripts/export_contracts.py', '--check'
    ))
    Invoke-CheckedDocker ($compose + @(
        'run', '--rm', '--no-deps', '--volume', "${sourcePath}:/workspace", '--workdir', '/workspace',
        '-e', "MODEL_TEST_DATABASE_URL=$testUrl", '-e', 'S3_INTERNAL_ENDPOINT=http://floci:4566',
        '-e', 'S3_PUBLIC_ENDPOINT=http://floci:4566', 'api', 'python', '-m', 'pytest', '-q', '-rs'
    ))
    if (-not $BackendOnly) {
        $schemaPath = (Join-Path $repoRoot 'services/api/schema').Replace('\', '/')
        foreach ($npmScript in @('check:types', 'typecheck', 'test', 'build')) {
            $npmArguments = @('run', $npmScript)
            if ($npmScript -eq 'test') { $npmArguments += @('--', '--run') }
            Invoke-CheckedDocker ($compose + @('run', '--rm', '--no-deps', '--volume', "${schemaPath}:/services/api/schema:ro", 'web', 'npm') + $npmArguments)
        }
    }
} finally {
    if ($created) {
        if ($testDatabase -notmatch '^upclose_test_verify_[0-9a-f]{32}$') {
            throw 'Refusing cleanup: unexpected test database name.'
        }
        Invoke-CheckedDocker ($compose + @('exec', '-T', 'postgres', 'dropdb', '-U', 'upclose', $testDatabase))
    }
}
Write-Host 'Local verification passed. The disposable test database was removed.'
