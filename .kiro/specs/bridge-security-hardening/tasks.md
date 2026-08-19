# Implementation Plan

## Overview

This plan covers only the 7 defects in bugfix.md/design.md (C-1, C-2, H-1, H-2, H-3, H-6, H-7).
H-4 (persistent/pooled bridge connections), H-5 (full automated test suite buildout), and all
P2/P3/P4 items are explicitly out of scope and have no tasks below. The plan is strictly
sequential: tasks are numbered continuously 1–29 across all 7 subsystems plus a final validation
task, in the exact order design.md mandates. Each subsystem's fix must be implemented and its
fix-check + preservation-check tests must be green before the next subsystem's exploration test is
even written, since each subsystem is verified against the code state left by the ones before it
— see the Task Dependency Graph at the end for the full wave-by-wave breakdown.

## Notes

- **Test framework**: the project currently has no `pytest`/`hypothesis` dependency — `tests/`
  today holds plain async scripts run directly via `python tests/<name>.py` (see
  `test_bridge_client.py`, `test_send_command.py`, `test_local.py`, `test_generate_prompt.py`). To
  avoid adding a new dependency, every new test file below follows that same convention: a
  standalone script using `assert` statements, `unittest.mock` (stdlib) for mocking
  `bridge_client`/`websockets`/`httpx` calls, and Python's stdlib `random` module (seeded, for
  reproducibility) to drive the property-based-style checks design.md recommends for C-2, H-2, and
  H-6. Each script exits non-zero (via an uncaught `AssertionError`) on failure and prints a pass
  summary on success, and is run with `python tests/<name>.py`.
- **Task numbering**: tasks are numbered continuously 1–29 across all 7 subsystems, in the exact
  order design.md mandates. Subsystems are strictly sequential — see the Task Dependency Graph at
  the end.

## Tasks

---

## Subsystem 1 — Bridge auth (C-1)

**Files:** `config.py`, `bridge.py` (`handle_connection`, `_handle_plugin`, `_handle_controller`,
`start_bridge`), `bridge_client.py` (`send_figma_command`), `figma-plugin/ui.html`, `.gitignore`.

- [x] 1. Write bug condition exploration test — `tests/test_bridge_auth.py`
  - **Property 1: Bug Condition** - Unauthenticated Bridge Access (C-1)
  - **IMPORTANT**: write and run this BEFORE implementing the fix, against the current, real `bridge.py`
  - **GOAL**: prove `handle_connection` accepts a connection into `_controllers`/`_plugins` with no token check
  - Construct a fake `ServerConnection`-like object (stdlib `unittest.mock`, async `recv()`/`close()`/`send()`) whose first message is `{"role": "controller"}` (no `token` field) and, separately, `{"role": "plugin"}`
  - Call `bridge.handle_connection(fake_ws)` directly for each case
  - Assert the fake websocket ends up added to `bridge._controllers` / `bridge._plugins` and `close()` is never called — this is `isBugCondition_C1` being true and unhandled
  - Run on UNFIXED code — **EXPECTED OUTCOME: test passes as written (confirms the bug: no auth is enforced)**; document this as the counterexample (`{"role": "controller"}` with no token is accepted)
  - _Requirements: 1.1_

- [x] 2. Write preservation property test — `tests/test_bridge_auth.py` (same file, added before the fix)
  - **Property 2: Preservation** - Authenticated Role Handshake
  - **IMPORTANT**: observation-first — first observe on UNFIXED code (today there is no token concept yet, so the "observed" behavior is: any `{"role": "controller"}` / `{"role": "plugin"}` message is accepted and dispatched to `_handle_controller`/`_handle_plugin`)
  - Record that observed accept-and-dispatch behavior as the baseline to preserve once the token gate is added
  - Write the test so that, once the fix lands, it presents the *correct* token alongside `{"role": ...}` and asserts the same accept-and-dispatch behavior — relay/result-routing round trip (controller message relayed to a mocked plugin; plugin result routed back via `request_id`) continues to work exactly as the pre-fix baseline
  - This test necessarily targets the post-fix message shape (token added); note in the script comments that it will only fully pass once `config.get_or_create_bridge_token()` exists — acceptable since Property 2's baseline (accept + relay/routing logic itself) is unchanged and independently verified against the current `_handle_plugin`/`_handle_controller` bodies
  - _Requirements: 3.1, 3.2_

