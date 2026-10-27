[CmdletBinding()]
param(
    [string]$WorkspacePath = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = (Join-Path (Split-Path -Parent $PSScriptRoot) '../load-forecast-fr-venv/Scripts/python.exe')
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $WorkspacePath
. (Join-Path $WorkspacePath 'scripts/load_env.ps1')
$env:PYTHONPATH = Join-Path $WorkspacePath 'src'
& $PythonPath -m load_forecast.deadline
exit $LASTEXITCODE
