# Bridge Security Hardening Bugfix Design

## Overview

This bugfix closes 7 confirmed security/robustness defects (2 CRITICAL, 5 HIGH) in the existing
transport and execution layer: MCP Server → Ollama planner → `DesignPlan` → Plan Executor →
Bridge Client → WebSocket Bridge → Figma Plugin → Figma Canvas.

The fix is delivered as 7 incremental, independently-verifiable subsystem changes, applied and
tested in this order (each preserving the ones before it):

1. **Bridge auth (C-1)** — shared-secret handshake added to the existing role-identification message.
2. **Parent_id allowlist (C-2)** — `plan_executor.py` becomes the authoritative gatekeeper for which node IDs may be used as `parent_id`.
3. **Config validation (H-1)** — `config.validate_config()` is actually invoked, at startup and defensively before REST calls.
4. **Malformed controller input (H-2)** — `bridge.py` validates and rejects bad JSON/shape instead of crashing or relaying it.
5. **Pending-request cleanup (H-3)** — `_pending_requests` gets TTL eviction + disconnect cleanup.
6. **Plan size/timeout caps (H-6)** — Pydantic-level size caps on `DesignPlan`/`SimplePlan`, and an overall `execute_plan` timeout.
7. **Fail-closed bind (H-7)** — `bridge.py` refuses to bind non-loopback hosts without an explicitly configured token.

No existing MCP tool, message shape, or module boundary is removed or renamed. Every change is
additive validation/guarding logic layered onto the current architecture. Ollama remains the
default planner backend and Anthropic remains optional/unused unless a key is configured — neither
is touched by this bugfix.

## Glossary

- **Bug_Condition (C)**: The condition that triggers a given defect (7 distinct conditions here, one per defect: C-1, C-2, H-1, H-2, H-3, H-6, H-7).
- **Property (P)**: The desired behavior once the bug condition is fixed, for that defect.
- **Preservation**: The subset of current behavior (bugfix.md clauses 3.1–3.9) that must be bit-for-bit unchanged after all 7 fixes land.
- **Bridge**: `bridge.py`, the local `websockets`-based relay process listening on `ws://localhost:8765` by default.
- **Controller**: A client that identifies with `{"role": "controller", ...}` — today, only `bridge_client.py` (used by the MCP server process).
- **Plugin**: A client that identifies with `{"role": "plugin", ...}` — today, only `figma-plugin/ui.html` running inside the Figma plugin iframe.
- **Bridge token**: A shared secret both controller and plugin must present in their role-identification message before the bridge will register them.
- **Created-node allowlist**: A per-`execute_plan`-call set of node IDs that the plan executor itself created via `bridge_client.send_figma_command`, used to validate any `parent_id` before it is forwarded to the bridge.
- **Loopback host**: `localhost`, `127.0.0.1`, or `::1` — the only hosts `start_bridge` may bind without an explicitly configured token.

## Bug Details

### Bug Condition — C-1 (Unauthenticated bridge)

Any process can open a WebSocket to the bridge, send `{"role": "controller"}` or
`{"role": "plugin"}`, and be trusted with no further check.

```
FUNCTION isBugCondition_C1(connection)
  INPUT: connection, a new WebSocket connection with a first_message
  OUTPUT: boolean

  RETURN connection.first_message.role IN ["controller", "plugin"]
         AND connection.first_message has no valid shared-secret token
         AND bridge accepts connection into _controllers or _plugins anyway
END FUNCTION
```

**Examples:**
- A browser tab on the same machine opens `ws://localhost:8765`, sends `{"role":"controller"}`, and can now issue `create_rectangle` commands that land on the user's actual open Figma file.
- A malicious local process registers as `{"role":"plugin"}` and receives every result meant for the real plugin (credential/data exposure via `request_id`-addressed messages it can now intercept).

### Bug Condition — C-2 (Unrestricted parent_id)

Once any client is treated as an authenticated controller (post-C-1-fix, this is the token
holder), the bridge relays whatever `action`/`payload` JSON it is given verbatim to plugins.
`plan_executor.py`'s `_build_payload` forwards its `parent_id` argument with no check, and
`code.js`'s `resolveParent` will happily attach to any node ID that resolves in the open file.

```
FUNCTION isBugCondition_C2(command)
  INPUT: command, a create_* action with payload.parent_id set
  OUTPUT: boolean

  RETURN command.payload.parent_id IS NOT NULL
         AND command.payload.parent_id NOT IN created_node_ids_this_execution
         AND command is forwarded to the plugin without rejection
END FUNCTION
```

