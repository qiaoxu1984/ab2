"""Message constructors for the Manager-Agent WebSocket protocol."""

from typing import Any


def message(kind: str, **payload: Any) -> dict[str, Any]:
    """Wrap a protocol payload with a stable message discriminator."""
    return {"type": kind, **payload}
