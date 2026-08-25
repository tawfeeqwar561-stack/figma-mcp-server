// Guarded so this file can be `require()`d from plain Node (see
// tests/test_plugin_placement.js) to unit-test computeRootPlacement()
// without a real Figma plugin sandbox, where the `figma`/`__html__`
// globals don't exist. Inside the actual Figma plugin runtime, `figma`
// is always defined and this behaves exactly as before.
if (typeof figma !== "undefined") {
  figma.showUI(__html__, { width: 300, height: 100 });
}

// ---- Shared helpers ----------------------------------------------------

function resolveParent(parent_id) {
  if (parent_id) {
    const node = figma.getNodeById(parent_id);
    if (node) return node;
  }
  return figma.currentPage;
}

function applyFill(node, color) {
  if (color) {
    node.fills = [{ type: "SOLID", color: { r: color.r, g: color.g, b: color.b } }];
  }
}

function applyEffects(node, effects) {
  if (!effects || effects.length === 0) return;
  node.effects = effects.map((e) => ({
    type: e.type,
    color: e.color ? { r: e.color.r, g: e.color.g, b: e.color.b, a: e.opacity ?? 0.25 } : { r: 0, g: 0, b: 0, a: e.opacity ?? 0.25 },
    offset: { x: e.offset_x ?? 0, y: e.offset_y ?? 2 },
    radius: e.radius ?? 4,
    visible: true,
    blendMode: "NORMAL",
  }));
}

function applyConstraints(node, constraints) {
  if (!constraints) return;
  if ("constraints" in node) {
    node.constraints = { horizontal: constraints.horizontal, vertical: constraints.vertical };
  }
}

// Additive: node opacity (0-1). No-op if not provided, so any payload
// built before this field existed renders identically (fully opaque).
function applyOpacity(node, opacity) {
  if (typeof opacity === "number" && "opacity" in node) {
    node.opacity = opacity;
  }
}

// Additive: border/stroke support. No-op if no stroke_color is given.
function applyStroke(node, strokeColor, strokeWeight) {
  if (strokeColor && "strokes" in node) {
    node.strokes = [{ type: "SOLID", color: { r: strokeColor.r, g: strokeColor.g, b: strokeColor.b } }];
    if ("strokeWeight" in node) {
      node.strokeWeight = strokeWeight || 1;
    }
  }
}

// Additive: how a node behaves as a child of an auto-layout parent
// (Figma's layoutAlign/layoutGrow). No-op if not provided -- existing
// payloads that never set these are completely unaffected.
function applyChildLayoutProps(node, layoutAlign, layoutGrow) {
  if (layoutAlign && "layoutAlign" in node) {
    node.layoutAlign = layoutAlign;
  }
  if (typeof layoutGrow === "number" && layoutGrow > 0 && "layoutGrow" in node) {
    node.layoutGrow = layoutGrow;
  }
}

function applyAutoLayout(frameNode, autoLayout) {
  if (!autoLayout) return;
  frameNode.layoutMode = autoLayout.direction;
  frameNode.itemSpacing = autoLayout.spacing ?? 8;

  // Per-side padding overrides, falling back to the uniform `padding`
  // value -- identical result to the original code for any payload that
  // only ever set `padding` (padding_top/right/bottom/left absent/null).
  const basePadding = autoLayout.padding ?? 16;
  frameNode.paddingTop = autoLayout.padding_top ?? basePadding;
  frameNode.paddingBottom = autoLayout.padding_bottom ?? basePadding;
  frameNode.paddingLeft = autoLayout.padding_left ?? basePadding;
  frameNode.paddingRight = autoLayout.padding_right ?? basePadding;

  const alignMap = { MIN: "MIN", CENTER: "CENTER", MAX: "MAX", SPACE_BETWEEN: "SPACE_BETWEEN" };
  frameNode.primaryAxisAlignItems = alignMap[autoLayout.align_items] || "MIN";

  // Additive knobs (counter-axis alignment, sizing modes). Any payload
  // built before these existed omits them; the `||` fallbacks below match
  // this file's previous, implicit behavior (Figma's own default of
  // "hug primary / fixed counter" once layoutMode is turned on) as
  // closely as a sensible default allows.
  frameNode.counterAxisAlignItems = autoLayout.counter_axis_align || "MIN";
  frameNode.primaryAxisSizingMode = autoLayout.primary_axis_sizing || "AUTO";
  frameNode.counterAxisSizingMode = autoLayout.counter_axis_sizing || "FIXED";
}