**Examples:**
- A future or hand-crafted `generate_screen` payload / raw controller command names an arbitrary existing node ID (e.g. another team's locked component, or a node outside the current screen) as `parent_id`; the plugin attaches a newly generated node under it, silently corrupting unrelated file content.
- Today `DesignNode` has no `parent_id` field, so `plan_executor.py` never receives an *externally* supplied one — but nothing stops a raw controller command (post-auth) from including one directly, and nothing in `plan_executor.py` would catch it if the schema ever grows that field. This is a latent/defense-in-depth gap, not only a currently-exploited one.

### Bug Condition — H-1 (Unenforced config validation)

```
FUNCTION isBugCondition_H1(startup_or_call)
  INPUT: startup_or_call, either server startup or a call requiring Figma REST access
  OUTPUT: boolean

  RETURN (FIGMA_ACCESS_TOKEN is None OR FIGMA_FILE_ID is None)
         AND config.validate_config() was never called before the first Figma REST call
END FUNCTION
```

**Example:** `.env` is missing `FIGMA_ACCESS_TOKEN`; the first call to `get_figma_file_overview`
fails deep inside `httpx` with a raw 401/`None` header value instead of the existing clear
`ValueError("FIGMA_ACCESS_TOKEN is not set. Check your .env file.")`.

### Bug Condition — H-2 (Unhandled malformed controller input)

```
FUNCTION isBugCondition_H2(raw_message)
  INPUT: raw_message, a string received from a controller connection
  OUTPUT: boolean

  RETURN (raw_message is not valid JSON)
         OR (parsed.action is not a string)
         OR (parsed.payload is not a dict, when present)
END FUNCTION
```

**Examples:**
- Controller sends `"not json at all"` → `json.loads` raises `JSONDecodeError` inside the `async for` loop → unhandled exception → connection handler crashes, controller connection drops.
- Controller sends `{"action": 42, "payload": "oops"}` → currently relayed to plugins as-is; `code.js`'s dispatcher does `commandHandlers[42]` → `undefined` → handled gracefully there today, but `bridge.py` did no validation and would have crashed on other malformed shapes (e.g. `payload` being a string when a handler expects `payload.x`).

### Bug Condition — H-3 (Pending request leak)

```
FUNCTION isBugCondition_H3(pending_requests, time_elapsed)
  INPUT: pending_requests, the dict of request_id -> controller_ws
  OUTPUT: boolean

  RETURN EXISTS request_id IN pending_requests
         WHERE (no plugin ever responds for that request_id)
         AND (entry is never removed, regardless of time_elapsed or controller disconnect)
END FUNCTION
```

**Example:** No plugin is connected (or it crashes mid-command). `bridge_client.send_figma_command`
times out client-side after 10s and raises `TimeoutError`, but the bridge's own
`_pending_requests[request_id]` entry is never popped — it stays forever, one entry per dropped
command, for the lifetime of the bridge process.

### Bug Condition — H-6 (Unbounded plan size/time)

```
FUNCTION isBugCondition_H6(plan_or_execution)
  INPUT: plan_or_execution, a SimplePlan/DesignPlan or an in-flight execute_plan call
  OUTPUT: boolean

  RETURN (count(plan.elements) OR count(any DesignNode.children) exceeds any enforced schema limit is FALSE today, i.e. no limit exists)
         OR (len(any content string) exceeds any enforced schema limit is FALSE today)
         OR (execute_plan has been running with no overall deadline)
END FUNCTION
```

**Examples:**
- The Ollama model ignores the "cap at 10 elements" prompt instruction (nothing stops it) and returns 200 elements; `build_design_plan` happily lays out 200+ nodes, each triggering a real 10s-capable bridge round trip in `plan_executor.py` with zero overall deadline.
- A hand-crafted `generate_screen` call nests thousands of `children` under one frame, or supplies a multi-megabyte `content` string, with nothing rejecting it before it reaches the plugin.

### Bug Condition — H-7 (No fail-closed bind restriction)

```
FUNCTION isBugCondition_H7(host, configured_token)
  INPUT: host, the bind host passed to start_bridge; configured_token, BRIDGE_AUTH_TOKEN env var
  OUTPUT: boolean

  RETURN host NOT IN ["localhost", "127.0.0.1", "::1"]
         AND configured_token is None
         AND start_bridge proceeds to bind and serve anyway
END FUNCTION
```

**Example:** An operator runs `start_bridge(host="0.0.0.0")` to make the bridge reachable from
another machine on the LAN, without ever setting `BRIDGE_AUTH_TOKEN`. Even after the C-1 fix, the
bridge would fall back to an auto-generated, file-based token intended for trusted-local-user
convenience — which is not an appropriate secret-distribution mechanism across a network — and the
bridge would still start, exposing the command channel.

## Expected Behavior

### Preservation Requirements

**Unchanged behaviors (bugfix.md 3.1–3.9), verified after all 7 fixes land:**
- A controller presenting a valid token and `{"role": "controller"}` is accepted and its commands relayed exactly as today (3.1).
- A plugin presenting a valid token and `{"role": "plugin"}` is accepted and results route back via `request_id` exactly as today (3.2).
- `parent_id` omitted, or referring to a node legitimately created earlier in the same plan/session, continues to attach under that parent exactly as today (3.3).
- With `FIGMA_ACCESS_TOKEN`/`FIGMA_FILE_ID` correctly configured, Figma API calls and file overviews work exactly as today (3.4).
- Well-formed controller commands (valid JSON, `action` a string, `payload` a dict) continue to relay and route results exactly as today (3.5).
- A plugin response arriving before any timeout/eviction still routes to the correct controller exactly as today (3.6).
- Plans within the new size/content limits, completing within the new timeout, still build the full node tree (frames, components, groups, component_sets, text, rectangles, ellipses, lines, image placeholders, icons, styles, variables) exactly as today, across all 4 themes and the Ollama→Template `FallbackPlanner` chain (3.7).
- `start_bridge(host="localhost")` (default) continues to bind without any config beyond the token needed for 2.1 (3.8).
- All 6 MCP tools continue to return the same successful results and canvas effects given valid inputs and an authenticated bridge/plugin pair (3.9).

**Scope:** Every fix below is additive validation. None of them change the wire format of
existing fields, the DesignPlan/DesignNode schema's existing fields, the dispatch-table shape in
`code.js`, or any MCP tool signature.

## Hypothesized Root Cause

1. **No handshake secret anywhere (C-1)**: the project was built single-user/local-only; `handle_connection` trusts the `role` field alone. There is no concept of a shared secret in `bridge.py`, `bridge_client.py`, or `ui.html` today.
2. **No trust boundary inside the relay (C-2)**: the bridge is a dumb relay by design (it forwards raw JSON), and `plan_executor.py`'s recursive walk was written assuming all `parent_id` values it produces are trustworthy because *it* generated them — but nothing enforces that assumption or defends against a future/parallel code path violating it.
3. **`validate_config()` was written but never wired in (H-1)**: `config.py` has the function; nothing calls it. Likely an oversight during initial development — the happy path (`.env` present) never surfaced the gap.
4. **No defensive parsing in the receive loop (H-2)**: `_handle_controller`'s `async for raw_message in websocket:` loop assumes well-formed JSON because the only real controller today is `bridge_client.py`, which always sends well-formed messages — there was no adversarial input to guard against during development.
5. **No cleanup path considered for the pending map (H-3)**: `_pending_requests` was designed for the happy path (plugin always eventually responds); disconnect and timeout paths were never wired to evict entries.
6. **Size/time limits exist only as a prompt instruction (H-6)**: the "cap at 10 elements" rule lives in `_SYSTEM_PROMPT` text, which an LLM can ignore; no code-level enforcement backs it up, and `execute_plan`'s recursive `await` chain has no wrapping deadline.
7. **`start_bridge` was never intended for non-loopback use (H-7)**: the `host` parameter exists for flexibility/testing but nothing stops an operator from passing a real network-facing host without realizing that removes the (until now, nonexistent) auth protection.

## Correctness Properties

Property 1: Bug Condition - Unauthenticated Bridge Access

_For any_ connection whose role-identification message is missing the bridge token or presents an
invalid token, the fixed `handle_connection`/`_handle_plugin`/`_handle_controller` flow SHALL
reject the connection (send a structured error and close it) without adding it to `_controllers`
or `_plugins`.

**Validates: Requirements 2.1**

Property 2: Preservation - Authenticated Role Handshake

_For any_ connection presenting the currently-valid bridge token together with
`{"role": "controller"}` or `{"role": "plugin"}`, the fixed system SHALL accept and route it
exactly as the original system did for any role-identification message.

**Validates: Requirements 3.1, 3.2**

Property 3: Bug Condition - Unrestricted Parent Targeting

_For any_ create_* command whose `parent_id` is not `None` and not a member of the current plan
execution's created-node allowlist, the fixed `plan_executor.py` SHALL reject that node (return an
error result for it) and SHALL NOT forward the command to `bridge_client.send_figma_command`.