- [x] 3. Fix for unauthenticated bridge access (C-1)
  - [x] 3.1 Implement the fix
    - Add `get_or_create_bridge_token()` to `config.py`: read `BRIDGE_AUTH_TOKEN` env var, else read/create `./.bridge_token` via `secrets.token_urlsafe(32)`, `os.chmod(path, 0o600)` best-effort; add `.bridge_token` to `.gitignore`
    - In `bridge.py`'s `handle_connection`: after parsing `first_message`, read `data.get("token")` and compare against `config.get_or_create_bridge_token()` with `hmac.compare_digest`; on mismatch/missing, send `{"status": "error", "message": "Invalid or missing bridge token"}` and close, mirroring the existing unknown-role close path — do this before dispatching to `_handle_plugin`/`_handle_controller`
    - Add a `process_request` hook to the `websockets.serve(...)` call in `start_bridge` so a plain `GET /token` HTTP request returns `{"token": "<token>"}` as JSON, only when bound to a loopback host; all other requests pass through unchanged
    - Update `bridge_client.py`'s `send_figma_command` to send `{"role": "controller", "token": config.get_or_create_bridge_token()}` instead of `{"role": "controller"}` — no signature change
    - Update `figma-plugin/ui.html` to `fetch('http://localhost:8765/token')` before opening the WebSocket and include the token in `{"role": "plugin", "token": ...}`
    - Do not modify `_handle_plugin`/`_handle_controller` internals — validation lives entirely in `handle_connection`
    - _Bug_Condition: isBugCondition_C1(connection) — role in [controller, plugin] with no valid token, yet accepted_
    - _Expected_Behavior: fixed handle_connection rejects (structured error + close) without adding to _controllers/_plugins_
    - _Preservation: a correct-token handshake is accepted and routed exactly as the original role-only handshake_
    - _Requirements: 2.1_

  - [x] 3.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Unauthenticated Bridge Access (C-1)
    - Re-run the exact test from task 1 (missing/wrong token) against the fixed code
    - **EXPECTED OUTCOME**: connection is now closed with a structured error and never added to `_controllers`/`_plugins`
    - _Requirements: 2.1_

  - [x] 3.3 Verify preservation test still passes
    - **Property 2: Preservation** - Authenticated Role Handshake
    - Re-run the test from task 2 (correct token) against the fixed code
    - **EXPECTED OUTCOME**: accept + relay + result-routing round trip behaves exactly as the pre-fix baseline
    - _Requirements: 3.1, 3.2_

- [x] 4. Checkpoint — ensure all Subsystem 1 tests pass before starting Subsystem 2
  - Run `tests/test_bridge_auth.py` and confirm both properties hold on the fixed code
  - Do not proceed to Subsystem 2 until this is green; ask the user if anything is unclear

---

## Subsystem 2 — Parent_id allowlist (C-2)

**Files:** `plan_executor.py` (`execute_plan`, `execute_node`, `_execute_post_hoc_container`,
`_build_payload`, new `_validate_parent_id`), `figma-plugin/code.js` (`resolveParent` — tradeoff
documented only, no functional change per design.md).

- [x] 5. Write bug condition exploration test — `tests/test_parent_allowlist.py`
  - **Property 1: Bug Condition** - Unrestricted Parent Targeting (C-2)
  - **IMPORTANT**: write and run this BEFORE implementing the fix, against the current `plan_executor.py` (Subsystem 1 fix already applied and green)
  - Mock `bridge_client.send_figma_command` to return `{"status": "ok", "node_id": "<generated>"}`
  - Call `plan_executor.execute_node(node, parent_id="not-created-by-this-execution")` directly with a simple `DesignNode` (e.g. `type="rectangle"`), where `"not-created-by-this-execution"` was never returned by the mocked `send_figma_command` call
  - Assert the mocked `bridge_client.send_figma_command` IS called with that `parent_id` forwarded unchecked (this is `isBugCondition_C2` being true and unhandled)
  - Run on UNFIXED code — **EXPECTED OUTCOME: test passes as written (confirms the bug: no allowlist check exists)**; document the counterexample (arbitrary `parent_id` string forwarded verbatim)
  - _Requirements: 1.2_

- [x] 6. Write preservation property tests — `tests/test_parent_allowlist.py` (same file, before the fix)
  - **Property 2: Preservation** - Legitimate Parent Attachment
  - **IMPORTANT**: observation-first — run a multi-level plan (frame → children → nested rectangle) through `execute_plan` with mocked `bridge_client.send_figma_command`, observe on UNFIXED code that every child's `parent_id` argument equals the `node_id` returned by its actual parent's mocked create call, and note the `succeeded`/`failed` counts
  - Write a property-based-style test: use `random` (seeded) to generate several randomly-shaped node trees (varying `type`, depth, children count) where every `parent_id` used is `None` or a `node_id` legitimately produced earlier in the same execution — assert `execute_node`/`execute_plan` attaches every node under its intended parent and the mocked `send_figma_command` is called once per node, exactly matching the observed baseline
  - Verify these tests PASS on UNFIXED code (baseline is unaffected by the still-missing allowlist since no test case here violates it)
  - _Requirements: 3.3_

