"""
Plan Executor.
Walks a DesignPlan tree and executes it against the Figma plugin via
bridge_client, resolving parent/child relationships, groups, and
component variants at runtime (since node IDs only exist after creation).
"""

import logging
from typing import Any

import bridge_client
from design_plan import DesignPlan, DesignNode

logger = logging.getLogger(__name__)

# Node types that are "containers built from already-created children"
# rather than "create me, then create my children inside me".
_POST_HOC_CONTAINERS = {"group", "component_set"}


async def execute_plan(plan: DesignPlan) -> dict[str, Any]:
    """Execute a full DesignPlan: design tokens first, then the element tree."""
    token_results = await _apply_design_tokens(plan)

    element_results: list[dict[str, Any]] = []
    for node in plan.elements:
        element_results.append(await execute_node(node, parent_id=None))

    flat = _flatten(element_results)
    succeeded = sum(1 for r in flat if r.get("status") == "ok")

    return {
        "screen_name": plan.screen_name,
        "design_tokens": token_results,
        "total_nodes": len(flat),
        "succeeded": succeeded,
        "failed": len(flat) - succeeded,
        "results": flat,
    }


async def execute_node(node: DesignNode, parent_id: str | None) -> dict[str, Any]:
    """
    Execute one node (and recursively, its children), returning its result.
    Only container-capable types (frame, component) can actually hold
    children in Figma â€” if a non-container node has children (a model
    mistake), they are hoisted up to be siblings under this node's OWN
    parent instead of being attached to this node, which would crash
    the plugin (RectangleNode/TextNode have no appendChild).
    """
    if node.type in _POST_HOC_CONTAINERS:
        return await _execute_post_hoc_container(node, parent_id)

    payload = _build_payload(node, parent_id)
    action = _ACTION_MAP[node.type]

    result = await bridge_client.send_figma_command(action, payload)
    node_id = result.get("node_id")

    _CONTAINER_TYPES = {"frame", "component"}
    child_results = []
    if node.children and result.get("status") == "ok" and node_id:
        # Only frame/component can hold real children in Figma's API.
        # For anything else (rectangle, text, etc.), attach children to
        # THIS node's parent instead, to avoid an invalid appendChild call.
        effective_parent = node_id if node.type in _CONTAINER_TYPES else parent_id
        for child in node.children:
            child_results.append(await execute_node(child, parent_id=effective_parent))

    result["_children"] = child_results
    return result

async def _execute_post_hoc_container(node: DesignNode, parent_id: str | None) -> dict[str, Any]:
    """
    Handle 'group' and 'component_set': children must be created FIRST
    (as independent nodes), then combined into the container.
    """
    if node.type == "component_set":
        child_ids = []
        for child in node.children:
            # Each child of a component_set must itself be a 'component'
            # node; its name is set to Figma's variant-naming convention
            # ("Prop1=Value1, Prop2=Value2") so combineAsVariants groups
            # them correctly.
            variant_name = ", ".join(f"{k}={v}" for k, v in child.variant_properties.items())
            child_payload = _build_payload(child, parent_id)
            child_payload["name"] = variant_name or child.name or "Variant"
            child_result = await bridge_client.send_figma_command("create_component", child_payload)
            if child_result.get("status") == "ok":
                child_ids.append(child_result["node_id"])

        result = await bridge_client.send_figma_command("create_component_set", {
            "child_node_ids": child_ids,
            "name": node.name or "Component Set",
            "x": node.x, "y": node.y,
        })
        return result

    if node.type == "group":
        child_results = [await execute_node(c, parent_id) for c in node.children]
        child_ids = [r["node_id"] for r in child_results if r.get("status") == "ok" and r.get("node_id")]

        result = await bridge_client.send_figma_command("create_group", {
            "child_node_ids": child_ids,
            "name": node.name or "Group",
        })
        result["_children"] = child_results
        return result

    raise ValueError(f"Unhandled post-hoc container type: {node.type}")


_ACTION_MAP: dict[str, str] = {
    "frame": "create_frame",
    "component": "create_component",
    "text": "create_text",
    "rectangle": "create_rectangle",
    "ellipse": "create_ellipse",
    "line": "create_line",
    "image_placeholder": "create_image_placeholder",
    "icon": "create_icon",
}


def _build_payload(node: DesignNode, parent_id: str | None) -> dict[str, Any]:
    """Flatten a DesignNode into the JSON payload the plugin expects."""
    return {
        "x": node.x, "y": node.y,
        "width": node.width, "height": node.height,
        "name": node.name,
        "content": node.content,
        "font_size": node.font_size,
        "corner_radius": node.corner_radius,
        "color": node.color.model_dump() if node.color else None,
        "text_color": node.text_color.model_dump() if node.text_color else None,
        "auto_layout": node.auto_layout.model_dump() if node.auto_layout else None,
        "constraints": node.constraints.model_dump() if node.constraints else None,
        "effects": [e.model_dump() for e in node.effects],
        "parent_id": parent_id,
    }


async def _apply_design_tokens(plan: DesignPlan) -> dict[str, Any]:
    """Create color styles, text styles, and variables before any elements."""
    results: dict[str, Any] = {"color_styles": [], "text_styles": [], "variables": []}

    for cs in plan.color_styles:
        r = await bridge_client.send_figma_command(
            "apply_color_style", {"name": cs.name, "color": cs.color.model_dump()}
        )
        results["color_styles"].append({"name": cs.name, "result": r})

    for ts in plan.text_styles:
        r = await bridge_client.send_figma_command(
            "apply_text_style",
            {"name": ts.name, "font_size": ts.font_size, "font_weight": ts.font_weight},
        )
        results["text_styles"].append({"name": ts.name, "result": r})

    for var in plan.variables:
        value = var.color_value.model_dump() if var.color_value else (
            var.number_value if var.number_value is not None else var.string_value
        )
        r = await bridge_client.send_figma_command(
            "create_variable",
            {"name": var.name, "collection": var.collection, "var_type": var.var_type, "value": value},
        )
        results["variables"].append({"name": var.name, "result": r})

    return results


def _flatten(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten nested '_children' results into one list, for a simple summary."""
    flat: list[dict[str, Any]] = []
    for r in results:
        children = r.pop("_children", [])
        flat.append(r)
        flat.extend(_flatten(children))
    return flat
