[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Get-PythonCommand {
    foreach ($candidate in @('python', 'py')) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
    }
    throw 'Python 3.10 or newer is required. Install Python, then rerun this script.'
}

$python = Get-PythonCommand
$isPyLauncher = (Split-Path -Leaf $python) -ieq 'py.exe'
$versionOutput = if ($isPyLauncher) { & $python -3 --version 2>&1 } else { & $python --version 2>&1 }
if ($LASTEXITCODE -ne 0) { throw "Could not run Python: $versionOutput" }

$installRoot = Join-Path $HOME '.codex-kb'
$app = Join-Path $installRoot 'app'
$bin = Join-Path $installRoot 'bin'
$sourceRoot = $PSScriptRoot

New-Item -ItemType Directory -Force -Path $app, $bin | Out-Null
Copy-Item -Path (Join-Path $sourceRoot 'codex-kb.py') -Destination (Join-Path $app 'codex-kb.py') -Force
Copy-Item -Path (Join-Path $sourceRoot 'codex_kb') -Destination $app -Recurse -Force

$wrapper = Join-Path $bin 'codex-kb.cmd'
@"
@echo off
set "ENTRY=%~dp0..\app\codex-kb.py"
where py >nul 2>nul && (py -3 "%ENTRY%" %* & exit /b %ERRORLEVEL%)
python "%ENTRY%" %*
"@ | Set-Content -Path $wrapper -Encoding ascii

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$parts = @()
if ($userPath) { $parts = $userPath -split ';' | Where-Object { $_ } }
if ($parts -notcontains $bin) {
    [Environment]::SetEnvironmentVariable('Path', (($parts + $bin) -join ';'), 'User')
}

if ($isPyLauncher) {
    & $python -3 (Join-Path $app 'codex-kb.py') init
} else {
    & $python (Join-Path $app 'codex-kb.py') init
}

Write-Host ''
Write-Host "Installed codex-kb to $installRoot"
Write-Host "Added $bin to your user PATH. Open a new terminal, then run: codex-kb status"
Write-Host 'Codex integration is opt-in; see README.md for the MCP and Hook snippets.'
