from __future__ import annotations

import hashlib
from importlib import resources
import json
from pathlib import PurePosixPath

import pytest

from project_knowledge.adapters import (
    AdapterContractError,
    CapturedArtifact,
    adapter_for,
)
from project_knowledge.compatibility import (
    EvidenceCapabilities,
    resolve_graphify_compatibility,
)


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def test_packaged_fixture_is_reviewed_real_0951_output() -> None:
    payload = resources.files("project_knowledge.compatibility_fixtures").joinpath(
        "graphify_0_9_51.json"
    ).read_bytes()
    document = json.loads(payload)
    contract = resolve_graphify_compatibility("0.9.51")

    assert payload == canonical_json(document)
    assert hashlib.sha256(payload).hexdigest() == (
        "31769f6a5b467720112de5d20e8b251923acd12f1fd537544ac22dc2c4f8036d"
    )
    assert contract.compatibility_fixture_digest == hashlib.sha256(payload).hexdigest()
    assert document["graphify_version"] == "0.9.51"
    assert document["schema_version"] == 1
    assert document["native_graph"]["edges"]
    assert b"/Users/" not in payload
    assert b"/home/" not in payload
    assert b"/tmp/" not in payload


def test_0951_matches_reviewed_0948_surface_but_keeps_navigation_only_evidence() -> None:
    legacy = json.loads(
        resources.files("project_knowledge.compatibility_fixtures")
        .joinpath("graphify_0_9_48.json")
        .read_bytes()
    )
    current = json.loads(
        resources.files("project_knowledge.compatibility_fixtures")
        .joinpath("graphify_0_9_51.json")
        .read_bytes()
    )
    legacy_contract = resolve_graphify_compatibility("0.9.48")
    current_contract = resolve_graphify_compatibility("0.9.51")

    assert current["source_digest"] == legacy["source_digest"]
    assert current["native_graph"] == legacy["native_graph"]
    assert current["diagnosis"] == legacy["diagnosis"]
    assert (
        current_contract.native_schema_fingerprint
        == legacy_contract.native_schema_fingerprint
    )
    assert (
        current_contract.diagnostic_schema_fingerprint
        == legacy_contract.diagnostic_schema_fingerprint
    )
    assert (
        current_contract.clustered_schema_fingerprint
        == legacy_contract.clustered_schema_fingerprint
    )
    assert current_contract.evidence == EvidenceCapabilities(True, False, False)
    assert current_contract.lineage_reason_codes == frozenset()


def test_0951_adapter_accepts_the_reviewed_native_fixture() -> None:
    document = json.loads(
        resources.files("project_knowledge.compatibility_fixtures")
        .joinpath("graphify_0_9_51.json")
        .read_bytes()
    )
    contract = resolve_graphify_compatibility("0.9.51")
    artifact = CapturedArtifact.from_payload(
        PurePosixPath("raw/graph.json"), canonical_json(document["native_graph"])
    )

    native = adapter_for(contract).parse_post_dedup(artifact)

    assert len(native.nodes) == 4
    assert len(native.edges) == 7


def test_0951_adapter_diagnostics_name_the_candidate_version() -> None:
    contract = resolve_graphify_compatibility("0.9.51")
    malformed = CapturedArtifact.from_payload(
        PurePosixPath("raw/graph.json"), b'{"nodes":[]}\n'
    )

    with pytest.raises(
        AdapterContractError, match=r"Graphify 0\.9\.51 native schema is invalid"
    ):
        adapter_for(contract).parse_post_dedup(malformed)