**Validates: Requirements 2.2**

Property 4: Preservation - Legitimate Parent Attachment

_For any_ create_* command whose `parent_id` is `None` or is a node ID created earlier in the same
plan execution, the fixed `plan_executor.py` SHALL forward it and attach the node exactly as the
original system did.

**Validates: Requirements 3.3**

Property 5: Bug Condition - Missing Config Validation

_For any_ startup or Figma-REST-dependent call where `FIGMA_ACCESS_TOKEN` or `FIGMA_FILE_ID` is
unset, the fixed system SHALL raise the existing clear `ValueError` from `validate_config()` before
any HTTP call is attempted.

**Validates: Requirements 2.3**

Property 6: Preservation - Valid Config Operation

_For any_ call made when both `FIGMA_ACCESS_TOKEN` and `FIGMA_FILE_ID` are set, the fixed system
SHALL behave exactly as the original system (no new errors, same return values).

**Validates: Requirements 3.4**

Property 7: Bug Condition - Malformed Controller Input

_For any_ raw message from a controller that is not valid JSON, or whose parsed `action` is not a
string, or whose parsed `payload` (when present) is not a dict, the fixed `_handle_controller`
SHALL send a structured error response to that controller (including `request_id` if present and
parseable), SHALL NOT relay the message to plugins, and SHALL NOT crash the connection handler.

**Validates: Requirements 2.4**

Property 8: Preservation - Well-formed Command Relay

_For any_ controller message that is valid JSON with a string `action` and dict `payload`, the
fixed system SHALL relay it to plugins and route the result back exactly as the original system
did.

**Validates: Requirements 3.5**

Property 9: Bug Condition - Pending Request Leak

_For any_ pending request whose plugin response never arrives within the configured TTL, or whose
issuing controller disconnects first, the fixed system SHALL evict the corresponding
`_pending_requests` entry, so that `len(_pending_requests)` does not grow unboundedly over time
under sustained never-answered requests.

**Validates: Requirements 2.5**

Property 10: Preservation - Timely Result Routing

_For any_ pending request whose plugin response arrives before TTL expiry and before the issuing
controller disconnects, the fixed system SHALL route that result back to the correct controller
exactly as the original system did.

**Validates: Requirements 3.6**

Property 11: Bug Condition - Unbounded Plan Size/Time

_For any_ `SimplePlan`/`DesignPlan` whose element/child counts or content-string lengths exceed the
enforced schema limits, the fixed system SHALL raise a validation error at construction time; and
_for any_ `execute_plan` call whose tree walk has not completed within the configured timeout, the
fixed system SHALL return a clear timeout error instead of hanging indefinitely.

