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

Bind safety: by default the spawned gateway listens on `127.0.0.1`
(loopback — no LAN or WSL reach). Passing `--host 0.0.0.0` makes it
reachable from WSL and any LAN host; the Stats popup shows a
`[LAN-EXPOSED]` posture line so the user always sees what they're
listening on. Runtime re-bind on Enable-for-WSL toggle is deferred
(see `docs/design/windows-app/plan.md` §"Out of Scope").

Smoke mode: `python3 tray_app.py --smoke` runs platform/dependency probes,
prints a one-line summary per probe, and exits — used for dev-side validation
on Mac (where the tray itself isn't expected to render).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

GATEWAY_DEFAULT_HOST = "127.0.0.1"
GATEWAY_DEFAULT_PORT = 8787
STATS_POLL_INTERVAL_S = 2.0
HTTP_TIMEOUT_S = 1.5

# `TkFixedFont` is Tk's built-in named font alias for the platform-native
# monospace face (Consolas on Windows, Menlo on macOS, sensible default on
# Linux). Using a hardcoded family like "Menlo" silently falls back to a
# proportional default on platforms that don't ship it.
MONO_FONT = "TkFixedFont"

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
    """Decode subprocess output that might be UTF-16LE (BOM-prefixed) or
    plain UTF-8. `wsl.exe -l -q` is the only command that writes UTF-16LE
    with a BOM (bot-triage residual #3/#8). Anything run *inside* a distro
    via `wsl.exe -d <name> -- sh -c '...'` relays distro stdout as plain
    UTF-8. Probing the BOM byte before attempting UTF-16 keeps codex-finding
    `tray_app.py:81` from silently rendering UTF-8 bytes as Chinese chars."""
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _is_loopback(host: str | None) -> bool:
    """True iff `host` is a loopback address (127.0.0.0/8, ::1) or the name
    "localhost". Used by enable_for_wsl to refuse silently-broken configs
    when the gateway can't be reached from a non-mirrored WSL distro."""
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, ImportError):
        return False


def _port_available(host: str, port: int) -> bool:
    """Probe-bind to (host, port) — true if available, false if something
    else holds it. Used to surface port-conflicts BEFORE spawning gateway.py
    (which would otherwise die silently with EADDRINUSE on suppressed stderr).
    Uses SO_REUSEADDR off — we want the exact same semantics as gateway.py's
    HTTPServer. Family is auto-resolved so an IPv6 host like `::1` works."""
    bind_host = "0.0.0.0" if host in ("0.0.0.0", "") else host
    try:
        infos = socket.getaddrinfo(bind_host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    if not infos:
        return False
    family, socktype, proto, _, sockaddr = infos[0]
    s = socket.socket(family, socktype, proto)
    try:
        s.bind(sockaddr)
        return True
    except OSError:
        return False
    finally:
        s.close()


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
        return f"http://{self._probe_host()}:{self.port}/stats"

    @property
    def logs_url(self) -> str:
        return f"http://{self._probe_host()}:{self.port}/logs"

    @property
    def health_url(self) -> str:
        return f"http://{self._probe_host()}:{self.port}/health"

    def _probe_host(self) -> str:
        """Hostname for our own HTTP probes. For a 0.0.0.0 / '' bind we
        target 127.0.0.1 (which 0.0.0.0 listens on too), but `start()`
        refuses to attach in that case — see the docstring there."""
        return "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host

    def already_running(self) -> bool:
        """True iff /health returns 200 with a JSON body shaped like the
        gateway's health response (must contain BOTH 'version' and 'status'
        keys — generic services exposing just {"status": "ok"} no longer
        false-attach). Narrower than "port is bound": a 404 from an unrelated
        HTTP service on the port shouldn't make us attach (codex P2). The
        port-conflict case is handled by _port_available's pre-spawn probe."""
        try:
            with urllib.request.urlopen(self.health_url, timeout=HTTP_TIMEOUT_S) as resp:
                body = json.loads(resp.read().decode())
                return (isinstance(body, dict)
                        and "version" in body and "status" in body)
        except (urllib.error.URLError, socket.timeout, ConnectionError,
                json.JSONDecodeError, OSError):
            return False

    def start(self) -> str:
        """Return one of: 'spawned', 'attached', 'missing-gateway',
        'port-busy'.

        For a `--host 0.0.0.0` bind we always spawn rather than attach to
        whatever is already on 127.0.0.1, because an existing loopback-only
        gateway would silently defeat the user's LAN-reach intent (per
        copilot-pull-request-reviewer finding on `health_url`). Before
        spawning, we pre-check port availability with a probe-bind — if the
        port is held by anything (incl. a loopback-only gateway that would
        collide with our 0.0.0.0 bind), we return 'port-busy' so the caller
        can surface a clear error instead of leaving a child Popen that
        immediately dies with EADDRINUSE on stdout we suppressed.
        """
        if self.host not in ("0.0.0.0", "") and self.already_running():
            self.attached = True
            return "attached"
        if not GATEWAY_PY.exists():
            return "missing-gateway"
        if not _port_available(self.host, self.port):
            return "port-busy"
        cmd = [sys.executable, str(GATEWAY_PY),
               "--host", self.host, "--port", str(self.port)]
        # New process group so we can kill the gateway AND its children
        # (gateway.py auto-launches demo.py — codex finding tray_app.py:185).
        # On Windows that's a job-object via CREATE_NEW_PROCESS_GROUP; on
        # POSIX it's a fresh session via start_new_session=True.
        popen_kw = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            popen_kw["creationflags"] = _CREATE_NO_WINDOW | 0x00000200  # CREATE_NEW_PROCESS_GROUP
        else:
            popen_kw["start_new_session"] = True
        self.proc = subprocess.Popen(cmd, **popen_kw)
        return "spawned"

    def stop(self):
        if self.attached or self.proc is None:
            return
        # Kill the process tree so gateway.py's child demo.py also terminates.
        # On Windows we use `taskkill /F /T /PID` rather than
        # CTRL_BREAK_EVENT — the latter requires an attached console, which
        # the packaged --noconsole tray doesn't have, so the signal silently
        # no-ops (codex P2 finding on packaged Windows builds). On POSIX we
        # signal the process group we created via start_new_session=True.
        try:
            if sys.platform == "win32":
                _quiet_run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                           timeout=5)
            else:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError, ValueError,
                subprocess.TimeoutExpired):
            pass
        try:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    if sys.platform != "win32":
                        os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                    else:
                        self.proc.kill()
                except (OSError, ProcessLookupError):
                    pass
                self.proc.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass


