# Windows App — gateway tray + dashboard for Windows + WSL clients

| | |
|---|---|
| Status     | Active |
| Priority   | Medium |
| Complexity | Medium |
| Depends On | — |
| Owner      | lili |

## TL;DR

Wrap the existing Python `gateway.py` in a Windows system-tray app + per-host
dashboard so `claude.exe` (Windows) and `claude` (WSL) share **one** gateway
process. One gateway runs natively on Windows; two "enable" toggles inject the
right env vars into Windows and WSL so both environments transparently proxy
through it. The gateway tags each request by client source IP (loopback =
Windows, 172.16/12 = WSL2) so logs and the dashboard split traffic into two
columns. Ship as a single PyInstaller `.exe`.

This plan is the canonical spec for Items 2–5 of the
`copilot-gateway-2026-06-01` orchestration run. Each downstream item reads
exclusively from this doc — not from the orchestration scaffold-prompt.

## Key Decisions

- ✅ **Option A: one gateway, two toggles** — single Python process on Windows;
  WSL reaches it via the Windows host IP (resolved from `/etc/resolv.conf`).
  Simpler operationally, single point of stats/logs, matches the user's
  authored brief.
- ❌ **Rejected Option B: two gateways (Windows-native + WSL-native)** — doubles
  the moving parts, splits stats, creates port-conflict edge cases. Explicitly
  rejected by user in the inline brief.
- ✅ **Origin tagging by source IP** — `BaseHTTPRequestHandler.client_address`
  is already available; loopback ⇒ `windows`, `172.16.0.0/12` ⇒ `wsl`,
  everything else ⇒ `other`. ~10-line change in `gateway.py`. No client-side
  cooperation required.
- ✅ **Tray stack: `pystray` + `tkinter`** for the tray icon + popups; `tkinter`
  ships with the stdlib Python embedded by PyInstaller. `pywebview` reserved as
  a fallback for the dashboard window if `tkinter` rendering proves too clunky.
  **Threading model**: `tkinter` runs on the **main thread** (its `mainloop()`
  is non-reentrant and `tkinter` is not thread-safe); `pystray` runs in a
  worker thread. Menu callbacks invoked by `pystray` marshal UI work back to
  `tkinter` via `root.after(0, fn)`, which queues `fn` for execution on the
  tkinter main loop. Long-running probe work (Test buttons, env writes) runs
  in its own thread so the tray + UI stay responsive.
- ✅ **PyInstaller single-file `.exe`** — bundles `gateway.py`, `tray_app.py`,
  `demo.py`, `demo.html`, plus the `pystray`/`tkinter` deps. PowerShell build
  script (`build-windows.ps1`) for reproducibility.
- ✅ **Mirror Mac menubar feature surface, not its code** — `menubar.swift` is
  the reference for *what the tray app should do* (stats, logs, copy-command,
  toggles). The Python tray is a from-scratch rewrite; no Swift bridging.

## Acceptance Criteria

Each criterion is testable manually via `test-copilot-api.sh` against a running
gateway, plus visual inspection of the tray + dashboard.

- [ ] `gateway.py` tags every incoming request with `origin ∈ {windows, wsl, other}`
      and exposes per-origin counts on `/stats` (Item 2).
- [ ] `tray_app.py` shows live per-host request count + token count in the tray
      title (Item 3).
- [ ] Tray menu includes: Stats popup, View logs, Copy claude command, Copy codex
      command, Enable for Windows toggle, Enable for WSL toggle, Stop & quit (Item 3).
- [ ] "Enable for Windows" toggle writes `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`
      to user env via `setx` AND to `%USERPROFILE%\.claude\settings.json` env block;
      `[Test]` button shells a one-shot probe and pops green/red toast (Item 3).
- [ ] "Enable for WSL" toggle enumerates distros via `wsl.exe -l -q`, lets user
      pick, writes env to `~/.bashrc` + `~/.claude/settings.json` inside the
      chosen distro, with the dynamically-resolved Windows host IP (Item 3).
- [ ] Dashboard (`demo.html`) shows two-column stats table: Windows | WSL | Total
      (requests, tokens-in, tokens-out, top model, last-request time) (Item 4).
