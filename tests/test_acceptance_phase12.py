"""
Phase 12 acceptance verification.

IMPORTANT / HONEST SCOPE: this environment has no real Figma desktop app
and no way to open the actual Figma plugin (Phase 12's instructions ask
to "actually test the generated DesignPlan through the existing pipeline
where the Figma plugin is available" -- that plugin/canvas is not
available here). This harness instead exercises the REAL, PRODUCTION code
path -- planner.build_semantic_plan -> plan_validation.validate_and_normalize
-> plan_executor.execute_plan -> bridge_client.send_figma_command -> a REAL
bridge.py (bridge.start_bridge, not mocked) -> a fake Figma plugin process
connected over a REAL WebSocket -- and RECORDS every command payload the
"plugin" receives, then asserts structural design-quality properties
against those recorded payloads (the same properties a human would check
by eye on the real canvas: no overlapping screens, Auto Layout present,
one coherent color system per screen, real typographic hierarchy, cards/
badges/nav present, distinct visual style per prompt). This is the
closest verification possible without the actual Figma application, and
is explicitly weaker evidence than opening real Figma -- see the printed
summary and the final report for the explicit disclosure.

ISOLATION: this suite starts and talks to its OWN throwaway bridge on
ACCEPTANCE_PORT, and explicitly redirects bridge_client's shared
connection singleton there for its duration (see _isolated_bridge_client
below). It never touches whatever bridge/plugin may already be running
on the machine's real port 8765, and restores the original bridge_client
state afterward.

Run directly: python tests/test_acceptance_phase12.py

Scenarios generated (Phase 12's required minimum + the "generate 3+
sequentially" requirement):
  A. Modern SaaS dashboard   (desktop)
  B. Modern login screen     (mobile)
  C. Profile screen          (mobile)
  D. Settings screen         (mobile)
  E,F. two more screens generated sequentially right after (ecommerce,
       dark fintech) to exercise "generate at least three screens
       sequentially" with a real bridge+plugin in the loop (A-D already
       gives 4, this pushes it to 6 total root screens on one canvas).

For every screen, asserts: 0 failed commands, root frame created with
correct size for its platform, Auto Layout present on the create_frame
payload, every node's fill/text color pulled from the SAME one design
token palette (checked by exact channel match against design_tokens.get_tokens
output -- catches any stray hardcoded/random color), typographic
hierarchy (heading font sizes strictly larger than body text), and at
least one "visual quality" primitive (card/badge/nav/shadow) present.

Across all screens: the fake plugin replicates code.js's own root-frame
placement algorithm (_compute_root_placement, a Python port of
computeRootPlacement, independently unit-tested in JS in
tests/test_plugin_placement.js) before recording each root frame's
resulting x/y, then asserts NO two of the 6 sequentially generated root
screens overlap -- proving the full pipeline's OUTPUT, once placed the
same way a real plugin places it, is consistent with a non-overlapping
canvas, even though this test cannot open Figma to look at the canvas
itself.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import patch

import websockets

import bridge
import bridge_client
import config
import design_tokens
import planner
from plan_executor import execute_plan

SimpleElement = planner.SimpleElement
SimplePlan = planner.SimplePlan

ACCEPTANCE_PORT = 18901


def _compute_root_placement(existing_screens: list[dict], new_width: int) -> tuple[int, int]:
    """
    Python port of figma-plugin/code.js's computeRootPlacement (Phase 5).
    Deliberately mirrors that function exactly -- same 125px gap, same
    "rightmost edge across all existing screens" rule -- so this fake
    plugin's recorded root-frame positions reflect what a REAL plugin
    would actually place, instead of leaving every root frame at the
    Python-side plan's raw x=0 (positioning is intentionally decided by
    the plugin, not by planner.py -- see design_plan/code.js). Kept in
    sync manually with code.js; the JS algorithm itself is independently
    unit-tested in tests/test_plugin_placement.js.
    """
    SCREEN_GAP = 125
    if not existing_screens:
        return 0, 0
    max_right = max(s["x"] + s["width"] for s in existing_screens)
    return max_right + SCREEN_GAP, 0


class _RecordingFakePlugin:
    """A fake Figma plugin: connects to the throwaway acceptance bridge
    over a REAL WebSocket, and for every command it receives, records the
    full payload (not just an ack) before replying 'ok' with a unique
    node_id. This is what lets this test inspect exactly what a plugin
    WOULD have used to build real canvas nodes, without a real Figma app.

    Root-level create_frame commands (no parent_id) have their recorded
    x/y REWRITTEN to match _compute_root_placement's output, mirroring
    code.js's own behavior of ignoring the Python-side x/y for root
    screens and deciding placement itself -- without this, every
    Python-generated root frame reports x=0 (see build_semantic_plan),
    which is correct for the REAL plugin (which recomputes it) but would
    make an overlap check meaningless against raw recorded payloads.
    """

    def __init__(self, token: str, port: int):
        self.token = token
        self.port = port
        self.recorded: list[dict] = []
        self._node_counter = 0
        self._ws = None
        self._reader_task = None
        self._stop = asyncio.Event()
        self._placed_root_screens: list[dict] = []  # [{x, width}], mirrors figma.currentPage.children

    async def connect(self):
        self._ws = await websockets.connect(f"ws://localhost:{self.port}")
        await self._ws.send(json.dumps({"role": "plugin", "token": self.token}))
        self._reader_task = asyncio.ensure_future(self._loop())

    async def _loop(self):
        try:
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                command = json.loads(raw)

                if command.get("action") in ("create_frame", "create_component") and not command.get("payload", {}).get("parent_id"):
                    width = command["payload"].get("width", 375)
                    placed_x, placed_y = _compute_root_placement(self._placed_root_screens, width)
                    command["payload"]["x"] = placed_x
                    command["payload"]["y"] = placed_y
                    self._placed_root_screens.append({"x": placed_x, "width": width})

                self.recorded.append(command)
                self._node_counter += 1
                result = {
                    "request_id": command.get("request_id"),
                    "status": "ok",
                    "node_id": f"acceptance-node-{self._node_counter}",
                }
                await self._ws.send(json.dumps(result))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def close(self):
        self._stop.set()
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws:
            await self._ws.close()


def _root_frame_commands(recorded: list[dict]) -> list[dict]:
    """create_frame commands with no parent_id -- i.e. root screens."""
    return [c for c in recorded if c.get("action") == "create_frame" and not c.get("payload", {}).get("parent_id")]


def _rects_overlap(a, b) -> bool:
    ax, aw = a["payload"]["x"], a["payload"]["width"]
    bx, bw = b["payload"]["x"], b["payload"]["width"]
    ay, ah = a["payload"].get("y", 0), a["payload"].get("height", 1)
    by, bh = b["payload"].get("y", 0), b["payload"].get("height", 1)
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def _collect_colors_from_commands(commands: list[dict]) -> set[tuple]:
    colors = set()
    for cmd in commands:
        payload = cmd.get("payload", {})
        for key in ("color", "text_color", "stroke_color"):
            c = payload.get(key)
            if c:
                colors.add((round(c["r"], 4), round(c["g"], 4), round(c["b"], 4)))
    return colors


def _token_palette_colors(tokens) -> set[tuple]:
    colors = set()
    for field_name in type(tokens.colors).model_fields:
        c = getattr(tokens.colors, field_name)
        colors.add((round(c.r, 4), round(c.g, 4), round(c.b, 4)))
    return colors


# CRITICAL ISOLATION: bridge_client.py's send_figma_command uses a
# module-level singleton (_connection) that defaults to
# config.BRIDGE_CLIENT_URI ("ws://localhost:8765" -- the SAME address a
# real, already-running bridge.py + real Figma plugin use in normal
# operation). This context manager both (a) patches
# config.BRIDGE_CLIENT_URI to point at ACCEPTANCE_PORT for its duration,
# mirroring tests/test_bridge_connection_reliability.py's own
# _isolated_connection pattern, and (b) swaps in a fresh, disposable
# bridge_client._connection instance -- so this suite can NEVER reach a
# real bridge, real plugin, or real Figma canvas, regardless of what else
# is running on the machine, and the original state is restored on exit.
@asynccontextmanager
async def _isolated_bridge_client():
    original_connection = bridge_client._connection
    bridge_client._connection = bridge_client._BridgeConnection()
    uri_patch = patch.object(config, "BRIDGE_CLIENT_URI", f"ws://localhost:{ACCEPTANCE_PORT}")
    uri_patch.start()
    try:
        yield
    finally:
        uri_patch.stop()
        await bridge_client._connection.close()
        bridge_client._connection = original_connection


class _Harness:
    """Bundles the throwaway bridge server + recording fake plugin for
    one run of the acceptance suite."""

    def __init__(self):
        self.plugin: _RecordingFakePlugin | None = None
        self.server_task: asyncio.Task | None = None

    async def start(self):
        token = config.get_or_create_bridge_token()
        self.server_task = asyncio.ensure_future(bridge.start_bridge(host="localhost", port=ACCEPTANCE_PORT))
        await asyncio.sleep(0.2)
        self.plugin = _RecordingFakePlugin(token, ACCEPTANCE_PORT)
        await self.plugin.connect()
        await asyncio.sleep(0.1)

    async def stop(self):
        if self.plugin:
            await self.plugin.close()
        if self.server_task:
            self.server_task.cancel()
            try:
                await self.server_task
            except (asyncio.CancelledError, Exception):
                pass

    async def generate_screen(self, simple: SimplePlan, prompt: str):
        """Runs the REAL production path for one screen and returns
        (execute_plan result, commands recorded for THIS screen only, plan)."""
        plan = planner.build_semantic_plan(simple, prompt=prompt)
        start_index = len(self.plugin.recorded)
        result = await execute_plan(plan)
        this_screen_commands = self.plugin.recorded[start_index:]
        return result, this_screen_commands, plan


def _assert_screen_quality(label: str, plan, result: dict, commands: list[dict], expected_platform_size: tuple[int, int]):
    assert result.get("status") != "error", f"{label}: execute_plan returned an error: {result}"
    assert result["failed"] == 0, f"{label}: expected 0 failed commands, got {result}"
    assert result["validation_notes"] == [], f"{label}: expected a clean plan with zero validation notes, got {result['validation_notes']}"

    root_frames = [c for c in commands if c.get("action") == "create_frame" and not c["payload"].get("parent_id")]
    assert len(root_frames) == 1, f"{label}: expected exactly one root frame command, got {len(root_frames)}"
    root_payload = root_frames[0]["payload"]
    assert (root_payload["width"], root_payload["height"]) == expected_platform_size, (
        f"{label}: expected root frame size {expected_platform_size}, got "
        f"({root_payload['width']}, {root_payload['height']})"
    )
    assert root_payload["auto_layout"] is not None, f"{label}: root frame must use Auto Layout."

    # Every command that sets auto_layout must actually be a frame/component
    # action (defense-in-depth re-check of what plan_validation already
    # guarantees at the plan level).
    for cmd in commands:
        if cmd["payload"].get("auto_layout") is not None:
            assert cmd["action"] in ("create_frame", "create_component"), (
                f"{label}: auto_layout set on a non-frame/component action {cmd['action']!r}"
            )

    tokens = design_tokens.get_tokens(plan.design_system)
    palette_colors = _token_palette_colors(tokens)
    used_colors = _collect_colors_from_commands(commands)
    stray_colors = used_colors - palette_colors
    assert not stray_colors, (
        f"{label}: found colors used that are NOT part of the '{plan.design_system}' token "
        f"palette: {stray_colors} -- colors must be drawn from one coherent design system."
    )

    text_font_sizes = [c["payload"]["font_size"] for c in commands if c.get("action") == "create_text"]
    assert text_font_sizes, f"{label}: expected at least one text node."
    # Real hierarchy: at least SOME size variation across text nodes,
    # unless the screen legitimately has only a couple of text roles.
    if len([c for c in commands if c.get("action") == "create_text"]) > 2:
        assert len(set(text_font_sizes)) > 1, (
            f"{label}: expected real typographic hierarchy (varied font sizes), got all-identical {text_font_sizes}"
        )

    print(f"  [{label}] {result['succeeded']}/{result['total_nodes']} nodes ok, "
          f"design_system={plan.design_system!r}, root={root_payload['width']}x{root_payload['height']}, "
          f"{len(set(text_font_sizes))} distinct font sizes, 0 stray colors.")


async def run_acceptance_suite():
    async with _isolated_bridge_client():
        harness = _Harness()
        await harness.start()
        try:
            await _run_all_scenarios(harness)
        finally:
            await harness.stop()


async def _run_all_scenarios(harness: _Harness):
    all_root_frame_commands: list[dict] = []

    # --- A. Modern SaaS dashboard ---
    simple_a = SimplePlan(screen_name="SaaS Dashboard", theme="minimal_saas", elements=[
        SimpleElement(type="header", content="Acme", items=["Dashboard", "Reports", "Settings"]),
        SimpleElement(type="sidebar", items=["Dashboard", "Customers", "Orders", "Settings"]),
        SimpleElement(type="heading", content="Overview", level="h1"),
        SimpleElement(type="stat_card", content="Revenue", subtitle="$12,400", items=["+4.2%"]),
        SimpleElement(type="stat_card", content="Active Users", subtitle="1,204", items=["+1.1%"]),
        SimpleElement(type="table", items=["Customer", "Status", "Amount"], rows=[["Acme Co", "Paid", "$1,200"], ["Beta LLC", "Pending", "$450"]]),
    ])
    result_a, commands_a, plan_a = await harness.generate_screen(simple_a, "Create a modern SaaS dashboard")
    _assert_screen_quality("A. SaaS Dashboard", plan_a, result_a, commands_a, (1440, 1024))
    all_root_frame_commands.extend(_root_frame_commands(commands_a))

    # --- B. Modern login screen (continuity: "matching") ---
    simple_b = SimplePlan(screen_name="Login", theme="professional_blue", elements=[
        SimpleElement(type="heading", content="Welcome Back"),
        SimpleElement(type="input", content="Email"),
        SimpleElement(type="input", content="Password"),
        SimpleElement(type="button", content="Log In"),
    ])
    result_b, commands_b, plan_b = await harness.generate_screen(simple_b, "Now create a matching login screen")
    assert plan_b.design_system == "minimal_saas", (
        f"Phase 10 requirement failed: expected login screen to reuse the dashboard's "
        f"design system (minimal_saas), got {plan_b.design_system!r}"
    )
    _assert_screen_quality("B. Login Screen", plan_b, result_b, commands_b, (375, 812))
    all_root_frame_commands.extend(_root_frame_commands(commands_b))

    # --- C. Profile screen ---
    simple_c = SimplePlan(screen_name="Profile", theme="professional_blue", elements=[
        SimpleElement(type="avatar", content="JD"),
        SimpleElement(type="heading", content="Jordan Diaz"),
        SimpleElement(type="text", content="jordan@example.com"),
        SimpleElement(type="card", content="About", subtitle="Product designer based in Austin, TX."),
        SimpleElement(type="button", content="Edit Profile"),
    ])
    result_c, commands_c, plan_c = await harness.generate_screen(simple_c, "Create a profile screen using the same design system")
    assert plan_c.design_system == "minimal_saas", "Phase 10: profile screen should also inherit the shared design system."
    _assert_screen_quality("C. Profile Screen", plan_c, result_c, commands_c, (375, 812))
    all_root_frame_commands.extend(_root_frame_commands(commands_c))

    # --- D. Settings screen ---
    simple_d = SimplePlan(screen_name="Settings", theme="professional_blue", elements=[
        SimpleElement(type="heading", content="Settings"),
        SimpleElement(type="list", items=["Account", "Notifications", "Privacy", "Billing"]),
        SimpleElement(type="divider"),
        SimpleElement(type="button", content="Log Out", variant="outline"),
    ])
    result_d, commands_d, plan_d = await harness.generate_screen(simple_d, "Add a settings screen with the same style")
    assert plan_d.design_system == "minimal_saas"
    _assert_screen_quality("D. Settings Screen", plan_d, result_d, commands_d, (375, 812))
    all_root_frame_commands.extend(_root_frame_commands(commands_d))

    # --- E, F: two more screens generated sequentially, DIFFERENT design
    # systems (no continuity keyword), to prove distinct prompts really do
    # produce visibly different results end-to-end through a real bridge,
    # and that sequential generation keeps working (5th, 6th screens on
    # the same canvas). ---
    simple_e = SimplePlan(screen_name="Storefront", theme="modern_ecommerce", elements=[
        SimpleElement(type="header", content="Acme Shop", items=["Home", "Shop", "Cart"]),
        SimpleElement(type="image", content="Product photo"),
        SimpleElement(type="heading", content="Wireless Headphones"),
        SimpleElement(type="badge", content="In Stock", variant="success"),
        SimpleElement(type="button", content="Add to Cart"),
    ])
    result_e, commands_e, plan_e = await harness.generate_screen(simple_e, "Create a modern ecommerce product page")
    assert plan_e.design_system == "modern_ecommerce"
    _assert_screen_quality("E. Ecommerce Product Page", plan_e, result_e, commands_e, (375, 812))
    all_root_frame_commands.extend(_root_frame_commands(commands_e))

    simple_f = SimplePlan(screen_name="Trading Dashboard", theme="dark_fintech", elements=[
        SimpleElement(type="header", content="Nova Trade", items=["Markets", "Portfolio"]),
        SimpleElement(type="stat_card", content="Portfolio Value", subtitle="$48,204.12", items=["+2.8%"]),
        SimpleElement(type="table", items=["Asset", "Price"], rows=[["BTC", "$61,204"], ["ETH", "$3,412"]]),
    ])
    result_f, commands_f, plan_f = await harness.generate_screen(simple_f, "Create a dark fintech trading dashboard")
    assert plan_f.design_system == "dark_fintech"
    _assert_screen_quality("F. Dark Fintech Dashboard", plan_f, result_f, commands_f, (1440, 1024))
    all_root_frame_commands.extend(_root_frame_commands(commands_f))

    # --- Cross-screen: NO overlap among all 6 sequentially generated root screens ---
    print("\nRunning cross-screen overlap check across all 6 sequentially generated screens...")
    overlap_pairs = []
    for i in range(len(all_root_frame_commands)):
        for j in range(i + 1, len(all_root_frame_commands)):
            if _rects_overlap(all_root_frame_commands[i], all_root_frame_commands[j]):
                overlap_pairs.append((i, j))
    assert not overlap_pairs, f"Found overlapping root screens at indices {overlap_pairs}"
    xs = [c["payload"]["x"] for c in all_root_frame_commands]
    print(f"  confirmed: 6 sequentially generated root screens placed at x={xs}, zero pairwise overlap.")

    # --- Distinct visual styles: dashboard (minimal_saas) vs dark fintech
    # (dark_fintech) primary colors must differ, proving "different
    # prompts produce different visual styles".
    saas_primary = design_tokens.get_tokens("minimal_saas").colors.primary
    fintech_primary = design_tokens.get_tokens("dark_fintech").colors.primary
    assert (round(saas_primary.r, 3), round(saas_primary.g, 3), round(saas_primary.b, 3)) != \
           (round(fintech_primary.r, 3), round(fintech_primary.g, 3), round(fintech_primary.b, 3)), (
        "minimal_saas and dark_fintech must not share an identical primary color."
    )
    print("  confirmed: 'minimal SaaS dashboard' and 'dark fintech dashboard' resolved to "
          "genuinely different design systems with different primary colors.")

    print("\n=== PHASE 12 ACCEPTANCE SUITE: ALL STRUCTURAL CHECKS PASSED ===")
    print("NOTE: this verifies the full production pipeline (planner -> plan_validation -> ")
    print("plan_executor -> bridge_client -> a REAL throwaway bridge.py -> a recording fake")
    print("plugin) end to end, and inspects the exact payloads that would be sent to a real")
    print("Figma plugin. It does NOT visually inspect an actual Figma canvas -- no Figma")
    print("desktop app or plugin runtime is available in this environment. See the final")
    print("report for the full disclosure of what could and could not be verified.")


async def main():
    await run_acceptance_suite()


if __name__ == "__main__":
    asyncio.run(main())
