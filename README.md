# Figma MCP Server — AI-Driven UI Generation

An MCP (Model Context Protocol) server that lets an AI assistant generate real, editable UI screens directly inside Figma — from either structured input or a single natural-language prompt.

```
User Prompt
   │
   ▼
Planner (LLM)              — Ollama (local, default) / Anthropic (optional) / Template (offline fallback)
   │  produces UI intent (semantic elements: cards, nav, forms, tables...)
   ▼
Semantic Composer           — components.py builds an Auto-Layout DesignNode tree from a
   │                           coherent DesignTokens palette (design_tokens.py)
   ▼
Plan Validation             — plan_validation.py checks + auto-corrects the DesignPlan
   │                           (dimensions, duplicate names, invalid layout, overlap, ...)
   ▼
Plan Executor               — walks the plan tree, resolves parent/child/component relationships
   │
   ▼
MCP Server (server.py)     — exposes tools to any MCP-compatible AI assistant
   │
   ▼
Bridge (bridge.py)         — WebSocket relay, role-based routing, request/response correlation
   │
   ▼
Figma Plugin (code.js)     — runs inside real Figma, creates actual canvas nodes,
   │                           auto-arranges each new root screen next to prior ones
   ▼
Figma Canvas               — real frames, text, rectangles, groups, variants, styles, components
```

## Status: working end-to-end

Every arrow in the diagram above is real, tested code — not a mock. Confirmed working:

- MCP server with 6 tools (`ping`, `echo`, `get_figma_file_overview`, `create_figma_rectangle`, `generate_screen`, `generate_ui_from_prompt`)
- Real Figma REST API integration (read file structure)
- An authenticated WebSocket bridge (shared-secret token handshake) with role-based routing and request/response correlation (not fire-and-forget); the last plugin disconnecting fails any in-flight requests immediately instead of leaving them to a timeout; configurable WS ping/pong keepalive
- A persistent, auto-reconnecting bridge connection on the MCP side — one connection reused across calls instead of a new socket per command, with bounded exponential backoff on disconnect and safe handling of concurrent in-flight requests
- A real Figma Plugin, running inside actual Figma, connected live to the bridge, with its own bounded auto-reconnect if the connection drops, and a buffered result queue so a command that finishes executing while the socket is briefly reconnecting is never silently lost
- A dispatch-table command handler in the plugin covering: frames, text, rectangles, ellipses, lines, image placeholders, icons (placeholder), groups, components, component instances, component sets (variants), color styles, text styles, variables, opacity, and borders/strokes
- A recursive `DesignPlan` schema supporting nested containers (frame/component/group/component_set/instance with children), rich Auto Layout (per-side padding, alignment, sizing modes), and per-node semantic metadata
- A `Planner` abstraction with three interchangeable backends (see below), now producing genuine UI intent (headers, sidebars, cards, tables, forms, badges, avatars, tabs, modals) instead of only heading/text/input/button primitives
- A coherent design-token system (`design_tokens.py`): 8 named visual styles (including "minimal SaaS dashboard", "dark fintech dashboard", "modern ecommerce", "healthcare mobile") each with its own color palette, typography scale, spacing scale, radius scale, and shadows — every node on one screen draws from the same palette, never random per-node colors
- A plan validation/normalization pass (`plan_validation.py`) that runs before every execution: clamps invalid dimensions, de-duplicates top-level names, strips Auto Layout from types that can't use it, resolves overlapping root elements, fills empty semantic text, resolves/repairs component↔instance references, and rejects (never silently truncates) an oversized plan — always reporting what it changed
- Automatic canvas placement for sequentially generated screens: each new root-level screen (frame or component) is placed to the right of the rightmost existing screen with a safe gap, so generating dashboard → login → profile → settings never overlaps
- `generate_ui_from_prompt`: a single natural-language sentence → structured plan → multiple Figma nodes created automatically, confirmed working end-to-end both via automated tests and a live run against a real connected Figma plugin (e.g. "a modern SaaS dashboard with a sidebar, a header, two stat cards ... and a table of recent customers" → 30/30 nodes created successfully)

## Architecture decisions worth knowing