**Validates: Requirements 2.6**

Property 12: Preservation - Full Node Tree Execution Within Limits

_For any_ `SimplePlan`/`DesignPlan` within the enforced limits that completes within the configured
timeout, the fixed system SHALL build the full node tree (all node types, all themes, all planner
backends) exactly as the original system did.

**Validates: Requirements 3.7**

Property 13: Bug Condition - Non-Loopback Bind Without Token

_For any_ call to `start_bridge` with a `host` that is not a loopback address and with no
explicitly configured `BRIDGE_AUTH_TOKEN`, the fixed system SHALL raise a clear fail-closed error
and SHALL NOT bind or serve.

**Validates: Requirements 2.7**

Property 14: Preservation - Default Loopback Bind

_For any_ call to `start_bridge` with its default `host="localhost"` (or another loopback address),
the fixed system SHALL bind and serve without requiring any configuration beyond the token needed
to satisfy Property 1, exactly as the original system did (module config beyond that is
unchanged).

**Validates: Requirements 3.8**

## Fix Implementation

### 1. Bridge auth (C-1)

**Files:** `config.py` (new token helper), `bridge.py` (`handle_connection`, `_handle_plugin`,
`_handle_controller`), `bridge_client.py` (`send_figma_command`), `figma-plugin/ui.html`,
`.gitignore`.

**Token source and precedence:**
- Primary: `BRIDGE_AUTH_TOKEN` env var, read in `config.py` alongside the existing settings.
- Fallback: if unset, `config.py` exposes `get_or_create_bridge_token()` which reads
  `./.bridge_token` (project root, sibling of `.env`) if it exists, or generates one via
  `secrets.token_urlsafe(32)`, writes it to that file, and attempts `os.chmod(path, 0o600)` for
  restricted permissions. This file is added to `.gitignore`.
  - *Platform note:* `os.chmod` on Windows does not enforce the same ACL semantics as POSIX; this
    is defense-in-depth for POSIX hosts and best-effort on Windows. Since the file only protects a
    same-machine loopback flow by default (see fix 7 below), the residual risk is acceptable and
    documented rather than silently assumed away.
- This design (env var override + auto-generated local file) is chosen over "always require an
  env var" so that the tool keeps working with zero required configuration for the common
  loopback/local-dev case (matches "no paid/required dependency" and "smallest safe change" for
  existing users), while still allowing an operator to pin an explicit secret when needed (required
  for fix 7, non-loopback binds).

**Handshake shape (additive field, not a replacement):**
```
{"role": "controller", "token": "<token>"}
{"role": "plugin", "token": "<token>"}
```

**`bridge.py` changes:**
- `handle_connection`: after parsing `first_message`, also read `data.get("token")`. Compare
  against `config.get_or_create_bridge_token()` using `hmac.compare_digest` (constant-time) before
  dispatching to `_handle_plugin`/`_handle_controller`. On mismatch or missing token: send
  `{"status": "error", "message": "Invalid or missing bridge token"}` and close — mirroring the
  existing "unknown role" close path, so no new connection-handling pattern is introduced.
- `_handle_plugin`/`_handle_controller` signatures are unchanged; validation happens entirely in
  `handle_connection` before either is called, so their internal relay/routing logic is untouched
  (this is what keeps 3.1/3.2/3.5/3.6 intact — the fix is purely a gate in front of existing logic).

**`bridge_client.py` changes:** `send_figma_command` now calls
`config.get_or_create_bridge_token()` and sends `{"role": "controller", "token": token}` instead of
`{"role": "controller"}`. No signature change to `send_figma_command` itself — callers in
`tools.py`/`plan_executor.py` are untouched.

**`figma-plugin/ui.html` changes:** the plugin iframe cannot read `.env` or the local filesystem.
Since `manifest.json`'s `devAllowedDomains` already includes `http://localhost:8765` (no manifest
change needed), `ui.html` performs one `fetch('http://localhost:8765/token')` before opening the
WebSocket, and includes the returned token in `{"role": "plugin", "token": ...}`.
- To serve this without adding a second server/port, `bridge.py`'s `websockets.serve(...)` call
  adds a `process_request` hook: for a plain HTTP `GET /token` request (not a WebSocket upgrade),
  it returns a `200` JSON body `{"token": "<token>"}`; every other request is passed through
  unchanged (returns `None`) so the existing WebSocket upgrade path is untouched. This endpoint is
  only registered/served when the bridge is bound to a loopback host (ties into fix 7) — on a
  non-loopback bind it is not exposed, consistent with fail-closed intent.

**Preserves 3.1/3.2:** the accepted-message shape (`{"role": ...}`) is a superset of before; once
the token check passes, the exact same `_handle_plugin`/`_handle_controller` code paths run.

**Test focus:**
- *Fix check:* connection with missing/wrong token is closed before being added to `_controllers`/`_plugins`; no relay occurs for it.
- *Preservation check:* connection with the correct token behaves identically to today's role-only handshake (relay + result routing round-trip test).

### 2. Parent_id allowlist (C-2)

**Files:** `plan_executor.py` (`execute_plan`, `execute_node`, `_execute_post_hoc_container`,
`_build_payload`), `figma-plugin/code.js` (`resolveParent`, documented tradeoff only).

**Mechanism:**
- `execute_plan` creates one `created_node_ids: set[str] = set()` local to that call (not a module
  global — avoids cross-request leakage between concurrent/successive `generate_screen` calls).
