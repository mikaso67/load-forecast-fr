$ErrorActionPreference = 'Stop'
$LffLoader = Join-Path $PSScriptRoot '../scripts/load_env.ps1'
$LffFakeFolder = Join-Path ([IO.Path]::GetTempPath()) ('lff-env-check-' + [guid]::NewGuid())
$LffKeys = @('LFF_TEST_ENV_SINGLE', 'LFF_TEST_ENV_DOUBLE', 'LFF_TEST_ENV_PLAIN',
             'LFF_TEST_ENV_EXPORT', 'LFF_TEST_ENV_EMPTY', 'LFF_TEST_ENV_OVERWRITE',
             'LFF_TEST_ENV_ATOMIC', 'LFF_TEST_ENV_BOM')
$LffPrevious = @{}
foreach ($LffKey in $LffKeys) {
    $LffPrevious[$LffKey] = [Environment]::GetEnvironmentVariable($LffKey, 'Process')
}
New-Item -ItemType Directory -Path $LffFakeFolder | Out-Null
Push-Location $LffFakeFolder
try {
    $env:LFF_TEST_ENV_OVERWRITE = 'old'
    $LffFake = @'
# Fake values only. The loader must never output them or execute them.
LFF_TEST_ENV_SINGLE=' literal $value # = ` $(throw "must not execute") '
LFF_TEST_ENV_DOUBLE=" dollar $value # = C:\fake\path " # trailing comment
LFF_TEST_ENV_PLAIN=plain=literal#hash # comment
export LFF_TEST_ENV_EXPORT = exported
LFF_TEST_ENV_EMPTY=''
LFF_TEST_ENV_OVERWRITE=first
LFF_TEST_ENV_OVERWRITE=last
'@
    [IO.File]::WriteAllText((Join-Path $LffFakeFolder '.env'), $LffFake, [Text.Encoding]::UTF8)
    $LffOutput = @(. $LffLoader)
    if ($LffOutput.Count -ne 0) { throw 'Loader emitted output.' }
    if ($env:LFF_TEST_ENV_SINGLE -cne ' literal $value # = ` $(throw "must not execute") ') {
        throw 'Single quoted literal was changed.'
    }
    if ($env:LFF_TEST_ENV_DOUBLE -cne ' dollar $value # = C:\fake\path ') {
        throw 'Double quoted literal was changed.'
    }
    if ($env:LFF_TEST_ENV_PLAIN -cne 'plain=literal#hash' -or
        $env:LFF_TEST_ENV_EXPORT -cne 'exported' -or
        $env:LFF_TEST_ENV_OVERWRITE -cne 'last' -or $env:LFF_TEST_ENV_EMPTY) {
        throw 'Plain, exported, duplicate or empty assignment failed.'
    }
    $LffFakePath = Join-Path $LffFakeFolder 'bom.env'
    [IO.File]::WriteAllText($LffFakePath, 'LFF_TEST_ENV_BOM=bom', (New-Object Text.UTF8Encoding $true))
    . $LffLoader -EnvFile $LffFakePath
    if ($env:LFF_TEST_ENV_BOM -cne 'bom') { throw 'UTF-8 BOM handling failed.' }
    foreach ($LffInvalid in @("LFF_TEST_ENV_ATOMIC=uncommitted`nINVALID-LINE",
                             "LFF_TEST_ENV_ATOMIC='unclosed", 'LFF_TEST_ENV_ATOMIC="unclosed')) {
        [IO.File]::WriteAllText($LffFakePath, $LffInvalid)
        $LffFailed = $false
        try { . $LffLoader -EnvFile $LffFakePath } catch {
            $LffFailed = $_.Exception.Message -match 'line [12]'
        }
        if (-not $LffFailed -or $env:LFF_TEST_ENV_ATOMIC) { throw 'Invalid file was accepted.' }
    }
    $LffFailed = $false
    try { . $LffLoader -EnvFile (Join-Path $LffFakeFolder 'missing.env') } catch {
        $LffFailed = $_.Exception.Message -match 'missing'
    }
    if (-not $LffFailed) { throw 'Missing file did not fail clearly.' }
    Write-Output 'PASS: fake environment loading, literal quotes, no output, BOM and failure checks.'
} finally {
    Pop-Location
    foreach ($LffKey in $LffKeys) {
        [Environment]::SetEnvironmentVariable($LffKey, $LffPrevious[$LffKey], 'Process')
    }
    # Only these named fake files are removed; no real .env is opened by this check.
    foreach ($LffName in @('.env', 'bom.env')) {
        $LffFakePath = Join-Path $LffFakeFolder $LffName
        if (Test-Path -LiteralPath $LffFakePath) { Remove-Item -LiteralPath $LffFakePath }
    }
    Remove-Item -LiteralPath $LffFakeFolder
}
