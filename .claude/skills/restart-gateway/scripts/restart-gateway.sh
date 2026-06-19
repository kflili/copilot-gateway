#!/usr/bin/env bash
# restart-gateway.sh — safely kill + relaunch the local copilot-gateway python
# child so an updated gateway.py is loaded from disk, without leaving :8787 down.
#
# Safe-restart knowledge encoded here (the reason this skill exists):
#   * Find the kill target BY COMMAND PATTERN (pgrep/pkill -f), never
#     `lsof -ti:PORT | head -1` — :8787 is held by BOTH the menu-bar app wrapper
#     and the python child; head -1 can return the APP pid, killing the
#     supervisor while orphaning the old python that keeps serving STALE code
#     (and /health still answers ok, so it looks like a clean restart when
#     nothing actually reloaded).
#   * Proof of a real reload is a FRESH pid / start time, NOT `/health` ok alone
#     — an orphaned old process answers ok too. (A genuine restart also zeroes
#     the /health requests/tokens counters, but the helper gates on pid/start
#     time + health, not the counters.)
#   * Self-restart paradox: when run from a session that itself routes through
#     :8787, the kill drops the caller's own connection mid-flight; doing
#     kill+relaunch+health-wait as ONE blocking step means the caller's next
#     inference only fires after :8787 is healthy again.
#
# All targets are env-overridable so this is mock-testable on a scratch port
# without ever touching the live :8787 (see SKILL.md / SPEC Validation).
set -uo pipefail

log()  { printf '%s\n' "$*" >&2; }