# ─── Bind-host selection (SECURITY-HIGH mitigation from PR #2 #6) ─────────────


def select_bind_host(wsl_enabled: bool, distro: str | None) -> tuple[str, str]:
    """Return (host, posture) where posture is one of:
        'loopback'      — 127.0.0.1, no LAN exposure
        'wsl-bridge'    — specific 172.16/12 or 192.168/16, only WSL reachable
        'lan-exposed'   — 0.0.0.0, ANY LAN/VPN client could reach us
    Prefers the safest option that satisfies the toggle state.
    """
    if not wsl_enabled:
        return ("127.0.0.1", "loopback")
    if distro:
        bridge = _probe_wsl_bridge_ip(distro)
        if bridge:
            return (bridge, _classify_bind_posture(bridge))
    return ("0.0.0.0", "lan-exposed")


def _classify_bind_posture(host: str) -> str:
    """Map a bind host to its security posture for the Stats popup.

    A `wsl-bridge` label is reserved for IPs in the WSL2 NAT range
    (172.16/12) — a generic LAN IP like 192.168.1.50 is `lan-bound` because
    it's reachable from any LAN/VPN client on that subnet, not WSL-only.
    """
    if host == "127.0.0.1":
        return "loopback"
    if host in ("0.0.0.0", ""):
        return "lan-exposed"
    # Parse the IP; treat anything in 172.16/12 as the WSL2 bridge.
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
        if ip in ipaddress.ip_network("172.16.0.0/12"):
            return "wsl-bridge"
    except (ValueError, ImportError):
        pass
    return "lan-bound"


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


