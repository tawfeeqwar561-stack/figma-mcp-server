"""
Plan Executor.
Walks a DesignPlan tree and executes it against the Figma plugin via
bridge_client, resolving parent/child relationships, groups, and
component variants at runtime (since node IDs only exist after creation).
"""

import asyncio
import logging
from typing import Any

import bridge_client
import config
from design_plan import DesignPlan, DesignNode

logger = logging.getLogger(__name__)

# Node types that are "containers built from already-created children"
# rather than "create me, then create my children inside me".
_POST_HOC_CONTAINERS = {"group", "component_set"}


async def execute_plan(plan: DesignPlan) -> dict[str, Any]:
    """
    Execute a full DesignPlan: design tokens first, then the element tree.
    Wrapped in an overall timeout (config.PLAN_EXECUTION_TIMEOUT_SECONDS)
    so an adversarial/hung plan cannot block a tool call indefinitely --
    see H-6 in bridge-security-hardening.
    """
    async def _run() -> dict[str, Any]:
        token_results = await _apply_design_tokens(plan)

        # A per-call allowlist of node IDs actually created during THIS
        # execution. Any parent_id that isn't None and isn't in this set is
        # rejected before it ever reaches the bridge (see _validate_parent_id).
        # Deliberately local, not a module global, to avoid leaking allowlist
        # state across concurrent/successive generate_screen calls.
        created_node_ids: set[str] = set()

        element_results: list[dict[str, Any]] = []
        for node in plan.elements:
            element_results.append(await execute_node(node, parent_id=None, created_node_ids=created_node_ids))

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

    try:
        return await asyncio.wait_for(_run(), timeout=config.PLAN_EXECUTION_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.error("Plan execution timed out after %ss: %s", config.PLAN_EXECUTION_TIMEOUT_SECONDS, plan.screen_name)
        return {
            "screen_name": plan.screen_name,
            "status": "error",
            "message": f"Plan execution timed out after {config.PLAN_EXECUTION_TIMEOUT_SECONDS}s",
        }


def _validate_parent_id(parent_id: str | None, created_node_ids: set[str]) -> str | None:
    """
    Return an error message if `parent_id` is set but was not created
    earlier in this same plan execution, else None.

    Every parent_id plan_executor.py generates internally is always either
    None or a node_id it just created itself, so this is a no-op on all
    current legitimate inputs -- it only fires as a defense-in-depth guard
    against a future schema addition, or a raw (post-auth) controller
    command, smuggling in an untrusted parent_id.
    """
    if parent_id is not None and parent_id not in created_node_ids:
        return f"parent_id {parent_id} is not part of this plan execution"
    return None


async def _send_command_safe(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Call bridge_client.send_figma_command, converting a per-command
    TimeoutError/ConnectionError (e.g. no plugin currently connected --
    an expected, everyday occurrence, not a plan-size problem) into a
    normal per-node error result instead of letting it propagate.

    Without this, a single dropped/timed-out command would bubble up
    through execute_plan's `_run()` closure and be caught by the SAME
    `except asyncio.TimeoutError:` that guards the overall
    PLAN_EXECUTION_TIMEOUT_SECONDS deadline (TimeoutError and
    asyncio.TimeoutError are the same class since Python 3.11), producing
    a misleading "Plan execution timed out after <N>s" message after only
    one bridge_client.RESPONSE_TIMEOUT_SECONDS (10s) and discarding every
    other node's results -- see the H-6 follow-up fix in
    bridge-security-hardening. This does not change the outer
    asyncio.wait_for/timeout behavior: wait_for cancels its task
    independently of any try/except inside it, so a genuine overall-runtime
    overrun is still caught exactly as before.
    """
    try:
        return await bridge_client.send_figma_command(action, payload)
    except (TimeoutError, ConnectionError) as exc:
        logger.warning("Command %r failed: %s", action, exc)
        return {"status": "error", "message": str(exc), "node_id": None}


async def execute_node(
    node: DesignNode, parent_id: str | None, created_node_ids: set[str]
) -> dict[str, Any]:
    """
    Execute one node (and recursively, its children), returning its result.
    Only container-capable types (frame, component) can actually hold
    children in Figma â€” if a non-container node has children (a model
    mistake), they are hoisted up to be siblings under this node's OWN
    parent instead of being attached to this node, which would crash
    the plugin (RectangleNode/TextNode have no appendChild).
    """
    if node.type in _POST_HOC_CONTAINERS:
        return await _execute_post_hoc_container(node, parent_id, created_node_ids)

    error_message = _validate_parent_id(parent_id, created_node_ids)
    if error_message:
        logger.warning("Rejecting node %r: %s", node.name or node.type, error_message)
        return {"status": "error", "message": error_message, "node_id": None, "_children": []}

    payload = _build_payload(node, parent_id)
    action = _ACTION_MAP[node.type]

    result = await _send_command_safe(action, payload)
    node_id = result.get("node_id")

    _CONTAINER_TYPES = {"frame", "component"}
    child_results = []
    if node.children and result.get("status") == "ok" and node_id:
        created_node_ids.add(node_id)
        # Only frame/component can hold real children in Figma's API.
        # For anything else (rectangle, text, etc.), attach children to
        # THIS node's parent instead, to avoid an invalid appendChild call.
        effective_parent = node_id if node.type in _CONTAINER_TYPES else parent_id
        for child in node.children:
            child_results.append(await execute_node(child, parent_id=effective_parent, created_node_ids=created_node_ids))
    elif result.get("status") == "ok" and node_id:
        created_node_ids.add(node_id)

    result["_children"] = child_results
    return result

async def _execute_post_hoc_container(
    node: DesignNode, parent_id: str | None, created_node_ids: set[str]
) -> dict[str, Any]:
    """
    Handle 'group' and 'component_set': children must be created FIRST
    (as independent nodes), then combined into the container.
    """
    if node.type == "component_set":
        error_message = _validate_parent_id(parent_id, created_node_ids)
        if error_message:
            logger.warning("Rejecting component_set %r: %s", node.name or node.type, error_message)
            return {"status": "error", "message": error_message, "node_id": None}

        child_ids = []
        for child in node.children:
            # Each child of a component_set must itself be a 'component'
            # node; its name is set to Figma's variant-naming convention
            # ("Prop1=Value1, Prop2=Value2") so combineAsVariants groups
            # them correctly.
            variant_name = ", ".join(f"{k}={v}" for k, v in child.variant_properties.items())
            child_payload = _build_payload(child, parent_id)
            child_payload["name"] = variant_name or child.name or "Variant"
            child_result = await _send_command_safe("create_component", child_payload)
            if child_result.get("status") == "ok" and child_result.get("node_id"):
                child_ids.append(child_result["node_id"])
                created_node_ids.add(child_result["node_id"])

        result = await _send_command_safe("create_component_set", {
            "child_node_ids": child_ids,
            "name": node.name or "Component Set",
            "x": node.x, "y": node.y,
        })
        if result.get("status") == "ok" and result.get("node_id"):
            created_node_ids.add(result["node_id"])
        return result

    if node.type == "group":
        error_message = _validate_parent_id(parent_id, created_node_ids)
        if error_message:
            logger.warning("Rejecting group %r: %s", node.name or node.type, error_message)
            return {"status": "error", "message": error_message, "node_id": None, "_children": []}

        child_results = [await execute_node(c, parent_id, created_node_ids) for c in node.children]
        child_ids = [r["node_id"] for r in child_results if r.get("status") == "ok" and r.get("node_id")]

        result = await _send_command_safe("create_group", {
            "child_node_ids": child_ids,
            "name": node.name or "Group",
        })
        if result.get("status") == "ok" and result.get("node_id"):
            created_node_ids.add(result["node_id"])
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
        r = await _send_command_safe(
            "apply_color_style", {"name": cs.name, "color": cs.color.model_dump()}
        )
        results["color_styles"].append({"name": cs.name, "result": r})

    for ts in plan.text_styles:
        r = await _send_command_safe(
            "apply_text_style",
            {"name": ts.name, "font_size": ts.font_size, "font_weight": ts.font_weight},
        )
        results["text_styles"].append({"name": ts.name, "result": r})

    for var in plan.variables:
        value = var.color_value.model_dump() if var.color_value else (
            var.number_value if var.number_value is not None else var.string_value
        )
        r = await _send_command_safe(
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