- [ ] Dashboard live call-flow has `[WIN]` blue / `[WSL]` green prefix per line (Item 4).
- [ ] `build-windows.ps1` + `pyinstaller.spec` produce a single `.exe` that runs
      the gateway + tray + dashboard on a fresh Windows VM with no manual deps (Item 5).
- [ ] README.md gains a "Windows" section documenting install + first-run UX (Item 5).
- [ ] No regressions in macOS code paths: `menubar.swift`, `CopilotGateway.app/`,
      `CopilotGateway.swift` remain unmodified.

## Context

The repo currently ships:
- `gateway.py` — a ~1,000-line stdlib HTTP proxy that fronts the GitHub Copilot
  API for `claude` and `codex` CLIs. Single-process, threaded
  `BaseHTTPRequestHandler`. Tracks per-model stats in a `RequestStats`
  accumulator (see `gateway.py:382-445`). Endpoints `/stats` and `/logs` are
  already exposed for read-only telemetry.
- `demo.py` + `demo.html` — a small dashboard that calls the Copilot API
  directly (not through the gateway) for chat demos. Reads `/stats` and `/logs`
  for live visualization.
- `menubar.swift` + `CopilotGateway.app` — a macOS-only menubar wrapper. Not
  touched by this plan.
- `test-copilot-api.sh` — manual smoke test that hits the gateway with sample
  prompts. Verified at scaffold time: no `pytest.ini`, no `tests/` dir
  (`ls tests/` ⇒ no such file; `test -f pytest.ini` ⇒ MISSING). This is a
  zero-test-harness Python project; all verification is manual smoke.

Windows is the missing host. Today a Windows user has no equivalent of the Mac
menubar — they'd have to run `gateway.py` from a terminal and manually set env
vars in two places (Windows + each WSL distro). This plan closes that gap.

WSL networking note: WSL2 sees the Windows host at the IP listed as the first
`nameserver` in `/etc/resolv.conf`. When the user has enabled
`hostForwarding=true` in `.wslconfig`, `host.docker.internal` also resolves to
the Windows host. The WSL toggle must resolve and persist whichever the user
has available.

## Technical Approach

- **Architecture**: Option A — one gateway process on Windows, env-var
  injection into Windows + each WSL distro, source-IP tagging for traffic split.
- **Languages**: Python 3 (gateway, tray, dashboard); PowerShell (build script);
  HTML/JS (dashboard view, served by `demo.py`).
- **No new external deps for Item 2** — pure stdlib. Items 3 + 5 add `pystray`,
  `Pillow` (pystray dep), and PyInstaller as packaging-time deps.

### Item 2 — Gateway origin-tagging

**Scope**: ~10-line change in `gateway.py`. Doc-only PRs and feature PRs MUST
NOT bundle this; this is its own small PR (`feat/gateway-origin-tagging`).

**Files**:
- `gateway.py` — add `_classify_origin(client_address) -> str` helper; thread
  origin through `_forward()` into log lines and `RequestStats`.

**Mechanics**:
- Origin classifier: examine `self.client_address[0]` (string IP):
  - `127.0.0.1` or `::1` ⇒ `"windows"`
  - In `172.16.0.0/12` ⇒ `"wsl"` (use `ipaddress.ip_network("172.16.0.0/12")`
    from stdlib; covers the full WSL2 NAT range)
  - Anything else ⇒ `"other"`
- **Mirrored-mode caveat**: WSL2 `networkingMode=mirrored` (WSL 2.0+) makes WSL
  traffic appear from loopback, so the classifier above will tag mirrored-mode
  WSL traffic as `"windows"`. This is a known limitation; users who run
  mirrored mode see their WSL traffic in the Windows column. Listed under Risks
  with the proposed remediation (an optional client-side header
  `X-Gateway-Origin: wsl` written by the WSL toggle).
- Extend `RequestStats` snapshot: add `per_origin: {windows: {...}, wsl: {...},
  other: {...}}` with the same shape as the top-level counters (requests,
  input/output tokens, last_request_at).
