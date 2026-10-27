# Publishes the static site folder to a local clone of the public repository on gh-pages.
# Uses Git Credential Manager for push authentication (no credentials stored or passed).

[CmdletBinding()]
param (
    [Parameter(Mandatory = $true)]
    [string]$PublicRepoPath,

    [Parameter(Mandatory = $false)]
    [string]$Branch = "gh-pages",

    [Parameter(Mandatory = $false)]
    [string]$SitePath = '',

    [Parameter(Mandatory = $false)]
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PublicRepoPath)) {
    throw "Target path does not exist: $PublicRepoPath"
}

$ResolvedRepo = (Resolve-Path $PublicRepoPath).Path
if (-not (Test-Path (Join-Path $ResolvedRepo ".git"))) {
    throw "Target directory is not a git repository: $ResolvedRepo"
}

if (-not $SitePath) { $SitePath = Join-Path $PSScriptRoot '..\site' }
$ResolvedSite = (Resolve-Path -LiteralPath $SitePath).Path

function Invoke-SiteGit {
    param([string[]]$GitArguments)
    $Result = & git -C $ResolvedRepo @GitArguments
    if ($LASTEXITCODE -ne 0) { throw 'Site git operation failed.' }
    return $Result
}

Write-Host "Publishing site from $ResolvedSite to $ResolvedRepo on branch $Branch..."

# The configured clone is dedicated to publication and already on the target branch.
$CurrentBranch = (Invoke-SiteGit @('branch', '--show-current')).Trim()
if ($CurrentBranch -ne $Branch) {
    throw 'Site clone must already be on the configured branch.'
}
if (Invoke-SiteGit @('status', '--porcelain')) {
    throw 'Site clone must have a clean working tree before publication.'
}

# Copy files from site/ into repository root
Get-ChildItem -Path $ResolvedSite -Recurse | ForEach-Object {
    $RelPath = $_.FullName.Substring($ResolvedSite.Length + 1)
    $DestPath = Join-Path $ResolvedRepo $RelPath
    if ($_.PSIsContainer) {
        if (-not (Test-Path $DestPath)) {
            New-Item -ItemType Directory -Path $DestPath -Force | Out-Null
        }
    } else {
        $DestDir = Split-Path $DestPath -Parent
        if (-not (Test-Path $DestDir)) {
            New-Item -ItemType Directory -Path $DestDir -Force | Out-Null
        }
        Copy-Item -Path $_.FullName -Destination $DestPath -Force
    }
}

# Check git status for modifications
$Status = Invoke-SiteGit @('status', '--porcelain')
if ($Status) {
    Write-Host "Data changes detected. Staging and committing..."
    Invoke-SiteGit @('add', '-A')
    $Timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Invoke-SiteGit @('commit', '-m', "Update public site data ($Timestamp)")

} else {
    Write-Host "No data changes detected. Public site is up to date."
}

# Retry a previous failed publication even if its local commit already exists.
if (-not $DryRun) {
    Write-Host "Pushing to remote origin branch $Branch..."
    Invoke-SiteGit @('push', 'origin', $Branch)
} else {
    Write-Host "Dry run enabled: local commit only."
}