async function loadDefaultFont() {
  await figma.loadFontAsync({ family: "Inter", style: "Regular" });
}

// ---------------------------------------------------------------------
// Root screen placement (Phase 5: automatic canvas positioning)
// ---------------------------------------------------------------------
//
// Pure function, no figma.* calls -- deliberately factored out so it can
// be unit-tested directly from Node (see tests/test_plugin_placement.js)
// without a real Figma plugin sandbox.
//
// Places a new root-level "screen" to the right of the current rightmost
// existing root node, with a fixed safe gap. Given ANY existing root's
// {x, width}, works for a screen of any width (never hardcodes one
// screen size).
function computeRootPlacement(existingScreens, newWidth) {
  const SCREEN_GAP = 125;
  if (!existingScreens || existingScreens.length === 0) {
    return { x: 0, y: 0 };
  }
  let maxRight = 0;
  for (const existing of existingScreens) {
    const right = existing.x + existing.width;
    if (right > maxRight) {
      maxRight = right;
    }
  }
  return { x: maxRight + SCREEN_GAP, y: 0 };
}

// Applies computeRootPlacement to any newly-created top-level node
// (frame OR component -- previously ONLY frame was auto-arranged here,
// so a root-level component could silently overlap prior screens; this
// generalizes the fix to any "screen-like" root type). Nested/child
// nodes always keep the coordinates the layout engine assigned, exactly
// as before. Returns the resolved parent so the caller can appendChild.
function placeRootNodeIfNeeded(node, payload) {
  const { x, y, width, parent_id } = payload;
  const parent = resolveParent(parent_id);

  if (!parent_id && parent === figma.currentPage) {
    const existingScreens = figma.currentPage.children
      .filter((n) => n !== node && n.name !== "__MCP_INTERNAL__")
      .map((n) => ({ x: n.x, width: n.width }));

    const placement = computeRootPlacement(existingScreens, width || node.width || 375);
    node.x = placement.x;
    node.y = placement.y;
  } else {
    // Child node: never override the AI/layout-engine coordinates.
    node.x = x || 0;
    node.y = y || 0;
  }

  return parent;
}

// ---- Command handlers ---------------------------------------------------

