# Plan: Validate extra models before showing in demo UI

## Context

The GitHub Copilot API `/models` endpoint lists models like `minimax-m2.5` and `goldeneye` as callable, but they actually return 400 "model_not_supported" when used. The Copilot CLI works around this with a hardcoded allowlist. Our demo UI shows all API-listed models, so users see models that don't work.

**Fix already applied**: Added `"/v1/responses": "/responses"` to PATH_MAP in `gateway.py:637` (goldeneye path issue).

**Goal**: On first `/api/models` request per mode, smoke-test "extra" models and hide failures from the picker.

## Objectives

- Filter out broken models from the demo UI model dropdown
- Only smoke-test models outside the CLI's known-good set (plus known-problematic ones like `goldeneye`)
- Keep validation fast (parallel, lazy, cached) and non-blocking for known-good models
- Surface failed models transparently in the API response for debugging

## Approach

All changes in `demo.py` only.

### 1. Constants (`demo.py`, after line 108 — after `_models_lock`)

```python
import concurrent.futures  # add to imports at top

# Models the Copilot CLI shows — known to generally work
CLI_KNOWN_MODELS = {
    "claude-sonnet-4.6", "claude-sonnet-4.5", "claude-haiku-4.5",
    "claude-opus-4.7", "claude-opus-4.6", "claude-opus-4.6-1m",
    "claude-opus-4.5", "claude-sonnet-4",
    "goldeneye",
    "gpt-5.4", "gpt-5.3-codex", "gpt-5.2-codex", "gpt-5.2",
    "gpt-5.4-mini", "gpt-5-mini", "gpt-4.1",
}

# CLI-listed but known to fail for most users (e.g. "(Internal Only)")
ALWAYS_VALIDATE = {"goldeneye"}

_validation_cache: dict[str, set] = {}  # mode -> set of failed model IDs
_validation_lock = threading.Lock()
```

### 2. `_smoke_test_model(model_id, mode)` function (after `pick_endpoint()`)

Sends a minimal request **directly to upstream** using `get_mode_token_and_base(mode)`. Does NOT go through the gateway (which runs in a single global mode and would give wrong results for cross-mode validation).

```python
def _smoke_test_model(model_id: str, mode: str) -> bool | None:
    """Smoke-test a model. Returns True=ok, False=rejected, None=inconclusive."""
    token, api_base = get_mode_token_and_base(mode)
    if not token:
        return None
    path, fmt = pick_endpoint(model_id, mode)

    # Build format-specific minimal body
    if fmt == "anthropic":
        body = {"model": model_id, "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]}
        headers = {"anthropic-version": "2023-06-01"}
    elif fmt == "responses":
        body = {"model": model_id, "input": "hi"}
        headers = {}
    else:  # openai
        body = {"model": model_id, "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]}
        headers = {}

    # Copilot requires integration ID
    headers["Copilot-Integration-Id"] = "vscode-chat" if mode == "vscode" else "copilot-developer-cli"
    headers["Authorization"] = f"Bearer {token}"
    headers["Content-Type"] = "application/json"

    # Copilot API paths: strip /v1/ prefix (same mapping as gateway PATH_MAP)
    upstream_path = path
    if path == "/v1/chat/completions":
        upstream_path = "/chat/completions"
    elif path == "/v1/responses":
        upstream_path = "/responses"

    url = api_base + upstream_path
    data = json.dumps(body).encode()

    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        resp = urllib.request.urlopen(req, timeout=10)
        resp.read()  # consume response
        return True
    except urllib.error.HTTPError as e:
        if e.code in (400, 403):
            return False  # definitive rejection
        return None  # 429, 5xx = inconclusive
    except Exception:
        return None  # timeout, network error = inconclusive
```

### 3. `_validate_extra_models(models, mode)` function

Per-mode cached validation with in-flight deduplication. Uses `threading.Event` so only one request runs smoke tests per mode; concurrent callers wait for the same result.

