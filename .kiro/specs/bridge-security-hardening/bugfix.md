# Bugfix Requirements Document

## Introduction

A security audit of the figma-mcp-server project (MCP Server → Ollama planner → DesignPlan → Plan Executor → Bridge Client → WebSocket Bridge → Figma Plugin → Figma Canvas) identified 7 confirmed CRITICAL (P0) and HIGH (P1) defects in the bridge/plugin transport layer, the parent-node targeting logic, config validation, and plan-size/timeout handling. This bugfix addresses those 7 defects only. All fixes must be additive/corrective on top of the existing architecture — the 6 MCP tools (`ping`, `echo`, `get_figma_file_overview`, `create_figma_rectangle`, `generate_screen`, `generate_ui_from_prompt`), the dispatch-table command handlers in `figma-plugin/code.js`, the `DesignPlan`/`DesignNode` schema, the role-based relay protocol between `bridge_client.py` and `figma-plugin/ui.html`, and the `Planner` abstraction (Ollama/Template/Fallback) must all continue to work exactly as they do today.

Out of scope for this bugfix (explicitly deferred): H-4 (persistent/pooled bridge connections with reconnect/backoff), H-5 (full automated test suite), and all P2/P3/P4 items (multi-session routing, per-user file isolation, component_set nesting, new node/asset types, dependency pinning, logging/retention policy).

## Bug Analysis

### Current Behavior (Defect)

1.1 (C-1 — Unauthenticated bridge) WHEN any process or browser page opens a WebSocket connection to `ws://localhost:8765` and sends `{"role": "controller"}` or `{"role": "plugin"}` THEN `bridge.py`'s `handle_connection` accepts the connection into `_controllers` or `_plugins` with no token, secret, or Origin/Host check, allowing an untrusted party to issue arbitrary Figma canvas commands or intercept commands/results meant for the legitimate plugin.

1.2 (C-2 — Unrestricted parent_id) WHEN a `create_*` command payload includes a `parent_id` THEN `figma-plugin/code.js`'s `resolveParent` calls `figma.getNodeById(parent_id)` and `plan_executor.py` forwards whatever `parent_id` it is given without restriction, allowing any node in the entire currently-open Figma file (not just nodes created by the current plan/session) to be targeted as a parent.

1.3 (H-1 — Unenforced config validation) WHEN `FIGMA_ACCESS_TOKEN` or `FIGMA_FILE_ID` is missing from `.env` THEN the system does not call `config.validate_config()` anywhere, so the first Figma API call in `figma_client.py` fails late with a raw, opaque `httpx`/HTTP error instead of a clear configuration error.

1.4 (H-2 — Unhandled malformed controller input) WHEN a controller connection sends a message that is not valid JSON, or valid JSON where `action` is not a string or `payload` is not a dict THEN `bridge.py`'s `_handle_controller` receive loop raises an unhandled exception from `json.loads(raw_message)` (or relays the malformed command anyway), instead of returning a structured error response to that controller.

1.5 (H-3 — Pending request leak) WHEN a controller sends a command with a `request_id` and the connected plugin never responds (e.g. the plugin disconnects, crashes, or is not actually running) THEN the corresponding entry in `bridge.py`'s `_pending_requests` dict is never removed, causing unbounded memory growth over the bridge's uptime.

1.6 (H-6 — Unbounded plan size/time) WHEN an LLM-generated or otherwise adversarial `SimplePlan`/`DesignPlan` contains an excessive number of elements/children or excessively long content strings, or WHEN `plan_executor.py`'s `execute_plan` is processing any plan THEN there is no schema-level cap enforced (the "10 elements" limit exists only as a prompt instruction to the LLM, which it can ignore) and no overall operation timeout, so the `generate_screen`/`generate_ui_from_prompt` tool call can hang for an unbounded amount of time, one 10-second bridge round-trip at a time.

1.7 (H-7 — No fail-closed bind restriction) WHEN `bridge.py`'s `start_bridge` is invoked with a `host` value other than a loopback address (e.g. `0.0.0.0`) and no auth token is configured THEN the bridge still binds and starts serving, exposing the unauthenticated command channel described in 1.1 to the network.

### Expected Behavior (Correct)

2.1 (C-1) WHEN any process or browser page opens a WebSocket connection to the bridge and sends an initial role-identification message THEN the system SHALL require a valid shared-secret/token in that handshake before adding the connection to `_controllers` or `_plugins`, and SHALL close the connection with a clear error if the token is missing or invalid, while preserving the existing `{"role": "controller"}` / `{"role": "plugin"}` message shape expected by `bridge_client.py` and `figma-plugin/ui.html` (the token is added to, not a replacement for, the existing handshake message).

