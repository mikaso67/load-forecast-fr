param(
    [string]$WorkspacePath = (Split-Path -Parent $PSScriptRoot)
)
$ErrorActionPreference = 'Stop'
# The watchdog runs every 15 minutes: no progress bars in its window.
$ProgressPreference = 'SilentlyContinue'
Set-Location $WorkspacePath
$PythonPath = (Resolve-Path '../load-forecast-fr-venv/Scripts/python.exe').Path
. ./scripts/load_env.ps1
$env:PYTHONPATH = Join-Path $WorkspacePath 'src'
$env:LFF_CODE_COMMIT = (git rev-parse HEAD).Trim()
# Prefect keeps its own state in PostgreSQL: SQLite locks under the morning catch-up load.
$PrefectUser = [uri]::EscapeDataString($env:POSTGRES_USER)
$PrefectPassword = [uri]::EscapeDataString($env:POSTGRES_PASSWORD)
$PrefectHost = if ($env:POSTGRES_HOST) { $env:POSTGRES_HOST } else { '127.0.0.1' }
$PrefectPort = if ($env:POSTGRES_PORT) { $env:POSTGRES_PORT } else { '5432' }
$env:PREFECT_API_DATABASE_CONNECTION_URL = "postgresql+asyncpg://${PrefectUser}:${PrefectPassword}@${PrefectHost}:${PrefectPort}/prefect"

function Invoke-Native {
    param([scriptblock]$Command)
    # Docker writes progress on stderr; only the exit code says whether it worked.
    $Previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Command 2>&1 | Out-Null
    } finally {
        $ErrorActionPreference = $Previous
    }
    return $LASTEXITCODE
}
function Get-UnreachablePort {
    foreach ($HostPort in 5432, 5000) {
        if (-not (Test-NetConnection -ComputerName 127.0.0.1 -Port $HostPort -InformationLevel Quiet -WarningAction SilentlyContinue)) {
            return $HostPort
        }
    }
    return $null
}
$DockerReady = $false
for ($i = 0; $i -lt 30; $i++) {
    if ((Invoke-Native { docker info --format '{{.ServerVersion}}' }) -eq 0) { $DockerReady = $true; break }
    Start-Sleep -Seconds 10
}
if (-not $DockerReady) { throw 'Docker engine not ready after five minutes. Is Docker Desktop running?' }
$Compose = @('compose', '--env-file', '.env', '-f', 'infra/docker-compose.infra.yml')
if ((Invoke-Native { docker @Compose up -d --wait }) -ne 0) { throw 'docker compose up failed: PostgreSQL or MLflow is not healthy.' }
# Windows can reserve a published port at boot: the container then runs without its host binding.
$Unreachable = Get-UnreachablePort
if ($Unreachable) {
    Write-Warning "Port $Unreachable not reachable from Windows; recreating the containers."
    if ((Invoke-Native { docker @Compose up -d --wait --force-recreate }) -ne 0) { throw 'docker compose recreate failed.' }
    $Unreachable = Get-UnreachablePort
    if ($Unreachable) { throw "Port $Unreachable still not reachable. Reserve it: see docs/operations.md, section 7." }
}

