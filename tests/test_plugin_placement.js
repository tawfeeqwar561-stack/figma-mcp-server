/**
 * Node-based unit test for figma-plugin/code.js's computeRootPlacement()
 * (Phase 5: automatic canvas positioning).
 *
 * code.js exports this ONE pure, figma-API-free function specifically so
 * it can be verified outside a real Figma plugin sandbox (which requires
 * the actual Figma desktop app and cannot run in a CI/terminal
 * environment). Everything else in code.js calls real figma.* APIs and is
 * NOT covered here -- this is a narrow, mechanical check that the
 * placement math itself is correct for both frames and any other
 * "screen-like" root type width, addressing the specific bug this phase
 * targets (root-level non-frame nodes previously bypassed placement
 * entirely and could overlap).
 *
 * Run directly: node tests/test_plugin_placement.js
 * Exits non-zero on any failed assertion.
 */

const assert = require("assert");
const path = require("path");

const { computeRootPlacement } = require(path.join(__dirname, "..", "figma-plugin", "code.js"));

function test_first_screen_placed_at_origin() {
  const result = computeRootPlacement([], 375);
  assert.deepStrictEqual(result, { x: 0, y: 0 }, "First screen (no existing screens) must be placed at (0, 0).");
  console.log("  confirmed: with no existing screens, placement is (0, 0).");
}

function test_second_screen_placed_right_of_first_with_gap() {
  const existing = [{ x: 0, width: 375 }];
  const result = computeRootPlacement(existing, 375);
  assert.strictEqual(result.x, 375 + 125, `Expected x = 500 (375 width + 125 gap), got ${result.x}`);
  assert.strictEqual(result.y, 0);
  console.log("  confirmed: second screen placed at rightmost.x + rightmost.width + 125 gap.");
}

function test_uses_rightmost_screen_not_just_last_in_list() {
  // Screens are not necessarily processed/stored in x-order -- the
  // function must find the actual rightmost edge, not just look at the
  // last array entry.
  const existing = [
    { x: 800, width: 400 }, // rightmost: right edge = 1200
    { x: 0, width: 375 },
    { x: 400, width: 300 },
  ];
  const result = computeRootPlacement(existing, 1440);
  assert.strictEqual(result.x, 1200 + 125, `Expected x = 1325, got ${result.x}`);
  console.log("  confirmed: placement uses the true rightmost edge across all existing screens, not array order.");
}

function test_works_for_any_screen_width_not_hardcoded() {
  // A 1440px desktop screen next to a 375px mobile screen -- placement
  // must never hardcode one screen size (Phase 5 requirement).
  const existingMobile = [{ x: 0, width: 375 }];
  const desktopResult = computeRootPlacement(existingMobile, 1440);
  assert.strictEqual(desktopResult.x, 375 + 125);

  const existingDesktop = [{ x: 0, width: 1440 }];
  const mobileResult = computeRootPlacement(existingDesktop, 375);
  assert.strictEqual(mobileResult.x, 1440 + 125);

  console.log("  confirmed: placement math is size-agnostic -- works identically for mobile (375) and desktop (1440) widths, in either order.");
}

function test_four_sequential_screens_never_overlap() {
  // End-to-end simulation of "Generate dashboard, login, profile,
  // settings" sequentially -- the exact example from the task's Phase 5.
  const widths = [1440, 375, 375, 375]; // dashboard (desktop), login/profile/settings (mobile)
  const placedScreens = [];

  for (const width of widths) {
    const placement = computeRootPlacement(placedScreens, width);
    // Check the new screen's rect doesn't overlap ANY previously placed one.
    for (const prior of placedScreens) {
      const overlap = placement.x < prior.x + prior.width && placement.x + width > prior.x;
      assert.strictEqual(overlap, false, `New screen at x=${placement.x} (width=${width}) overlapped prior screen at x=${prior.x} (width=${prior.width})`);
    }
    placedScreens.push({ x: placement.x, width });
  }

  assert.strictEqual(placedScreens.length, 4);
  console.log(`  confirmed: 4 sequentially "generated" screens (widths ${widths.join(", ")}) placed at x=[${placedScreens.map((s) => s.x).join(", ")}] with zero overlap.`);
}

function test_internal_marker_frame_excluded() {
  // The caller (placeRootNodeIfNeeded in code.js) filters out any node
  // named "__MCP_INTERNAL__" before calling computeRootPlacement -- this
  // test documents that computeRootPlacement itself is agnostic to that
  // filtering (it just trusts whatever list it's given), so the
  // exclusion responsibility stays where it belongs (the caller, which
  // has access to node.name; this pure function only ever receives
  // {x, width} pairs).
  const existing = [{ x: 0, width: 375 }]; // caller already filtered out any internal marker
  const result = computeRootPlacement(existing, 375);
  assert.strictEqual(result.x, 500);
  console.log("  confirmed: computeRootPlacement trusts the caller's pre-filtered list (marker-frame exclusion is the caller's responsibility, verified separately by code review of placeRootNodeIfNeeded).");
}

function main() {
  const tests = [
    test_first_screen_placed_at_origin,
    test_second_screen_placed_right_of_first_with_gap,
    test_uses_rightmost_screen_not_just_last_in_list,
    test_works_for_any_screen_width_not_hardcoded,
    test_four_sequential_screens_never_overlap,
    test_internal_marker_frame_excluded,
  ];

  for (const test of tests) {
    console.log(`Running ${test.name}...`);
    test();
    console.log(`${test.name}: PASSED\n`);
  }
  console.log("All test_plugin_placement checks completed.");
}

main();