- This set is threaded as an explicit parameter through `execute_node(node, parent_id, created_node_ids)`
  and `_execute_post_hoc_container(node, parent_id, created_node_ids)`. Every place that currently
  does `child_ids.append(child_result["node_id"])` or reads `result.get("node_id")` after a
  successful `bridge_client.send_figma_command` call also does `created_node_ids.add(node_id)`.
- Before calling `bridge_client.send_figma_command` for any create_* action, a new
  `_validate_parent_id(parent_id, created_node_ids)` check runs: if `parent_id is not None and
  parent_id not in created_node_ids`, the node is **not** sent to the bridge at all; `execute_node`
  returns `{"status": "error", "message": "parent_id <id> is not part of this plan execution", "node_id": None}`
  for that node (and skips its children, since it was never created) instead of raising an
  exception that would abort the whole plan — this keeps one bad node from failing an otherwise
  legitimate plan, consistent with the existing per-node `succeeded`/`failed` accounting in
  `execute_plan`'s summary.
- Since `DesignNode` has no `parent_id` field today, every `parent_id` this code passes internally
  is always one it just created itself (`effective_parent = node_id`) or `None` — so this check is
  provably a no-op on all current legitimate inputs, and only fires if a future schema addition or
  a non-`plan_executor.py` path ever tries to smuggle in an untrusted `parent_id`.

**Tradeoff on `code.js`'s `resolveParent` (explicitly documented, not silently skipped):**
Client-side (`code.js`) validation cannot know which node IDs belong to "this session" — the
plugin sandbox has no visibility into `plan_executor.py`'s per-call state, and adding shared
session state would require a much larger protocol change (e.g. the bridge tagging commands with
a session ID and the plugin maintaining its own allowlist) that is out of scope for this bugfix.
`resolveParent`'s existing fallback (unresolvable `parent_id` → `figma.currentPage`) is left as-is;
it already fails safe for a *missing* node, it just cannot detect a *valid-but-wrong* node. The
primary and sufficient enforcement point is therefore server-side in `plan_executor.py`, which is
the only component with the authoritative session-scoped knowledge of which node IDs are
legitimate.

**Preserves 3.3:** all `parent_id` values used by real plan executions today are already
internally generated and therefore always in `created_node_ids` — the allowlist check never
rejects a legitimate attachment.

**Test focus:**
- *Fix check:* unit test that manually invokes `execute_node`/`_build_payload` with a `parent_id`
  string not present in `created_node_ids` — assert `bridge_client.send_figma_command` (mocked) is
  never called for that node and the result has `status: "error"`.
- *Preservation check:* run an existing multi-level plan (frame → children → nested rectangle)
  through `execute_plan` with mocked `bridge_client` — assert every child's `parent_id` argument
  equals a `node_id` returned by its actual parent's mocked create call, and the plan's
  `succeeded`/`failed` counts match today's behavior exactly.

### 3. Config validation (H-1)

**Files:** `server.py` (module-level, before `mcp.run()`), `figma_client.py` (`get_file`, lazy
guard).

