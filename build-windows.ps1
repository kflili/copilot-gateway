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

# Repo root = directory of this script. cd here so relative paths in the
# spec (`'demo.html'`, `'gateway.py'`, `'demo.py'`) resolve regardless of
# where the user invoked the script from.
$RepoRoot = $PSScriptRoot
Set-Location $RepoRoot

# Bound the bootstrap concretely so failures surface here, not deep inside
# PyInstaller analysis. `??` is PowerShell 7+ only — Win10/11 ship Windows
# PowerShell 5.1 inbox, so use the 5.1-compatible `if` fallback instead.
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $python) {
    Write-Error "Python not found on PATH. Install Python 3.10+ from python.org and retry."
}
Write-Host "Using $($python.Source)"
& $python.Source --version

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
    # pyinstaller is the build tool. Versions intentionally unpinned —
    # the gateway runtime is stdlib-only, so version drift is low-risk.
    & $python.Source -m pip install --upgrade pip
    & $python.Source -m pip install pyinstaller pystray pillow
}

# Hand off to PyInstaller. The spec drives everything (entry point, hidden
# imports, datas, --noconsole, output name). Bare invocation = no overrides.
Write-Host "Running PyInstaller…"
& $python.Source -m PyInstaller pyinstaller.spec

$exe = Join-Path $RepoRoot 'dist\copilot-gateway.exe'
if (Test-Path $exe) {
    Write-Host ""
    Write-Host "Build succeeded: $exe"
    Write-Host "Run it: .\dist\copilot-gateway.exe"
} else {
    Write-Error "Build finished but $exe not found. Inspect .\build\copilot-gateway\warn-copilot-gateway.txt for missing modules."
}