const commandHandlers = {
  ping_plugin: () => ({ status: "ok", message: "pong" }),

  create_frame: (payload) => {
    const {
      width, height, name, color, auto_layout, constraints, effects,
      opacity, stroke_color, stroke_weight, layout_align, layout_grow,
    } = payload;

    const frame = figma.createFrame();
    frame.resize(width || 375, height || 812);
    frame.name = name || "Frame";

    applyFill(frame, color);
    applyStroke(frame, stroke_color, stroke_weight);
    applyOpacity(frame, opacity);
    applyAutoLayout(frame, auto_layout);
    applyEffects(frame, effects);

    const parent = placeRootNodeIfNeeded(frame, payload);
    parent.appendChild(frame);

    applyConstraints(frame, constraints);
    applyChildLayoutProps(frame, layout_align, layout_grow);

    return { status: "ok", node_id: frame.id };
  },

  create_component: (payload) => {
    const {
      width, height, name, color, auto_layout, constraints, effects,
      opacity, stroke_color, stroke_weight, layout_align, layout_grow,
    } = payload;

    const comp = figma.createComponent();
    comp.resize(width, height);
    comp.name = name || "Component";

    applyFill(comp, color);
    applyStroke(comp, stroke_color, stroke_weight);
    applyOpacity(comp, opacity);
    applyAutoLayout(comp, auto_layout);
    applyEffects(comp, effects);

    // Previously this handler ALWAYS used raw payload x/y, even for a
    // root-level component -- meaning a root component could silently
    // overlap prior screens (only create_frame had placement logic). Now
    // shares the same placement fix create_frame gets; non-root
    // components (the common case: parent_id set) behave identically to
    // before (placeRootNodeIfNeeded's else-branch is the same raw x/y
    // assignment the original code did directly).
    const parent = placeRootNodeIfNeeded(comp, payload);
    parent.appendChild(comp);

    applyConstraints(comp, constraints);
    applyChildLayoutProps(comp, layout_align, layout_grow);

    return { status: "ok", node_id: comp.id };
  },

  create_component_set: (payload) => {
    const { child_node_ids, name, x, y } = payload;
    const nodes = child_node_ids.map((id) => figma.getNodeById(id)).filter(Boolean);
    if (nodes.length === 0) {
      return { status: "error", message: "No valid child nodes to combine into variants" };
    }
    const set = figma.combineAsVariants(nodes, figma.currentPage);
    set.name = name || "Component Set";
    if (typeof x === "number") set.x = x;
    if (typeof y === "number") set.y = y;
    return { status: "ok", node_id: set.id };
  },

  create_group: (payload) => {
    const { child_node_ids, name } = payload;
    const nodes = child_node_ids.map((id) => figma.getNodeById(id)).filter(Boolean);
    if (nodes.length === 0) {
      return { status: "error", message: "No valid child nodes to group" };
    }
    const group = figma.group(nodes, figma.currentPage);
    group.name = name || "Group";
    return { status: "ok", node_id: group.id };
  },

  create_rectangle: (payload) => {
    const {
      x, y, width, height, color, corner_radius, constraints, effects, parent_id,
      opacity, stroke_color, stroke_weight, layout_align, layout_grow,
    } = payload;
    const rect = figma.createRectangle();
    rect.x = x; rect.y = y;
    rect.resize(width, height);
    rect.cornerRadius = corner_radius || 0;
    applyFill(rect, color);
    applyStroke(rect, stroke_color, stroke_weight);
    applyOpacity(rect, opacity);
    applyEffects(rect, effects);
    resolveParent(parent_id).appendChild(rect);
    applyConstraints(rect, constraints);
    applyChildLayoutProps(rect, layout_align, layout_grow);
    return { status: "ok", node_id: rect.id };
  },

  create_ellipse: (payload) => {
    const {
      x, y, width, height, color, effects, constraints, parent_id,
      opacity, stroke_color, stroke_weight, layout_align, layout_grow,
    } = payload;
    const ellipse = figma.createEllipse();
    ellipse.x = x; ellipse.y = y;
    ellipse.resize(width, height);
    applyFill(ellipse, color);
    applyStroke(ellipse, stroke_color, stroke_weight);
    applyOpacity(ellipse, opacity);
    applyEffects(ellipse, effects);
    resolveParent(parent_id).appendChild(ellipse);
    applyConstraints(ellipse, constraints);
    applyChildLayoutProps(ellipse, layout_align, layout_grow);
    return { status: "ok", node_id: ellipse.id };
  },

  create_line: (payload) => {
    const { x, y, width, color, parent_id, opacity, layout_align, layout_grow } = payload;
    const line = figma.createLine();
    line.x = x; line.y = y;
    line.resize(width || 100, 0);
    if (color) {
      line.strokes = [{ type: "SOLID", color: { r: color.r, g: color.g, b: color.b } }];
    }
    line.strokeWeight = 1;
    applyOpacity(line, opacity);
    resolveParent(parent_id).appendChild(line);
    applyChildLayoutProps(line, layout_align, layout_grow);
    return { status: "ok", node_id: line.id };
  },

  create_text: async (payload) => {
    const {
      x, y, width, height, content, font_size, font_weight, text_auto_resize,
      color, opacity, parent_id, name, layout_align, layout_grow,
    } = payload;
    const weight = font_weight || "Regular";
    await figma.loadFontAsync({ family: "Inter", style: weight });
    const text = figma.createText();
    text.x = x; text.y = y;
    text.fontName = { family: "Inter", style: weight };
    text.fontSize = font_size || 16;
    text.characters = content || "";
    applyFill(text, color || { r: 0, g: 0, b: 0 });
    applyOpacity(text, opacity);

    // Preserve the ORIGINAL heuristic exactly when text_auto_resize isn't
    // explicitly set (plans built before this field existed -- e.g. the
    // legacy build_design_plan path -- never set it): any node other than
    // "button_label" with a width got textAutoResize="HEIGHT".
    const resizeMode = text_auto_resize || (name !== "button_label" && width ? "HEIGHT" : null);
    if (resizeMode) {
      text.textAutoResize = resizeMode;
      if (resizeMode === "HEIGHT" && width) {
        text.resize(width, text.height);
      } else if ((resizeMode === "NONE" || resizeMode === "TRUNCATE") && width && height) {
        text.resize(width, height);
      }
      // WIDTH_AND_HEIGHT: both dimensions auto-fit; resize() is not called.
    }

    resolveParent(parent_id).appendChild(text);
    applyChildLayoutProps(text, layout_align, layout_grow);
    return { status: "ok", node_id: text.id };
  },

  // Composite: gray box + caption. No plugin-level "image" concept exists
  // without real asset upload, so this is an explicit placeholder.
  create_image_placeholder: async (payload) => {
    const { x, y, width, height, content, parent_id, opacity, layout_align, layout_grow } = payload;
    const parent = resolveParent(parent_id);

    const box = figma.createRectangle();
    box.x = x; box.y = y;
    box.resize(width, height);
    box.fills = [{ type: "SOLID", color: { r: 0.85, g: 0.85, b: 0.85 } }];
    box.cornerRadius = payload.corner_radius || 0;
    applyOpacity(box, opacity);
    parent.appendChild(box);
    applyChildLayoutProps(box, layout_align, layout_grow);

    await loadDefaultFont();
    const label = figma.createText();
    label.fontSize = 12;
    label.characters = content || "Image";
    label.fills = [{ type: "SOLID", color: { r: 0.5, g: 0.5, b: 0.5 } }];
    label.x = x + Math.max(0, width / 2 - 20);
    label.y = y + height / 2 - 8;
    parent.appendChild(label);

    return { status: "ok", node_id: box.id };
  },

  // Best-effort icon placeholder: a small circle + 1-2 char label.
  // Real icon-set/SVG import is a documented future extension, not
  // implemented here (Figma has no built-in icon primitive).
  create_icon: async (payload) => {
    const { x, y, width, height, content, color, parent_id, opacity, layout_align, layout_grow } = payload;
    const parent = resolveParent(parent_id);
    const size = Math.min(width || 24, height || 24);

    const circle = figma.createEllipse();
    circle.x = x; circle.y = y;
    circle.resize(size, size);
    applyFill(circle, color || { r: 0.9, g: 0.9, b: 0.9 });
    applyOpacity(circle, opacity);
    parent.appendChild(circle);
    applyChildLayoutProps(circle, layout_align, layout_grow);

    if (content) {
      await loadDefaultFont();
      const label = figma.createText();
      label.fontSize = Math.max(8, Math.floor(size / 2));
      label.characters = content.slice(0, 2);
      label.x = x + size / 4;
      label.y = y + size / 4;
      parent.appendChild(label);
    }

    return { status: "ok", node_id: circle.id };
  },

  // Additive (Phase 6: component quality). Creates a real Figma instance
  // of a component created earlier in the SAME plan execution
  // (component_node_id is resolved by plan_executor.py's per-execution
  // component_registry, mirroring the existing created_node_ids allowlist
  // pattern). Optionally overrides a single text label -- the common case
  // for the repeated atoms components.py deduplicates (buttons, badges,
  // title-only list rows): see components.py's _component_or_instance.
  create_instance: async (payload) => {
    const {
      x, y, width, height, name, component_node_id, content, parent_id,
      opacity, layout_align, layout_grow, constraints,
    } = payload;

    const componentNode = component_node_id ? figma.getNodeById(component_node_id) : null;
    if (!componentNode || componentNode.type !== "COMPONENT") {
      return { status: "error", message: `Component not found for instance (component_node_id=${component_node_id})`, node_id: null };
    }

    const instance = componentNode.createInstance();
    instance.x = x || 0;
    instance.y = y || 0;
    if (typeof width === "number" && typeof height === "number") {
      try {
        instance.resize(width, height);
      } catch (err) {
        console.log("create_instance: resize skipped:", err);
      }
    }
    if (name) instance.name = name;

    // Best-effort text override: only when the instance contains EXACTLY
    // one text node. Multiple text nodes are left untouched rather than
    // guessing which one the caller meant.
    if (content) {
      const textNodes = instance.findAll((n) => n.type === "TEXT");
      if (textNodes.length === 1) {
        try {
          await figma.loadFontAsync(textNodes[0].fontName);
          textNodes[0].characters = content;
        } catch (err) {
          console.log("create_instance: could not override text:", err);
        }
      }
    }

    resolveParent(parent_id).appendChild(instance);
    applyOpacity(instance, opacity);
    applyChildLayoutProps(instance, layout_align, layout_grow);
    applyConstraints(instance, constraints);
    return { status: "ok", node_id: instance.id };
  },

  apply_color_style: (payload) => {
    const { name, color } = payload;
    const style = figma.createPaintStyle();
    style.name = name;
    style.paints = [{ type: "SOLID", color: { r: color.r, g: color.g, b: color.b } }];
    return { status: "ok", style_id: style.id };
  },

  apply_text_style: async (payload) => {
    const { name, font_size, font_weight } = payload;
    const weight = font_weight || "Regular";
    await figma.loadFontAsync({ family: "Inter", style: weight });
    const style = figma.createTextStyle();
    style.name = name;
    style.fontSize = font_size || 16;
    style.fontName = { family: "Inter", style: weight };
    return { status: "ok", style_id: style.id };
  },

  // Best-effort: requires the Figma Variables API (may be unavailable
  // depending on plan/editor type). Wrapped safely by the outer try/catch.
  create_variable: (payload) => {
    const { name, collection, var_type, value } = payload;
    let coll = figma.variables.getLocalVariableCollections().find((c) => c.name === collection);
    if (!coll) {
      coll = figma.variables.createVariableCollection(collection);
    }
    const variable = figma.variables.createVariable(name, coll, var_type);
    const modeId = coll.modes[0].modeId;
    if (var_type === "COLOR" && value) {
      variable.setValueForMode(modeId, { r: value.r, g: value.g, b: value.b, a: 1 });
    } else {
      variable.setValueForMode(modeId, value);
    }
    return { status: "ok", variable_id: variable.id };
  },
};