$Logs = Join-Path $env:LFF_RUNTIME_DIR 'logs'
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
function Invoke-OrphanRecovery {
    param([int]$OlderThan, [switch]$Restart)
    $RecoveryLog = Join-Path $Logs 'orphan_recovery.log'
    $RecoveryArgs = @('-m', 'load_forecast.ops', 'cancel-orphans', '--older-than', "$OlderThan")
    if ($Restart) { $RecoveryArgs += '--restart' }
    try {
        $RecoveryErrorAction = $ErrorActionPreference
        try {
            # Redirected native stderr must not turn a successful exit into an incident.
            $ErrorActionPreference = 'Continue'
            & $PythonPath @RecoveryArgs 2>&1 | Out-File -FilePath $RecoveryLog -Append -Encoding UTF8
        } finally {
            $ErrorActionPreference = $RecoveryErrorAction
        }
        if ($LASTEXITCODE -ne 0) { throw "cancel-orphans exited with code $LASTEXITCODE" }
    } catch {
        $Incident = @{
            event = 'orphan_recovery_failed'
            time = [DateTimeOffset]::UtcNow.ToString('o')
            older_than = $OlderThan
            restart = [bool]$Restart
            error = $_.Exception.Message
        }
        Write-Warning "Orphan recovery failed; production continues. See $RecoveryLog"
        try {
            Add-Content -Path $RecoveryLog -Value ($Incident | ConvertTo-Json -Compress) -Encoding UTF8
            $Incidents = Join-Path $env:LFF_RUNTIME_DIR 'incidents'
            New-Item -ItemType Directory -Force -Path $Incidents | Out-Null
            $IncidentPath = Join-Path $Incidents ("orphan_recovery_$([guid]::NewGuid().ToString('N')).json")
            $Incident | ConvertTo-Json | Set-Content -Path $IncidentPath -Encoding UTF8
        } catch {
            Write-Warning 'Could not persist orphan recovery diagnostics; production continues.'
        }
    }
}
function Test-PrefectHealth {
    param([int]$Attempts)
    for ($i = 0; $i -lt $Attempts; $i++) {
        try { if (Invoke-RestMethod "$env:PREFECT_API_URL/health" -TimeoutSec 5) { return $true } } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

# Windows (Hyper-V, WSL) reserves TCP port ranges that move at boot: the server port is chosen
# at start among candidates that can really be bound, and recorded for later health checks.
$PortFile = Join-Path $env:LFF_RUNTIME_DIR 'prefect_port.txt'
function Set-PrefectPort {
    param([int]$Port)
    $env:PREFECT_API_URL = "http://127.0.0.1:$Port/api"
}
if (Test-Path $PortFile) { Set-PrefectPort -Port ([int](Get-Content $PortFile -Raw).Trim()) }

# Idempotent watchdog: keep a healthy server with a live runner, otherwise restart both cleanly.
$Production = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'prefect|load_forecast\.flows' })
$Runner = $Production | Where-Object { $_.CommandLine -match 'load_forecast\.flows serve' }
if ($Runner -and (Test-PrefectHealth -Attempts 15)) {
    Invoke-OrphanRecovery -OlderThan 900
    Write-Output 'Production processes healthy.'
    exit 0
}

# A process left over from sleep or a crash can hold the server port without answering.
$Production | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-NetTCPConnection -LocalPort 4200, 8420, 8421, 8422, 8423, 8424 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3

$Port = $null
foreach ($Candidate in 4200, 8420, 8421, 8422, 8423, 8424) {
    try {
        $Probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Candidate)
        $Probe.Start(); $Probe.Stop(); $Port = $Candidate; break
    } catch { }
}
if (-not $Port) { throw 'No candidate port can be bound for the Prefect server.' }
Set-Content -Path $PortFile -Value $Port
Set-PrefectPort -Port $Port

Start-Process -FilePath $PythonPath -ArgumentList @('-m', 'prefect', 'server', 'start', '--host', '127.0.0.1', '--port', "$Port") -WindowStyle Hidden -RedirectStandardOutput "$Logs/server.out.log" -RedirectStandardError "$Logs/server.err.log"
if (-not (Test-PrefectHealth -Attempts 60)) { throw 'Prefect server did not become healthy within two minutes.' }
Invoke-OrphanRecovery -OlderThan 0 -Restart
Start-Process -FilePath $PythonPath -ArgumentList @('-m', 'load_forecast.flows', 'serve') -WorkingDirectory $WorkspacePath -WindowStyle Hidden -RedirectStandardOutput "$Logs/runner.out.log" -RedirectStandardError "$Logs/runner.err.log"
Write-Output "Prefect server and runner started on port $Port."