**Mechanism:**
- `server.py`: call `config.validate_config()` once, inside `if __name__ == "__main__":`, before
  `mcp.run()`. This is the "at startup" enforcement point (2.3's primary requirement) and needs no
  new error handling — the existing `ValueError` message is already clear per the requirement, so
  it is allowed to propagate and terminate startup.
- `figma_client.py`: `get_file` calls `config.validate_config()` as its first line, before building
  `_headers()`/making the `httpx.get` call. This is the defensive "at minimum before the first call
  that requires the config" guard for any code path that imports/calls `tools.py`/`figma_client.py`
  without going through `server.py`'s `__main__` block (e.g. tests, alternate entry points).

**Preserves 3.4:** `validate_config()` is a no-op (returns `None`) whenever both env vars are set,
so the happy path executes identically — this is purely an added early check, not a behavior
change on valid config.

**Test focus:**
- *Fix check:* unit test with `FIGMA_ACCESS_TOKEN`/`FIGMA_FILE_ID` monkeypatched to `None` —
  assert `get_file()` raises the existing `ValueError` message, and assert no `httpx` call is made
  (mock/spy on `httpx.get`).
- *Preservation check:* unit test with both env vars set — assert `get_file()` still performs the
  HTTP call and returns the parsed JSON exactly as before (existing `figma_client` tests, if any,
  continue passing unmodified).

### 4. Malformed controller input handling (H-2)

**Files:** `bridge.py` (`_handle_controller`).

**Mechanism:**
- Wrap the per-message body of the `async for raw_message in websocket:` loop in
  `_handle_controller` with a `try`/`except (json.JSONDecodeError,) as exc:` around
  `json.loads(raw_message)`, and follow it with an explicit shape check:
  `isinstance(data, dict) and isinstance(data.get("action"), str) and isinstance(data.get("payload", {}), dict)`.
- On either failure, build `error_response = {"request_id": data.get("request_id") if isinstance(data, dict) else None, "status": "error", "message": "<reason>"}`,
  `await websocket.send(json.dumps(error_response))`, and `continue` the loop — the malformed
  message is neither relayed via `_relay_to_plugins` nor allowed to raise out of the `async for`
  loop (which today would tear down the whole connection handler).
- No change to `_relay_to_plugins`, `_route_result_to_controller`, or the plugin-side loop — only
  the controller receive loop gains a guard clause in front of its existing logic.

**Preserves 3.5:** any message that already parses as JSON with a string `action` and dict
`payload` (i.e. every message `bridge_client.py` sends today) passes the new checks trivially and
falls through to the exact same `_relay_to_plugins(raw_message)` call as before.

**Test focus:**
- *Fix check:* send a non-JSON string, then a JSON object with `action: 42`, then one with
  `payload: "oops"` — assert the controller receives a structured `status: "error"` response for
  each, `_relay_to_plugins` (mocked) is never called, and the connection remains open (can still
  send a valid message afterward).
- *Preservation check:* send a well-formed command — assert it is relayed exactly as today
  (existing relay test, unmodified) and no error response is sent instead.

### 5. Pending-request cleanup (H-3)

**Files:** `bridge.py` (`_pending_requests`, `_handle_controller`, `_route_result_to_controller`,
`start_bridge`).

**Mechanism:**
- Change `_pending_requests: dict[str, ServerConnection]` to
  `_pending_requests: dict[str, tuple[ServerConnection, float]]`, storing
  `(controller_ws, time.monotonic())` when a `request_id` is first seen in `_handle_controller`.
- Add `_PENDING_REQUEST_TTL_SECONDS = 20.0` (chosen to sit above `bridge_client.py`'s
  `RESPONSE_TIMEOUT_SECONDS = 10.0`, so the bridge does not evict an entry the controller is still
  legitimately waiting on).
- Add `async def _sweep_pending_requests():` — an infinite loop with `await asyncio.sleep(5)` that
  removes any `_pending_requests` entry older than the TTL. `start_bridge` launches it once via
  `asyncio.create_task(_sweep_pending_requests())` alongside `websockets.serve(...)`, and cancels it
  in a `finally` block (or lets it be cancelled when the process exits — `start_bridge` currently
  runs forever via `asyncio.Future()`, so the sweep task's lifetime matches the bridge's).
- `_handle_controller`'s existing `finally: _controllers.discard(websocket)` block also removes any
  `_pending_requests` entries whose stored `controller_ws is websocket` — a disconnected controller
  can never receive a result, so its entries are pruned immediately rather than waiting for TTL.
- `_route_result_to_controller` unpacks the tuple:
  `controller_ws, _ts = _pending_requests.pop(request_id, (None, None))` — the rest of its logic
  (send to `controller_ws`, warn if `None`) is unchanged.
- The dead second copy of `request_id = data.get("request_id")` / `_pending_requests.pop(...)` at
  the bottom of `_route_result_to_controller` (currently unreachable/no-op code after the function
  already returns or falls through) is removed as part of this same touch, since it operates on the
  same dict this fix is changing shape.

**Preserves 3.6:** a plugin response that arrives before TTL expiry and before the issuing
controller disconnects still finds its `(controller_ws, timestamp)` entry in `_pending_requests`
and routes correctly — only the added tuple unpacking changes, not the routing decision.

**Test focus:**
- *Fix check:* insert a pending entry with a synthetic old timestamp (or use a short TTL constant
  in the test), run one sweep iteration directly (call the sweep body, not the infinite loop) —
  assert the entry is removed. Separately: register a pending entry for controller A, simulate A's
  disconnect (`finally` block), assert A's entries are gone while an unrelated controller B's
  entries remain.
- *Preservation check:* register a pending entry, immediately deliver a matching plugin result —
  assert it routes to the correct controller and the entry is popped, identical to today.

### 6. Plan size/timeout caps (H-6)

**Files:** `design_plan.py` (`DesignNode`, `DesignPlan`), `planner.py` (`SimplePlan`,
`SimpleElement`), `plan_executor.py` (`execute_plan`), `config.py` (timeout setting).

**Mechanism — schema caps (Pydantic, enforced at construction, not just prompted):**
- `design_plan.py`:
  - `DesignNode.content: str = Field(default="", max_length=2000)`
  - `DesignNode.children: list["DesignNode"] = Field(default_factory=list, max_length=50)`
  - `DesignPlan.elements: list[DesignNode] = Field(max_length=20)`
- `planner.py`:
  - `SimpleElement.content: str` gains `Field(max_length=500)` — tighter than `DesignNode.content`
    because this is pre-layout-expansion raw model output.
  - `SimplePlan.elements: list[SimpleElement] = Field(min_length=1, max_length=10)` — turns the
    existing "cap at 10" *prompt instruction* into a real, enforced ceiling; a model that ignores
    the instruction now gets a `ValidationError` inside `_parse_simple_plan`'s existing
    try/parse/retry loop (already caught there today for other malformed shapes), which already
    triggers the existing `OllamaPlanner` retry-then-`FallbackPlanner`-to-`TemplatePlanner` chain —
    no new error-handling code needed in `planner.py` beyond the field constraints themselves.

**Mechanism — execution timeout:**
- `config.py` gains `PLAN_EXECUTION_TIMEOUT_SECONDS: float = float(os.getenv("PLAN_EXECUTION_TIMEOUT_SECONDS", "120"))`.
  120s default is sized generously above a realistic worst case within the new caps (up to ~10
  elements → roughly a dozen node-creation round trips at up to `bridge_client`'s 10s timeout
  each), while still bounding runaway executions.
- `plan_executor.py`'s `execute_plan` wraps its existing body (`_apply_design_tokens` + the
  `for node in plan.elements:` loop) in
  `await asyncio.wait_for(_run(), timeout=config.PLAN_EXECUTION_TIMEOUT_SECONDS)` (the existing body
  is factored into an inner `async def _run():` closure so the wrapping is a pure addition around
  unchanged logic). On `asyncio.TimeoutError`, `execute_plan` returns
  `{"screen_name": plan.screen_name, "status": "error", "message": "Plan execution timed out after <N>s"}`
  instead of propagating an unhandled exception or hanging the MCP tool call.

