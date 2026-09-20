from __future__ import annotations

from typing import Any

# Route sender ownership used to depend on Python import order. That is unsafe in
# a layered V12 runtime because importing an older telemetry module could silently
# replace the final low-latency sender. Every transport layer now registers with
# an explicit authority priority instead.
ROUTE_PRIORITY_BASE = 50
ROUTE_PRIORITY_EPOCH = 100
ROUTE_PRIORITY_REPAIRS = 200
ROUTE_PRIORITY_SUB10MS = 300
ROUTE_PRIORITY_FINAL = 400


def install_route_sender(
    core: Any,
    sender: type[Any],
    *,
    priority: int,
    authority: str,
) -> type[Any]:
    current_priority = int(getattr(core, "_v12_route_sender_priority", -1))
    current_sender = getattr(core, "RouteSender", None)
    if priority >= current_priority:
        core.RouteSender = sender
        core._v12_route_sender_priority = int(priority)
        core._v12_route_sender_authority = str(authority)
        return sender
    return current_sender


def route_authority(core: Any) -> dict[str, Any]:
    sender = getattr(core, "RouteSender", None)
    return {
        "priority": int(getattr(core, "_v12_route_sender_priority", -1)),
        "authority": str(getattr(core, "_v12_route_sender_authority", "legacy")),
        "sender_module": getattr(sender, "__module__", None),
        "sender_name": getattr(sender, "__name__", None),
    }
