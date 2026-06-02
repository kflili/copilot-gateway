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
#
# Intentionally a SIMPLE function — no [CmdletBinding] / [Parameter]
# attributes. Advanced functions strictly validate args, so hyphen-prefixed
# tokens like `-m`, `--version`, `--upgrade` would error out as unknown
# parameter names before the call ever reaches the native command. Simple
# functions instead collect every unbound arg in the automatic `$args`
# variable, which is exactly what we want for forwarding to `&`.
function Invoke-Checked {
    param(
        [string]$Description,
        $Command
    )
    & $Command $args
    if ($LASTEXITCODE -ne 0) {
        Write-Error "$Description failed (exit code $LASTEXITCODE)"
    }
}

# Probe a candidate Python CommandInfo to confirm it's a usable interpreter.
# Catches three Windows-specific traps:
#   1. The Microsoft Store app-execution alias for `python` / `python3` lives
#      at `…\WindowsApps\python.exe` as a 0-byte stub — invoking it pops the
#      Store install prompt instead of running Python.
#   2. An ancient Python on PATH (e.g., 2.7) that `tray_app.py`'s PEP 604
#      union syntax wouldn't parse.
#   3. Python 3.9 or older: tray_app.py:90 uses `str | None` (PEP 604) and
#      requires 3.10+. Accepting 3.9 would let the build start but PyInstaller
#      would fail at the analysis step or the .exe would crash at first call.
# Returns $true iff `& $Command --version` exits 0 and reports Python ≥3.10.
# The version regex is intentionally NOT anchored to `^` — PYTHONSTARTUP can
# emit lines before the version string.
function Test-PythonCandidate {
    param($Command)
    if (-not $Command) { return $false }
    # Wrap the probe in try/catch — under `$ErrorActionPreference = 'Stop'`,
    # a corrupted / unreadable / permission-denied interpreter would throw
    # a terminating error and crash the whole script instead of letting the
    # picker loop fall through to the next candidate.
    try {
        if ((Get-Item -LiteralPath $Command.Path -ErrorAction Stop).Length -eq 0) { return $false }
        $output = & $Command.Path --version 2>&1
        if ($LASTEXITCODE -ne 0) { return $false }
        if (([string]$output) -match 'Python\s+(\d+)\.(\d+)') {
            $major = [int]$matches[1]
            $minor = [int]$matches[2]
            return ($major -gt 3) -or ($major -eq 3 -and $minor -ge 10)
        }
    } catch {
        return $false
    }
    return $false
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
# Each candidate is probed via Test-PythonCandidate so the MS Store alias
# stub and pre-3.x interpreters are skipped silently, not accepted.
$python = $null
foreach ($name in @('python', 'python3', 'py')) {
    # `-CommandType Application` restricts the lookup to executables only,
    # so a user-defined `python` alias or function in the PS profile can't
    # shadow the real interpreter. Test-PythonCandidate would catch most
    # impostors via its --version probe anyway, but the CommandType filter
    # is the cheaper first-line defense.
    $candidate = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (Test-PythonCandidate $candidate) {
        $python = $candidate
        break
    }
}
if (-not $python) {
    Write-Error "No usable Python 3.x found on PATH (tried python, python3, py — Microsoft Store alias and 0-byte stubs are skipped). Install Python 3.10+ from python.org and retry."
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
    # Plan §Item 5 specifies installing into a venv — isolates PyInstaller +
    # pystray + pillow + zstandard from the user's system Python (avoids
    # global pollution + permission failures on Windows Store / system-managed
    # Pythons). Create `.venv` next to the script and rebind `$python` to its
    # interpreter for the remainder of the build.
    $venvDir = Join-Path $RepoRoot '.venv'
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    # Probe the interpreter itself, not just the directory — a corrupt or
    # empty `.venv\` from an interrupted previous `python -m venv` would
    # pass a bare `Test-Path $venvDir` but then fail at `Get-Command
    # $venvPython` with a confusing "command not found" downstream.
    if (-not (Test-Path $venvPython)) {
        Write-Host "Creating virtual environment at .venv\…"
        Invoke-Checked 'python -m venv .venv' $python -m venv $venvDir
    }
    if (-not (Test-Path $venvPython)) {
        Write-Error "Expected venv interpreter at $venvPython but file is missing."
    }
    $venvCmd = Get-Command $venvPython
    # Even an existing-and-non-empty `.venv\Scripts\python.exe` can be
    # broken: zero-byte stub from an interrupted copy, dangling reference
    # to a base Python that's since been uninstalled/upgraded, missing
    # site-packages, etc. Probe it the same way we probe the bootstrap
    # interpreter — rebuild if it doesn't pass the --version smoke.
    if (-not (Test-PythonCandidate $venvCmd)) {
        Write-Host "venv at .venv\ exists but isn't a usable Python — recreating…"
        Remove-Item -Recurse -Force $venvDir -ErrorAction SilentlyContinue
        Invoke-Checked 'python -m venv .venv' $python -m venv $venvDir
        if (-not (Test-Path $venvPython)) {
            Write-Error "Failed to provision venv at $venvDir."
        }
        $venvCmd = Get-Command $venvPython
        if (-not (Test-PythonCandidate $venvCmd)) {
            Write-Error "Rebuilt venv interpreter at $venvPython is still not usable. Check base Python install."
        }
    }
    $python = $venvCmd
    Write-Host "Using venv python: $($python.Path)"

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
} else {
    # -SkipDeps assumes the venv (or whatever python the user supplied) is
    # already set up; reuse it if present AND usable. A bare Test-Path was
    # insufficient — a zero-byte stub or broken interpreter from an
    # interrupted prior run would pass it. Test-PythonCandidate runs the
    # candidate's --version to verify it's actually a working Python.
    $venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path $venvPython) {
        $venvCmd = Get-Command $venvPython
        if (Test-PythonCandidate $venvCmd) {
            $python = $venvCmd
            Write-Host "Reusing venv python: $($python.Path)"
        } else {
            Write-Error "Venv at .venv\ is broken (interpreter at $venvPython does not respond to --version). Re-run without -SkipDeps to rebuild."
        }
    }
}

# Remove any stale `dist\copilot-gateway.exe` from a prior build BEFORE
# invoking PyInstaller. Defensive belt-and-suspenders: Invoke-Checked
# already halts on nonzero exit (via Write-Error + $ErrorActionPreference =
# 'Stop'), but explicitly clearing the target ensures the post-build
# Test-Path check can only succeed on a fresh artifact.
$exe = Join-Path $RepoRoot 'dist\copilot-gateway.exe'
if (Test-Path $exe) {
    Write-Host "Removing stale $exe"
    Remove-Item -Force $exe
}

# Hand off to PyInstaller. The spec drives everything (entry point, hidden
# imports, datas, --noconsole, output name). Bare invocation = no overrides.
Write-Host "Running PyInstaller…"
Invoke-Checked 'PyInstaller' $python -m PyInstaller pyinstaller.spec

if (Test-Path $exe) {
    Write-Host ""
    Write-Host "Build succeeded: $exe"
    Write-Host "Run it: .\dist\copilot-gateway.exe"
} else {
    Write-Error "Build finished but $exe not found. Inspect .\build\copilot-gateway\warn-copilot-gateway.txt for missing modules."
}
