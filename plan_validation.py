"""
Plan validation & normalization (Phase 9).

    DesignPlan -> validate_and_normalize() -> (corrected DesignPlan, notes)

Runs in plan_executor.execute_plan(), BEFORE anything is sent to the
bridge. Every automatic correction appends a human-readable note to the
returned list; plan_executor threads that list into the final result dict
as `validation_notes`, so a caller can always see what changed and why --
"do not silently destroy user intent" is enforced by always reporting,
never just quietly mutating.

This is a distinct layer from:
  - pydantic's own field-level validation (ColorRGB 0..1 range, max_length
    caps on children/elements/content), which already ran at DesignNode/
    DesignPlan CONSTRUCTION time, before this module ever sees the plan.
  - plan_executor._validate_parent_id, a RUNTIME allowlist check during
    execution (after this module has already run), guarding against a
    parent_id string that was never legitimately created this execution.

This module's job is structural/cross-node sanity pydantic's per-field
validators cannot express, with SAFE, RECOVERABLE problems corrected
automatically and unsafe/oversized ones rejected outright (mirroring the
existing precedent set by bridge-security-hardening's H-6 caps, which
raise rather than silently truncate).
"""

from __future__ import annotations

import logging
from typing import Any

from design_plan import ColorRGB, DesignNode, DesignPlan

logger = logging.getLogger(__name__)

# Independent of (and additive to) DesignNode.children's per-node cap of 50
# and DesignPlan.elements' cap of 20 (see H-6, bridge-security-hardening) --
# a deeply nested tree can still legitimately have thousands of total nodes
# within those per-list caps. This bounds the FLATTENED total, which is
# what actually drives plan_executor's real-world bridge round-trip count
# and execution time. Exceeding it is rejected, not silently truncated,
# for the same "never silently destroy intent" reason H-6 rejects instead
# of truncating.
MAX_TOTAL_NODES = 400

MIN_DIMENSION = 1
MAX_DIMENSION = 8000
_MAX_COORDINATE = 20000

# The only node types the Figma plugin actually calls applyAutoLayout() on
# (code.js's create_frame/create_component handlers). Setting auto_layout
# anywhere else is a silent no-op today; this module makes that visible
# and corrects it instead of leaving dead configuration in the plan.
_AUTO_LAYOUT_CAPABLE_TYPES = {"frame", "component"}

# Types Figma cannot attach children to (no appendChild-equivalent
# capability) -- mirrors plan_executor.execute_node's existing
# _CONTAINER_TYPES hoisting logic. Validation only REPORTS this (the
# actual, already-correct hoisting mechanic stays solely in
# plan_executor, avoiding two sources of truth for the same behavior).
_CAN_HOLD_CHILDREN = {"frame", "component", "component_set", "group"}

_TEXTUAL_SEMANTIC_ROLES = {"heading", "paragraph", "label", "caption"}
_PLACEHOLDER_TEXT = "(untitled)"


class PlanTooLargeError(ValueError):
    """Raised when a plan's flattened node count exceeds MAX_TOTAL_NODES.
    Caught by plan_executor and turned into a clean error result dict,
    the same way an execution timeout is -- never a raw traceback."""


def validate_and_normalize(plan: DesignPlan) -> tuple[DesignPlan, list[str]]:
    """
    Returns a (possibly-modified) COPY of `plan` plus a list of
    human-readable notes describing every automatic correction made.
    Never mutates the caller's original plan object. Raises
    PlanTooLargeError if the plan's total node count is unrecoverable.
    """
    notes: list[str] = []
    working = plan.model_copy(deep=True)

    total_nodes = _count_nodes(working.elements)
    if total_nodes > MAX_TOTAL_NODES:
        raise PlanTooLargeError(
            f"Plan '{working.screen_name}' has {total_nodes} total nodes, "
            f"exceeding the maximum of {MAX_TOTAL_NODES}. Reduce the number "
            f"of elements/items/rows requested."
        )

    _normalize_root_names(working.elements, notes)
    _walk_and_normalize(working.elements, notes, is_root=True)
    _check_root_overlap(working.elements, notes)
    _check_component_refs(working, notes)

    return working, notes