- Add `origin` field to every log line emitted by `_forward()` and friends.
- `/stats` JSON gains the `per_origin` key alongside existing top-level fields.
  Existing fields stay unchanged for backward compatibility with the current
  `demo.py`.

**Dependencies**: none — `ipaddress` is stdlib.

**Out of scope for this item**: dashboard rendering of per-origin (that's Item 4),
tray-side display (Item 3), packaging (Item 5).

### Item 3 — Tray app + Windows/WSL toggles

**Scope**: new `tray_app.py`. Mirrors the Mac menubar feature surface (see
`menubar.swift` for reference UX only — no code reuse).

**Files**:
- `tray_app.py` (new) — tray icon, menu, toggles, status polling.
- `requirements-windows.txt` (new) — `pystray`, `Pillow`. (Optional file; can
  also be tracked inline in `pyinstaller.spec`.)

**Dependencies on Item 2**: the per-host counters in the tray title + stats popup
read `per_origin` from `/stats`. Item 3 must merge AFTER Item 2.

**Gateway bind address**: by default `gateway.py` listens on
`LISTEN_HOST=127.0.0.1` (loopback only), which makes it unreachable from WSL
distros over the host-side IP. The tray app launches `gateway.py` with
`--host 0.0.0.0` (or the explicit Windows-facing IP) **only when the WSL toggle
is enabled** — keeping the default loopback-only bind when only Windows is
enabled, to avoid exposing the gateway to other machines on the LAN
unnecessarily. The bind-host choice is surfaced in the tray Stats popup so users
know what they're listening on.

**Tray title** (live, polled from `/stats` every ~2s):
```
[WIN 47 reqs / 12.3k tok] [WSL 18 reqs / 4.2k tok]
```

**Menu items**:
- **Stats** — popup window with the two-column per-host table (read from
  `/stats per_origin`).
- **View logs** — popup tailing `/logs`; entries color-coded by origin
  (`windows` ⇒ blue, `wsl` ⇒ green, `other` ⇒ gray).
- **Copy claude command** — clipboards `claude` invocation with the correct
  base-url env prefix for the current host.
- **Copy codex command** — same for `codex`.
- **Enable for Windows** — toggle (checked = enabled). When toggled on:
  - Run `setx ANTHROPIC_BASE_URL http://localhost:8787`
  - Run `setx ANTHROPIC_AUTH_TOKEN dummy`
  - Update `%USERPROFILE%\.claude\settings.json` `env` block (preserve other
    keys; create file if missing)
  - Post-write UI: `setx` only updates the master environment in the registry;
    it does NOT affect already-running processes (Command Prompts, PowerShell
    windows, VS Code, etc.). Show a "Restart your terminals / IDEs to pick up
    the new env" toast after a successful enable, and a link to a help dialog
    explaining why.
  - `[Test]` button: spawn a one-shot `claude --help` probe in a **new** shell
    (which inherits the freshly-set env) and pop a green toast on success / red
    on failure.
- **Enable for WSL** — submenu listing distros from `wsl.exe -l -q`.
  Selecting a distro:
  - **Detect host-reachability mode** inside the chosen distro, in priority
    order:
    1. If `/etc/wsl.conf` or the parent `.wslconfig` enables
       `networkingMode=mirrored` (WSL 2.0+), the Windows host is reachable at
       `localhost` / `127.0.0.1` — use that. No nameserver lookup needed.
    2. Else if `host.docker.internal` resolves (user enabled
       `hostForwarding=true`), use it.
    3. Else read the first `nameserver` line from `/etc/resolv.conf` and use
       that IP.
    The toggle persists the resolved value rather than re-resolving each
    session, so a corporate `resolv.conf` rewrite doesn't break things later.
  - **Detect the user's default shell** (`getent passwd $USER | cut -d: -f7`)
    and write the env exports to the matching rc file with an idempotent marker
    comment:
    - `bash` → `~/.bashrc`
    - `zsh` → `~/.zshrc` (common on Oh My Zsh installs)
    - `fish` → `~/.config/fish/config.fish` (using `set -gx` syntax, not
      `export`)
    - other → fall back to `~/.profile` and surface a warning toast
  - Always also update `~/.claude/settings.json` `env` block (shell-agnostic,
    picked up by `claude` itself regardless of shell).
  - `[Test]` button: `wsl.exe -d <distro> -- bash -lc 'claude --help'` (forces
    a login shell so rc files load), same green/red toast.
- **Stop & quit** — graceful gateway shutdown + tray exit.

**State**: tray reads from `/stats` and `/logs`; does NOT maintain its own
counters. Single source of truth is the gateway process.

**Out of scope for this item**: dashboard rendering (Item 4), packaging (Item 5).

### Item 4 — Dashboard per-host split

**Scope**: extend the existing `demo.py` + `demo.html` to render the per-host
breakdown. Existing direct-Copilot chat demo stays as-is; this is additive.

**Files**:
- `demo.py` — fetch `/stats` (the now-populated `per_origin` key) and `/logs`;
  pass to the HTML template.
- `demo.html` — add a per-host stats table + color-coded log stream.

**Dependencies on Item 2**: reads `per_origin`. Must merge AFTER Item 2 (can
run in parallel with Item 3 since the surfaces are disjoint).

**Per-host stats table** (new section above existing dashboard panels):

| | Windows | WSL | Total |
|-|---------|-----|-------|
| Requests              | … | … | … |
| Tokens in             | … | … | … |
| Tokens out            | … | … | … |
| Top model             | … | … | … |
| Last request          | … | … | … |

**Live call-flow log**: each line prefixed with `[WIN]` (blue) or `[WSL]`
(green) or `[OTHER]` (gray). Origin field comes from the log line populated by
Item 2.

**Out of scope for this item**: tray UI (Item 3), packaging (Item 5), changes
to the direct-Copilot chat demo.

### Item 5 — PyInstaller packaging

**Scope**: bundle gateway + tray + dashboard into a single `.exe`.

**Files**:
- `build-windows.ps1` (new) — PowerShell build script; installs PyInstaller +
  app deps into a venv, runs PyInstaller against the spec, drops `.exe` in
  `dist/`.
- `pyinstaller.spec` (new) — declares entry point (`tray_app.py`), data files
  (`demo.html`), hidden imports for `pystray`/`tkinter`/`Pillow`.
- `README.md` — new "Windows" section: download `.exe`, double-click, tray
  appears, click "Enable for Windows" + "Enable for WSL", done.

**Dependencies on Items 3 + 4**: bundles `tray_app.py` (created in Item 3) AND
the post-split `demo.py` / `demo.html` (modified in Item 4). Must merge LAST,
after BOTH Item 3 and Item 4 have landed; otherwise the bundled `.exe` ships a
stale (pre-split) dashboard. The brief's dep arrow shows only Item 3 → Item 5,
but Item 4 → Item 5 is a real soft dependency made explicit here so a
parallel-execution variant of this plan still produces a correct bundle.

**Entry point**: `tray_app.py` — it spawns `gateway.py` as a child thread (or
in-process module call), then `demo.py`'s HTTP server as another thread, then
runs the pystray loop.

**Single-file vs one-folder mode**: prefer single-file (`--onefile`) for the
user-facing artifact; document the slower cold-start as an acceptable trade-off.

**Code signing**: out of scope (R3 — documented below).

**Out of scope for this item**: macOS packaging (already covered by
`CopilotGateway.app/`), MSI installer (could be future work), auto-update.

## Risks

- **`tkinter` rendering on Windows** can look dated — mitigation: if the
  Stats/Logs popups feel too clunky, fall back to `pywebview` (already on the
  fallback list).
- **`pystray` + `tkinter` threading mishap** — `tkinter` is not thread-safe and
  expects its main loop on the process main thread; `pystray` menu callbacks
  may fire on background threads (platform-dependent). Mitigation: keep
  `tkinter.mainloop()` on the main thread, run `pystray.Icon.run_detached()`
  in a worker thread, and marshal all UI updates back via `root.after(0, fn)`.
  Documented as the canonical pattern in Item 3's Key Decisions.
- **WSL2 `networkingMode=mirrored` misclassifies WSL traffic as `windows`** —
  mirrored mode (WSL 2.0+) makes WSL connect from loopback, defeating IP-based
  classification. Mitigation v1: documented limitation; users see WSL traffic
  in the Windows column. Mitigation v2 (future): the WSL toggle writes an
  optional `X-Gateway-Origin: wsl` request header into the WSL-side env
  (`ANTHROPIC_CUSTOM_HEADERS`) and `gateway.py` honors it as an override when
  present. Held off v1 to keep Item 2 small.
- **WSL host-IP resolution edge cases** — users on locked-down corporate
  networks may have `/etc/resolv.conf` overridden; mitigation: the `[Test]`
  button surfaces the failure immediately, the toggle persists the
  user-chosen IP rather than re-resolving each session, and the priority order
  (mirrored → host.docker.internal → resolv.conf) gives the WSL toggle three
  fallbacks before failing.
- **WSL default-shell variance** — Oh My Zsh / fish users would have plain
  `~/.bashrc` writes silently ignored. Mitigation: Item 3's "Enable for WSL"
  flow detects `$SHELL` and writes to the matching rc file, plus always
  updates `~/.claude/settings.json` (shell-agnostic) as a belt-and-braces
  fallback.
- **`setx` does not affect already-running processes** — the master env is
  updated in the registry but existing terminals / IDEs keep their inherited
  env until restart. Mitigation: post-enable toast prompts the user to
  restart terminals; the `[Test]` button always spawns a fresh shell so users
  see immediate green/red feedback without restarting anything.
- **`setx` PATH-length limits** (1024 chars on some Windows versions) —
  mitigation: only write the two env vars we control; do not concatenate.
- **PyInstaller cold-start** on first launch (a few seconds while the bundle
  unpacks) — accepted; document in README.
- **Origin misclassification** when WSL traffic egresses through an unusual
  bridge IP — mitigation: the `other` bucket catches it; users see traffic
  appear under `other` and can file an issue with their IP.
- **Backward-compat for existing `/stats` consumers** — Item 2 adds the
  `per_origin` key without modifying any existing top-level fields; current
  `demo.py` keeps working until Item 4 lands.

## Out of Scope (intentional)

These are explicitly carved out per the inline brief. If any becomes important
later, it gets its own plan under `docs/design/`.

- **macOS code paths** — `menubar.swift`, `CopilotGateway.app/`,
  `CopilotGateway.swift` are unchanged by every PR in this run.
  - Destination: existing Mac code stands as-is.
  - When-it-should-be-done: only if a Mac-side refactor becomes necessary,
    which is unrelated to Windows delivery.
- **Option B two-gateway design** (separate Windows-native + WSL-native
  gateways).
  - Destination: rejected. Would need a new plan if revived.
  - When-it-should-be-done: not anticipated.
- **Upstream Copilot API auth changes** — reuse the existing gateway auth
  logic as-is. No changes to `.gateway-token.json` flow.
  - Destination: existing `gateway.py` auth code.
  - When-it-should-be-done: only if GitHub changes the Copilot auth contract.
- **`~/.claude/CLAUDE.md` and other user-level config** — never touched.
  - Destination: user-owned.
  - When-it-should-be-done: never from this run.
- **Code signing the `.exe`** (Authenticode) — adds publisher cost + cert
  management; SmartScreen will warn on first launch.
  - Destination: future `docs/design/windows-app-signing/plan.md` if/when needed.
  - When-it-should-be-done: when distribution moves beyond personal/dev use.
- **MSI installer / auto-update** — `.exe` is sufficient for v1.
  - Destination: future plan if distribution needs grow.
  - When-it-should-be-done: post-MVP, only if there's a real user need.
- **Adding a test harness** (`pytest`, `tests/`) — out of scope for this run.
  Verified at scaffold + on entry: `ls tests/` ⇒ no such file; `test -f
  pytest.ini` ⇒ MISSING. All verification stays manual via
  `test-copilot-api.sh` per repo convention.
  - Destination: future `docs/design/test-harness/plan.md` if/when the project
    grows enough to warrant unit tests.
  - When-it-should-be-done: when the repo gains a second maintainer or the
    code surface doubles.