def _resolve_static_wsl_base_url(distro: str, port: int) -> str | None:
    """Pick a stable ANTHROPIC_BASE_URL for settings.json inside the chosen
    distro. settings.json is loaded by `claude` regardless of shell — useful
    for non-rc invocations (VS Code WSL extension). Returns None when no
    stable URL is available (caller writes a `_comment_base_url` then).

    Stability ladder:
      1. mirrored mode → http://127.0.0.1:<port> (stable, no DNS)
      2. host.docker.internal resolves → http://host.docker.internal:<port>
         (stable per Windows instance; user enables it in .wslconfig)

    The default-route fallback used in the rc-block isn't used here because
    that IP rotates per WSL2 restart and would silently break the next day.
    """
    if shutil.which("wsl.exe") is None:
        return None
    # 1. mirrored mode check — read Windows-side .wslconfig directly from
    #    Python (cheaper than a wsl.exe subprocess round-trip).
    if wsl_mirrored_mode():
        return f"http://127.0.0.1:{port}"
    # 2. host.docker.internal probe (must run inside the distro because the
    #    Windows-side host doesn't have an /etc/hosts entry for it).
    try:
        r = _quiet_run(
            ["wsl.exe", "-d", distro, "--", "sh", "-c",
             "getent hosts host.docker.internal >/dev/null 2>&1 && echo OK || true"],
            timeout=5,
        )
        if r.returncode == 0 and b"OK" in r.stdout:
            return f"http://host.docker.internal:{port}"
    except (subprocess.TimeoutExpired, OSError):
        pass
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
    `%USERPROFILE%\\.claude\\settings.json`. Also sets `ANTHROPIC_CUSTOM_HEADERS`
    so Windows-side traffic is tagged with `X-Gateway-Origin: windows`
    explicitly (matching the WSL side). The header makes per-origin stats
    accurate even when the gateway bind is on a non-loopback IP that would
    otherwise IP-classify ambiguously. Returns (ok, message)."""
    if sys.platform != "win32":
        return (False, "Enable-for-Windows only runs on Windows hosts. "
                       "On macOS/Linux the toggle is a no-op (dev mode).")
    env_pairs = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": "dummy",
        "ANTHROPIC_CUSTOM_HEADERS": "X-Gateway-Origin: windows",
    }
    for name, value in env_pairs.items():
        ok, msg = _setx_user(name, value)
        if not ok:
            return (False, msg)
    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    cfg = home / ".claude" / "settings.json"
    try:
        _merge_claude_settings(cfg, env_pairs)
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
        # setx often reports the actual error to stdout, not stderr — include
        # both so the toast surfaces something actionable.
        out = (r.stdout + b"\n" + r.stderr).decode(errors="replace").strip()
        return (False, f"setx returned {r.returncode}: {out[:300]}")
    return (True, "")


def _merge_claude_settings(path: Path, env_updates: dict[str, str]) -> None:
    """Merge env_updates into the file's `env` block, preserving everything
    else. Creates parent dir + file if missing. UTF-8 explicit because
    Path.read_text()/write_text() default to the system code page on Windows
    (typically CP1252), which corrupts non-ASCII content in settings.json.

    If the existing file is unparseable (bad JSON, or top-level isn't a JSON
    object), we back it up to `<path>.cg-backup` before overwriting — the
    silent reset codex flagged would lose user content otherwise."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
            else:
                _backup_unreadable(path, raw, reason="top-level JSON is not an object")
        except (json.JSONDecodeError, OSError) as e:
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw = ""
            _backup_unreadable(path, raw, reason=f"unparseable: {e}")
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    env.update(env_updates)
    data["env"] = env
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _backup_unreadable(path: Path, raw: str, reason: str) -> None:
    """Stash unreadable settings.json content so we don't silently lose it."""
    bak = path.with_suffix(path.suffix + ".cg-backup")
    try:
        bak.write_text(raw, encoding="utf-8")
    except OSError:
        pass  # best-effort; can't fail the main flow on backup failure


# ─── WSL env injection (Enable for WSL toggle) ────────────────────────────────

WSL_RC_BLOCK_MARKER = "# >>> copilot-gateway env >>>"
WSL_RC_BLOCK_END = "# <<< copilot-gateway env <<<"


