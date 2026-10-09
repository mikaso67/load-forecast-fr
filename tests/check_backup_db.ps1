$ErrorActionPreference = 'Stop'
$LffScript = Join-Path $PSScriptRoot '../scripts/backup_db.ps1'
$LffFakeRoot = Join-Path ([IO.Path]::GetTempPath()) ('lff-backup-check-' + [guid]::NewGuid())
$LffFakeRepo = Join-Path $LffFakeRoot 'fake repository'
$LffBackups = Join-Path $LffFakeRoot 'backups with spaces'
$LffDocker = Join-Path $LffFakeRoot 'fake docker.exe'
$LffLog = Join-Path $LffFakeRoot 'docker-args.log'
$LffPrevious = @{}
foreach ($LffKey in @('LFF_TEST_DOCKER_LOG', 'LFF_TEST_BACKUP_FAIL_DB', 'LFF_TEST_BACKUP_INVALID')) {
    $LffPrevious[$LffKey] = [Environment]::GetEnvironmentVariable($LffKey, 'Process')
}
$null = [IO.Directory]::CreateDirectory((Join-Path $LffFakeRepo 'infra'))
[IO.File]::WriteAllText((Join-Path $LffFakeRepo '.env'), '# fake only; no real env is read')
[IO.File]::WriteAllText((Join-Path $LffFakeRepo 'infra/docker-compose.infra.yml'), '# fake')
powershell.exe -NoProfile -File (Join-Path $PSScriptRoot 'build_fake_docker.ps1') -OutputPath $LffDocker
if ($LASTEXITCODE -ne 0) { throw 'Fake executable compilation failed.' }
Push-Location $LffFakeRepo
try {
    $env:LFF_TEST_DOCKER_LOG = $LffLog
    $env:LFF_TEST_BACKUP_FAIL_DB = ''
    $env:LFF_TEST_BACKUP_INVALID = ''
    $LffExpectedBytes = [byte[]]@(80, 71, 68, 77, 80, 0, 10, 13, 128, 255, 1, 2, 3)
    for ($LffRun = 0; $LffRun -lt 9; $LffRun++) {
        $LffOutput = @(& $LffScript -BackupRoot $LffBackups -PostgresUser 'fake user' -DockerExecutable $LffDocker)
        if ($LffOutput.Count -ne 2 -or @($LffOutput | Where-Object { $_ -notmatch '\.dump 13 bytes$' }).Count) {
            throw 'Backup output was not limited to names and sizes.'
        }
    }
    $LffSets = @(Get-ChildItem -LiteralPath $LffBackups -Directory | Sort-Object Name)
    if ($LffSets.Count -ne 7) { throw 'Retention did not keep exactly seven successful sets.' }
    foreach ($LffSet in $LffSets) {
        foreach ($LffDatabase in @('load_forecast', 'mlflow')) {
            $LffBytes = [IO.File]::ReadAllBytes((Join-Path $LffSet.FullName ($LffDatabase + '.dump')))
            if ([Convert]::ToBase64String($LffBytes) -cne [Convert]::ToBase64String($LffExpectedBytes)) {
                throw 'Archive binary stream was corrupted.'
            }
        }
    }
    $LffArgLines = [IO.File]::ReadAllLines($LffLog)
    if ($LffArgLines.Count -ne 18 -or @($LffArgLines | Where-Object {
        $_ -notmatch '^compose\|--env-file\|\.env\|-f\|infra/docker-compose.infra.yml\|exec\|-T\|postgres\|pg_dump\|-U\|fake user\|-Fc\|-d\|(load_forecast|mlflow)$'
    }).Count) { throw 'Native arguments were not preserved.' }
    $LffBefore = ($LffSets.Name -join '|')
    $env:LFF_TEST_BACKUP_FAIL_DB = 'mlflow'
    $LffFailed = $false
    $LffFailureOutput = @()
    try {
        $LffFailureOutput = @(& $LffScript -BackupRoot $LffBackups -PostgresUser 'fake user' -DockerExecutable $LffDocker)
    } catch {
        $LffFailed = $_.Exception.Message -match 'backup failed' -and
                     $_.Exception.Message -notmatch 'FAKE_PRIVATE'
    }
    if (-not $LffFailed -or $LffFailureOutput.Count) { throw 'Failed dump was accepted or leaked output.' }
    $LffComplete = @(Get-ChildItem -LiteralPath $LffBackups -Directory | Where-Object {
        Test-Path -LiteralPath (Join-Path $_.FullName 'backup.complete')
    } | Sort-Object Name)
    if (($LffComplete.Name -join '|') -cne $LffBefore) { throw 'Failed backup pruned successful sets.' }
    $env:LFF_TEST_BACKUP_FAIL_DB = ''
    $env:LFF_TEST_BACKUP_INVALID = '1'
    $LffFailed = $false
    try { & $LffScript -BackupRoot $LffBackups -PostgresUser 'fake user' -DockerExecutable $LffDocker } catch {
        $LffFailed = $_.Exception.Message -match 'backup failed'
    }
    if (-not $LffFailed) { throw 'Invalid archive header was accepted.' }
    $LffFailed = $false
    try { & $LffScript -BackupRoot (Join-Path $LffFakeRepo 'backups') -PostgresUser 'fake user' -DockerExecutable $LffDocker } catch {
        $LffFailed = $_.Exception.Message -match 'outside'
    }
    if (-not $LffFailed) { throw 'Repository backup location was accepted.' }
    $LffFailed = $false
    try { & $LffScript -BackupRoot $LffBackups -PostgresUser 'lff_agent' -DockerExecutable $LffDocker } catch {
        $LffFailed = $_.Exception.Message -match 'administrator'
    }
    if (-not $LffFailed) { throw 'Read-only credentials were accepted.' }
    $env:LFF_TEST_BACKUP_INVALID = ''
    $LffFailed = $false
    try { & $LffScript -BackupRoot $LffBackups -PostgresUser 'fake user' -DockerExecutable (Join-Path $LffFakeRoot 'missing.exe') } catch {
        $LffFailed = $_.Exception.Message -match 'backup failed'
    }
    if (-not $LffFailed) { throw 'Missing native executable did not fail safely.' }
    $LffUnrelated = Join-Path $LffBackups 'unrelated folder'
    $null = [IO.Directory]::CreateDirectory($LffUnrelated)
    [IO.File]::WriteAllText((Join-Path $LffUnrelated 'keep.txt'), 'fake unrelated file')
    $LffOutput = @(& $LffScript -BackupRoot $LffBackups -PostgresUser 'fake user' -DockerExecutable $LffDocker)
    $LffComplete = @(Get-ChildItem -LiteralPath $LffBackups -Directory | Where-Object {
        Test-Path -LiteralPath (Join-Path $_.FullName 'backup.complete')
    })
    $LffPartialFiles = @(Get-ChildItem -LiteralPath $LffBackups -Recurse -File -Filter '*.partial')
    if ($LffOutput.Count -ne 2 -or $LffComplete.Count -ne 7 -or $LffPartialFiles.Count -ne 3 -or
        -not (Test-Path -LiteralPath (Join-Path $LffUnrelated 'keep.txt'))) {
        throw 'Successful pruning removed partial backups or unrelated files.'
    }
    $LffUnexpected = Join-Path $LffBackups '20000101T000000000Z-00000000000000000000000000000000'
    $null = [IO.Directory]::CreateDirectory($LffUnexpected)
    foreach ($LffName in @('backup.complete', 'load_forecast.dump', 'mlflow.dump', 'keep.txt')) {
        [IO.File]::WriteAllText((Join-Path $LffUnexpected $LffName), 'fake only')
    }
    $LffFailed = $false
    try { & $LffScript -BackupRoot $LffBackups -PostgresUser 'fake user' -DockerExecutable $LffDocker } catch {
        $LffFailed = $_.Exception.Message -match 'unexpected files'
    }
    if (-not $LffFailed -or @(Get-ChildItem -LiteralPath $LffUnexpected).Count -ne 4) {
        throw 'Unexpected contents were pruned.'
    }
    Write-Output 'PASS: binary archives, quoted argv, seven-set retention, failures and path checks.'
} finally {
    Pop-Location
    foreach ($LffKey in $LffPrevious.Keys) {
        [Environment]::SetEnvironmentVariable($LffKey, $LffPrevious[$LffKey], 'Process')
    }
    # Verify the precise, generated temporary target before recursively removing fake files.
    $LffResolved = [IO.Path]::GetFullPath($LffFakeRoot)
    $LffTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/')
    if (-not $LffResolved.StartsWith($LffTemp + [IO.Path]::DirectorySeparatorChar,
                                    [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($LffResolved) -notmatch '^lff-backup-check-[0-9a-f-]{36}$') {
        throw 'Refusing to remove an unexpected temporary target.'
    }
    Remove-Item -LiteralPath $LffResolved -Recurse
}