```python
_validation_in_flight: dict[str, threading.Event] = {}  # mode -> Event (add with other cache vars)

def _validate_extra_models(models: list, mode: str) -> set[str]:
    """Validate extra models not in CLI known set. Returns failed model IDs."""
    with _validation_lock:
        cached = _validation_cache.get(mode)
        if cached is not None:
            return cached
        event = _validation_in_flight.get(mode)
        if event is None:
            # We're the first — claim this mode
            event = threading.Event()
            _validation_in_flight[mode] = event
            is_owner = True
        else:
            is_owner = False

    if not is_owner:
        # Another thread is validating — wait for it
        event.wait(timeout=60)
        with _validation_lock:
            return _validation_cache.get(mode, set())

    # --- We own validation for this mode ---
    failed = set()
    try:
        to_test = []
        for m in models:
            mid = m.get("id", "")
            endpoints = m.get("supported_endpoints", [])
            if not endpoints:
                continue
            if mid not in CLI_KNOWN_MODELS or mid in ALWAYS_VALIDATE:
                to_test.append(mid)

        if not to_test:
            logger.info(f"no extra models to validate for {mode}")
        else:
            logger.info(f"validating {len(to_test)} extra models for {mode}: {to_test}")
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
                futures = {pool.submit(_smoke_test_model, mid, mode): mid for mid in to_test}
                for fut in concurrent.futures.as_completed(futures):
                    mid = futures[fut]
                    result = fut.result()
                    if result is False:
                        failed.add(mid)
                        logger.info(f"  model {mid} [{mode}]: FAILED (removed from picker)")
                    elif result is None:
                        logger.info(f"  model {mid} [{mode}]: inconclusive (keeping)")
                    else:
                        logger.info(f"  model {mid} [{mode}]: OK")
    except Exception as e:
        logger.error(f"model validation error for {mode}: {e}")
    finally:
        with _validation_lock:
            _validation_cache[mode] = failed
            _validation_in_flight.pop(mode, None)
        event.set()  # wake any waiters
    return failed
```

### 4. Filter in `_handle_models()` (`demo.py:412-458`)

After getting models, validate and filter:

```python
# In _handle_models(), after `models = get_models(mode)`:
failed = _validate_extra_models(models, mode)

# In the formatted loop, skip failed models:
for m in models:
    if m.get("id", "") in failed:
        continue
    # ... existing formatting logic ...

# Add to response:
"failed_validation": [{"id": mid, "reason": "smoke test returned 400/403"} for mid in failed],
```

## Implementation Steps

1. Add `import concurrent.futures` to imports (line ~17)
2. Add constants `CLI_KNOWN_MODELS`, `ALWAYS_VALIDATE`, `_validation_cache`, `_validation_lock`, `_validation_in_flight` after line 108
3. Add `_smoke_test_model()` function after `pick_endpoint()` (~line 225)
4. Add `_validate_extra_models()` function after `_smoke_test_model()`
5. Modify `_handle_models()` to call validation and filter results
6. Fix `_handle_chat()` non-streaming Responses branch (~line 581-592): normalize `"/v1/responses"` to `"/responses"` before building URL, and always set `Copilot-Integration-Id` for both modes (not just vscode). This ensures validated models also work in real chat — without this fix, a model can pass validation but fail in actual use.

## Success Criteria

- `minimax-m2.5` does not appear in model picker
- `goldeneye` does not appear for non-Microsoft users
- Known-good models (claude-*, gpt-*) appear without delay (not smoke-tested)
- `demo.log` shows validation results
- `/api/models` response includes `failed_validation` field

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Smoke tests slow down first `/api/models` response | Tests run in parallel (5 workers), only ~2-3 models to test, 10s timeout |
| Model availability changes after cache | Cache is per-session; restart demo to re-validate |
| `CLI_KNOWN_MODELS` goes stale as GitHub updates | `ALWAYS_VALIDATE` catches known-problematic ones; unknown new models get tested automatically |
| Smoke test consumes premium request quota | `max_tokens: 1` minimizes cost; only 2-3 models tested |

## Related Context

- `gateway.py` PATH_MAP fix for `/v1/responses` → `/responses` was already applied. This fixes the gateway proxy path but is NOT a dependency of the demo.py validation flow, which calls upstream directly and does its own path translation.

## Open Questions / Deferred Decisions

- None — the pre-existing `Copilot-Integration-Id` bug at `demo.py:591-592` is now included as implementation step 6.