- [x] 7. Fix for unrestricted parent_id targeting (C-2)
  - [x] 7.1 Implement the fix
    - In `plan_executor.execute_plan`: create a local `created_node_ids: set[str] = set()`, not a module global
    - Thread `created_node_ids` as an explicit parameter through `execute_node(node, parent_id, created_node_ids)` and `_execute_post_hoc_container(node, parent_id, created_node_ids)`; add every successfully-created `node_id` to the set at each call site that currently reads `result.get("node_id")`
    - Add `_validate_parent_id(parent_id, created_node_ids)`: if `parent_id is not None and parent_id not in created_node_ids`, do NOT call `bridge_client.send_figma_command`; return `{"status": "error", "message": "parent_id <id> is not part of this plan execution", "node_id": None}` for that node and skip its children
    - Call `_validate_parent_id` immediately before every `bridge_client.send_figma_command` call site for create_* actions in `execute_node` and `_execute_post_hoc_container`
    - No change to `_build_payload`'s signature or `figma-plugin/code.js`'s `resolveParent` — the tradeoff (client-side cannot validate session-scoped ownership) stays documented-only per design.md, not implemented
    - _Bug_Condition: isBugCondition_C2(command) — parent_id set, not in created_node_ids_this_execution, forwarded anyway_
    - _Expected_Behavior: fixed execute_node returns status "error" for that node and never calls bridge_client.send_figma_command for it_
    - _Preservation: parent_id None or created earlier in the same execution continues to attach exactly as before_
    - _Requirements: 2.2_

  - [x] 7.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Unrestricted Parent Targeting (C-2)
    - Re-run the exact test from task 5 against the fixed code
    - **EXPECTED OUTCOME**: mocked `bridge_client.send_figma_command` is NOT called for that node; result has `status: "error"`
    - _Requirements: 2.2_

  - [x] 7.3 Verify preservation tests still pass
    - **Property 2: Preservation** - Legitimate Parent Attachment
    - Re-run the tests from task 6 (random legitimate-parent trees) against the fixed code
    - **EXPECTED OUTCOME**: identical attachment behavior and identical `succeeded`/`failed` counts as the pre-fix baseline
    - _Requirements: 3.3_

- [x] 8. Checkpoint — ensure all Subsystem 1 + 2 tests pass before starting Subsystem 3
  - Re-run `tests/test_bridge_auth.py` and `tests/test_parent_allowlist.py` together
  - Do not proceed to Subsystem 3 until both are green

---

## Subsystem 3 — Config validation (H-1)

**Files:** `server.py` (module-level, before `mcp.run()`, inside `if __name__ == "__main__":`),
`figma_client.py` (`get_file`).

- [x] 9. Write bug condition exploration test — `tests/test_config_validation.py`
  - **Property 1: Bug Condition** - Missing Config Validation (H-1)
  - **IMPORTANT**: write and run this BEFORE implementing the fix, against current `figma_client.get_file`
  - Monkeypatch `config.FIGMA_ACCESS_TOKEN = None` (and/or `config.FIGMA_FILE_ID = None`), mock `httpx.get` to detect if it is invoked
  - Call `figma_client.get_file()`
  - Assert it does NOT raise the clear `ValueError` from `config.validate_config()`, and that `httpx.get` IS called (or raises a raw `httpx`/auth-shaped error instead) — this is `isBugCondition_H1` being true and unhandled
  - Run on UNFIXED code — **EXPECTED OUTCOME: test passes as written (confirms the bug: no early validation)**; document the counterexample (opaque HTTP failure instead of the clear `ValueError`)
  - _Requirements: 1.3_

- [x] 10. Write preservation property test — `tests/test_config_validation.py` (same file, before the fix)
  - **Property 2: Preservation** - Valid Config Operation
  - **IMPORTANT**: observation-first — with both `FIGMA_ACCESS_TOKEN` and `FIGMA_FILE_ID` set (monkeypatched to valid dummy values) and `httpx.get` mocked to return a fixed JSON body, observe on UNFIXED code that `get_file()` calls `httpx.get` once and returns the parsed JSON unchanged
  - Write the test asserting that exact observed return value and call count
  - Verify it PASSES on UNFIXED code
  - _Requirements: 3.4_

- [x] 11. Fix for unenforced config validation (H-1)
  - [x] 11.1 Implement the fix
    - In `server.py`, inside `if __name__ == "__main__":`, call `config.validate_config()` once before `mcp.run()`
    - In `figma_client.py`'s `get_file`, call `config.validate_config()` as the first line, before `_headers()`/`httpx.get`
    - No new error handling — let the existing `ValueError` propagate
    - _Bug_Condition: isBugCondition_H1(startup_or_call) — token/file id unset and validate_config() never called before the first REST call_
    - _Expected_Behavior: fixed get_file()/server startup raises the existing ValueError before any HTTP call_
    - _Preservation: validate_config() is a no-op when both env vars are set, so the happy path is unchanged_
    - _Requirements: 2.3_

  - [x] 11.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Missing Config Validation (H-1)
    - Re-run the exact test from task 9 against the fixed code
    - **EXPECTED OUTCOME**: `ValueError` is raised before `httpx.get` is ever called (assert the mock was not invoked)
    - _Requirements: 2.3_

  - [x] 11.3 Verify preservation test still passes
    - **Property 2: Preservation** - Valid Config Operation
    - Re-run the test from task 10 against the fixed code
    - **EXPECTED OUTCOME**: `get_file()` still performs the HTTP call and returns the same parsed JSON as the pre-fix baseline
    - _Requirements: 3.4_

