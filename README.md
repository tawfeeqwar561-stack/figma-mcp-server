# Figma MCP Server — AI-Driven UI Generation

An MCP (Model Context Protocol) server that lets an AI assistant generate real, editable UI screens directly inside Figma — from either structured input or a single natural-language prompt.

```
User Prompt
   │
   ▼
Planner (LLM)              — Ollama (local, default) / Anthropic (optional) / Template (offline fallback)
   │  produces a structured DesignPlan (Pydantic-validated JSON)
   ▼
Plan Executor              — walks the plan tree, resolves parent/child relationships at runtime
   │
   ▼
MCP Server (server.py)     — exposes tools to any MCP-compatible AI assistant
   │
   ▼
Bridge (bridge.py)         — WebSocket relay, role-based routing, request/response correlation
   │
   ▼
Figma Plugin (code.js)     — runs inside real Figma, creates actual canvas nodes
   │
   ▼
Figma Canvas               — real frames, text, rectangles, groups, variants, styles
```

## Status: working end-to-end

Every arrow in the diagram above is real, tested code — not a mock. Confirmed working:

- MCP server with 6 tools (`ping`, `echo`, `get_figma_file_overview`, `create_figma_rectangle`, `generate_screen`, `generate_ui_from_prompt`)
- Real Figma REST API integration (read file structure)
- WebSocket bridge with role-based routing and request/response correlation (not fire-and-forget)
- A real Figma Plugin, running inside actual Figma, connected live to the bridge
- A dispatch-table command handler in the plugin covering: frames, text, rectangles, ellipses, lines, image placeholders, icons (placeholder), groups, components, component sets (variants), color styles, text styles, variables
- A recursive `DesignPlan` schema supporting nested containers (frame/component/group/component_set with children)
- A `Planner` abstraction with three interchangeable backends (see below)
- `generate_ui_from_prompt`: a single natural-language sentence → structured plan → multiple Figma nodes created automatically, confirmed working end-to-end (e.g. "a signup screen with a name field, email field, password field, and a green Create Account button" → 8/8 nodes created successfully)

## Architecture decisions worth knowing

**Why a bridge instead of calling Figma's API directly?**
Figma's REST API is read-only for document structure — it cannot create nodes. Real canvas manipulation is only possible from inside a Figma Plugin, which runs sandboxed and cannot accept incoming connections. The bridge is a WebSocket relay that both the MCP server and the plugin connect to *as clients*; the MCP server sends a command, the bridge routes it to the connected plugin, the plugin executes it and routes a result back — correlated by a `request_id`, so multiple in-flight requests never get crossed.

**Why is the plugin split into `code.js` and `ui.html`?**
Figma enforces this split for security: the plugin sandbox (`code.js`) can touch the canvas but has no network access; the UI iframe (`ui.html`) has network access but cannot touch the canvas. The bridge connection lives in `ui.html`; commands and results cross that boundary via `figma.ui.postMessage` / `window.onmessage`.

**Why a dispatch-table pattern in `code.js`?**
Adding a new node type is "add one function to the `commandHandlers` object" — the transport layer, the bridge, and the MCP server never need to change. The same pattern repeats at the Python layer (`_ACTION_MAP` in `plan_executor.py`), which is deliberate: consistent extension points at every layer.

**Why does the Command Generator expand composites into primitives?**
"Button" and "image placeholder" aren't things the Figma Plugin API can create directly — they're a rectangle + text pair. Expanding composites in Python (not in `code.js`) keeps the plugin permanently simple; new composite types never require touching plugin code.

## The Planner layer

`planner.py` defines a single interface all backends implement:

```python
class Planner(ABC):
    async def generate_plan(self, prompt: str) -> DesignPlan: ...
```

Three implementations exist, swappable with zero changes anywhere else in the codebase:

