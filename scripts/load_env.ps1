# Dot-source from the desired repository folder to populate the current process.
[CmdletBinding()]
param([string]$EnvFile = (Join-Path (Get-Location) '.env'))

& {
    param([string]$LffEnvPath)
    if (-not (Test-Path -LiteralPath $LffEnvPath -PathType Leaf)) {
        throw 'Environment file is missing. Create the current folder .env first.'
    }
    try {
        $LffLines = [IO.File]::ReadAllLines($LffEnvPath, [Text.Encoding]::UTF8)
    } catch {
        throw 'Environment file could not be read.'
    }
    $LffValues = @{}
    $LffLineNumber = 0
    foreach ($LffLine in $LffLines) {
        $LffLineNumber++
        if ([string]::IsNullOrWhiteSpace($LffLine) -or $LffLine.TrimStart().StartsWith('#')) {
            continue
        }
        if ($LffLine -notmatch '^\s*(?:export\s+)?(?<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)$') {
            throw "Invalid environment assignment at line $LffLineNumber."
        }
        $LffKey = $Matches['key']
        $LffValue = $Matches['value'].Trim()
        if ($LffValue.StartsWith("'") -or $LffValue.StartsWith('"')) {
            if ($LffValue -notmatch '^([''"])(?<literal>.*?)\1\s*(?:#.*)?$') {
                throw "Invalid quoted environment value at line $LffLineNumber."
            }
            $LffValue = $Matches['literal']
        } else {
            $LffValue = ($LffValue -replace '\s+#.*$', '').TrimEnd()
        }
        if ($LffValue.Contains([char]0)) {
            throw "Invalid environment value at line $LffLineNumber."
        }
        $LffValues[$LffKey] = $LffValue
    }
    # Parse the entire file before changing the environment; never evaluate its contents.
    foreach ($LffKey in $LffValues.Keys) {
        try {
            [Environment]::SetEnvironmentVariable($LffKey, $LffValues[$LffKey], 'Process')
        } catch {
            throw 'An environment assignment failed.'
        }
    }
} $EnvFile