- [x] 12. Checkpoint — ensure all Subsystem 1–3 tests pass before starting Subsystem 4
  - Re-run `tests/test_bridge_auth.py`, `tests/test_parent_allowlist.py`, `tests/test_config_validation.py` together
  - Do not proceed to Subsystem 4 until all are green

---

## Subsystem 4 — Malformed controller input handling (H-2)

**Files:** `bridge.py` (`_handle_controller`).

- [x] 13. Write bug condition exploration test — `tests/test_malformed_controller_input.py`
  - **Property 1: Bug Condition** - Malformed Controller Input (H-2)
  - **IMPORTANT**: write and run this BEFORE implementing the fix, against current `bridge._handle_controller` (Subsystems 1–3 fixes applied and green)
  - Using a fake `ServerConnection`-like object whose async iteration yields a non-JSON string (`"not json at all"`), call `bridge._handle_controller(fake_ws)`
  - Assert the call raises an unhandled `json.JSONDecodeError` out of the receive loop (or otherwise crashes the handler) instead of sending a structured error — this is `isBugCondition_H2` being true and unhandled
  - **Scoped PBT approach**: also feed `{"action": 42, "payload": "oops"}` (wrong types) as a second concrete case and assert it is relayed via `_relay_to_plugins` unchecked rather than rejected
  - Run on UNFIXED code — **EXPECTED OUTCOME: test passes as written / crashes as documented (confirms the bug)**; document both counterexamples
  - _Requirements: 1.4_

- [x] 14. Write preservation property tests — `tests/test_malformed_controller_input.py` (same file, before the fix)
  - **Property 2: Preservation** - Well-formed Command Relay
  - **IMPORTANT**: observation-first — feed a well-formed message (`{"action": "create_rectangle", "payload": {...}, "request_id": "..."}`) through `_handle_controller` with `_relay_to_plugins` mocked, observe on UNFIXED code that it is relayed exactly once with the original raw JSON string and a `_pending_requests` entry is created
  - Write a property-based-style test: use `random` (seeded) to generate many well-formed messages (valid JSON, string `action` from a realistic action-name set, dict `payload` with random-but-valid nested values, optional `request_id`) — assert each is relayed unchanged and pending-request bookkeeping matches the observed baseline
  - Verify tests PASS on UNFIXED code
  - _Requirements: 3.5_

- [x] 15. Fix for unhandled malformed controller input (H-2)
  - [x] 15.1 Implement the fix
    - In `bridge._handle_controller`'s `async for raw_message in websocket:` loop, wrap `json.loads(raw_message)` in `try`/`except json.JSONDecodeError`
    - Add an explicit shape check: `isinstance(data, dict) and isinstance(data.get("action"), str) and isinstance(data.get("payload", {}), dict)`
    - On either failure, build `error_response = {"request_id": data.get("request_id") if isinstance(data, dict) else None, "status": "error", "message": "<reason>"}`, `await websocket.send(json.dumps(error_response))`, and `continue` — do not call `_relay_to_plugins` and do not let the exception propagate
    - No change to `_relay_to_plugins`, `_route_result_to_controller`, or the plugin-side loop
    - _Bug_Condition: isBugCondition_H2(raw_message) — not valid JSON, or action not a string, or payload not a dict_
    - _Expected_Behavior: fixed _handle_controller sends a structured error, does not relay, and the handler survives_
    - _Preservation: well-formed messages (valid JSON, string action, dict payload) continue to relay exactly as before_
    - _Requirements: 2.4_

  - [x] 15.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Malformed Controller Input (H-2)
    - Re-run the exact test from task 13 (non-JSON, then wrong-typed `action`/`payload`) against the fixed code
    - **EXPECTED OUTCOME**: a structured `status: "error"` response is sent for each case, `_relay_to_plugins` is never called, and the handler does not crash (can still process a subsequent valid message)
    - _Requirements: 2.4_

  - [x] 15.3 Verify preservation tests still pass
    - **Property 2: Preservation** - Well-formed Command Relay
    - Re-run the tests from task 14 against the fixed code
    - **EXPECTED OUTCOME**: every well-formed message is still relayed exactly as the pre-fix baseline, no error response sent for these
    - _Requirements: 3.5_

- [x] 16. Checkpoint — ensure all Subsystem 1–4 tests pass before starting Subsystem 5
  - Re-run all four test files together
  - Do not proceed to Subsystem 5 until all are green