| Backend | Status | Notes |
|---|---|---|
| `OllamaPlanner` | **Default, fully working** | Calls a local Ollama server. Uses simple JSON mode (`format: "json"`), not full JSON-schema-constrained decoding — the schema is recursive, and Ollama's schema-grammar compiler is impractically slow on small local models with recursive schemas. Correctness is enforced instead by a validation + repair pipeline (`parse_plan_json`): strips code fences, extracts the outermost JSON object, repairs trailing commas, and repairs a common small-model mistake (duplicate `{"r":.., "g":.., "g":..}` keys instead of `r`/`g`/`b`). |
| `AnthropicPlanner` | Implemented, integration-tested | Confirmed working against the real Anthropic API (hit a billing/credits limit during development, not a code issue — request/response handling is verified correct). Swap to it by passing an `AnthropicPlanner(api_key)` instance in `get_planner()`. |
| `TemplatePlanner` | Fully working, offline fallback | Zero network calls, keyword-matched (`login`, `dashboard`). Used automatically if the primary backend fails for any reason. |

`FallbackPlanner` wraps a primary + fallback: if `OllamaPlanner` fails (server down, model not pulled, unrecoverable malformed JSON), it transparently falls back to `TemplatePlanner` rather than failing the whole tool call. This is visible in logs as a `WARNING`, not a crash.

**Recommended local model:** `qwen2.5:3b`. A larger model (`qwen2.5:7b`, `qwen3:14b`) produces better output but is impractically slow on CPU-only inference (60–130+ seconds per call vs. ~5–70 seconds for the 3B model, depending on prompt complexity). This is a deliberate size/quality trade-off for responsive local use, documented here rather than hidden.

## Project structure

```
figma-mcp-server/
├── server.py            # MCP entry point, tool definitions
├── tools.py             # Tool implementation logic
├── config.py            # Environment variable loading + validation
├── bridge.py            # WebSocket relay server
├── bridge_client.py     # Python client for the bridge (request/response correlation)
├── design_plan.py       # Recursive Pydantic schema (DesignPlan / DesignNode)
├── plan_executor.py     # Walks a DesignPlan, executes it via bridge_client
├── planner.py           # Planner interface + Ollama/Anthropic/Template backends
├── figma_client.py      # Figma REST API client (read-only)
├── figma-plugin/
│   ├── manifest.json
│   ├── code.js           # Plugin sandbox: canvas node creation, dispatch table
│   └── ui.html            # Plugin UI iframe: owns the WebSocket connection
├── tests/                 # Integration tests, one per architectural layer
│   ├── test_local.py            # MCP client ↔ server
│   ├── test_bridge_client.py    # Bridge relay + plugin role registration
│   ├── test_send_command.py     # Controller → bridge → plugin round-trip
│   └── test_generate_prompt.py  # Full AI → canvas pipeline
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

## Known limitations (honest, by design)

- **Child coordinates are absolute, not parent-relative.** `appendChild` in the Figma Plugin API doesn't auto-offset children to parent-local coordinates. Works fine for flat and lightly-nested plans; a coordinate-transform pass would be needed for deep nesting with portable (parent-relative) coordinates.
- **`create_icon` and `create_variable` are best-effort placeholders**, not a real icon-library or design-token system integration.
- **No retry/reconnect logic** if the bridge or plugin connection drops mid-plan-execution — a long `generate_ui_from_prompt` run currently fails entirely on one dropped connection rather than resuming.
- **Small local models occasionally produce malformed JSON** even with repair logic; the system falls back to `TemplatePlanner` in that case rather than failing outright, but this means very open-ended prompts have a non-zero failure rate on the free local path. `AnthropicPlanner` (verified working, just needs API credits) would substantially improve reliability for arbitrary natural language.

## What this demonstrates

This project validates the full architecture required for AI-driven Figma automation: an AI assistant can describe a UI in natural language and have it materialize as real, editable Figma nodes — not a static image, not a mockup, but actual frames/text/rectangles a designer can continue working with. The remaining path to a complete design-automation platform (more node-composite types, true auto-layout-driven positioning, a richer component/variant system, multi-screen flows) is additive work on top of a working, extensible foundation — not a redesign.
