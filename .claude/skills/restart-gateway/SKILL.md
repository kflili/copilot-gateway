---
name: restart-gateway
description: "Restart the local copilot-gateway to load updated gateway.py from disk, safely, without leaving :8787 down."
---

# restart-gateway

**Mission**: Reload `gateway.py` from disk = restart the python child that binds `:8787`, without ever leaving the port down.

**When**: After merging or editing `gateway.py`; when the running gateway is serving stale code; the CLI equivalent of quit + reopen the menu-bar icon.

**How**: Run the bundled helper — ONE blocking step:

```
.claude/skills/restart-gateway/scripts/restart-gateway.sh
```

It does kill → relaunch → health-wait in a single command. A gateway-routed caller (cgcc / cmux Claude) routes its own inference through `:8787`, so one blocking run means the caller's next request only fires once `:8787` is healthy again (the self-restart paradox below).

**Process model**: `python3 <repo>/gateway.py --port 8787` (binds `127.0.0.1:8787`), launched and supervised as a child of the menu-bar app `<repo>/CopilotGateway.app`. Some launchers (e.g. `tray_app.py`) spawn it as `gateway.py --host <host> --port 8787`, so the match pattern tolerates intervening flags.

**Pitfalls** (the load-bearing knowledge):
- Kill the python **by command name** (the helper enumerates matching pids with `pgrep -f -- 'gateway\.py.*--port 8787'` — args-tolerant so it matches whether or not a `--host` flag sits between `gateway.py` and `--port`, and excluding its own / the caller's shell pid — then `kill`s those pids), NEVER `lsof -ti:8787 | head -1`. `:8787` is held by BOTH the app wrapper and the python child; `head -1` can return the **app** pid — killing the supervisor while orphaning the old python, which keeps serving STALE code while `/health` still lies `ok`.
- Proof of a real reload is a **FRESH pid / start time**, NOT `/health ok` alone — an orphaned old process answers `ok` too. (A genuine restart also zeroes the `/health` `requests`/`tokens` counters, visible as corroboration, but the helper gates on pid/start time + health, not the counters.)
- **Self-restart paradox**: when the caller routes through `:8787`, do kill+relaunch+health-wait as ONE blocking command so the caller recovers before it returns.
- **Never leave `:8787` down**: the helper fires a one-shot fallback relaunch at half the wait budget and exits non-zero with a loud manual-recovery hint if it can't bring the port back.

**Verify**: `/health` returns `status: ok` **and** a `version` field (the gateway's health contract is status+version — checking both avoids treating an unrelated service on the port as healthy) AND a new pid / start time. Exit codes: **0** = healthy and a fresh process confirmed (the normal success), OR healthy but no matching process could be confirmed (e.g. it's supervised externally — emits a warning, since the port is serving it isn't a hard failure); **2** = port answers ok but the process is unchanged (same pid AND start time — suspected orphan serving stale code); **1** = no healthy gateway `/health` after the wait (port down, or another service answering on it).

**Mock-test** (never touch the live `:8787`): every target is env-overridable — point `GW_PORT`, `GW_PROC_PATTERN`, `GW_HEALTH_URL`, `GW_RELAUNCH_CMD`, `GW_FALLBACK_CMD`, `GW_WAIT_SECS` at a throwaway stub process on a scratch port. The stub's `/health` must return both `"status": "ok"` and a `"version"` field, or the helper won't accept it as healthy.
