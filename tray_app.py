"""tray_app.py — system-tray UI for the Copilot Gateway on Windows.

Mirrors the macOS `menubar.swift` feature surface (stats, logs, copy-command,
stop & quit) and adds two new toggles unique to this platform: "Enable for
Windows" (writes user-env via `setx` + `%USERPROFILE%\\.claude\\settings.json`)
and "Enable for WSL" (enumerates distros, writes a shell-function wrapper into
the chosen distro's rc-file so the WSL host-IP is resolved at every shell
start).

Threading model (per `docs/design/windows-app/plan.md` §"Item 3"): pystray's
`Icon.run()` runs on a worker thread; tkinter's event loop owns the main
thread. Menu callbacks (executing on pystray's worker) marshal UI work back to
the tkinter thread via `root.after(0, fn)`. The stats poller is its own
daemon thread.

Bind safety: when the WSL toggle is on, the tray prefers binding to the
specific WSL bridge IP (discovered by asking the chosen distro what its
default-route gateway is). If that fails, it falls back to `0.0.0.0` with a
prominent LAN-exposure warning toast, and the Stats popup always shows the
current bind host with a coloured badge.

Smoke mode: `python3 tray_app.py --smoke` runs platform/dependency probes,
prints a one-line summary per probe, and exits — used for dev-side validation
on Mac (where the tray itself isn't expected to render).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

GATEWAY_DEFAULT_HOST = "127.0.0.1"
GATEWAY_DEFAULT_PORT = 8787
STATS_POLL_INTERVAL_S = 2.0
HTTP_TIMEOUT_S = 1.5

HERE = Path(__file__).resolve().parent
GATEWAY_PY = HERE / "gateway.py"

# ─── Subprocess hygiene ───────────────────────────────────────────────────────
# Plan §"Subprocess hygiene": every subprocess call in a `--noconsole`
# PyInstaller build flashes a black cmd window unless CREATE_NO_WINDOW is set.
# Wrap once, use everywhere.

if sys.platform == "win32":
    _CREATE_NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW, py3.7+
else:
    _CREATE_NO_WINDOW = 0


def _quiet_run(cmd, **kw):
    """subprocess.run wrapper that suppresses console windows on Windows."""
    if sys.platform == "win32":
        kw.setdefault("creationflags", _CREATE_NO_WINDOW)
    kw.setdefault("capture_output", True)
    kw.setdefault("text", False)  # keep bytes so caller can pick the codec
    return subprocess.run(cmd, **kw)


def _decode_wsl_output(raw: bytes) -> str:
    """`wsl.exe` writes UTF-16LE with a BOM (bot-triage residual #3/#8 from
    PR #2). Decode that, falling back to UTF-8 if a future WSL release ever
    flips the default."""
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le", errors="replace").lstrip("﻿")
    try:
        return raw.decode("utf-16-le", errors="strict").lstrip("﻿")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


# ─── Gateway lifecycle ────────────────────────────────────────────────────────


class GatewayProcess:
    """Spawns and supervises `gateway.py`. If a gateway is already responding
    on the target host:port, attaches in read-only mode (no subprocess)."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.proc: subprocess.Popen | None = None
        self.attached = False

    @property
    def stats_url(self) -> str:
        h = "localhost" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{h}:{self.port}/stats"

    @property
    def logs_url(self) -> str:
        h = "localhost" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{h}:{self.port}/logs"

    @property
    def health_url(self) -> str:
        h = "localhost" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{h}:{self.port}/health"

    def already_running(self) -> bool:
        try:
            with urllib.request.urlopen(self.health_url, timeout=HTTP_TIMEOUT_S):
                return True
        except urllib.error.HTTPError:
            # An HTTP error (500, 403, etc.) means *something* is bound to this
            # port and answering — spawning a second gateway would race for the
            # same socket and OSError. Treat as "running" so we attach instead.
            return True
        except (urllib.error.URLError, socket.timeout, ConnectionError):
            return False

    def start(self) -> str:
        """Return one of: 'spawned', 'attached', 'missing-gateway'."""
        if self.already_running():
            self.attached = True
            return "attached"
        if not GATEWAY_PY.exists():
            return "missing-gateway"
        cmd = [sys.executable, str(GATEWAY_PY),
               "--host", self.host, "--port", str(self.port)]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return "spawned"

    def stop(self):
        if self.attached or self.proc is None:
            return
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        except ProcessLookupError:
            pass


# ─── Bind-host selection (SECURITY-HIGH mitigation from PR #2 #6) ─────────────


def select_bind_host(wsl_enabled: bool, distro: str | None) -> tuple[str, str]:
    """Return (host, posture) where posture is one of:
        'loopback'      — 127.0.0.1, no LAN exposure
        'wsl-bridge'    — specific 172.16.x.x / 192.168.x.x, only WSL reachable
        'lan-exposed'   — 0.0.0.0, ANY LAN/VPN client could reach us
    Prefers the safest option that satisfies the toggle state.
    """
    if not wsl_enabled:
        return ("127.0.0.1", "loopback")
    if distro:
        bridge = _probe_wsl_bridge_ip(distro)
        if bridge:
            return (bridge, "wsl-bridge")
    return ("0.0.0.0", "lan-exposed")


def _probe_wsl_bridge_ip(distro: str) -> str | None:
    """Ask the chosen WSL distro what its default-route gateway is — that's
    the Windows host IP that WSL clients will reach us at. Bind to exactly
    that address rather than `0.0.0.0` so only WSL traffic can hit us.
    Returns None if `wsl.exe` is absent or the probe fails."""
    if shutil.which("wsl.exe") is None:
        return None
    try:
        # `ip route show default` may return multiple lines on a VPN-active
        # box (bot-triage residual #10) — pin to first via `head -1`.
        r = _quiet_run(
            ["wsl.exe", "-d", distro, "--",
             "sh", "-c", "ip route show default | head -1 | awk '{print $3}'"],
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    ip = _decode_wsl_output(r.stdout).strip()
    # Sanity-check: must look like an IPv4 address.
    parts = ip.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) < 256 for p in parts):
        return ip
    return None


