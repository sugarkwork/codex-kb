[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-z0-9][a-z0-9_.-]{2,31}$')]
    [string]$AccountUsername,

    [string]$RepositoryUrl = 'https://github.com/sugarkwork/codex-kb.git',

    [string]$Branch = 'main',

    [string]$InstallDirectory = (Join-Path $HOME 'source\codex-kb'),

    [string]$PasswordEnvironmentVariable = 'CODEX_KB_ACCOUNT_PASSWORD',

    [string]$PassphraseEnvironmentVariable = 'CODEX_KB_ENCRYPTION_PASSPHRASE',

    [string]$CredentialsPath,

    [switch]$UseExistingCheckout
)

$ErrorActionPreference = 'Stop'

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is required but was not found on PATH."
    }
}

function Invoke-CodexKb {
    param([string[]]$Arguments)
    & $script:Cli @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "codex-kb exited with code $LASTEXITCODE."
    }
}

Require-Command git

if (-not [Environment]::GetEnvironmentVariable($PasswordEnvironmentVariable, 'Process') -or -not [Environment]::GetEnvironmentVariable($PassphraseEnvironmentVariable, 'Process')) {
    throw "Set non-empty process environment variables $PasswordEnvironmentVariable and $PassphraseEnvironmentVariable before running this script."
}

try {
    $resolvedInstallDirectory = [System.IO.Path]::GetFullPath($InstallDirectory)
    if (Test-Path -LiteralPath $resolvedInstallDirectory) {
        if (-not $UseExistingCheckout) {
            throw "Install directory already exists: $resolvedInstallDirectory. Re-run with -UseExistingCheckout only when it is the intended codex-kb checkout."
        }
    } else {
        $parent = Split-Path -Parent $resolvedInstallDirectory
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        & git clone --depth 1 --branch $Branch $RepositoryUrl $resolvedInstallDirectory
        if ($LASTEXITCODE -ne 0) {
            throw "git clone failed with code $LASTEXITCODE."
        }
    }

    $installScript = Join-Path $resolvedInstallDirectory 'install.ps1'
    if (-not (Test-Path -LiteralPath $installScript -PathType Leaf)) {
        throw "install.ps1 was not found in $resolvedInstallDirectory."
    }

    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
    & $installScript
    if ($LASTEXITCODE -ne 0) {
        throw "install.ps1 exited with code $LASTEXITCODE."
    }

    $script:Cli = Join-Path $HOME '.codex-kb\bin\codex-kb.cmd'
    if (-not (Test-Path -LiteralPath $script:Cli -PathType Leaf)) {
        throw "The installed codex-kb command was not found at $script:Cli."
    }

    $loginArguments = @(
        'remote', 'login', '--username', $AccountUsername,
        '--password-env', $PasswordEnvironmentVariable,
        '--passphrase-env', $PassphraseEnvironmentVariable
    )
    if ($CredentialsPath) {
        $loginArguments += @('--credentials', $CredentialsPath)
    }
    Invoke-CodexKb $loginArguments

    $verificationArguments = @('remote')
    if ($CredentialsPath) {
        $verificationArguments += @('--credentials', $CredentialsPath)
    }
    Invoke-CodexKb ($verificationArguments + 'whoami')
    Invoke-CodexKb ($verificationArguments + 'list')

    Write-Host "Remote codex-kb setup is complete for $AccountUsername."
} finally {
    # The values are not written to disk by this script. Remove them from this
    # PowerShell process after the child codex-kb command has received them.
    Remove-Item "Env:$PasswordEnvironmentVariable" -ErrorAction SilentlyContinue
    Remove-Item "Env:$PassphraseEnvironmentVariable" -ErrorAction SilentlyContinue
}