2.2 (C-2) WHEN a `create_*` command payload includes a `parent_id` THEN the system SHALL reject the command with a clear error unless that `parent_id` refers to a node ID that was actually created earlier within the current plan/session, enforced server-side in `plan_executor.py` (an allowlist of node IDs created by the executor) and defensively in `figma-plugin/code.js`'s `resolveParent`/`create_*` handlers.

2.3 (H-1) WHEN the MCP server starts up (or, at minimum, before the first call that requires `FIGMA_ACCESS_TOKEN`/`FIGMA_FILE_ID`) THEN the system SHALL invoke `config.validate_config()` and SHALL fail fast with the existing clear `ValueError` message if required configuration is missing, rather than allowing a later opaque HTTP failure.

2.4 (H-2) WHEN a controller connection sends a message that is not valid JSON, or where `action` is not a string or `payload` is not a dict THEN `bridge.py`'s `_handle_controller` SHALL catch the parsing/validation failure, SHALL send a structured error response back to that controller (including the `request_id` if one was present and parseable), and SHALL NOT relay the malformed message to plugins or crash the connection handler.

2.5 (H-3) WHEN a controller's pending request is not answered by a plugin within a bounded time, or WHEN the controller that issued a pending request disconnects THEN the system SHALL evict the corresponding `_pending_requests` entry (via TTL-based expiry and/or cleanup tied to controller disconnect), preventing unbounded growth of `_pending_requests` over the bridge's uptime.

2.6 (H-6) WHEN a `SimplePlan`/`DesignPlan` is constructed or received THEN the system SHALL enforce a schema-level maximum on the number of elements/children and on individual content string length (rejecting oversized plans with a clear validation error), and WHEN `plan_executor.py`'s `execute_plan` runs THEN the system SHALL enforce an overall operation timeout, returning a clear timeout error instead of hanging indefinitely.

2.7 (H-7) WHEN `bridge.py`'s `start_bridge` is invoked with a `host` value that is not a loopback address AND no valid auth token is configured THEN the system SHALL refuse to bind and SHALL raise/log a clear fail-closed error explaining that a non-loopback bind requires a configured auth token.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a controller presents a valid auth token and sends `{"role": "controller"}` THEN the system SHALL CONTINUE TO accept the connection and relay its commands to connected plugins exactly as today.

3.2 WHEN a plugin presents a valid auth token and sends `{"role": "plugin"}` THEN the system SHALL CONTINUE TO accept the connection and have its results routed back to the correct requesting controller via `request_id` exactly as today.

3.3 WHEN a `create_*` command's `parent_id` is omitted, or refers to a node legitimately created earlier in the same plan/session THEN the system SHALL CONTINUE TO attach the new node under that parent exactly as today.

3.4 WHEN `FIGMA_ACCESS_TOKEN` and `FIGMA_FILE_ID` are both correctly configured THEN the system SHALL CONTINUE TO call the Figma API and return file overviews exactly as today.

3.5 WHEN a controller sends a well-formed command (valid JSON, `action` as a string, `payload` as a dict) THEN the system SHALL CONTINUE TO relay it to plugins and route the result back exactly as today.

3.6 WHEN a plugin responds to a pending request before any timeout/eviction occurs THEN the system SHALL CONTINUE TO route that result back to the correct controller exactly as today.

3.7 WHEN a `SimplePlan`/`DesignPlan` is within the enforced size and content-length limits, and `execute_plan` completes within the enforced timeout THEN the system SHALL CONTINUE TO build and execute the full node tree (frames, components, groups, component_sets, text, rectangles, ellipses, lines, image placeholders, icons, color/text styles, variables) exactly as today, including the `professional_blue`/`midnight_premium`/`calm_wellness`/`warm_friendly` themes and the Ollama → Template `FallbackPlanner` chain.

3.8 WHEN `bridge.py`'s `start_bridge` is invoked with its default `host="localhost"` (or another loopback address) THEN the system SHALL CONTINUE TO bind and serve without requiring any additional configuration beyond the auth token needed to satisfy 2.1.

3.9 WHEN all 6 existing MCP tools (`ping`, `echo`, `get_figma_file_overview`, `create_figma_rectangle`, `generate_screen`, `generate_ui_from_prompt`) are invoked with valid inputs and a properly authenticated bridge/plugin pair THEN the system SHALL CONTINUE TO return the same successful results and Figma canvas effects as today.