// ---- Duplicate-delivery guard --------------------------------------------
//
// bridge_client.py's retry policy only retries a command send that FAILED
// before confirmation -- but whether the message actually reached the
// plugin before that failure is genuinely ambiguous at the transport level
// (see bridge_client.py's _send_with_retry docstring). In the rare case a
// "failed" send actually delivered and is then retried, the plugin would
// receive the same request_id twice. This bounded cache lets a repeat
// request_id replay its already-computed result instead of re-running the
// handler (and creating a duplicate canvas node) a second time.
const _recentResults = new Map(); // request_id -> result
const _MAX_RECENT_RESULTS = 200;

function _rememberResult(request_id, result) {
  if (!request_id) return;
  _recentResults.set(request_id, result);
  if (_recentResults.size > _MAX_RECENT_RESULTS) {
    const oldestKey = _recentResults.keys().next().value;
    _recentResults.delete(oldestKey);
  }
}

// ---- Dispatch loop --------------------------------------------------------

if (typeof figma !== "undefined") {
  figma.ui.onmessage = async (command) => {
    console.log("code.js received command:", command);
    const { request_id, action, payload } = command;

    if (request_id && _recentResults.has(request_id)) {
      console.log("Duplicate request_id received, replaying cached result instead of re-executing:", request_id);
      figma.ui.postMessage({ request_id, ...(_recentResults.get(request_id)) });
      return;
    }

    const handler = commandHandlers[action];

    let result;
    if (!handler) {
      result = { status: "error", message: `Unknown action: ${action}` };
    } else {
      try {
        result = await handler(payload || {});
      } catch (err) {
        result = { status: "error", message: String(err) };
      }
    }

    _rememberResult(request_id, result);
    figma.ui.postMessage({ request_id, ...result });
  };
}

// Exposes pure, figma-API-free helpers for Node-based unit testing (see
// tests/test_plugin_placement.js) without needing a real Figma plugin
// sandbox. This block is a no-op inside the actual Figma plugin runtime,
// which has no CommonJS `module` global -- `typeof module` on an
// undeclared identifier safely evaluates to "undefined" there.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { computeRootPlacement };
}
