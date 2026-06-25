"""Profiles & config stacking for the kernel surface (in-process assembly).

``OFFLINE`` is the no-external-deps baseline profile. ``load_config`` stacks JSON
layers nearest-wins (later layers override earlier), mirroring how the CLI passes
``--config a.json b.json`` on top of ``OFFLINE``. The minimal reference build
ignores everything but the ``profile`` name and any ``policies`` map (forwarded to
the in-memory PolicyManager); a real build would select plugins/storage here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

OFFLINE: Dict[str, Any] = {"profile": "offline"}


@dataclass
class Config:
    """Resolved configuration handed to :func:`server.build`."""

    profile: str = "offline"
    policies: Dict[str, str] = field(default_factory=dict)
    settings: Dict[str, Any] = field(default_factory=dict)


def load_config(layers: List[Dict[str, Any]], spaces: Any = None) -> Config:
    """Merge config ``layers`` (nearest-wins) into a single :class:`Config`."""
    merged: Dict[str, Any] = {}
    for layer in layers:
        if layer:
            merged.update(layer)
    return Config(
        profile=str(merged.get("profile", "offline")),
        policies=dict(merged.get("policies", {})),
        settings=merged,
    )