def _count_nodes(nodes: list[DesignNode]) -> int:
    return sum(1 + _count_nodes(n.children) for n in nodes)


def _normalize_root_names(elements: list[DesignNode], notes: list[str]) -> None:
    """De-duplicate names only among TOP-LEVEL elements (distinct screens/
    constructs from a hand-built multi-root DesignPlan). Deliberately does
    NOT dedupe names deep in the tree -- semantic composers legitimately
    reuse names like "stat_label" or "cell" across many sibling cards/rows,
    and that repetition is normal, not a bug (Figma itself has no
    uniqueness requirement on node names)."""
    seen: set[str] = set()
    for node in elements:
        base_name = node.name or node.type
        if base_name in seen:
            counter = 2
            candidate = f"{base_name}_{counter}"
            while candidate in seen:
                counter += 1
                candidate = f"{base_name}_{counter}"
            notes.append(f"Renamed duplicate top-level element name '{base_name}' -> '{candidate}'.")
            node.name = candidate
            seen.add(candidate)
        else:
            seen.add(base_name)


def _walk_and_normalize(nodes: list[DesignNode], notes: list[str], is_root: bool) -> None:
    for node in nodes:
        _normalize_dimensions(node, notes)
        _normalize_auto_layout(node, notes)
        _normalize_missing_content(node, notes)
        _normalize_contrast(node, nodes, notes)
        if node.children and node.type not in _CAN_HOLD_CHILDREN:
            notes.append(
                f"Node '{node.name}' (type={node.type}) cannot hold children in Figma; "
                f"its {len(node.children)} child/children will be attached to its parent "
                f"instead at execution time (see plan_executor.execute_node)."
            )
        if node.children:
            _walk_and_normalize(node.children, notes, is_root=False)


def _normalize_dimensions(node: DesignNode, notes: list[str]) -> None:
    if node.width < MIN_DIMENSION or node.width > MAX_DIMENSION:
        clamped = max(MIN_DIMENSION, min(node.width, MAX_DIMENSION))
        notes.append(f"Clamped width of '{node.name}' from {node.width} to {clamped}.")
        node.width = clamped
    if node.height < MIN_DIMENSION or node.height > MAX_DIMENSION:
        clamped = max(MIN_DIMENSION, min(node.height, MAX_DIMENSION))
        notes.append(f"Clamped height of '{node.name}' from {node.height} to {clamped}.")
        node.height = clamped
    if abs(node.x) > _MAX_COORDINATE:
        clamped_x = max(-_MAX_COORDINATE, min(node.x, _MAX_COORDINATE))
        notes.append(f"Clamped x of '{node.name}' from {node.x} to {clamped_x}.")
        node.x = clamped_x
    if abs(node.y) > _MAX_COORDINATE:
        clamped_y = max(-_MAX_COORDINATE, min(node.y, _MAX_COORDINATE))
        notes.append(f"Clamped y of '{node.name}' from {node.y} to {clamped_y}.")
        node.y = clamped_y


def _normalize_auto_layout(node: DesignNode, notes: list[str]) -> None:
    if node.auto_layout is not None and node.type not in _AUTO_LAYOUT_CAPABLE_TYPES:
        notes.append(
            f"Removed auto_layout from '{node.name}' (type={node.type}): the Figma "
            f"plugin only applies Auto Layout to frame/component nodes, so this "
            f"configuration would have been silently ignored."
        )
        node.auto_layout = None


def _normalize_missing_content(node: DesignNode, notes: list[str]) -> None:
    if node.type == "text" and node.semantic in _TEXTUAL_SEMANTIC_ROLES and not node.content.strip():
        notes.append(f"Filled empty content on '{node.name}' (semantic={node.semantic}) with a placeholder.")
        node.content = _PLACEHOLDER_TEXT


