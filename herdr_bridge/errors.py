"""Stable errors for the private Bridge RPC boundary.

``outcome`` describes terminal effects of this invocation, not an earlier
durable attempt. It never authorizes retries. Unknown is the safe default:
neither an exception message nor a transport acknowledgement proves no write.
"""
from __future__ import annotations

from collections.abc import Mapping


class BridgeError(ValueError):
    def __init__(self, message: str, *, code: str = "bridge_error",
                 details: Mapping | None = None, outcome: str = "unknown"):
        super().__init__(message)
        if outcome not in ("not_sent", "unknown"):
            raise ValueError("Unsupported Bridge error outcome")
        self.code = code
        self.details = dict(details or {})
        self.outcome = outcome


def error_payload(exc: Exception, *, outcome: str | None = None) -> dict:
    """Keep provider codes while defaulting unclassified failures to unknown."""
    details = getattr(exc, "details", None)
    classified = outcome or getattr(exc, "outcome", "unknown")
    return {"code": getattr(exc, "code", "bridge_error"), "message": str(exc),
            "details": dict(details) if isinstance(details, Mapping) else {},
            "outcome": classified if classified in ("not_sent", "unknown") else "unknown"}
