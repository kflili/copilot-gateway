# build-windows.ps1 — Build the Copilot Gateway single-`.exe` for Windows.
#
# Usage:
#   .\build-windows.ps1                  # build into .\dist\
#   .\build-windows.ps1 -SkipDeps        # skip pip install (deps already on PATH)
#   .\build-windows.ps1 -Clean           # remove .\build\ and .\dist\ first
#
# Requirements: Python 3.10+ on PATH (`python --version`). Script invokes
# `python -m pip` and `python -m PyInstaller` rather than the bare `pip` /
# `pyinstaller` shims to stay tied to whichever interpreter the user picked.
#
# Output: .\dist\copilot-gateway.exe (single-file, no console window).
# See README.md § Windows for end-user run instructions.

[CmdletBinding()]
param(
    [switch]$SkipDeps,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'

# `$ErrorActionPreference = 'Stop'` only halts on PowerShell cmdlet errors,
# NOT native-command nonzero exits (`python -m pip`, `python -m PyInstaller`,
# …). Wrap every native invocation in this helper so a failed pip / build
# stops the script instead of falling through to a "successful" Test-Path on
# a stale dist\copilot-gateway.exe from a prior run.
function Invoke-Checked {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position=0)][string]$Description,
        [Parameter(Mandatory, Position=1)]$Command,
        [Parameter(ValueFromRemainingArguments=$true)][string[]]$CommandArgs
    )
    & $Command @CommandArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "$Description failed (exit code $LASTEXITCODE)"
    }
}

# Repo root = directory of this script. cd here so relative paths in the
# spec (`'demo.html'`, `'gateway.py'`, `'demo.py'`) resolve regardless of
# where the user invoked the script from.
$RepoRoot = $PSScriptRoot
Set-Location $RepoRoot

# Bound the bootstrap concretely so failures surface here, not deep inside
# PyInstaller analysis. `??` is PowerShell 7+ only — Win10/11 ship Windows
# PowerShell 5.1 inbox, so use the 5.1-compatible `if` fallback instead.
# Resolution order: python → python3 → py (the Windows Python Launcher,
# bundled with python.org installers; often the only Python on PATH).
# `Select-Object -First 1` is defensive: single-name `Get-Command` returns
# the resolved command, but the explicit slice future-proofs if `-All`
# semantics ever change.
$python = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $python) {
    $python = Get-Command python3 -ErrorAction SilentlyContinue | Select-Object -First 1
}
if (-not $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue | Select-Object -First 1
}
if (-not $python) {
    Write-Error "Python not found on PATH. Install Python 3.10+ from python.org and retry."
}
# Use `.Path` for printing (always populated on ApplicationInfo across PS
# 5.1 + 7), and invoke `$python` directly via the call operator `&` (which
# accepts CommandInfo natively) so we don't depend on `.Source` — empirically
# unreliable for external executables on Windows PowerShell 5.1.
Write-Host "Using $($python.Path)"
Invoke-Checked 'python --version' $python --version

if ($Clean) {
    foreach ($dir in @('build', 'dist')) {
        if (Test-Path $dir) {
            Write-Host "Removing .\$dir\"
            Remove-Item -Recurse -Force $dir
        }
    }
}

if (-not $SkipDeps) {
    Write-Host "Installing build + runtime deps…"
    # Single combined install so pip resolves once. pystray + pillow are
    # runtime deps the tray loads lazily (tray_app.py:903, 952, 998);
    # zstandard is an optional gateway dep that decodes Codex CLI's
    # `Content-Encoding: zstd` request bodies (gateway.py:47-54, 758-766) —
    # bundle it so the frozen `.exe` doesn't 400 on Codex clients;
    # pyinstaller is the build tool. Versions intentionally unpinned —
    # the gateway runtime is stdlib-only, so version drift is low-risk.
    Invoke-Checked 'pip install --upgrade pip' $python -m pip install --upgrade pip
    Invoke-Checked 'pip install (build + runtime deps)' $python -m pip install pyinstaller pystray pillow zstandard
}

# Hand off to PyInstaller. The spec drives everything (entry point, hidden
# imports, datas, --noconsole, output name). Bare invocation = no overrides.
Write-Host "Running PyInstaller…"
Invoke-Checked 'PyInstaller' $python -m PyInstaller pyinstaller.spec

$exe = Join-Path $RepoRoot 'dist\copilot-gateway.exe'
if (Test-Path $exe) {
    Write-Host ""
    Write-Host "Build succeeded: $exe"
    Write-Host "Run it: .\dist\copilot-gateway.exe"
} else {
    Write-Error "Build finished but $exe not found. Inspect .\build\copilot-gateway\warn-copilot-gateway.txt for missing modules."
}