**Preserves 3.7:** the caps (10/20 elements, 50 children, 500/2000 char content, 120s) are all set
above every existing template (`_login_template`, `_dashboard_template`, `_generic_fallback_template`
each use 3–4 elements) and above realistic prompt-driven output already instructed to cap at 10 —
no currently-passing plan construction or execution is rejected by these limits.

**Test focus:**
- *Fix check:* construct a `DesignNode` with 51 children → assert `pydantic.ValidationError` at
  construction. Construct a `SimplePlan` with 11 elements → assert `ValidationError`, and separately
  assert `OllamaPlanner.generate_plan` on a mocked over-sized LLM response falls back correctly
  (existing fallback test pattern). Mock `bridge_client.send_figma_command` to `await asyncio.sleep(large)`
  and call `execute_plan` with a short timeout override — assert it returns the timeout error
  promptly rather than hanging.
- *Preservation check:* run `_login_template()`/`_dashboard_template()` plans through
  `execute_plan` (mocked bridge) — assert identical `succeeded`/`failed`/`total_nodes` output to
  today, completing well under the timeout.

### 7. Fail-closed bind (H-7)

**Files:** `bridge.py` (`start_bridge`).

**Mechanism:**
- `_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}` module constant.
- At the top of `start_bridge(host="localhost", port=8765)`, before calling `websockets.serve(...)`:
  if `host.lower() not in _LOOPBACK_HOSTS` and `config.BRIDGE_AUTH_TOKEN` (the raw, explicitly
  configured env var — not the auto-generated file fallback from fix 1) is falsy, raise
  `RuntimeError("Refusing to bind bridge to non-loopback host '<host>' without an explicitly configured BRIDGE_AUTH_TOKEN. Set BRIDGE_AUTH_TOKEN in .env before exposing the bridge beyond localhost.")`.
  The auto-generated-file token from fix 1 is intentionally *not* sufficient here: it is designed
  for trusted-local-user convenience, not for a secret that must be provisioned to a remote party,
  so network exposure requires the operator to deliberately opt in with a real configured secret.
- This check runs before `websockets.serve` is ever called, so no partial bind occurs.

**Preserves 3.8:** `start_bridge()` with the default `host="localhost"` never enters the new
`host not in _LOOPBACK_HOSTS` branch, so it binds exactly as before, needing only whatever token
fix 1 already requires (auto-generated if absent).