def _wsl_rc_block(port: int, shell: str = "bash",
                  wsl_userprofile: str | None = None) -> str:
    """The shell-function wrapper written into the chosen distro's rc-file.

    Resolves the Windows host IP at every shell start (avoiding the
    static-IP-in-bashrc trap when WSL2 host IP changes per restart) and exports
    ANTHROPIC_* + OPENAI_* env. Bot-triage residual #11: Codex CLI reads
    `OPENAI_BASE_URL` / `OPENAI_API_KEY`, not `ANTHROPIC_*`, so we export both
    families. `shell` selects the syntax — fish uses `set -gx` and `function
    NAME ... end` whereas bash/zsh/sh use POSIX `function() {{ ... }}` +
    `export`. Writing the POSIX block into `config.fish` would break the
    shell at every start.

    `wsl_userprofile` is the WSL-resolved path to the CURRENT Windows user's
    `%USERPROFILE%` (e.g. `/mnt/c/Users/lili`). When supplied, the rc-block
    greps that exact `.wslconfig` instead of globbing `/mnt/c/Users/*/.wslconfig`
    — both faster on Windows hosts with many user profiles AND immune to a
    different-user's `.wslconfig` mirrored setting being false-positive
    matched. Falls back to the glob when not supplied (caller couldn't
    resolve via `wslpath`)."""
    if shell == "fish":
        return _wsl_rc_block_fish(port, wsl_userprofile=wsl_userprofile)
    # shlex.quote on the userprofile so a Windows username like O'Connor (which
    # wslpath round-trips with the apostrophe intact) doesn't break out of the
    # single-quoted shell literal. The fallback glob has no user-provided
    # content, so it stays unquoted.
    wslconfig_path = (shlex.quote(f"{wsl_userprofile}/.wslconfig")
                      if wsl_userprofile else "/mnt/c/Users/*/.wslconfig")
    return f"""{WSL_RC_BLOCK_MARKER}
# Auto-generated by copilot-gateway tray_app.py — do not edit by hand.
# Resolves Windows host IP at every shell start for use with the gateway.
_copilot_gateway_resolve_host() {{
    # 1. WSL2 mirrored mode: gateway is reachable at 127.0.0.1.
    #    The setting lives in Windows-side .wslconfig — we point at the
    #    current Windows user's profile (resolved via wslpath at enable time)
    #    when known, else glob /mnt/c/Users/*.
    if grep -lE 'networkingMode[[:space:]]*=[[:space:]]*mirrored' \
            {wslconfig_path} 2>/dev/null >/dev/null; then
        echo 127.0.0.1; return 0
    fi
    # 2. host.docker.internal (if user enabled it in .wslconfig)
    _cg_hd=$(getent hosts host.docker.internal 2>/dev/null | awk '{{print $1}}' | head -1)
    if [ -n "$_cg_hd" ]; then echo "$_cg_hd"; unset _cg_hd; return 0; fi
    unset _cg_hd
    # 3. Default-route gateway (robust against systemd-resolved, custom DNS).
    #    `head -1` because VPN-active machines may report multiple defaults.
    _cg_gw=$(ip route show default 2>/dev/null | head -1 | awk '{{print $3}}')
    if [ -n "$_cg_gw" ]; then echo "$_cg_gw"; unset _cg_gw; return 0; fi
    unset _cg_gw
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


def _wsl_rc_block_fish(port: int, wsl_userprofile: str | None = None) -> str:
    """Fish-syntax variant of the rc-block (used when the user's WSL shell is
    fish). Fish doesn't speak POSIX `function() {{ }}` or `export VAR=val` —
    it wants `function NAME ... end` and `set -gx VAR val`. `wsl_userprofile`
    has the same meaning as in `_wsl_rc_block`."""
    if wsl_userprofile:
        wslconfig_path = shlex.quote(f"{wsl_userprofile}/.wslconfig")
        # specific path → no glob handling needed
        mirrored_check = (
            f"if test -e {wslconfig_path}\n"
            f"        if grep -lE 'networkingMode[[:space:]]*=[[:space:]]*mirrored' {wslconfig_path} 2>/dev/null >/dev/null\n"
            f"            echo 127.0.0.1; return 0\n"
            f"        end\n"
            f"    end"
        )
    else:
        # Fish errors on a glob that matches nothing — and the error fires
        # BEFORE `set` or `count` can run, defeating the bash-style guard.
        # Skip the mirrored-mode check entirely in this fallback path; the
        # other 3 probes below (host.docker.internal, default route, resolv)
        # cover mirrored mode too (getent hosts host.docker.internal → 127.0.0.1
        # in mirrored mode).
        mirrored_check = "# (mirrored-mode check skipped: wslpath unavailable)"
    return f"""{WSL_RC_BLOCK_MARKER}
# Auto-generated by copilot-gateway tray_app.py — do not edit by hand.
# Resolves Windows host IP at every shell start for use with the gateway.
function _copilot_gateway_resolve_host
    # 1. WSL2 mirrored mode: gateway is reachable at 127.0.0.1.
    {mirrored_check}
    # 2. host.docker.internal (if user enabled it in .wslconfig). In mirrored
    #    mode this typically resolves to 127.0.0.1, so it also covers
    #    mirrored-mode users when step 1 is skipped.
    set -l hd (getent hosts host.docker.internal 2>/dev/null | awk '{{print $1}}' | head -1)
    if test -n "$hd"; echo $hd; return 0; end
    # 3. Default-route gateway
    set -l gw (ip route show default 2>/dev/null | head -1 | awk '{{print $3}}')
    if test -n "$gw"; echo $gw; return 0; end
    # 4. /etc/resolv.conf fallback
    awk '/^nameserver/ && $2 !~ /^127\\./ {{print $2; exit}}' /etc/resolv.conf 2>/dev/null
end
set -l _cg_host (_copilot_gateway_resolve_host)
if test -n "$_cg_host"
    set -gx ANTHROPIC_BASE_URL "http://$_cg_host:{port}"
    set -gx ANTHROPIC_AUTH_TOKEN dummy
    set -gx ANTHROPIC_CUSTOM_HEADERS "X-Gateway-Origin: wsl"
    set -gx OPENAI_BASE_URL "http://$_cg_host:{port}/v1"
    set -gx OPENAI_API_KEY dummy
