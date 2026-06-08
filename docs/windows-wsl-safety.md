# Windows + WSL Setup — Safety & Rollback Guide

Companion to [`claude-code-integration.md`](claude-code-integration.md) (which is
macOS-focused). This doc covers the **Windows + WSL** path via `tray_app.py`,
with explicit backup and rollback steps so you can revert cleanly if anything
breaks `claude`.

Last updated: 2026-06-04.

## Why this doc exists

`tray_app.py` ships two one-click toggles — **Enable for Windows** and
**Enable for WSL** — that wire `claude` (and Codex CLI) to the local gateway.
They are convenient, but:

- The toggles do NOT auto-backup a valid `settings.json` (only unparseable
  files are saved to `<path>.cg-backup`).
- There is **no "Disable" button** in the tray; the README directs you to
  manually undo the changes.
- Changes touch multiple surfaces (user env via `setx`, Windows
  `settings.json`, WSL rc-file, WSL `settings.json`).

The good news: every change is **additive and well-isolated**, so a manual
pre-flight backup is cheap and rollback is a handful of commands.

## What gets touched

| Surface | Toggle | Change |
|---|---|---|
| `%USERPROFILE%\.claude\settings.json` | Enable for Windows | Merges 3 keys into the `env` block: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_CUSTOM_HEADERS`. Other keys preserved. |
| Windows user env (registry, via `setx`) | Enable for Windows | Sets the same 3 vars at User scope. New shells inherit them; already-running shells do not. |
| WSL distro rc-file (`~/.bashrc` / `~/.zshrc` / `~/.config/fish/config.fish` / `~/.profile`) | Enable for WSL → \<distro\> | Inserts a function block between markers `# >>> copilot-gateway env >>>` and `# <<< copilot-gateway env <<<`. Re-resolves Windows host IP at every shell start. Also exports `OPENAI_*` for Codex. |
| WSL `~/.claude/settings.json` (inside the distro) | Enable for WSL → \<distro\> | Same 3-key merge as Windows. Created if absent. |

Nothing else is touched. No registry edits outside the standard user-env keys,
no service installs, no git history rewrites.

## Step 1 — Pre-flight backup (run BEFORE any tray toggle)

Copy-paste this PowerShell block as one unit:

```powershell
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'

# 1. Windows settings.json
$src = "$env:USERPROFILE\.claude\settings.json"
if (Test-Path $src) {
  $bak = "$src.pre-cg-$ts.bak"
  Copy-Item $src $bak
  "Windows settings.json backed up to: $bak"
} else {
  "No Windows settings.json yet — nothing to back up."
}

# 2. Snapshot current user env (so we know what was/wasn't set before)
$snap = "$env:USERPROFILE\.claude\env-snapshot.pre-cg-$ts.txt"
@('ANTHROPIC_BASE_URL','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS') |
  ForEach-Object { "$_ = $([Environment]::GetEnvironmentVariable($_,'User'))" } |
  Out-File $snap
"User-env snapshot: $snap"

# 3. WSL .bashrc (adjust distro if not Ubuntu)
$distro = 'Ubuntu'
wsl -d $distro -- sh -c "test -f ~/.bashrc && cp -n ~/.bashrc ~/.bashrc.pre-cg.bak && echo 'WSL .bashrc backed up' || echo 'no WSL .bashrc'"

# 4. WSL settings.json (if exists)
wsl -d $distro -- sh -c "test -f ~/.claude/settings.json && cp -n ~/.claude/settings.json ~/.claude/settings.json.pre-cg.bak && echo 'WSL settings.json backed up' || echo 'no WSL settings.json yet'"
```

Notes:
- Replace `Ubuntu` with your actual distro name (`wsl -l -q` to list).
- `cp -n` means "don't overwrite an existing `.pre-cg.bak`" — protects an
  earlier backup if you run this twice.

## Step 2 — Apply the toggles

1. Install tray dependencies: `pip install pystray pillow zstandard`.
2. Launch the tray: `pythonw tray_app.py` from the repo root.
3. In the ⚡️ tray menu, click **Enable for Windows**, then **[Test]**.
4. Click **Enable for WSL → \<distro\>**, then the per-distro **[Test]**.
5. Open a fresh PowerShell and a fresh WSL shell — already-running terminals
   do NOT pick up the new env (`setx` and rc-file changes apply on shell start
   only).

## Step 3 — Verify

```powershell
# Gateway is up
Invoke-RestMethod http://127.0.0.1:8787/health | ConvertTo-Json

# Windows env vars are set
@('ANTHROPIC_BASE_URL','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS') |
  ForEach-Object { "$_ = $([Environment]::GetEnvironmentVariable($_,'User'))" }

# Round-trip from a fresh shell — run `claude` against a trivial prompt,
# then confirm per-origin counters incremented:
(Invoke-RestMethod http://127.0.0.1:8787/stats).per_origin
```

Healthy `/stats` shows `per_origin.windows` and `per_origin.wsl` counters
growing as you hit the gateway from each environment.

## Step 4 — Rollback (run ANY TIME `claude` misbehaves)

This restores the exact state captured in Step 1.