**Why a bridge instead of calling Figma's API directly?**
Figma's REST API is read-only for document structure — it cannot create nodes. Real canvas manipulation is only possible from inside a Figma Plugin, which runs sandboxed and cannot accept incoming connections. The bridge is a WebSocket relay that both the MCP server and the plugin connect to *as clients*; the MCP server sends a command, the bridge routes it to the connected plugin, the plugin executes it and routes a result back — correlated by a `request_id`, so multiple in-flight requests never get crossed.

**Why is the plugin split into `code.js` and `ui.html`?**
Figma enforces this split for security: the plugin sandbox (`code.js`) can touch the canvas but has no network access; the UI iframe (`ui.html`) has network access but cannot touch the canvas. The bridge connection lives in `ui.html`; commands and results cross that boundary via `figma.ui.postMessage` / `window.onmessage`.

**Why a dispatch-table pattern in `code.js`?**
Adding a new node type is "add one function to the `commandHandlers` object" — the transport layer, the bridge, and the MCP server never need to change. The same pattern repeats at the Python layer (`_ACTION_MAP` in `plan_executor.py`), which is deliberate: consistent extension points at every layer.

**Why does the Command Generator expand composites into primitives?**
"Button" and "image placeholder" aren't things the Figma Plugin API can create directly — they're a rectangle + text pair. Expanding composites in Python (not in `code.js`) keeps the plugin permanently simple; new composite types never require touching plugin code.

**How does the bridge stay reliable?**
Every connection to the bridge — both the MCP-side controller and the Figma plugin — must present a shared-secret token before it's trusted. That token is generated automatically on first run into a local, gitignored `.bridge_token` file, or can be pinned explicitly via `BRIDGE_AUTH_TOKEN` in `.env`. On the MCP side, `bridge_client.py` keeps one persistent, authenticated connection instead of opening a new socket per command; concurrent tool calls share it safely, since each request is matched to its response by `request_id` rather than by arrival order. If the bridge or plugin connection drops, both sides reconnect automatically with bounded exponential backoff — a handful of attempts, then a clear failure, never an infinite retry loop. If no Figma plugin is connected at all, the bridge reports that immediately instead of making the caller wait out the full response timeout; if the LAST connected plugin disconnects while a request is in flight, that request is now also failed immediately rather than waiting on a timeout or the periodic sweep. Both `bridge.py` and `bridge_client.py` use a configurable WebSocket ping/pong interval (`BRIDGE_PING_INTERVAL_SECONDS`/`BRIDGE_PING_TIMEOUT_SECONDS`) so a dead peer (e.g. a sleeping laptop) is detected on a known cadence rather than an untouched library default. The plugin's own UI iframe buffers a result if the bridge socket happens to be reconnecting at that exact moment, instead of silently dropping a command that actually succeeded on the canvas. Two independent timeouts bound every call: `BRIDGE_RESPONSE_TIMEOUT_SECONDS` (default 10s) for a single command, and `PLAN_EXECUTION_TIMEOUT_SECONDS` (default 120s) for an entire `generate_screen`/`generate_ui_from_prompt` call — a single slow or dropped command inside a larger plan is reported as just that one node failing, not a false "the whole plan timed out."

## The Planner layer

`planner.py` defines a single interface all backends implement:

```python
class Planner(ABC):
    async def generate_plan(self, prompt: str) -> DesignPlan: ...
```

Three implementations exist, swappable with zero changes anywhere else in the codebase:

| Backend | Status | Notes |
|---|---|---|
| `OllamaPlanner` | **Default, fully working** | Calls a local Ollama server. Uses simple JSON mode (`format: "json"`), not full JSON-schema-constrained decoding — the schema is recursive, and Ollama's schema-grammar compiler is impractically slow on small local models with recursive schemas. Correctness is enforced instead by a validation + repair pipeline (`_parse_simple_plan`): strips code fences, extracts the outermost JSON object, and repairs trailing commas, followed by a semantic-quality gate that rejects placeholder-leakage output (the model echoing literal words like `"string"`). Its output is a rich `SimplePlan` of semantic elements (headings, cards, tables, nav, forms, badges, ...), turned into a full Auto-Layout node tree by `build_semantic_plan` (see "Design agent" below). |
| `AnthropicPlanner` | Implemented, integration-tested, **not wired in** | Confirmed working against the real Anthropic API in earlier development. Not referenced by `get_planner()` — Ollama remains the only local/default LLM. Swapping it in would require deliberately editing `get_planner()`; nothing wires it in automatically. |
| `TemplatePlanner` | Fully working, offline fallback | Zero network calls, keyword-matched (`login`, `dashboard`). Uses the original flat layout engine (`build_design_plan`) unchanged, so it stays a minimal, dependency-free fallback. Used automatically if the primary backend fails for any reason. |