end
{WSL_RC_BLOCK_END}
"""


def _wsl_resolve_userprofile(distro: str) -> str | None:
    """Resolve `%USERPROFILE%` to a WSL-side path (e.g. `/mnt/c/Users/lili`)
    via `wsl.exe -d <distro> -- wslpath -u "$USERPROFILE"`. wsl.exe carries
    Windows env (including USERPROFILE) into the distro's environment, so
    wslpath receives the right value. Returns None if wsl.exe is absent or
    the probe fails — caller falls back to the glob."""
    if shutil.which("wsl.exe") is None:
        return None
    try:
        r = _quiet_run(
            ["wsl.exe", "-d", distro, "--",
             "sh", "-c", 'wslpath -u "$USERPROFILE" 2>/dev/null'],
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    out = _decode_wsl_output(r.stdout).strip()
    return out if out.startswith("/") else None


def _wsl_detect_shell(distro: str) -> tuple[str, str]:
    """Return (shell_name, rc_file_path_in_distro) for the chosen distro's
    user. Uses `$HOME`-based paths (not `~`) so the rewrite_script can
    avoid `eval` entirely — `$HOME` is always set in the sh process spawned
    by `wsl.exe -- sh -c`. Falls back to (`sh`, `$HOME/.profile`) when
    detection fails."""
    fallback = ("sh", "$HOME/.profile")
    if shutil.which("wsl.exe") is None:
        return fallback
    try:
        r = _quiet_run(
            ["wsl.exe", "-d", distro, "--",
             "sh", "-c",
             "getent passwd \"$(whoami)\" 2>/dev/null "
             "|| grep \"^$(whoami):\" /etc/passwd"],
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return fallback
    if r.returncode != 0:
        return fallback
    line = _decode_wsl_output(r.stdout).strip().splitlines()
    shell_path = line[0].split(":")[-1] if line else ""
    name = shell_path.rsplit("/", 1)[-1] or "sh"
    rc = {
        "bash":  "$HOME/.bashrc",
        "zsh":   "$HOME/.zshrc",
        "fish":  "$HOME/.config/fish/config.fish",
    }.get(name, "$HOME/.profile")
    return (name, rc)


def enable_for_wsl(distro: str, port: int,
                   gateway_host: str | None = None) -> tuple[bool, str]:
    """Append the shell-function wrapper to the chosen distro's rc-file
    (idempotent — replaces an existing block matched by marker comments) and
    merge ANTHROPIC_* env into `~/.claude/settings.json` inside the distro.
    Returns (ok, message).

    `gateway_host` is the host the user's tray-spawned gateway is actually
    bound to. When it's a loopback IP and the distro isn't in mirrored mode,
    the WSL-side env we'd write points at a host where the gateway isn't
    listening — silent broken-config (codex P2). Refuse with a clear message
    so the user re-launches the tray with `--host 0.0.0.0` first."""
    if shutil.which("wsl.exe") is None:
        return (False, "wsl.exe not on PATH — Enable-for-WSL only runs on "
                       "Windows hosts with WSL installed.")
    if _is_loopback(gateway_host) and not wsl_mirrored_mode():
        return (False, "Gateway is bound to a loopback address "
                       f"({gateway_host}). WSL distros can't reach loopback "
                       "unless WSL2 is in mirrored networking mode. Restart "
                       "the tray with `python tray_app.py --host 0.0.0.0` "
                       "(accepts the LAN-exposed posture in Stats popup), or "
                       "enable mirrored mode in %USERPROFILE%\\.wslconfig.")
    shell_name, rc_path = _wsl_detect_shell(distro)
    wsl_userprofile = _wsl_resolve_userprofile(distro)
    block = _wsl_rc_block(port, shell=shell_name,
                          wsl_userprofile=wsl_userprofile)

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
        rc="{rc_path}"
        mkdir -p "$(dirname "$rc")"
        touch "$rc"
        # Safety pre-check: if the start marker is present but the end marker
        # is missing (user truncated or hand-edited), abort rather than
        # awk-delete-from-marker-to-EOF — which would silently drop unrelated
        # rc content below it. Re-running after the user repairs is safe.
        if grep -qF '{WSL_RC_BLOCK_MARKER}' "$rc" && \
                ! grep -qF '{WSL_RC_BLOCK_END}' "$rc"; then
            echo "copilot-gateway: start marker present but end marker missing in $rc" >&2
            echo "copilot-gateway: refusing to rewrite — restore both markers or delete the block" >&2
            exit 2
        fi
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
    # `claude` regardless of shell (covers VS Code WSL extension launches that
    # don't load the rc-file). The dynamic host-IP resolution lives in the rc
    # block; settings.json needs a static URL, so we pick the best stable one
    # for this distro at enable time:
    #
    #   - mirrored mode → 127.0.0.1 (stable)
    #   - host.docker.internal resolves → use it (stable per Windows instance)
    #   - else → omit BASE_URL with a _comment explaining why
    #
    # The default-route fallback used in the rc-block isn't appropriate here
    # because it produces a per-restart IP that goes stale.
    static_url = _resolve_static_wsl_base_url(distro, port)
    settings_script = (
        f"f=$HOME/.claude/settings.json; mkdir -p \"$(dirname \"$f\")\"; "
        f"[ -f \"$f\" ] || echo '{{}}' > \"$f\"; "
        f"BASE_URL='{static_url or ''}' python3 - \"$f\" <<'PY'\n"
        "import json, os, shutil, sys\n"
        "p = sys.argv[1]\n"
        "base = os.environ.get('BASE_URL', '')\n"
        "try:\n"
        "    raw = open(p).read()\n"
        "    parsed = json.loads(raw)\n"
        "    if isinstance(parsed, dict):\n"
        "        d = parsed\n"
        "    else:\n"
        "        shutil.copyfile(p, p + '.cg-backup'); d = {}\n"
        "except Exception:\n"
        "    try: shutil.copyfile(p, p + '.cg-backup')\n"
        "    except Exception: pass\n"
        "    d = {}\n"
        "env = d.get('env') if isinstance(d.get('env'), dict) else {}\n"
        # Only write the dummy token + origin header WHEN we also write a
        # BASE_URL. Otherwise VS Code WSL launches inherit a dummy token,
        # try to hit the real Anthropic API, and 401 — silently breaking
        # the user's pre-toggle auth. The rc-file wrapper still covers
        # interactive shells regardless.
        "if base:\n"
        "    env['ANTHROPIC_BASE_URL'] = base\n"
        "    env['ANTHROPIC_AUTH_TOKEN'] = 'dummy'\n"
        "    env['ANTHROPIC_CUSTOM_HEADERS'] = 'X-Gateway-Origin: wsl'\n"
        # Codex CLI on WSL reads OPENAI_*, not ANTHROPIC_* — mirror for it
        # too so VS Code WSL launches of codex also route through the gateway.
        "    env['OPENAI_BASE_URL'] = base.rstrip('/') + '/v1'\n"
        "    env['OPENAI_API_KEY'] = 'dummy'\n"
        "else:\n"
        "    env.pop('ANTHROPIC_BASE_URL', None)\n"
        "    env.pop('ANTHROPIC_AUTH_TOKEN', None)\n"
        "    env.pop('ANTHROPIC_CUSTOM_HEADERS', None)\n"
        "    env.pop('OPENAI_BASE_URL', None)\n"
        "    env.pop('OPENAI_API_KEY', None)\n"
        "    d['_comment_base_url'] = ('ANTHROPIC_BASE_URL omitted: '\n"
        "        'mirrored mode is off and host.docker.internal does not '\n"
        "        'resolve. Set it manually or enable mirrored mode in '\n"
        "        '.wslconfig. The shell wrapper in your rc-file still '\n"
        "        'resolves it dynamically for interactive shells.')\n"
        "d['env'] = env\n"
        "json.dump(d, open(p, 'w'), indent=2)\n"
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
        # `_stop_event` (not `_stop`) — threading.Thread has a private `_stop()`
        # method called during interpreter shutdown. Shadowing it with an
        # Event raises `TypeError: 'Event' object is not callable` on exit.
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            snap = self._fetch_once()
            try:
                self.on_update(snap)
            except Exception:  # noqa: BLE001 — never let UI bugs kill the poller
                pass
            self._stop_event.wait(STATS_POLL_INTERVAL_S)

    def _fetch_once(self) -> dict | None:
        try:
            with urllib.request.urlopen(self.gateway.stats_url,
                                        timeout=HTTP_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode())
        except Exception:  # noqa: BLE001
            # Includes URLError/HTTPError/socket.timeout/ConnectionError/OSError
            # plus the long tail of http.client.HTTPException, ssl.SSLError,
            # UnicodeDecodeError, and JSONDecodeError. Polling should NEVER
            # bring down the tray; a None return shows as "[gateway offline]".
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


def _raise_to_front(win):
    """Tkinter Toplevel windows on Windows can spawn behind other apps. lift +
    a brief focus_force pulls them forward without keeping the always-on-top
    state set (we drop -topmost after a tick so the window behaves normally
    once visible)."""
    try:
        win.lift()
        win.attributes("-topmost", True)
        win.focus_force()
        win.after(200, lambda: win.attributes("-topmost", False))
    except Exception:  # noqa: BLE001
        pass


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
        # Disable platform-only toggles when their host tooling is absent.
        win_avail = (sys.platform == "win32") and (shutil.which("setx") is not None)
        wsl_avail = shutil.which("wsl.exe") is not None
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
                "Enable for Windows" if win_avail else
                    "Enable for Windows (setx unavailable)",
                lambda *_: self._marshal(self._toggle_windows),
                checked=lambda _: self.win_enabled,
                enabled=win_avail,
            ),
            pystray.MenuItem("  [Test] Windows",
                             lambda *_: self._marshal(self._test_windows),
                             enabled=win_avail),
            pystray.MenuItem(
                "Enable for WSL" if wsl_avail else
                    "Enable for WSL (wsl.exe unavailable)",
                self._wsl_submenu(),
                enabled=wsl_avail,
            ),
            pystray.Menu.SEPARATOR,
            # "Stop & quit" when we own the gateway subprocess; just "Quit"
            # when we attached to an externally-started gateway (no subprocess
            # to stop).
            pystray.MenuItem(
                "Stop & quit" if not self.gateway.attached else "Quit",
                lambda *_: self._marshal(self.stop),
            ),
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
        """Called from the poller's background thread; marshal the actual
        UI mutation to the tkinter main thread. `icon.title=` is thread-safe
        on pystray but tk_root state mutations are not, and a future tweak
        that touches tk inline here would race silently."""
        self.root.after(0, lambda: self._on_stats_main(snap))

    def _on_stats_main(self, snap: dict | None):
        self.latest_snap = snap
        title = format_title(snap)
        try:
            self.icon.title = title
        except Exception:  # noqa: BLE001
            pass

    # — Popups —

    def _show_stats(self):
        snap = self.latest_snap
        if self.bind_posture == "attached":
            header = (f"Bind: attached to gateway at {self.gateway.host}:"
                      f"{self.gateway.port} (its actual --host is unknown — "
                      f"check the gateway's own startup log)")
        else:
            header = (f"Bind: {self.gateway.host}:{self.gateway.port}  "
                      f"[{self.bind_posture.upper()}]")
        lines = [header, ""]
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
        text = tk.Text(win, font=MONO_FONT, wrap="none")
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
            except Exception as e:  # noqa: BLE001
                # Catch the long tail (http.client.HTTPException, ssl.SSLError,
                # UnicodeDecodeError on a corrupt log) so the popup always
                # renders something rather than silently hanging on "loading…".
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
        # On Windows we target cmd.exe / PowerShell syntax (`set X=Y` or
        # `$env:X="Y"`) rather than POSIX `VAR=val cmd` which cmd.exe parses
        # as a bare command name. We emit PowerShell since most modern Windows
        # users have it open by default.
        if sys.platform == "win32":
            cmd = (f"$env:ANTHROPIC_BASE_URL='{self.base_url}'; "
                   f"$env:ANTHROPIC_AUTH_TOKEN='dummy'; claude")
        else:
            cmd = (f"ANTHROPIC_BASE_URL={self.base_url} "
                   f"ANTHROPIC_AUTH_TOKEN=dummy claude")
        ok = self._clip(cmd)
        msg = (f"Copied to clipboard:\n\n{cmd}" if ok else
               f"Clipboard busy. Copy manually:\n\n{cmd}")
        self._toast("Copied" if ok else "Clipboard busy", msg, ok=ok)

    def _copy_codex(self):
        if sys.platform == "win32":
            cmd = (f"$env:OPENAI_BASE_URL='{self.base_url}/v1'; "
                   f"$env:OPENAI_API_KEY='dummy'; codex")
        else:
            cmd = (f"OPENAI_BASE_URL={self.base_url}/v1 "
                   f"OPENAI_API_KEY=dummy codex")
        ok = self._clip(cmd)
        msg = (f"Copied to clipboard:\n\n{cmd}" if ok else
               f"Clipboard busy. Copy manually:\n\n{cmd}")
        self._toast("Copied" if ok else "Clipboard busy", msg, ok=ok)

    def _clip(self, text: str) -> bool:
        """Returns True on success, False on TclError (clipboard may be
        temporarily locked by another app). Uses update_idletasks() rather
        than update() — the latter processes ALL pending events including
        other tk callbacks, which can recurse into the same code path."""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            return True
        except self.tk.TclError:
            return False

    # — Toggles —

    def _run_off_main(self, fn, title: str, on_done=None):
        """Run a potentially-blocking action (setx, wsl.exe probes) on a
        worker thread, then marshal the (ok, msg) result back to a toast on
        the main thread. Optional on_done(ok) runs on the main thread after
        the toast (e.g. to flip enabled-state + update_menu)."""
        def _worker():
            try:
                ok, msg = fn()
            except Exception as e:  # noqa: BLE001
                ok, msg = False, f"unhandled error: {e}"
            def _done():
                self._toast(title, msg, ok=ok)
                if on_done is not None:
                    on_done(ok)
            self.root.after(0, _done)
        threading.Thread(target=_worker, daemon=True, name=f"toggle:{title}").start()

    def _toggle_windows(self):
        if self.win_enabled:
            self._toast("Already enabled",
                        "To disable, manually remove ANTHROPIC_* from your "
                        "user env (Environment Variables… in System Properties) "
                        "and from %USERPROFILE%\\.claude\\settings.json.",
                        ok=True)
            return
        def _on_done(ok):
            if ok:
                self.win_enabled = True
            # No need to call self.icon.update_menu() — `checked=` is a
            # dynamic lambda that re-evaluates on every menu open.
        self._run_off_main(lambda: enable_for_windows(self.base_url),
                           "Enable for Windows", on_done=_on_done)

    def _test_windows(self):
        self._run_off_main(test_windows_env, "Test Windows env")

    def _toggle_wsl(self, distro: str):
        if distro in self.wsl_enabled_distros:
            self._toast(f"Already enabled in {distro}",
                        "To disable, edit the rc-file (~/.bashrc / ~/.zshrc / "
                        "~/.config/fish/config.fish) and delete the block "
                        "between the copilot-gateway markers.", ok=True)
            return
        def _on_done(ok):
            if ok:
                self.wsl_enabled_distros.add(distro)
            # See _toggle_windows — checkmark lambdas are dynamic.
        self._run_off_main(
            lambda: enable_for_wsl(distro, self.gateway.port,
                                   gateway_host=self.gateway.host),
            f"Enable for WSL ({distro})", on_done=_on_done)

    def _test_wsl(self, distro: str):
        self._run_off_main(lambda: test_wsl_env(distro),
                           f"Test WSL ({distro})")

    # — Generic UI primitives —

    def _popup(self, title: str, body: str):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title(title)
        text = tk.Text(win, font=MONO_FONT, width=70, height=20)
        text.insert("1.0", body)
        text.config(state="disabled")
        text.pack(fill="both", expand=True, padx=8, pady=8)
        _raise_to_front(win)

    def _toast(self, title: str, msg: str, ok: bool):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title(title)
        bg = "#dcedc8" if ok else "#ffcdd2"
        win.configure(bg=bg)
        lbl = tk.Label(win, text=msg, bg=bg, font=MONO_FONT,
                       wraplength=520, justify="left", padx=12, pady=12)
        lbl.pack()
        btn = tk.Button(win, text="OK", command=win.destroy)
        btn.pack(pady=(0, 8))
        _raise_to_front(win)

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
                        "(loopback — not reachable from WSL or LAN). Pass "
                        "0.0.0.0 to make the gateway reachable from WSL "
                        "distros and any LAN host; the Stats popup surfaces "
                        "the resulting LAN-EXPOSED posture. Runtime re-bind "
                        "on Enable-for-WSL toggle is deferred (see "
                        "docs/design/windows-app/plan.md §Out of Scope).")
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
    # `args.host or DEFAULT` would mishandle `--host ''` (empty string is
    # falsy in Python) by falling through to default — but empty-string is
    # what some users pass to mean "all interfaces" (same as 0.0.0.0). Treat
    # None (flag absent) as default; treat '' explicitly as 0.0.0.0.
    if args.host is None:
        initial_host = GATEWAY_DEFAULT_HOST
    elif args.host == "":
        initial_host = "0.0.0.0"
    else:
        initial_host = args.host
    bind_posture = _classify_bind_posture(initial_host)

    gateway = GatewayProcess(initial_host, args.port)
    status = gateway.start()
    if status == "missing-gateway":
        print(f"[tray] gateway.py not found at {GATEWAY_PY}; aborting.",
              file=sys.stderr)
        return 2
    if status == "port-busy":
        print(f"[tray] port {args.port} is already in use on {initial_host}. "
              f"Stop the other gateway / process first, or pass --port to "
              f"choose another.", file=sys.stderr)
        return 4
    # When we attached to an externally-started gateway, we DON'T know what
    # --host it was started with — the user might have it on 0.0.0.0 but the
    # tray was launched with default 127.0.0.1. Mark posture as "attached"
    # so the Stats popup tells the truth instead of claiming "loopback".
    if status == "attached":
        bind_posture = "attached"

    # base_url is what we hand to clients (Copy claude / Copy codex). When
    # binding to a non-loopback IP, clients on this Windows box still reach
    # us via that IP — but we present 127.0.0.1 for loopback to keep the
    # copy-paste cleanest.
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