---

## Subsystem 5 — Pending-request cleanup (H-3)

**Files:** `bridge.py` (`_pending_requests`, `_handle_controller`, `_route_result_to_controller`,
`start_bridge`, new `_sweep_pending_requests`, `_PENDING_REQUEST_TTL_SECONDS`).

- [x] 17. Write bug condition exploration test — `tests/test_pending_request_cleanup.py`
  - **Property 1: Bug Condition** - Pending Request Leak (H-3)
  - **IMPORTANT**: write and run this BEFORE implementing the fix, against current `bridge._pending_requests` (Subsystems 1–4 fixes applied and green)
  - Directly insert an entry into `bridge._pending_requests` for a synthetic `request_id`, with no plugin ever responding and no sweep mechanism existing
  - Assert the entry is still present after simulating the passage of time (no TTL constant/sweep function exists yet to remove it) — this is `isBugCondition_H3` being true and unhandled
  - Run on UNFIXED code — **EXPECTED OUTCOME: test passes as written (confirms the bug: entry is never evicted)**; document the counterexample
  - _Requirements: 1.5_

- [x] 18. Write preservation property test — `tests/test_pending_request_cleanup.py` (same file, before the fix)
  - **Property 2: Preservation** - Timely Result Routing
  - **IMPORTANT**: observation-first — register a pending entry for controller A (fake websocket), immediately deliver a matching plugin result through `_route_result_to_controller`, observe on UNFIXED code that it routes to controller A and the entry is popped
  - Write the test asserting that exact observed routing behavior, plus a second case for an unrelated controller B's entry remaining untouched
  - Verify it PASSES on UNFIXED code
  - _Requirements: 3.6_

- [x] 19. Fix for pending-request leak (H-3)
  - [x] 19.1 Implement the fix
    - Change `_pending_requests` to `dict[str, tuple[ServerConnection, float]]`, storing `(controller_ws, time.monotonic())` when a `request_id` is first seen in `_handle_controller`
    - Add `_PENDING_REQUEST_TTL_SECONDS = 20.0` and `async def _sweep_pending_requests()`: infinite loop, `await asyncio.sleep(5)`, remove entries older than the TTL
    - `start_bridge` launches the sweep via `asyncio.create_task(_sweep_pending_requests())` alongside `websockets.serve(...)`
    - `_handle_controller`'s existing `finally: _controllers.discard(websocket)` block also removes any `_pending_requests` entries whose stored `controller_ws is websocket`
    - `_route_result_to_controller` unpacks the tuple: `controller_ws, _ts = _pending_requests.pop(request_id, (None, None))`; remove the dead unreachable second `request_id = ...` / `.pop(...)` lines at the bottom of that function
    - _Bug_Condition: isBugCondition_H3(pending_requests, time_elapsed) — no responder and entry never removed regardless of elapsed time or disconnect_
    - _Expected_Behavior: fixed system evicts the entry via TTL sweep or disconnect cleanup, so len(_pending_requests) does not grow unboundedly_
    - _Preservation: an in-time plugin response still routes to the correct controller before TTL/disconnect eviction_
    - _Requirements: 2.5_

  - [x] 19.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Pending Request Leak (H-3)
    - Re-run the sweep body directly (not the infinite loop) on a synthetic stale entry, and separately simulate controller A's disconnect
    - **EXPECTED OUTCOME**: the stale entry is removed by the sweep; controller A's entries are removed on disconnect while unrelated controller B's entries remain
    - _Requirements: 2.5_

  - [x] 19.3 Verify preservation test still passes
    - **Property 2: Preservation** - Timely Result Routing
    - Re-run the test from task 18 against the fixed code
    - **EXPECTED OUTCOME**: in-time delivery still routes to the correct controller and pops the entry, identical to the pre-fix baseline
    - _Requirements: 3.6_

- [x] 20. Checkpoint — ensure all Subsystem 1–5 tests pass before starting Subsystem 6
  - Re-run all five test files together
  - Do not proceed to Subsystem 6 until all are green

---

## Subsystem 6 — Plan size/timeout caps (H-6)

**Files:** `design_plan.py` (`DesignNode.content`, `DesignNode.children`, `DesignPlan.elements`),
`planner.py` (`SimpleElement.content`, `SimplePlan.elements`), `plan_executor.py` (`execute_plan`),
`config.py` (`PLAN_EXECUTION_TIMEOUT_SECONDS`).