# ─── WSL distro enumeration ───────────────────────────────────────────────────


def list_wsl_distros() -> list[str]:
    """Return the names of installed WSL distros. Empty list when wsl.exe is
    absent (Mac/Linux dev) or no distros are installed."""
    if shutil.which("wsl.exe") is None:
        return []
    try:
        r = _quiet_run(["wsl.exe", "-l", "-q"], timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if r.returncode != 0:
        return []
    text = _decode_wsl_output(r.stdout)
    return [line.strip() for line in text.splitlines() if line.strip()]


def wsl_mirrored_mode() -> bool:
    """Detect WSL2 `networkingMode=mirrored`. Bot-triage residual #2/#9/#12:
    `.wslconfig` lives on the Windows side at `%USERPROFILE%\\.wslconfig`, NOT
    inside the WSL distro at `/etc/wsl.conf` (that file is per-distro and
    cannot set `networkingMode`, which is a global WSL2 setting)."""
    userprofile = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    if not userprofile:
        return False
    cfg = Path(userprofile) / ".wslconfig"
    if not cfg.exists():
        return False
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key.strip().lower() == "networkingmode" and \
                value.strip().lower() == "mirrored":
            return True
    return False


# ─── Windows env injection (Enable for Windows toggle) ────────────────────────


def enable_for_windows(base_url: str) -> tuple[bool, str]:
    """Write ANTHROPIC_* env to user-scope env via `setx` + merge into
    `%USERPROFILE%\\.claude\\settings.json`. Returns (ok, message)."""
    if sys.platform != "win32":
        return (False, "Enable-for-Windows only runs on Windows hosts. "
                       "On macOS/Linux the toggle is a no-op (dev mode).")
    ok_setx, msg_setx = _setx_user("ANTHROPIC_BASE_URL", base_url)
    if not ok_setx:
        return (False, msg_setx)
    ok_tok, msg_tok = _setx_user("ANTHROPIC_AUTH_TOKEN", "dummy")
    if not ok_tok:
        return (False, msg_tok)
    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    cfg = home / ".claude" / "settings.json"
    try:
        _merge_claude_settings(cfg, {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_AUTH_TOKEN": "dummy",
        })
    except OSError as e:
        return (False, f"setx ok, but settings.json write failed: {e}")
    return (True, "Windows env updated. Restart any open terminals / IDEs to "
                  "pick up the new env (setx does not affect running processes).")


def _setx_user(name: str, value: str) -> tuple[bool, str]:
    try:
        r = _quiet_run(["setx", name, value], timeout=10)
    except (subprocess.TimeoutExpired, OSError) as e:
        return (False, f"setx invocation failed: {e}")
    if r.returncode != 0:
        return (False, f"setx returned {r.returncode}: "
                       f"{r.stderr.decode(errors='replace')[:200]}")
    return (True, "")


def _merge_claude_settings(path: Path, env_updates: dict[str, str]) -> None:
    """Merge env_updates into the file's `env` block, preserving everything
    else. Creates parent dir + file if missing. UTF-8 explicit because
    Path.read_text()/write_text() default to the system code page on Windows
    (typically CP1252), which corrupts non-ASCII content in settings.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    env.update(env_updates)
    data["env"] = env
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ─── WSL env injection (Enable for WSL toggle) ────────────────────────────────

WSL_RC_BLOCK_MARKER = "# >>> copilot-gateway env >>>"
WSL_RC_BLOCK_END = "# <<< copilot-gateway env <<<"


def _wsl_rc_block(port: int) -> str:
    """The shell-function wrapper written into the chosen distro's rc-file.

    Resolves the Windows host IP at every shell start (avoiding the
    static-IP-in-bashrc trap when WSL2 host IP changes per restart) and exports
    ANTHROPIC_* + OPENAI_* env. Bot-triage residual #11: Codex CLI reads
    `OPENAI_BASE_URL` / `OPENAI_API_KEY`, not `ANTHROPIC_*`, so we export both
    families."""
    return f"""{WSL_RC_BLOCK_MARKER}
# Auto-generated by copilot-gateway tray_app.py — do not edit by hand.
# Resolves Windows host IP at every shell start for use with the gateway.
_copilot_gateway_resolve_host() {{
    # 1. WSL2 mirrored mode: gateway is reachable at 127.0.0.1.
    #    The setting lives in Windows-side .wslconfig; the WSL-side username
    #    may differ from the Windows username, so glob /mnt/c/Users/*.
    if grep -lq 'networkingMode\\s*=\\s*mirrored' \
            /mnt/c/Users/*/.wslconfig 2>/dev/null; then
        echo 127.0.0.1; return 0
    fi
    # 2. host.docker.internal (if user enabled it in .wslconfig)
    local hd
    hd=$(getent hosts host.docker.internal 2>/dev/null | awk '{{print $1}}' | head -1)
    if [ -n "$hd" ]; then echo "$hd"; return 0; fi
    # 3. Default-route gateway (robust against systemd-resolved, custom DNS).
    #    `head -1` because VPN-active machines may report multiple defaults.
    local gw
    gw=$(ip route show default 2>/dev/null | head -1 | awk '{{print $3}}')
    if [ -n "$gw" ]; then echo "$gw"; return 0; fi
    # 4. Final fallback: first non-loopback nameserver from /etc/resolv.conf
    awk '/^nameserver/ && $2 !~ /^127\\./ {{print $2; exit}}' /etc/resolv.conf 2>/dev/null
}}
_cg_host=$(_copilot_gateway_resolve_host)
if [ -n "$_cg_host" ]; then
    export ANTHROPIC_BASE_URL="http://$_cg_host:{port}"
    export ANTHROPIC_AUTH_TOKEN=dummy
    export ANTHROPIC_CUSTOM_HEADERS="X-Gateway-Origin: wsl"
    export OPENAI_BASE_URL="http://$_cg_host:{port}/v1"
    export OPENAI_API_KEY=dummy
fi
unset _cg_host
{WSL_RC_BLOCK_END}
"""


def _wsl_detect_shell(distro: str) -> tuple[str, str]:
    """Return (shell_name, rc_file_path_in_distro) for the chosen distro's
    user. Falls back to (`sh`, `~/.profile`) when detection fails."""
    if shutil.which("wsl.exe") is None:
        return ("sh", "~/.profile")
    try:
        r = _quiet_run(
            ["wsl.exe", "-d", distro, "--",
             "sh", "-c",
             "getent passwd \"$(whoami)\" 2>/dev/null "
             "|| grep \"^$(whoami):\" /etc/passwd"],
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ("sh", "~/.profile")
    if r.returncode != 0:
        return ("sh", "~/.profile")
    line = _decode_wsl_output(r.stdout).strip().splitlines()
    shell_path = line[0].split(":")[-1] if line else ""
    name = shell_path.rsplit("/", 1)[-1] or "sh"
    rc = {
        "bash":  "~/.bashrc",
        "zsh":   "~/.zshrc",
        "fish":  "~/.config/fish/config.fish",
    }.get(name, "~/.profile")
    return (name, rc)


def enable_for_wsl(distro: str, port: int) -> tuple[bool, str]:
    """Append the shell-function wrapper to the chosen distro's rc-file
    (idempotent — replaces an existing block matched by marker comments) and
    merge ANTHROPIC_* env into `~/.claude/settings.json` inside the distro.
    Returns (ok, message)."""
    if shutil.which("wsl.exe") is None:
        return (False, "wsl.exe not on PATH — Enable-for-WSL only runs on "
                       "Windows hosts with WSL installed.")
    shell_name, rc_path = _wsl_detect_shell(distro)
    block = _wsl_rc_block(port)

    # Bot-triage residual #4: writing rc files through `\\wsl.localhost\<distro>`
    # introduces CRLF line endings that bash will refuse to parse. Instead,
    # shell out and let the WSL-side `sh` do the file mutation — line endings
    # stay LF natively.
    #
    # `set -e` makes any step (awk, cat, mv) abort the script on failure,
    # leaving the user's rc-file untouched (we only `mv` over it at the end).
    # If awk fails on full disk / permission flip, `$rc` is never clobbered;
    # `$rc.cg-tmp` may linger but is harmless and self-overwritten next run.
    rewrite_script = f"""
        set -e
        rc='{rc_path}'
        eval rc=\"$rc\"
        mkdir -p "$(dirname "$rc")"
        touch "$rc"
        awk '/{WSL_RC_BLOCK_MARKER}/{{skip=1}} \
             !skip; \
             /{WSL_RC_BLOCK_END}/{{skip=0; next}}' "$rc" > "$rc.cg-tmp"
        cat >> "$rc.cg-tmp" <<'CG_EOF'
{block}CG_EOF
        mv "$rc.cg-tmp" "$rc"
    """
    try:
        r = _quiet_run(["wsl.exe", "-d", distro, "--", "sh", "-c", rewrite_script],
                       timeout=10)
    except (subprocess.TimeoutExpired, OSError) as e:
        return (False, f"wsl.exe invocation failed: {e}")
    if r.returncode != 0:
        return (False, f"rc-file rewrite returned {r.returncode}: "
                       f"{_decode_wsl_output(r.stderr)[:200]}")

    # Also merge into ~/.claude/settings.json inside the distro — picked up by
    # `claude` regardless of shell (covers VS Code WSL extension launches).
    settings_script = (
        "f=$HOME/.claude/settings.json; mkdir -p \"$(dirname \"$f\")\"; "
        "[ -f \"$f\" ] || echo '{}' > \"$f\"; "
        "python3 - \"$f\" <<'PY'\n"
        "import json, sys\n"
        "p=sys.argv[1]\n"
        "try: d=json.load(open(p))\n"
        "except: d={}\n"
        "env=d.get('env') if isinstance(d.get('env'), dict) else {}\n"
        "env['ANTHROPIC_AUTH_TOKEN']='dummy'\n"
        "env['ANTHROPIC_CUSTOM_HEADERS']='X-Gateway-Origin: wsl'\n"
        "d['env']=env\n"
        "json.dump(d, open(p,'w'), indent=2)\n"
        "PY\n"
    )
    try:
        _quiet_run(["wsl.exe", "-d", distro, "--", "sh", "-c", settings_script],
                   timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass  # Best-effort; rc-file is the load-bearing path.

    return (True, f"WSL env wired into {distro} ({shell_name} → {rc_path}). "
                  f"Open a new shell inside the distro to pick it up.")


# ─── [Test] probes ────────────────────────────────────────────────────────────


def test_windows_env() -> tuple[bool, str]:
    """Spawn `claude --help` (or `claude --version`) in a NEW cmd.exe so it
    inherits the freshly-`setx`-updated env (bot-triage residual #7: env set
    via `setx` is NOT inherited by children of the current process)."""
    if sys.platform != "win32":
        return (True, "Windows test is a no-op on macOS/Linux (dev mode).")
    try:
        r = _quiet_run(["cmd.exe", "/c", "claude --version"], timeout=10)
    except (subprocess.TimeoutExpired, OSError) as e:
        return (False, f"probe spawn failed: {e}")
    out = (r.stdout + r.stderr).decode(errors="replace")
    if r.returncode == 0:
        return (True, f"claude responded: {out.strip()[:120]}")
    return (False, f"claude probe exited {r.returncode}: {out.strip()[:200]}")


def test_wsl_env(distro: str) -> tuple[bool, str]:
    """Invoke the detected shell in interactive mode so the rc-file wrapper
    actually loads, then run `claude --help`. The shell flag matters: bash
    needs `-i` to read `~/.bashrc`; fish reads `config.fish` for non-
    interactive shells too."""
    if shutil.which("wsl.exe") is None:
        return (False, "wsl.exe not on PATH.")
    shell_name, _ = _wsl_detect_shell(distro)
    if shell_name == "bash":
        cmd = ["wsl.exe", "-d", distro, "--", "bash", "-ic", "claude --version"]
    elif shell_name == "zsh":
        cmd = ["wsl.exe", "-d", distro, "--", "zsh", "-ic", "claude --version"]
    elif shell_name == "fish":
        cmd = ["wsl.exe", "-d", distro, "--", "fish", "-c", "claude --version"]
    else:
        cmd = ["wsl.exe", "-d", distro, "--", "sh", "-lc", "claude --version"]
    try:
        r = _quiet_run(cmd, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as e:
        return (False, f"WSL probe failed: {e}")
    out = _decode_wsl_output(r.stdout + r.stderr)
    if r.returncode == 0:
        return (True, f"WSL/{distro} claude responded: {out.strip()[:120]}")
    return (False, f"WSL/{distro} probe exited {r.returncode}: {out.strip()[:200]}")


# ─── Stats poller (background thread) ─────────────────────────────────────────


class StatsPoller(threading.Thread):
    """Polls /stats on a fixed cadence; pushes the latest snapshot to the UI
    via a callback. Daemon thread so it dies with the process."""

    def __init__(self, gateway: GatewayProcess, on_update):
        super().__init__(daemon=True, name="StatsPoller")
        self.gateway = gateway
        self.on_update = on_update
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            snap = self._fetch_once()
            try:
                self.on_update(snap)
            except Exception:  # noqa: BLE001 — never let UI bugs kill the poller
                pass
            self._stop.wait(STATS_POLL_INTERVAL_S)

    def _fetch_once(self) -> dict | None:
        try:
            with urllib.request.urlopen(self.gateway.stats_url,
                                        timeout=HTTP_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, socket.timeout,
                json.JSONDecodeError, ConnectionError, OSError):
            return None


def format_title(snap: dict | None) -> str:
    """Pack per-origin counts into the tray title. Falls back to a clearly-
    distinguishable string when the gateway is unreachable."""
    if snap is None:
        return "[gateway offline]"
    per = snap.get("per_origin") or {}
    win = per.get("windows") or {}
    wsl = per.get("wsl") or {}
    win_req = win.get("requests", 0)
    wsl_req = wsl.get("requests", 0)
    win_tok = (win.get("input_tokens", 0) + win.get("output_tokens", 0)) // 1000
    wsl_tok = (wsl.get("input_tokens", 0) + wsl.get("output_tokens", 0)) // 1000
    return f"[WIN {win_req} reqs / {win_tok}k tok] [WSL {wsl_req} reqs / {wsl_tok}k tok]"


# ─── Tkinter UI helpers ───────────────────────────────────────────────────────


def _build_tray_icon_image():
    """Return a Pillow Image for the tray icon. Kept tiny so this file stays
    runnable when Pillow is unavailable (smoke mode)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(20, 120, 200, 255))
    d.text((20, 18), "CG", fill=(255, 255, 255, 255))
    return img


class TrayUI:
    """Owns the tkinter root, all popups, and the pystray icon. Menu callbacks
    (running on pystray's worker thread) marshal UI work back to the tkinter
    event loop via `root.after(0, fn)`."""

    def __init__(self, gateway: GatewayProcess, bind_posture: str,
                 base_url: str, args):
        import tkinter as tk
        self.tk = tk
        self.gateway = gateway
        self.bind_posture = bind_posture
        self.base_url = base_url
        self.args = args
        self.latest_snap: dict | None = None
        self.distros: list[str] = list_wsl_distros()
        self.win_enabled = False
        self.wsl_enabled_distros: set[str] = set()

        self.root = tk.Tk()
        self.root.withdraw()  # invisible root; popups are children

        self.poller = StatsPoller(gateway, self._on_stats)
        self.icon = self._build_icon()

    # — pystray menu —

    def _build_icon(self):
        import pystray
        menu_items = [
            pystray.MenuItem("Stats…", lambda *_: self._marshal(self._show_stats)),
            pystray.MenuItem("View logs…", lambda *_: self._marshal(self._show_logs)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Copy claude command",
                             lambda *_: self._marshal(self._copy_claude)),
            pystray.MenuItem("Copy codex command",
                             lambda *_: self._marshal(self._copy_codex)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Enable for Windows",
                lambda *_: self._marshal(self._toggle_windows),
                checked=lambda _: self.win_enabled,
            ),
            pystray.MenuItem("  [Test] Windows",
                             lambda *_: self._marshal(self._test_windows)),
            pystray.MenuItem("Enable for WSL", self._wsl_submenu()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Stop & quit", lambda *_: self._marshal(self.stop)),
        ]
        return pystray.Icon(
            "copilot-gateway",
            _build_tray_icon_image(),
            title=format_title(None),
            menu=pystray.Menu(*menu_items),
        )

    def _wsl_submenu(self):
        import pystray
        if not self.distros:
            return pystray.Menu(pystray.MenuItem(
                "(no WSL distros detected)", None, enabled=False))
        items = []
        for distro in self.distros:
            d = distro  # bind for closure
            items.append(pystray.MenuItem(
                d,
                lambda *_, _d=d: self._marshal(lambda: self._toggle_wsl(_d)),
                checked=lambda _i, _d=d: _d in self.wsl_enabled_distros,
            ))
            items.append(pystray.MenuItem(
                f"  [Test] {d}",
                lambda *_, _d=d: self._marshal(lambda: self._test_wsl(_d)),
            ))
        return pystray.Menu(*items)

    # — Thread marshalling —

    def _marshal(self, fn):
        """Schedule `fn` onto the tkinter event loop (main thread). Menu
        callbacks fire from pystray's worker thread; tkinter is single-
        threaded and crashes if touched from outside."""
        self.root.after(0, fn)

    # — Stats callback (poller thread → marshalled to main) —

    def _on_stats(self, snap: dict | None):
        self.latest_snap = snap
        title = format_title(snap)
        try:
            self.icon.title = title
        except Exception:  # noqa: BLE001
            pass

    # — Popups —

    def _show_stats(self):
        snap = self.latest_snap
        lines = [f"Bind: {self.gateway.host}:{self.gateway.port}  "
                 f"[{self.bind_posture.upper()}]", ""]
        if snap is None:
            lines.append("(gateway not reachable — start it or check /health)")
        else:
            per = snap.get("per_origin") or {}
            lines.append(f"{'':<10} {'Requests':>10} {'In tok':>10} "
                         f"{'Out tok':>10} {'Failed':>8}")
            for o in ("windows", "wsl", "other"):
                v = per.get(o) or {}
                lines.append(f"{o:<10} {v.get('requests', 0):>10} "
                             f"{v.get('input_tokens', 0):>10} "
                             f"{v.get('output_tokens', 0):>10} "
                             f"{v.get('requests_failed', 0):>8}")
            lines.append("")
            lines.append(f"Total tokens: {snap.get('total_tokens', 0)}")
            lines.append(f"Premium reqs: "
                         f"{snap.get('estimated_premium_requests', 0)}")
        self._popup("Gateway stats", "\n".join(lines))

    def _show_logs(self):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title("Gateway logs (last 200)")
        win.geometry("900x500")
        text = tk.Text(win, font=("Menlo", 10), wrap="none")
        text.pack(fill="both", expand=True)
        text.tag_config("windows", foreground="#1565c0")
        text.tag_config("wsl", foreground="#2e7d32")
        text.tag_config("other", foreground="#666666")
        text.insert("end", "(loading logs…)\n", "other")
        text.config(state="disabled")

        # Fetch on a worker thread so a slow /logs response doesn't freeze the
        # entire tray + tkinter event loop for up to HTTP_TIMEOUT_S seconds.
        def _fetch():
            try:
                with urllib.request.urlopen(self.gateway.logs_url + "?n=200",
                                            timeout=HTTP_TIMEOUT_S) as resp:
                    body = resp.read().decode(errors="replace")
            except (urllib.error.URLError, socket.timeout, OSError) as e:
                body = f"(error fetching logs: {e})"
            self.root.after(0, lambda: _render(body))

        def _render(body: str):
            if not win.winfo_exists():
                return
            text.config(state="normal")
            text.delete("1.0", "end")
            for line in body.splitlines():
                tag = "other"
                if "origin=windows" in line:
                    tag = "windows"
                elif "origin=wsl" in line:
                    tag = "wsl"
                text.insert("end", line + "\n", tag)
            text.config(state="disabled")

        threading.Thread(target=_fetch, daemon=True,
                         name="logs-fetch").start()

    def _copy_claude(self):
        cmd = (f"ANTHROPIC_BASE_URL={self.base_url} "
               f"ANTHROPIC_AUTH_TOKEN=dummy claude")
        self._clip(cmd)
        self._toast("Copied", f"Copied to clipboard:\n\n{cmd}", ok=True)

    def _copy_codex(self):
        cmd = (f"OPENAI_BASE_URL={self.base_url}/v1 "
               f"OPENAI_API_KEY=dummy codex")
        self._clip(cmd)
        self._toast("Copied", f"Copied to clipboard:\n\n{cmd}", ok=True)

    def _clip(self, text: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()

    # — Toggles —

    def _toggle_windows(self):
        if self.win_enabled:
            self._toast("Already enabled",
                        "To disable, manually remove ANTHROPIC_* from your "
                        "user env (Environment Variables… in System Properties) "
                        "and from %USERPROFILE%\\.claude\\settings.json.",
                        ok=True)
            return
        ok, msg = enable_for_windows(self.base_url)
        if ok:
            self.win_enabled = True
        self._toast("Enable for Windows", msg, ok=ok)
        try:
            self.icon.update_menu()
        except Exception:  # noqa: BLE001
            pass

    def _test_windows(self):
        ok, msg = test_windows_env()
        self._toast("Test Windows env", msg, ok=ok)

    def _toggle_wsl(self, distro: str):
        if distro in self.wsl_enabled_distros:
            self._toast(f"Already enabled in {distro}",
                        "To disable, edit the rc-file (~/.bashrc / ~/.zshrc / "
                        "~/.config/fish/config.fish) and delete the block "
                        "between the copilot-gateway markers.", ok=True)
            return
        ok, msg = enable_for_wsl(distro, self.gateway.port)
        if ok:
            self.wsl_enabled_distros.add(distro)
        self._toast(f"Enable for WSL ({distro})", msg, ok=ok)
        try:
            self.icon.update_menu()
        except Exception:  # noqa: BLE001
            pass

    def _test_wsl(self, distro: str):
        ok, msg = test_wsl_env(distro)
        self._toast(f"Test WSL ({distro})", msg, ok=ok)

    # — Generic UI primitives —

    def _popup(self, title: str, body: str):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title(title)
        text = tk.Text(win, font=("Menlo", 10), width=70, height=20)
        text.insert("1.0", body)
        text.config(state="disabled")
        text.pack(fill="both", expand=True, padx=8, pady=8)

    def _toast(self, title: str, msg: str, ok: bool):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title(title)
        bg = "#dcedc8" if ok else "#ffcdd2"
        win.configure(bg=bg)
        lbl = tk.Label(win, text=msg, bg=bg, font=("Menlo", 10),
                       wraplength=520, justify="left", padx=12, pady=12)
        lbl.pack()
        btn = tk.Button(win, text="OK", command=win.destroy)
        btn.pack(pady=(0, 8))

    # — Lifecycle —

    def run(self):
        self.poller.start()
        # pystray on the worker; tkinter on the main thread per plan §"Item 3".
        threading.Thread(target=self.icon.run, daemon=True,
                         name="pystray").start()
        try:
            self.root.mainloop()
        finally:
            self.stop()

    def stop(self):
        self.poller.stop()
        try:
            self.icon.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.quit()
        except Exception:  # noqa: BLE001
            pass
        self.gateway.stop()


# ─── Smoke-mode (dev-side probe) ──────────────────────────────────────────────


def smoke():
    """Print one-line results for every platform / dependency probe and exit.
    Used as the Mac dev-side acceptance test (the WSL toggle is expected to
    report `wsl_available=False` and the Windows toggle `setx_available=False`)."""
    pystray_ok = _try_import("pystray")
    pillow_ok = _try_import("PIL.Image")
    tk_ok = _try_import("tkinter")
    setx_ok = shutil.which("setx") is not None
    wsl_ok = shutil.which("wsl.exe") is not None
    distros = list_wsl_distros() if wsl_ok else []
    mirrored = wsl_mirrored_mode() if sys.platform == "win32" else False
    print(f"platform={sys.platform}")
    print(f"python={sys.version.split()[0]}")
    print(f"pystray_importable={pystray_ok}")
    print(f"pillow_importable={pillow_ok}")
    print(f"tkinter_importable={tk_ok}")
    print(f"setx_available={setx_ok}")
    print(f"wsl_available={wsl_ok}")
    print(f"wsl_distros={distros}")
    print(f"wsl_mirrored_mode={mirrored}")
    bridge_host, posture = select_bind_host(
        wsl_enabled=bool(distros), distro=distros[0] if distros else None)
    print(f"bind_host_if_wsl_enabled={bridge_host} posture={posture}")


def _try_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


# ─── main() ───────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Copilot Gateway tray app (Windows)")
    p.add_argument("--host", default=None,
                   help="Bind host for the spawned gateway. Default: 127.0.0.1 "
                        "(loopback). Overridden to a WSL-bridge IP or 0.0.0.0 "
                        "if Enable-for-WSL is toggled on at runtime.")
    p.add_argument("--port", type=int, default=GATEWAY_DEFAULT_PORT)
    p.add_argument("--smoke", action="store_true",
                   help="Print platform/dep probes and exit. No tray.")
    args = p.parse_args(argv)

    if args.smoke:
        smoke()
        return 0

    # Pick an initial bind host. The WSL toggle, when activated at runtime,
    # would ideally re-bind the gateway — that requires a restart of the
    # gateway subprocess, deferred to a follow-up (`gateway-rebind-on-toggle`,
    # see Out of Scope in plan.md).
    distros = list_wsl_distros()
    initial_host = args.host or GATEWAY_DEFAULT_HOST
    bind_posture = "loopback" if initial_host == "127.0.0.1" else (
        "lan-exposed" if initial_host in ("0.0.0.0", "") else "wsl-bridge")

    gateway = GatewayProcess(initial_host, args.port)
    status = gateway.start()
    if status == "missing-gateway":
        print(f"[tray] gateway.py not found at {GATEWAY_PY}; aborting.",
              file=sys.stderr)
        return 2

    base_url = (f"http://localhost:{args.port}" if initial_host in ("0.0.0.0",
                "127.0.0.1") else f"http://{initial_host}:{args.port}")

    try:
        ui = TrayUI(gateway, bind_posture, base_url, args)
    except ImportError as e:
        print(f"[tray] missing dependency: {e}. Install with: "
              f"pip install pystray pillow", file=sys.stderr)
        gateway.stop()
        return 3

    try:
        ui.run()
    except KeyboardInterrupt:
        pass
    finally:
        ui.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