# Validate + normalize the numeric env inputs (system boundary) BEFORE they are
# interpolated into the pattern / URL / eval'd commands below. Order matters:
# digit-only check first (rejects injection like GW_PORT='8787; rm -rf ~'), then
# 10# base-10 normalize (a leading-zero override like 08787 would otherwise be
# read as octal by the -ge/-le arithmetic, where 8/9 are invalid octal digits),
# then range check. Fail closed.
GW_PORT="${GW_PORT:-8787}"
case "$GW_PORT" in ''|*[!0-9]*) log "ERROR: GW_PORT must be an integer 1..65535 (got '${GW_PORT}')"; exit 64;; esac
GW_PORT=$((10#$GW_PORT))
{ [ "$GW_PORT" -ge 1 ] && [ "$GW_PORT" -le 65535 ]; } || { log "ERROR: GW_PORT must be an integer 1..65535 (got '${GW_PORT}')"; exit 64; }

GW_WAIT_SECS="${GW_WAIT_SECS:-30}"
case "$GW_WAIT_SECS" in ''|*[!0-9]*) log "ERROR: GW_WAIT_SECS must be an integer >= 1 (got '${GW_WAIT_SECS}')"; exit 64;; esac
GW_WAIT_SECS=$((10#$GW_WAIT_SECS))
[ "$GW_WAIT_SECS" -ge 1 ] || { log "ERROR: GW_WAIT_SECS must be an integer >= 1 (got '${GW_WAIT_SECS}')"; exit 64; }

# pgrep/pkill -f treat this as an extended regex matched against the full argv.
# `gateway\.py.*--port N` tolerates intervening flags (e.g. tray_app.py spawns
# `gateway.py --host <host> --port <port>`, while CopilotGateway.app spawns
# `gateway.py --port <port>` with no --host) — only the universally-portable `.`
# and `*` operators, never `lsof -ti:PORT | head -1` (which can hit the wrapper).
GW_PROC_PATTERN="${GW_PROC_PATTERN:-gateway\.py.*--port ${GW_PORT}}"
GW_HEALTH_URL="${GW_HEALTH_URL:-http://127.0.0.1:${GW_PORT}/health}"

# Repo root from this script's own location: the helper lives at
# <repo>/.claude/skills/restart-gateway/scripts/restart-gateway.sh, so the repo
# root is four levels up. Keeps the helper portable to any clone location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"
# Shell-escape the path before embedding it in the default commands: those run
# through `eval` (needed so an env-supplied GW_*_CMD can carry shell syntax like
# `&&`, `>>`, `& disown`), and bare ${REPO_ROOT} under eval would re-interpret a
# path containing spaces or metacharacters ($(), backticks) — printf %q makes it
# a single literal token. No-op for ordinary paths.
SAFE_REPO_ROOT="$(printf '%q' "$REPO_ROOT")"

GW_RELAUNCH_CMD="${GW_RELAUNCH_CMD:-open ${SAFE_REPO_ROOT}/CopilotGateway.app}"
GW_FALLBACK_CMD="${GW_FALLBACK_CMD:-cd ${SAFE_REPO_ROOT} && mkdir -p logs && nohup python3 gateway.py --port ${GW_PORT} >> logs/gateway-console.log 2>&1 & disown}"

# Target pids = processes matching the pattern, EXCLUDING this script ($$) and
# its parent shell ($PPID), so a broad GW_PROC_PATTERN override can never make the
# helper signal its own (or the caller's) shell. `--` stops pgrep/pkill parsing a
# pattern that begins with `-` as an option.
target_pids() { pgrep -f -- "$GW_PROC_PATTERN" 2>/dev/null | grep -vFx -e "$$" -e "$PPID" || true; }
# Newest surviving match (highest pid; after the kill the old one is gone, so this
# resolves to the freshly-relaunched process), or empty.
proc_pid()    { target_pids | sort -n | tail -n1; }
proc_start()  { local p="${1:-}"; [ -n "$p" ] && ps -o lstart= -p "$p" 2>/dev/null | tr -s ' ' || true; }

# A real reload = a matching process whose pid OR start time differs from OLD.
# The start-time arm covers the rare pid-reuse case (same pid, genuinely newer
# process) — but only when both start times are readable, so a failed `ps` read
# (empty start) can't masquerade as a fresh process.
is_fresh()    { local pid="$1" start="$2"; [ -n "$pid" ] && { [ "$pid" != "$OLD" ] || { [ -n "$start" ] && [ -n "$OLD_START" ] && [ "$start" != "$OLD_START" ]; }; }; }

# Healthy = /health reports status:ok AND carries the gateway's version field
# (the repo health contract is status+version, per tray_app.py) — guards against
# an unrelated service on the port answering a bare status:ok.
health_ok()   { local b; b="$(curl -fsS --max-time 3 "$GW_HEALTH_URL" 2>/dev/null)" || return 1; printf '%s' "$b" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' && printf '%s' "$b" | grep -q '"version"[[:space:]]*:'; }
health_body() { curl -fsS --max-time 3 "$GW_HEALTH_URL" 2>/dev/null || true; }

OLD="$(proc_pid)"
OLD_START="$(proc_start "$OLD")"
log "── restart-gateway ──"
log "port=${GW_PORT} pattern='${GW_PROC_PATTERN}'"
log "old pid=${OLD:-<none>} start='${OLD_START:-<none>}'"

# 1. Graceful kill by pattern (TERM); escalate to KILL if still alive after ~5s.
#    Signal only target_pids (self/parent excluded), never a blind `pkill -f`.
if [ -n "$OLD" ]; then
  pids="$(target_pids)"
  [ -n "$pids" ] && kill $pids 2>/dev/null || true
  for _ in {1..10}; do
    [ -z "$(target_pids)" ] && break
    sleep 0.5
  done
  pids="$(target_pids)"
  if [ -n "$pids" ]; then
    log "TERM did not clear it; escalating to KILL"
    kill -9 $pids 2>/dev/null || true
    sleep 1
  fi
else
  log "no existing process matched — nothing to kill, will just (re)launch"
fi

# 2. Relaunch a fresh supervised gateway.
log "relaunch: ${GW_RELAUNCH_CMD}"
eval "${GW_RELAUNCH_CMD}" || log "relaunch command returned non-zero (continuing to health-wait)"

# 3. Health-wait up to GW_WAIT_SECS. Success = /health status:ok AND a fresh
#    process (pid or start time changed vs OLD). At ~half the budget, if still
#    fully down, run the one-shot fallback relaunch.
deadline=$((SECONDS + GW_WAIT_SECS))
halfway=$((SECONDS + GW_WAIT_SECS / 2))
fallback_done=0
while [ "$SECONDS" -lt "$deadline" ]; do
  NEW="$(proc_pid)"
  NEW_START="$(proc_start "$NEW")"
  if is_fresh "$NEW" "$NEW_START" && health_ok; then
    break
  fi
  if [ "$fallback_done" -eq 0 ] && [ "$SECONDS" -ge "$halfway" ] && [ -z "$NEW" ] && ! health_ok; then
    log "halfway budget elapsed and still down — running one-shot fallback relaunch"
    eval "${GW_FALLBACK_CMD}" || log "fallback command returned non-zero"
    fallback_done=1
  fi
  sleep 1
done

# 4. Report + exit status. Order: health is the gate — if /health isn't a healthy
#    gateway, that's the hard failure (exit 1). Given health is ok, a NEW pid or
#    start time proves a real reload (exit 0); an UNCHANGED pid AND start time is a
#    suspected orphan still serving stale code (exit 2); health ok but no fresh
#    match confirmable (e.g. an external supervisor) is not a hard down (exit 0,
#    warn). (A genuine restart also zeroes /health requests/tokens, but the helper
#    gates on pid/start time + health, not the counters.)
NEW="$(proc_pid)"
NEW_START="$(proc_start "$NEW")"
BODY="$(health_body)"
log "── result ──"
log "new pid=${NEW:-<none>} start='${NEW_START:-<none>}'"
log "/health: ${BODY:-<no response>}"

if ! health_ok; then
  log "FAILED: :${GW_PORT} did not return a healthy gateway /health (status+version) after ${GW_WAIT_SECS}s — the port may be down, or another service may be answering on it. Recover manually:"
  log "  ${GW_FALLBACK_CMD}"
  exit 1
fi

if is_fresh "$NEW" "$NEW_START"; then
  log "OK: :${GW_PORT} healthy with a FRESH process (pid ${OLD:-<none>}/start '${OLD_START:-<none>}' → ${NEW}/start '${NEW_START:-<none>}'). Reloaded from disk."
  exit 0
fi

if [ -n "$NEW" ] && [ "$NEW" = "$OLD" ] && [ "$NEW_START" = "$OLD_START" ]; then
  log "WARNING: :${GW_PORT} answers ok but the process is UNCHANGED (pid ${NEW}, same start time) — the OLD process is likely orphaned and still serving STALE code (did the kill hit the app wrapper instead of the python child?). Recover manually:"
  log "  pkill -9 -f -- '${GW_PROC_PATTERN}' ; ${GW_RELAUNCH_CMD}"
  exit 2
fi

# Healthy, but no fresh matching process could be confirmed (e.g. an external
# supervisor relaunched it under a command line the pattern doesn't match, or the
# process query came up empty). The port is serving, so this is not a hard down.
log "WARNING: :${GW_PORT} is healthy but a fresh matching process could not be confirmed (pid=${NEW:-<none>}, pattern='${GW_PROC_PATTERN}') — it may be supervised externally. Verify the start time manually if you need reload proof."
exit 0
