"""Versioned Graphify adapter resolution."""

from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType

from project_knowledge.compatibility import (
    GraphifyCompatibility,
    resolve_graphify_compatibility,
)

from .base import (
    AdapterContractError,
    CapturedArtifact,
    GraphifyAdapter,
    NativeGraph,
    NormalizationResult,
    ReasonCount,
    capture_native_artifact,
)
from .graphify_0_9_48 import Graphify0948Adapter
from .graphify_0_9_51 import Graphify0951Adapter


AdapterFactory = Callable[[GraphifyCompatibility], GraphifyAdapter]

_ADAPTER_FACTORIES = MappingProxyType(
    {
        "graphify-0.9.48": Graphify0948Adapter,
        "graphify-0.9.51": Graphify0951Adapter,
    }
)


def adapter_for(contract: GraphifyCompatibility) -> GraphifyAdapter:
    """Resolve an adapter only for an exact registry-owned contract."""
    try:
        registered = resolve_graphify_compatibility(contract.version)
    except (AttributeError, TypeError, ValueError) as error:
        raise AdapterContractError(
            "adapter requires a registered compatibility contract"
        ) from error
    if registered != contract:
        raise AdapterContractError("adapter requires a registered compatibility contract")
    factory = _ADAPTER_FACTORIES.get(contract.adapter_id)
    if factory is None:
        raise AdapterContractError("Graphify adapter is not implemented")
    return factory(contract)


__all__ = [
    "AdapterContractError",
    "CapturedArtifact",
    "Graphify0948Adapter",
    "Graphify0951Adapter",
    "GraphifyAdapter",
    "NativeGraph",
    "NormalizationResult",
    "ReasonCount",
    "adapter_for",
    "capture_native_artifact",
]
