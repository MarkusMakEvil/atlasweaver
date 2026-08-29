"""Graphify 0.9.51 evidence-boundary adapter."""

from __future__ import annotations

from dataclasses import dataclass

from .graphify_0_9_48 import Graphify0948Adapter


@dataclass(frozen=True)
class Graphify0951Adapter(Graphify0948Adapter):
    """Use the reviewed 0.9.x surface while retaining an exact adapter ID."""