**Test focus:**
- *Fix check:* call `start_bridge(host="0.0.0.0")` with `BRIDGE_AUTH_TOKEN` unset (mock
  `websockets.serve` to detect if it's ever invoked) — assert `RuntimeError` is raised and
  `websockets.serve` is never called.
- *Preservation check:* call `start_bridge(host="localhost")` (mocked `websockets.serve`) with no
  `BRIDGE_AUTH_TOKEN` set — assert it proceeds to call `websockets.serve` exactly as today (the
  auto-generated file token from fix 1 satisfies the connection-level handshake, independent of
  this bind-level check).

## Testing Strategy

### Validation Approach

Each of the 7 subsystems is verified independently and in the implementation order above: an
exploratory test first demonstrates the defect against the *unfixed* code for that subsystem, then
a fix-checking test confirms the property holds post-fix, then a preservation-checking test
confirms bugfix.md's 3.x clause(s) for that subsystem still hold. Later subsystems are only
implemented once earlier ones are green, so a regression introduced by fix N is caught before fix
N+1 is layered on top.

### Exploratory Bug Condition Checking

**Goal:** surface concrete counterexamples on the current, unfixed code before any change is made,
confirming each root-cause hypothesis above.

**Test Plan / Test Cases (run against unfixed code):**
1. **C-1**: open a raw `websockets` connection, send `{"role": "controller"}` with no token —
   observe it is accepted into `_controllers` (will fail/pass-through on unfixed code, confirming
   no auth exists).
2. **C-2**: call `execute_node` directly with a `parent_id` string that was never returned by any
   mocked `send_figma_command` call — observe it is forwarded to `bridge_client.send_figma_command`
   unchecked.
3. **H-1**: monkeypatch `FIGMA_ACCESS_TOKEN=None`, call `figma_client.get_file()` — observe the
   raw `httpx`/`KeyError`/401 failure instead of the clear `ValueError`.
4. **H-2**: send a non-JSON string to `_handle_controller` — observe the unhandled
   `JSONDecodeError` propagating out of the receive loop.
5. **H-3**: register a `request_id` in `_pending_requests` with no plugin connected, wait past any
   reasonable TTL — observe the entry is still present indefinitely.
6. **H-6**: construct a `DesignNode` with 500 children, or a `SimplePlan` with 50 elements —
   observe no `ValidationError` is raised; call `execute_plan` with a `bridge_client` mock that
   never returns — observe the call never completes.
7. **H-7**: call `start_bridge(host="0.0.0.0")` with no token configured — observe it proceeds to
   bind successfully.

**Expected counterexamples:** each case above reproduces exactly the defect described in "Bug
Details," confirming the root-cause hypotheses; no re-hypothesis is needed since each defect maps
directly to a specific missing check in a specific function already identified by the code
inspection above.

### Fix Checking

**Goal:** for each subsystem, verify that for all inputs where that subsystem's bug condition
holds, the fixed function produces the expected behavior.

```
FOR ALL input WHERE isBugCondition_C1(input) DO ASSERT connection is closed, never registered END FOR
FOR ALL input WHERE isBugCondition_C2(input) DO ASSERT node result.status == "error", bridge_client never called END FOR
FOR ALL input WHERE isBugCondition_H1(input) DO ASSERT ValueError raised before any HTTP call END FOR
FOR ALL input WHERE isBugCondition_H2(input) DO ASSERT structured error sent, no relay, handler survives END FOR
FOR ALL input WHERE isBugCondition_H3(input) DO ASSERT stale/orphaned entry evicted from _pending_requests END FOR
FOR ALL input WHERE isBugCondition_H6(input) DO ASSERT ValidationError at construction OR timeout error from execute_plan END FOR
FOR ALL input WHERE isBugCondition_H7(input) DO ASSERT RuntimeError raised, websockets.serve never called END FOR
```

### Preservation Checking

**Goal:** for each subsystem, verify that for all inputs where that subsystem's bug condition does
NOT hold, the fixed function produces the same result as the original function (bugfix.md 3.1–3.9).

```
FOR ALL input WHERE NOT isBugCondition_C1(input) DO ASSERT fixed handshake accepts == original accepts END FOR
FOR ALL input WHERE NOT isBugCondition_C2(input) DO ASSERT fixed attachment == original attachment END FOR
FOR ALL input WHERE NOT isBugCondition_H1(input) DO ASSERT fixed get_file() == original get_file() END FOR
FOR ALL input WHERE NOT isBugCondition_H2(input) DO ASSERT fixed relay == original relay END FOR
FOR ALL input WHERE NOT isBugCondition_H3(input) DO ASSERT fixed routing == original routing END FOR
FOR ALL input WHERE NOT isBugCondition_H6(input) DO ASSERT fixed execute_plan result == original execute_plan result END FOR
FOR ALL input WHERE NOT isBugCondition_H7(input) DO ASSERT fixed start_bridge behavior == original start_bridge behavior END FOR
```

**Testing approach:** property-based testing is recommended for C-2 (random node-ID/parent-id
graphs), H-2 (random malformed JSON/shape fuzzing), and H-6 (random element/child counts and
content lengths straddling the caps), since these have large, easily-generated input domains where
edge cases (off-by-one at the cap boundary, deeply nested vs. wide trees, unicode/very-long
strings) are likely to be missed by hand-written unit tests alone. C-1, H-1, H-3, and H-7 are
narrow enough (a handful of discrete states: token present/absent/valid/invalid; env var
present/absent; entry fresh/stale/orphaned; host loopback/non-loopback × token present/absent) that
targeted unit tests give equivalent confidence with less overhead.

**Test Plan:** for each subsystem, first observe behavior on the *unfixed* code for the
preservation-relevant inputs (steps above), then write the test asserting that exact behavior is
retained post-fix.

### Unit Tests

- Bridge handshake: missing token, wrong token, correct token (per role).
- `plan_executor.py`: `parent_id` in/out of allowlist, nested container flows (`group`,
  `component_set`) still populate the allowlist correctly from their own created children.
- `config.validate_config()`: both vars set, one missing, both missing.
- `_handle_controller`: non-JSON, wrong-typed `action`/`payload`, well-formed message.
- `_pending_requests`: TTL sweep removes stale entries, controller-disconnect cleanup, in-time
  delivery still routes.
- `design_plan.py`/`planner.py`: boundary values at exactly the cap and one over the cap for each
  new `max_length`.
- `start_bridge`: loopback + no token, non-loopback + no token, non-loopback + token.

### Property-Based Tests

- Generate random `parent_id` values (mix of valid-created-this-execution IDs and arbitrary
  strings) across randomly shaped plan trees — verify Property 3/4 (reject-if-not-allowlisted,
  accept-if-allowlisted) holds for every generated case.
- Generate random malformed JSON-like payloads (wrong types for `action`/`payload`, missing keys,
  deeply nested garbage) — verify Property 7/8 (structured error vs. relay) holds and the
  connection handler never raises.
- Generate random `DesignNode`/`SimplePlan` shapes with element/child counts and content lengths
  spanning below/at/above each cap — verify Property 11/12 (validation error above cap, successful
  construction and execution at/below cap).

### Integration Tests

- Full round trip: authenticated controller → bridge → authenticated plugin (mocked Figma API
  surface in `code.js`, or a lightweight fake plugin client) for a `generate_screen` call, asserting
  the same final `succeeded`/`failed`/`total_nodes` shape as before all 7 fixes, for all 6 MCP
  tools (3.9).
- Full round trip with an injected non-allowlisted `parent_id` command sent directly at the bridge
  level (bypassing `plan_executor.py`) — asserting the bridge relays it (bridge itself does not
  parse payload semantics) but a client-side integration test of `plan_executor.py`'s own call path
  never produces such a command in the first place.
- Bridge startup integration test: default `start_bridge()` still serves and accepts one full
  authenticated controller+plugin session end-to-end after all 7 fixes are applied together.