- [x] 21. Write bug condition exploration test — `tests/test_plan_size_timeout.py`
  - **Property 1: Bug Condition** - Unbounded Plan Size/Time (H-6)
  - **IMPORTANT**: write and run this BEFORE implementing the fix, against current `design_plan.py`/`planner.py`/`plan_executor.py` (Subsystems 1–5 fixes applied and green)
  - Construct a `DesignNode` with 500 children and assert no `pydantic.ValidationError` is raised (no cap exists yet)
  - Construct a `SimplePlan` with 50 `SimpleElement`s and assert no `ValidationError` is raised
  - Mock `bridge_client.send_figma_command` as an `async def` that `await asyncio.sleep(large_number)` and never returns; call `plan_executor.execute_plan(plan)` with a short outer `asyncio.wait_for` in the TEST HARNESS ONLY (not in the code under test) and assert the call never completes within that harness deadline — proving `execute_plan` itself has no internal timeout
  - This is `isBugCondition_H6` being true and unhandled — **EXPECTED OUTCOME: test passes as written (confirms the bug)**; document all three counterexamples
  - _Requirements: 1.6_

- [x] 22. Write preservation property tests — `tests/test_plan_size_timeout.py` (same file, before the fix)
  - **Property 2: Preservation** - Full Node Tree Execution Within Limits
  - **IMPORTANT**: observation-first — run `planner._login_template()` and `planner._dashboard_template()` (3–4 elements each, well under any future cap) through `execute_plan` with `bridge_client.send_figma_command` mocked to return `{"status": "ok", "node_id": "<n>"}`, observe on UNFIXED code the resulting `succeeded`/`failed`/`total_nodes` values
  - Write a property-based-style test: use `random` (seeded) to generate `DesignNode`/`SimplePlan` shapes with element/child counts and content lengths spanning comfortably below the limits design.md specifies (≤20 elements, ≤50 children, ≤2000/500 char content) — assert construction succeeds and `execute_plan` produces the same shape of result (matching node count, all `status: "ok"`) as the observed baseline, completing without hanging
  - Verify tests PASS on UNFIXED code
  - _Requirements: 3.7_

- [x] 23. Fix for unbounded plan size/time (H-6)
  - [x] 23.1 Implement the fix
    - In `design_plan.py`: `DesignNode.content: str = Field(default="", max_length=2000)`; `DesignNode.children: list["DesignNode"] = Field(default_factory=list, max_length=50)`; `DesignPlan.elements: list[DesignNode] = Field(max_length=20)`
    - In `planner.py`: `SimpleElement.content: str = Field(max_length=500)`; `SimplePlan.elements: list[SimpleElement] = Field(min_length=1, max_length=10)`
    - In `config.py`: add `PLAN_EXECUTION_TIMEOUT_SECONDS: float = float(os.getenv("PLAN_EXECUTION_TIMEOUT_SECONDS", "120"))`
    - In `plan_executor.py`'s `execute_plan`: factor the existing body into an inner `async def _run():` closure, wrap the call as `await asyncio.wait_for(_run(), timeout=config.PLAN_EXECUTION_TIMEOUT_SECONDS)`; on `asyncio.TimeoutError`, return `{"screen_name": plan.screen_name, "status": "error", "message": "Plan execution timed out after <N>s"}` instead of propagating or hanging
    - _Bug_Condition: isBugCondition_H6(plan_or_execution) — element/child/content counts exceed no enforced limit, or execute_plan has no overall deadline_
    - _Expected_Behavior: fixed system raises ValidationError at construction above the caps, and execute_plan returns a clear timeout error instead of hanging_
    - _Preservation: plans within the caps that complete within the timeout still build the full node tree exactly as before_
    - _Requirements: 2.6_

  - [x] 23.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Unbounded Plan Size/Time (H-6)
    - Re-run the exact tests from task 21 (51-child node, 11-element plan, never-returning mocked bridge call) against the fixed code
    - **EXPECTED OUTCOME**: `pydantic.ValidationError` at construction for the oversized cases; `execute_plan` returns the timeout error dict promptly rather than hanging
    - _Requirements: 2.6_

  - [x] 23.3 Verify preservation tests still pass
    - **Property 2: Preservation** - Full Node Tree Execution Within Limits
    - Re-run the tests from task 22 (templates + randomly-generated within-cap shapes) against the fixed code
    - **EXPECTED OUTCOME**: identical `succeeded`/`failed`/`total_nodes` output to the pre-fix baseline, completing well under the timeout
    - _Requirements: 3.7_

- [x] 24. Checkpoint — ensure all Subsystem 1–6 tests pass before starting Subsystem 7
  - Re-run all six test files together
  - Do not proceed to Subsystem 7 until all are green

---

## Subsystem 7 — Fail-closed bind (H-7)

**Files:** `bridge.py` (`start_bridge`, new `_LOOPBACK_HOSTS`).

- [x] 25. Write bug condition exploration test — `tests/test_fail_closed_bind.py`
  - **Property 1: Bug Condition** - Non-Loopback Bind Without Token (H-7)
  - **IMPORTANT**: write and run this BEFORE implementing the fix, against current `bridge.start_bridge` (Subsystems 1–6 fixes applied and green)
  - Mock `websockets.serve` (so no real socket is opened) and mock/clear `config.BRIDGE_AUTH_TOKEN` (falsy)
  - Call `await bridge.start_bridge(host="0.0.0.0")`
  - Assert `websockets.serve` IS called (no RuntimeError is raised) — this is `isBugCondition_H7` being true and unhandled
  - Run on UNFIXED code — **EXPECTED OUTCOME: test passes as written (confirms the bug: non-loopback bind proceeds without a token)**; document the counterexample
  - _Requirements: 1.7_