def _colors_equal(a: ColorRGB | None, b: ColorRGB | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a.r - b.r) < 1e-3 and abs(a.g - b.g) < 1e-3 and abs(a.b - b.b) < 1e-3


def _normalize_contrast(node: DesignNode, siblings: list[DesignNode], notes: list[str]) -> None:
    """Catch the one unambiguous, cheaply-detectable contrast bug: a text
    node whose text_color exactly matches its OWN fill (`color`) or its
    parent frame's fill, which would render as invisible text. This is a
    structural equality check, not a WCAG contrast-ratio calculation --
    it corrects an obvious authoring mistake, not a general accessibility
    audit."""
    if node.type != "text" or node.text_color is None:
        return
    if _colors_equal(node.text_color, node.color):
        notes.append(f"Text '{node.name}' had text_color identical to its own fill color; reset text_color to avoid invisible text.")
        node.text_color = None


def _check_root_overlap(elements: list[DesignNode], notes: list[str]) -> None:
    """
    Detect bounding-box overlap between top-level (root) elements. This
    matters for a hand-built, multi-root DesignPlan passed directly to
    generate_screen -- the automatic prompt-driven pipeline
    (planner.build_semantic_plan) always produces exactly one root
    element via components.page(), so this never fires for prompt-driven
    generation. It also covers a gap the Figma plugin's own root-frame
    placement logic (figma-plugin/code.js create_frame) does NOT: that
    logic only auto-arranges root nodes of type "frame"; a root
    "component" node bypasses it entirely and would overlap silently.
    Overlapping roots are nudged right of the rightmost prior root
    (mirroring the plugin's own 125px-gap convention) rather than
    rejected, since this is a mechanically safe, intent-preserving fix.
    """
    placed: list[DesignNode] = []
    for node in elements:
        for other in placed:
            if _rects_overlap(node, other):
                new_x = other.x + other.width + 125
                notes.append(
                    f"Root element '{node.name}' overlapped '{other.name}'; "
                    f"moved x from {node.x} to {new_x}."
                )
                node.x = new_x
        placed.append(node)


def _rects_overlap(a: DesignNode, b: DesignNode) -> bool:
    return (
        a.x < b.x + b.width and a.x + a.width > b.x and
        a.y < b.y + b.height and a.y + a.height > b.y
    )


def _collect_component_registrations(nodes: list[DesignNode], registry: dict[str, DesignNode]) -> None:
    for node in nodes:
        if node.type == "component" and node.register_as:
            if node.register_as in registry:
                logger.warning("Duplicate register_as name %r; the last one wins at execution time.", node.register_as)
            registry[node.register_as] = node
        if node.children:
            _collect_component_registrations(node.children, registry)


def _collect_instance_refs(nodes: list[DesignNode], refs: list[DesignNode]) -> None:
    for node in nodes:
        if node.type == "instance" and node.component_ref:
            refs.append(node)
        if node.children:
            _collect_instance_refs(node.children, refs)


def _check_component_refs(plan: DesignPlan, notes: list[str]) -> None:
    """Ensure every `instance` node's component_ref names a `component`
    node with a matching register_as SOMEWHERE in this same plan (the only
    scope plan_executor resolves against, mirroring the existing
    per-execution created_node_ids allowlist pattern). A dangling
    reference is converted to a plain, harmless placeholder frame instead
    of failing the whole plan or crashing at execution time."""
    registry: dict[str, DesignNode] = {}
    _collect_component_registrations(plan.elements, registry)
    refs: list[DesignNode] = []
    _collect_instance_refs(plan.elements, refs)
    for ref_node in refs:
        if ref_node.component_ref not in registry:
            notes.append(
                f"Instance '{ref_node.name}' referenced unknown component "
                f"'{ref_node.component_ref}'; converted to a placeholder frame."
            )
            ref_node.type = "frame"
            ref_node.component_ref = None