```powershell
$home_claude = "$env:USERPROFILE\.claude"

# Restore Windows settings.json (picks the most recent pre-cg backup)
$bak = Get-ChildItem "$home_claude\settings.json.pre-cg-*.bak" |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($bak) {
  Copy-Item $bak.FullName "$home_claude\settings.json" -Force
  "Restored from: $($bak.Name)"
} else {
  "No backup found — see 'Nuclear option' below."
}

# Clear the 3 user env vars
'ANTHROPIC_BASE_URL','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS' |
  ForEach-Object { [Environment]::SetEnvironmentVariable($_, $null, 'User') }
"User env vars cleared."

# Restore WSL .bashrc + settings.json (adjust distro if not Ubuntu)
$distro = 'Ubuntu'
wsl -d $distro -- sh -c "test -f ~/.bashrc.pre-cg.bak && cp ~/.bashrc.pre-cg.bak ~/.bashrc && echo 'WSL .bashrc restored' || echo 'no .bashrc backup'"
wsl -d $distro -- sh -c "test -f ~/.claude/settings.json.pre-cg.bak && cp ~/.claude/settings.json.pre-cg.bak ~/.claude/settings.json && echo 'WSL settings.json restored' || echo 'no WSL settings.json backup'"
```

After rollback, open a fresh terminal — `claude` runs against the official
Anthropic API again exactly as before.

### Surgical alternative (no backup needed)

If you only want to drop the gateway env without touching the rest of your
config:

```powershell
# Remove the 3 keys from Windows settings.json (preserves all other keys)
$f = "$env:USERPROFILE\.claude\settings.json"
$j = Get-Content $f -Raw | ConvertFrom-Json
if ($j.env) {
  'ANTHROPIC_BASE_URL','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS' |
    ForEach-Object { $j.env.PSObject.Properties.Remove($_) }
  $j | ConvertTo-Json -Depth 20 | Set-Content $f -Encoding utf8
}

# Clear user env vars
'ANTHROPIC_BASE_URL','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS' |
  ForEach-Object { [Environment]::SetEnvironmentVariable($_, $null, 'User') }

# Strip the WSL rc-block by markers
$distro = 'Ubuntu'
wsl -d $distro -- sed -i '/>>> copilot-gateway env >>>/,/<<< copilot-gateway env <<</d' ~/.bashrc
```

### Nuclear option (backup lost AND surgical didn't work)

Delete `%USERPROFILE%\.claude\settings.json` entirely. Claude Code regenerates
defaults on next launch. You lose any other customisations (hooks, status
line, model preference, etc.) — restore from your own version control if you
have one.

## Operational notes

- **Already-running shells**: `setx` and rc-file edits only apply to shells
  launched AFTER the toggle. If `claude` still goes to the Anthropic API
  after enabling, you're in a stale shell — open a new one.
- **Per-origin telemetry**: the toggles inject
  `ANTHROPIC_CUSTOM_HEADERS: X-Gateway-Origin: windows` (or `wsl`) so
  `/stats` can split usage by host. The header is harmless if Anthropic ever
  sees it; it's stripped by the gateway before the upstream request.
- **WSL2 host IP drift**: the rc-block re-resolves the Windows host IP at
  every shell start. Don't hard-code the IP in `~/.claude/settings.json`
  inside WSL — the tray uses a stable mirror URL there instead.
- **Backup retention**: keep `.pre-cg-*.bak` files for at least a week after
  enabling. They're tiny.

## Known issues & required fixes (this fork)

These were discovered in this environment (Windows 11 + WSL 2.6.3 + bash 5.2)
and patched in `tray_app.py` on 2026-06-08. If you re-clone upstream, you may
need to re-apply them.

1. **Headerless UTF-16LE from `wsl -l -q`** (WSL 2.6+ on Win11). The original
   `_decode_wsl_output()` only handled BOM-prefixed UTF-16LE, so the WSL
   submenu rendered as garbled single letters / empty rows. Fix: detect
   headerless UTF-16LE via odd-byte-NUL heuristic before falling through to
   UTF-8.

2. **WSL 2.6 login-shell wrapping of `wsl.exe -d <distro> -- <cmd>`.** Newer
   WSL silently wraps the command in the user's default login shell, which
   mangles inline variable assignments (`p=foo; echo $p` → empty) and breaks
   `$(...)` command substitution of shell functions. Symptom: `enable_for_wsl`
   pops up `rc-file rewrite returned 1: _copilot_gateway_resolve_host: command
   not found`. Fix: use the `_wsl_cmd(distro, *args)` helper, which always
   inserts `--shell-type none --` to bypass the wrapper. All 9 in-tree call
   sites migrated. Interactive shells launched by the user from Windows
   Terminal are NOT affected (only the tray's programmatic calls were).

3. **AUMID for tray icon name.** Without
   `SetCurrentProcessExplicitAppUserModelID`, the tray icon shows up unnamed
   in *Settings → Personalization → Taskbar → Other system tray icons*. Fixed
   via `_set_app_user_model_id("CopilotGateway.Tray")` called early in
   `main()`.

4. **Loopback unreachable from WSL** (default WSL2 NAT mode). The tray refuses
   to enable WSL routing if it can't reach the gateway from inside the distro.
   Fix once at the Windows side by writing `%USERPROFILE%\.wslconfig`:
   ```
   [wsl2]
   networkingMode=mirrored
   dnsTunneling=true
   ```
   then `wsl --shutdown`. After that the WSL toggle can use the simple
   `http://127.0.0.1:8787` URL.