- [x] 26. Write preservation property test — `tests/test_fail_closed_bind.py` (same file, before the fix)
  - **Property 2: Preservation** - Default Loopback Bind
  - **IMPORTANT**: observation-first — mock `websockets.serve`, clear `config.BRIDGE_AUTH_TOKEN`, call `await bridge.start_bridge(host="localhost")` on UNFIXED code, observe that it proceeds to call `websockets.serve` without error
  - Write the test asserting that exact observed proceed-to-serve behavior for `host="localhost"` (and `"127.0.0.1"`, `"::1"`)
  - Verify it PASSES on UNFIXED code
  - _Requirements: 3.8_

- [x] 27. Fix for missing fail-closed bind restriction (H-7)
  - [x] 27.1 Implement the fix
    - Add module constant `_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}` to `bridge.py`
    - At the top of `start_bridge(host="localhost", port=8765)`, before `websockets.serve(...)` is called: if `host.lower() not in _LOOPBACK_HOSTS` and `config.BRIDGE_AUTH_TOKEN` (the raw env var, not the auto-generated file token) is falsy, raise `RuntimeError("Refusing to bind bridge to non-loopback host '<host>' without an explicitly configured BRIDGE_AUTH_TOKEN. Set BRIDGE_AUTH_TOKEN in .env before exposing the bridge beyond localhost.")`
    - Ensure this check runs before `websockets.serve` so no partial bind occurs
    - _Bug_Condition: isBugCondition_H7(host, configured_token) — non-loopback host, no configured token, bind proceeds anyway_
    - _Expected_Behavior: fixed start_bridge raises RuntimeError and never calls websockets.serve_
    - _Preservation: default host="localhost" (or another loopback address) continues to bind exactly as before_
    - _Requirements: 2.7_

  - [x] 27.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Non-Loopback Bind Without Token (H-7)
    - Re-run the exact test from task 25 against the fixed code
    - **EXPECTED OUTCOME**: `RuntimeError` is raised and `websockets.serve` is never called
    - _Requirements: 2.7_

  - [x] 27.3 Verify preservation test still passes
    - **Property 2: Preservation** - Default Loopback Bind
    - Re-run the test from task 26 against the fixed code
    - **EXPECTED OUTCOME**: loopback hosts still proceed to call `websockets.serve` exactly as the pre-fix baseline
    - _Requirements: 3.8_

- [x] 28. Checkpoint — ensure all Subsystem 1–7 tests pass
  - Re-run all seven new test files together
  - Do not proceed to the final regression task until all are green; ask the user if questions arise

---

## Final Validation

- [x] 29. Run full existing test suite plus all new tests together; confirm no new runtime dependency was introduced
  - Run the 4 pre-existing scripts: `tests/test_bridge_client.py`, `tests/test_send_command.py`, `tests/test_local.py`, `tests/test_generate_prompt.py` (these exercise MCP tool listing/`echo`, controller command send, and `generate_ui_from_prompt` — covers Requirements 3.9 for the tools they touch)
  - Run the 7 new scripts from Subsystems 1–7: `tests/test_bridge_auth.py`, `tests/test_parent_allowlist.py`, `tests/test_config_validation.py`, `tests/test_malformed_controller_input.py`, `tests/test_pending_request_cleanup.py`, `tests/test_plan_size_timeout.py`, `tests/test_fail_closed_bind.py`
  - Additionally run one end-to-end check per bugfix.md 3.9: with a locally running bridge + a stubbed/faked plugin session (or the existing manual `tests/test_send_command.py` pattern extended with the new token handshake), confirm all 6 MCP tools (`ping`, `echo`, `get_figma_file_overview`, `create_figma_rectangle`, `generate_screen`, `generate_ui_from_prompt`) still return the same successful shapes given valid inputs and an authenticated bridge/plugin pair
  - Diff `requirements.txt` against its current content (`mcp[cli]`, `httpx`, `python-dotenv`, `websockets`) and confirm it is UNCHANGED — design.md's 7 fixes use only stdlib (`hmac`, `secrets`, `os`, `time`, `asyncio`, `json`) and existing dependencies (`pydantic` via `mcp`), so no update is needed; only touch `requirements.txt` if this check surfaces an actual gap
  - _Requirements: 3.1–3.9 (full regression)_

---

## Task Dependency Graph

Strictly sequential by design — each subsystem's fix must be implemented and its fix-check +
preservation-check tests must be green before the next subsystem's exploration test is even
written, since each subsystem is verified against the code state left by the ones before it.