`FallbackPlanner` wraps a primary + fallback: if `OllamaPlanner` fails (server down, model not pulled, unrecoverable malformed JSON), it transparently falls back to `TemplatePlanner` rather than failing the whole tool call. This is visible in logs as a `WARNING`, not a crash.

**Recommended local model:** `qwen2.5:3b`. A larger model (`qwen2.5:7b`, `qwen3:14b`) produces better output but is impractically slow on CPU-only inference. This is a deliberate size/quality trade-off for responsive local use, documented here rather than hidden.

## The Design Agent layer (planner.py + components.py + design_tokens.py)

The planner no longer just places `heading`/`text`/`input`/`button` primitives at fixed offsets. The LLM is asked for **UI intent** — a small ordered list of semantic elements (`header`, `sidebar`, `card`, `stat_card`, `table`, `list`, `form`, `badge`, `avatar`, `tabs`, `divider`, `image`, `icon`, `section`, plus the original basics) with their content, and a **visual style** name — never raw x/y/width/height/color, so positioning and styling stay deterministic regardless of model quality.

- **`design_tokens.py`** defines one `DesignTokens` bundle per named style: a full color system (primary/secondary/background/surface/border/text-primary/text-secondary/success/warning/error), a typography scale (display/h1/h2/h3/body/small/caption/label/button), a spacing scale, a radius scale, two shadow presets, and component sizes (button/input/nav height, sidebar width, avatar/icon size). Eight styles ship today: the original `professional_blue` / `midnight_premium` / `calm_wellness` / `warm_friendly`, plus `minimal_saas`, `dark_fintech`, `modern_ecommerce`, and `healthcare_mobile` — each with its own distinct palette and type scale, so different prompts produce visibly different results instead of one recolored template.
- **`components.py`** turns each semantic element into a small DesignNode tree built with real Auto Layout (nested frames for page → header/sidebar → content → sections → cards, never manual per-child x/y where layout can express it instead), reading every color/size/weight from the screen's one `DesignTokens` instance. Repeated atoms that only differ by one text label (buttons in a group, badges in a row, title-only list rows) are deduplicated into a real Figma `component` + `instance`s instead of duplicated node trees.
- **`planner.build_semantic_plan`** assembles the full page (desktop 1440×1024 or mobile 375×812, chosen by a deterministic keyword classifier, never by the LLM) and remembers the `DesignTokens` it used — so a follow-up prompt like "now create a matching login screen" or "using the same design system" reuses the exact same visual style automatically.
- **`plan_validation.py`** runs before every execution and auto-corrects safe, recoverable problems (clamps out-of-range dimensions, de-duplicates top-level screen names, strips Auto Layout from a type that can't use it, nudges apart overlapping root elements, fills empty semantic text, repairs a dangling component/instance reference) while rejecting outright anything unrecoverably oversized — every correction is reported back in the result's `validation_notes`, never applied silently.

The original flat engine (`build_design_plan`) and its keyword templates (`_login_template`, `_dashboard_template`) are unchanged and still power `TemplatePlanner`, the offline fallback.

## Project structure

```
figma-mcp-server/
├── server.py            # MCP entry point, tool definitions
├── tools.py             # Tool implementation logic
├── config.py            # Environment variable loading + validation
├── bridge.py            # WebSocket relay server (token-authenticated, request/response correlation)
├── bridge_client.py     # Persistent, auto-reconnecting bridge client (request_id correlation, bounded backoff)
├── design_plan.py       # Recursive Pydantic schema (DesignPlan / DesignNode)
├── design_tokens.py     # Coherent design-token bundles (colors, typography, spacing, radius, shadows) per visual style
├── components.py        # Semantic composers (card/nav/sidebar/table/form/badge/...) built with Auto Layout
├── plan_validation.py   # Validates + auto-corrects a DesignPlan before execution
├── plan_executor.py     # Walks a DesignPlan, executes it via bridge_client, resolves component/instance refs
├── planner.py           # Planner interface + Ollama/Anthropic/Template backends, semantic layout engine v2
├── figma_client.py      # Figma REST API client (read-only)
├── figma-plugin/
│   ├── manifest.json
│   ├── code.js           # Plugin sandbox: canvas node creation, dispatch table, root-screen placement
│   └── ui.html            # Plugin UI iframe: owns the WebSocket connection, buffers results across reconnects
├── tests/                 # Script-based tests (no pytest) -- see "Development / testing" below
│   ├── test_local.py                          # MCP client <-> server (stdio)
│   ├── test_send_command.py                   # Live controller -> bridge -> plugin round-trip (manual check)
│   ├── test_generate_prompt.py                # Full Ollama -> planner -> canvas pipeline (manual check)
│   ├── test_bridge_auth.py                    # Bridge token authentication
│   ├── test_parent_allowlist.py               # parent_id session-scoped allowlisting
│   ├── test_config_validation.py              # Fail-fast config validation
│   ├── test_malformed_controller_input.py     # Malformed/invalid controller messages
│   ├── test_pending_request_cleanup.py        # Pending-request TTL + disconnect cleanup
│   ├── test_plan_size_timeout.py              # Plan size caps + execution timeout
│   ├── test_fail_closed_bind.py               # Non-loopback bind requires an explicit token
│   ├── test_plan_command_failure_isolation.py # One failed command isn't mistaken for a whole-plan timeout
│   ├── test_bridge_connection_reliability.py  # Persistent connection, reconnect/backoff, concurrency
│   ├── test_plugin_disconnect_cleanup.py      # Last-plugin-disconnect fails in-flight requests immediately
│   ├── test_full_regression_e2e.py            # End-to-end regression across all of the above
│   ├── test_design_tokens.py                  # All 8 visual styles load, are coherent, and are distinct
│   ├── test_components.py                     # Semantic composers use only existing primitives + Auto Layout
│   ├── test_plan_validation.py                # Every validation/normalization category + non-mutation
│   ├── test_planner_semantic.py               # Semantic engine, platform detection, style continuity
│   ├── test_regression_scenarios.py           # login/signup/dashboard/profile/settings/ecommerce/dark/mobile
│   ├── test_acceptance_phase12.py             # Full pipeline through a real bridge + recording fake plugin
│   └── test_plugin_placement.js               # Node-based unit test of the plugin's screen-placement math
├── requirements.txt
├── .env                   # Not committed — see Setup
└── .gitignore
```

## Setup

### 1. Python environment
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Environment variables
Create `.env` in the project root:
```
FIGMA_ACCESS_TOKEN=your_figma_personal_access_token
FIGMA_FILE_ID=your_figma_file_id
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5:3b
```
Figma token scopes needed: `file_content:read`, `file_metadata:read` (add `file_content:write`-equivalent scopes only if Figma exposes them for your use case — the current architecture creates nodes via the Plugin API, not the REST API, so no write scope is required for that path).

**Required:**

| Variable | Description |
|---|---|
| `FIGMA_ACCESS_TOKEN` | Figma personal access token. Server fails fast at startup if missing. |
| `FIGMA_FILE_ID` | Default Figma file key used by `get_figma_file_overview`. Server fails fast at startup if missing. |

**Optional — all have working defaults, only set these to override:**

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama server URL. |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Ollama model used by `OllamaPlanner`. |
| `BRIDGE_AUTH_TOKEN` | *(unset)* | Explicit shared secret for the bridge handshake. If unset, a token is auto-generated into a local `.bridge_token` file (gitignored) on first use. **Required** if you ever bind the bridge to a non-loopback host — see Known limitations. |
| `BRIDGE_CLIENT_URI` | `ws://localhost:8765` | Bridge address the MCP server connects to as a controller. |
| `BRIDGE_RESPONSE_TIMEOUT_SECONDS` | `10.0` | How long a single bridge command waits for a plugin response before raising `TimeoutError`. |
| `BRIDGE_CONNECT_MAX_ATTEMPTS` | `3` | Max connect/reconnect attempts per cycle (bounded — never retries forever). |
| `BRIDGE_RECONNECT_BASE_DELAY_SECONDS` | `0.5` | Starting delay for exponential backoff between reconnect attempts. |
| `BRIDGE_RECONNECT_MAX_DELAY_SECONDS` | `4.0` | Backoff delay ceiling. |
| `PLAN_EXECUTION_TIMEOUT_SECONDS` | `120` | Overall deadline for one `generate_screen`/`generate_ui_from_prompt` call, regardless of how many nodes it creates. |

None of these need to be set for normal local use — the defaults reproduce a working setup out of the box.

### 3. Ollama (local LLM, for `generate_ui_from_prompt`)
```powershell
ollama pull qwen2.5:3b
```
Ollama's server typically runs automatically after install; verify with:
```powershell
curl -UseBasicParsing http://localhost:11434/api/tags
```

### 4. Run the system (three things must run simultaneously)

| Terminal | Command |
|---|---|
| 1 | `python bridge.py` |
| Figma app (desktop, not browser) | Plugins → Development → Import plugin from manifest → select `figma-plugin/manifest.json`, then run it |
| 2 | `mcp dev server.py` (opens MCP Inspector for manual testing) |

### 5. Try it
In the Inspector, call `generate_ui_from_prompt` with:
```
a login screen with email, password, and a blue log in button
```
Watch the Figma canvas — a real frame with real nodes will appear within roughly 10–70 seconds depending on hardware.

## Development / testing

There is no `pytest`/CI setup — every file in `tests/` is a standalone script, run directly with the venv's Python interpreter and asserting real conditions with a non-zero exit code on failure:

```powershell
.\venv\Scripts\python.exe tests\test_bridge_auth.py
```

Two groups of tests exist:

- **Fully self-contained** (no live infra needed — each spins up its own bridge/fake plugin, or mocks the relevant layer directly): `test_bridge_auth.py`, `test_parent_allowlist.py`, `test_config_validation.py`, `test_malformed_controller_input.py`, `test_pending_request_cleanup.py`, `test_plan_size_timeout.py`, `test_fail_closed_bind.py`, `test_plan_command_failure_isolation.py`, `test_bridge_connection_reliability.py`, `test_plugin_disconnect_cleanup.py`, `test_full_regression_e2e.py`, `test_design_tokens.py`, `test_components.py`, `test_plan_validation.py`, `test_planner_semantic.py`, `test_regression_scenarios.py`, `test_acceptance_phase12.py`. These are safe to run anytime and are the closest thing this project has to a regression suite. `test_plugin_placement.js` (run with `node tests/test_plugin_placement.js`) covers the one piece of plugin-side logic that has no Python equivalent.
- **Manual, live checks** (require a real `python bridge.py` process, and sometimes a running Ollama or the actual Figma plugin open): `test_local.py`, `test_send_command.py`, `test_generate_prompt.py`. These print a clear pass/fail and exit non-zero on failure, but they exercise your actual local setup rather than a hermetic fixture — useful for confirming "does my machine actually work end to end," not for CI.

## Known limitations (honest, by design)

- **Child coordinates are absolute, not parent-relative** at the plan-authoring layer (they're always mediated by Auto Layout for anything `components.py` builds, so this mostly matters for a hand-built `DesignPlan` with manual positioning). `appendChild` in the Figma Plugin API doesn't auto-offset children to parent-local coordinates.
- **`create_icon` is a best-effort placeholder** (a small circle + 1-2 character label), not a real icon-library/SVG import. `create_variable` similarly depends on the Figma Variables API being available for the current file/plan.
- **One bridge connection at a time.** The bridge tracks a single plugin connection; a second plugin instance connecting concurrently would receive a duplicate broadcast of every command, not isolated per-user routing. This is fine for the current single-developer/single-designer use case; per-user session routing is a later-phase concern (see Roadmap).
- **Generic placeholder content when the user doesn't specify exact values.** If a prompt says "a checkout screen with a total price," the model may generate its own reasonable placeholder value (e.g. "$99.99") rather than leaving it blank — expected behavior for a mockup generator, not a bug, but worth knowing before a live demo with very open-ended prompts.
- **`OllamaPlanner` includes a retry loop (up to 2 attempts) and a semantic quality gate** that rejects output with placeholder-leakage before it reaches the canvas, plus a `plan_validation.py` pass that auto-corrects structural problems after the LLM output is turned into a plan. This meaningfully improved reliability, but is not a formal guarantee for arbitrary prompts — highly unusual or very complex requests may still occasionally exhaust retries and fall back to `TemplatePlanner` (which uses the original, simpler flat layout engine, not the semantic one). `AnthropicPlanner` (implemented and integration-tested, but not wired into `get_planner()` and not required) would substantially improve reliability further for arbitrary natural language if you choose to enable it.
- **Component/instance reuse only covers the single-text-difference case.** Repeated atoms where only one label varies (buttons in a group, badges in a row, title-only list rows) become a real Figma component + instances; anything with more than one varying part (icon+label, avatar+title+subtitle) is still composed as plain, fully independent frames — correct, but not deduplicated.
- **Style continuity ("now build a matching login screen") is process-wide, not per-conversation.** The last-used design tokens are remembered in a module-level variable, matching this project's existing single-shared-state model (one bridge token, one Figma file) rather than a real per-session/per-user memory.
- **The design-quality acceptance testing in this environment was structural, not visual** — see Phase 12 in the final report for the exact scope and why (no Figma desktop app is available in the sandbox this upgrade was built in).

## Project phase: this is Phase 1 (MCP foundation)

This repository is being built in explicit phases:

1. **Figma MCP foundation** (this repo, current phase) — the MCP server, planner, bridge, and Figma plugin pipeline described above, hardened for security and connection reliability.
2. **Import the existing Role-Based Access Control (RBAC) system** — built and demonstrated separately. Not part of this repository yet.
3. **Connect RBAC identity/authorization to this MCP server** — gate tool calls and bridge/plugin actions by authenticated user and role.
4. **Improve the combined system** based on what phases 2–3 reveal.

Nothing in this README describes RBAC as integrated, because it isn't yet. Single shared local token/config (see Setup) is expected and correct for Phase 1.

## Roadmap to production (NeGD / organizational deployment)

This is a validated working prototype, not yet a deployable service. Here's the honest gap, and what's already been closed today.

**Already addressed:**
- **Data sovereignty**: the LLM (`OllamaPlanner`) runs entirely locally — no prompt, design content, or Figma data is ever sent to a third-party API. This is usually the hardest compliance requirement for an AI tool in a government context, and this architecture satisfies it by design.
- **Audit trail**: every `generate_ui_from_prompt` call is now logged to `audit_log.jsonl` (timestamp, prompt, screen name, success/failure, node counts) via `audit_log.py`. Minimal but real — a production deployment would extend this to a centralized log store with real user identity attached.

**Required before any internal pilot (even 1–2 users):**
- **Real per-user authentication/authorization.** Currently a single shared Figma token in `.env` and a single shared bridge token. This is the RBAC import + connection work described in "Project phase" above (Phases 2–3) — intentionally not part of this repository yet.
- **A real deployment**, not three manually-started terminals on one laptop — the bridge needs to run as an always-on service (containerized, auto-restarting) that a team can actually rely on.
- **User-facing error messages.** Failures currently surface as raw Python tracebacks in a terminal; a pilot needs clean, actionable error messages instead.

**Required before multi-user rollout:**
- **Multi-connection bridge.** The current bridge tracks one plugin connection at a time; concurrent users would cross-talk or silently fail. Needs per-user session routing.
- **Per-user Figma file isolation.** Currently one shared `FIGMA_FILE_ID`; each user's generations need to go to their own file/session.
- **Automated test suite.** Everything verified so far was manual, interactive testing during development — a real deployment needs repeatable, automated regression tests.

**Valuable but not blocking:**
- Real icon-set/SVG asset import (currently a placeholder glyph).
- Per-conversation (not process-wide) design-system continuity memory.
- Broader component/variant support beyond the current single-text-difference reuse case.

**Closed in this upgrade** (previously listed here as open): multi-screen generation with real navigation/hierarchy, custom visual theming per request (8 distinct design systems, not one flat default), richer component/variant support for repeated atoms, and non-overlapping automatic multi-screen canvas placement.

None of the above requires redesigning the architecture — the MCP → Bridge → Plugin pipeline, the Planner abstraction, and the token-driven semantic layout engine all stay as-is. This is additive engineering work with a clear scope, not an open-ended unknown.

## What this demonstrates

This project validates the full architecture required for AI-driven Figma automation: an AI assistant can describe a UI in natural language and have it materialize as real, editable Figma nodes — not a static image, not a mockup, but actual frames/text/rectangles a designer can continue working with. The remaining path to a complete design-automation platform (more node-composite types, true auto-layout-driven positioning, a richer component/variant system, multi-screen flows) is additive work on top of a working, extensible foundation — not a redesign.
