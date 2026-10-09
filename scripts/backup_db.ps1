# Developer-only backup of the already running stack. No container lifecycle commands.
[CmdletBinding()]
param(
    [string]$BackupRoot = (Join-Path $HOME 'backups/load-forecast-fr'),
    [string]$PostgresUser = $env:POSTGRES_USER,
    [string]$DockerExecutable = 'docker.exe'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function ConvertTo-LffNativeArgument([string]$Argument) {
    # ProcessStartInfo.Arguments follows Windows argv quoting, without invoking a shell.
    return '"' + (($Argument -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}

function Assert-LffPlainPath([string]$Path) {
    $LffItem = New-Object IO.DirectoryInfo ([IO.Path]::GetFullPath($Path))
    while ($null -ne $LffItem) {
        if ($LffItem.Exists -and ($LffItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Backup paths must not contain symbolic links or junctions.'
        }
        $LffItem = $LffItem.Parent
    }
}

function Write-LffDump([string]$Database, [string]$Destination) {
    $LffArguments = @('compose', '--env-file', '.env', '-f', 'infra/docker-compose.infra.yml',
                      'exec', '-T', 'postgres', 'pg_dump', '-U', $PostgresUser,
                      '-Fc', '-d', $Database)
    $LffInfo = New-Object Diagnostics.ProcessStartInfo
    $LffInfo.FileName = $DockerExecutable
    $LffInfo.Arguments = ($LffArguments | ForEach-Object { ConvertTo-LffNativeArgument $_ }) -join ' '
    $LffInfo.WorkingDirectory = $LffRepository
    $LffInfo.UseShellExecute = $false
    $LffInfo.CreateNoWindow = $true
    $LffInfo.RedirectStandardOutput = $true
    $LffInfo.RedirectStandardError = $true
    $LffProcess = New-Object Diagnostics.Process
    $LffProcess.StartInfo = $LffInfo
    $LffStream = $null
    $LffStarted = $false
    try {
        if (-not $LffProcess.Start()) { throw 'Backup process could not start.' }
        $LffStarted = $true
        # Drain stderr concurrently to avoid a full pipe, but never display its contents.
        $LffErrors = $LffProcess.StandardError.ReadToEndAsync()
        $LffStream = [IO.File]::Open($Destination, 'CreateNew', 'Write', 'None')
        $LffProcess.StandardOutput.BaseStream.CopyTo($LffStream)
        $LffStream.Dispose()
        $LffStream = $null
        $LffProcess.WaitForExit()
        $null = $LffErrors.GetAwaiter().GetResult()
        if ($LffProcess.ExitCode -ne 0) { throw "Database backup failed for $Database." }
        $LffStream = [IO.File]::OpenRead($Destination)
        $LffHeader = New-Object byte[] 5
        if ($LffStream.Length -le 5 -or $LffStream.Read($LffHeader, 0, 5) -ne 5 -or
            [Text.Encoding]::ASCII.GetString($LffHeader) -cne 'PGDMP') {
            throw "Database backup is not a custom archive for $Database."
        }
    } catch {
        # Do not expose native stderr, configuration values or process exceptions.
        throw "Database backup failed for $Database. Incomplete files are retained."
    } finally {
        if ($null -ne $LffStream) { $LffStream.Dispose() }
        if ($LffStarted -and -not $LffProcess.HasExited) {
            # Ends only this client's docker exec connection, not any container.
            $LffProcess.Kill()
            $LffProcess.WaitForExit()
        }
        $LffProcess.Dispose()
    }
}

$LffRepository = [IO.Path]::GetFullPath((Get-Location).ProviderPath).TrimEnd('\', '/')
if (-not (Test-Path -LiteralPath (Join-Path $LffRepository '.env') -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $LffRepository 'infra/docker-compose.infra.yml') -PathType Leaf)) {
    throw 'Run from the developer repository root with its .env and infrastructure Compose file.'
}
if ([string]::IsNullOrWhiteSpace($PostgresUser) -or $PostgresUser -eq 'lff_agent' -or
    $PostgresUser.IndexOfAny([char[]]@([char]0, [char]10, [char]13)) -ge 0) {
    throw 'Supply the administrator POSTGRES_USER through the process environment or -PostgresUser.'
}
$LffRoot = [IO.Path]::GetFullPath($BackupRoot).TrimEnd('\', '/')
if ($LffRoot -eq $LffRepository -or
    $LffRoot.StartsWith($LffRepository + [IO.Path]::DirectorySeparatorChar,
                       [StringComparison]::OrdinalIgnoreCase)) {
    throw 'BackupRoot must be outside the repository.'
}
Assert-LffPlainPath $LffRoot
$null = [IO.Directory]::CreateDirectory($LffRoot)
$LffName = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [guid]::NewGuid().ToString('N')
$LffFolder = Join-Path $LffRoot $LffName
$null = [IO.Directory]::CreateDirectory($LffFolder)
foreach ($LffDatabase in @('load_forecast', 'mlflow')) {
    Write-LffDump $LffDatabase (Join-Path $LffFolder ($LffDatabase + '.dump.partial'))
}
foreach ($LffDatabase in @('load_forecast', 'mlflow')) {
    Move-Item -LiteralPath (Join-Path $LffFolder ($LffDatabase + '.dump.partial')) `
              -Destination (Join-Path $LffFolder ($LffDatabase + '.dump'))
}
[IO.File]::WriteAllText((Join-Path $LffFolder 'backup.complete'), '')

# Retire only completed, recognized sets, never unrelated folders or partial backups.
$LffCompleted = @(Get-ChildItem -LiteralPath $LffRoot -Directory | Where-Object {
    $_.Name -match '^\d{8}T\d{9}Z-[0-9a-f]{32}$' -and
    (Test-Path -LiteralPath (Join-Path $_.FullName 'backup.complete') -PathType Leaf)
} | Sort-Object Name -Descending)
foreach ($LffOld in ($LffCompleted | Select-Object -Skip 7)) {
    $LffOldPath = [IO.Path]::GetFullPath($LffOld.FullName)
    if (-not $LffOldPath.StartsWith($LffRoot + [IO.Path]::DirectorySeparatorChar,
                                  [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing to prune a backup outside BackupRoot.'
    }
    Assert-LffPlainPath $LffOldPath
    $LffFiles = @(Get-ChildItem -LiteralPath $LffOldPath -Force)
    if ($LffFiles.Count -ne 3 -or @($LffFiles | Where-Object {
        $_.PSIsContainer -or ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        $_.Name -notin @('load_forecast.dump', 'mlflow.dump', 'backup.complete')
    }).Count -gt 0) {
        throw 'Refusing to prune a backup set containing unexpected files.'
    }
    foreach ($LffFile in $LffFiles) { Remove-Item -LiteralPath $LffFile.FullName }
    Remove-Item -LiteralPath $LffOldPath
}
foreach ($LffDatabase in @('load_forecast', 'mlflow')) {
    $LffFile = Get-Item -LiteralPath (Join-Path $LffFolder ($LffDatabase + '.dump'))
    Write-Output ('{0} {1} bytes' -f $LffFile.FullName, $LffFile.Length)
}