Since this plan is strictly sequential, each wave below contains exactly one subsystem's tasks
(waves 1–7), plus a final wave for the full-regression validation task:

```json
{
  "waves": [
    { "wave": 1, "subsystem": "Bridge auth (C-1)", "tasks": [1, 2, 3, 4] },
    { "wave": 2, "subsystem": "Parent_id allowlist (C-2)", "tasks": [5, 6, 7, 8] },
    { "wave": 3, "subsystem": "Config validation (H-1)", "tasks": [9, 10, 11, 12] },
    { "wave": 4, "subsystem": "Malformed controller input handling (H-2)", "tasks": [13, 14, 15, 16] },
    { "wave": 5, "subsystem": "Pending-request cleanup (H-3)", "tasks": [17, 18, 19, 20] },
    { "wave": 6, "subsystem": "Plan size/timeout caps (H-6)", "tasks": [21, 22, 23, 24] },
    { "wave": 7, "subsystem": "Fail-closed bind (H-7)", "tasks": [25, 26, 27, 28] },
    { "wave": 8, "subsystem": "Final Validation", "tasks": [29] }
  ]
}
```

The human-readable arrow diagram below shows the same sequential dependency at a finer grain
(including the internal explore -> preserve -> implement -> verify sub-steps within each wave):

```
1. Explore (C-1)
2. Preserve (C-1)
3. Implement (C-1) [3.1 fix -> 3.2 verify explore -> 3.3 verify preserve]
4. Checkpoint (C-1)
        |
        v
5. Explore (C-2)
6. Preserve (C-2)
7. Implement (C-2) [7.1 -> 7.2 -> 7.3]
8. Checkpoint (C-2)
        |
        v
9. Explore (H-1)
10. Preserve (H-1)
11. Implement (H-1) [11.1 -> 11.2 -> 11.3]
12. Checkpoint (H-1)
        |
        v
13. Explore (H-2)
14. Preserve (H-2)
15. Implement (H-2) [15.1 -> 15.2 -> 15.3]
16. Checkpoint (H-2)
        |
        v
17. Explore (H-3)
18. Preserve (H-3)
19. Implement (H-3) [19.1 -> 19.2 -> 19.3]
20. Checkpoint (H-3)
        |
        v
21. Explore (H-6)
22. Preserve (H-6)
23. Implement (H-6) [23.1 -> 23.2 -> 23.3]
24. Checkpoint (H-6)
        |
        v
25. Explore (H-7)
26. Preserve (H-7)
27. Implement (H-7) [27.1 -> 27.2 -> 27.3]
28. Checkpoint (H-7)
        |
        v
29. Final Validation (full regression + dependency check)
```

Within each subsystem block, ordering is also strict: explore-test -> preserve-test -> implement
-> re-verify explore -> re-verify preserve -> checkpoint. No subsystem's implementation task (3.1,
7.1, 11.1, 15.1, 19.1, 23.1, 27.1) may begin before that subsystem's own explore/preserve tests
(steps 1–2, 5–6, 9–10, 13–14, 17–18, 21–22, 25–26) are written and run against the code state left
by the previous checkpoint.

---

## Post-Implementation Note

During task 29's final validation, one additional defect was found and fixed. This was not one
of the original 7 bugfix.md defects, but a bug in the H-6 fix's own implementation, discovered
while validating it end-to-end.

- **Defect found during final validation**: `execute_plan`'s outer `asyncio.wait_for(..., timeout=...)` in plan_executor.py caught `asyncio.TimeoutError` to report the overall-timeout condition (H-6). Since Python 3.11, `TimeoutError` and `asyncio.TimeoutError` are the same class, and `bridge_client.send_figma_command` independently raises a plain `TimeoutError` on its own 10s per-command wait whenever no plugin is connected (an expected, everyday occurrence, not a plan-size problem). A single dropped/timed-out command was therefore being mistaken for the overall 120s plan deadline, producing a misleading "Plan execution timed out after 120.0s" message after only ~10 seconds and discarding all other nodes' results.
- **Fix applied**: added `plan_executor._send_command_safe()`, which wraps every `bridge_client.send_figma_command` call site (in `execute_node`, `_execute_post_hoc_container`, and `_apply_design_tokens`) and converts a per-command `TimeoutError`/`ConnectionError` into that node's own `{"status": "error", ...}` result, matching the existing per-node error-result shape. The outer `asyncio.wait_for` deadline itself was NOT changed and still correctly bounds genuine overall-runtime overruns.
- **Test added**: `tests/test_plan_command_failure_isolation.py` (isolated TimeoutError case, isolated ConnectionError case, and a preservation case confirming all-succeed plans are unaffected) -- all passing.
- Note that this fix is in-scope because it corrects a defect in H-6's own implementation (subsystem 6), not new functionality, and does not touch any other subsystem.
