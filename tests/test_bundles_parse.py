from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import zipfile

import pytest

from project_knowledge.bundles import (
    ArtifactManifest,
    BundleError,
    LocalTransport,
    PayloadDescriptor,
    inspect_bundle_manifest,
    parse_bundle,
)


def _payloads() -> dict[str, bytes]:
    return {
        "graphify-out/GRAPH_EVIDENCE.json": b"{}\n",
        "graphify-out/GRAPH_REPORT.md": b"# report\n",
        "graphify-out/graph.json": b'{"nodes":[],"links":[]}\n',
    }


def _manifest() -> ArtifactManifest:
    payloads = _payloads()
    return ArtifactManifest(
        schema_version=1,
        atlasweaver_version="0.2.2",
        project_id="demo",
        project_uid="4ed9af24-5aa2-4eac-8d0a-3f622cc74948",
        graphify_version="0.9.48",
        adapter_id="graphify-0.9.48",
        source_digest="1" * 64,
        projection_digest="2" * 64,
        graph_digest=hashlib.sha256(payloads["graphify-out/graph.json"]).hexdigest(),
        generation_digest="4" * 64,
        git=None,
        build_epoch=1_777_777_777,
        transport=LocalTransport(),
        payloads=tuple(
            PayloadDescriptor(PurePosixPath(path), hashlib.sha256(payload).hexdigest(), len(payload))
            for path, payload in payloads.items()
        ),
    )


def _write_bundle(path: Path, *, compression: int = zipfile.ZIP_STORED) -> None:
    manifest = _manifest()
    entries = {"artifact.json": manifest.to_bytes(), **_payloads()}
    with zipfile.ZipFile(path, "w", compression=compression, allowZip64=False) as archive:
        for name, payload in entries.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = compression
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, payload)


def test_artifact_manifest_is_closed_canonical_and_strict() -> None:
    manifest = _manifest()
    payload = manifest.to_bytes()

    assert payload == ArtifactManifest.from_bytes(payload).to_bytes()
    assert list(json.loads(payload)) == sorted(json.loads(payload))
    with pytest.raises(BundleError, match="bundle_invalid"):
        ArtifactManifest.from_bytes(payload.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'))
    with pytest.raises(BundleError, match="bundle_invalid"):
        ArtifactManifest.from_bytes(payload[:-2] + b',"unknown":true}\n')


def test_strict_parser_maps_only_approved_payloads_and_closes_reads(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.zip"
    _write_bundle(bundle)

    assert inspect_bundle_manifest(bundle) == _manifest()
    with parse_bundle(bundle, tmp_path / "candidate") as parsed:
        assert sorted(path.name for path in parsed.root.iterdir()) == [
            "GRAPH_EVIDENCE.json", "GRAPH_REPORT.md", "graph.json"
        ]
        assert parsed.read_payload(
            PurePosixPath("graphify-out/graph.json"), 1024
        ) == _payloads()["graphify-out/graph.json"]
    with pytest.raises(BundleError, match="bundle_invalid"):
        parsed.read_payload(PurePosixPath("graphify-out/graph.json"), 1024)


def test_parser_rejects_compression_and_trailing_data_without_destination(tmp_path: Path) -> None:
    compressed = tmp_path / "compressed.zip"
    _write_bundle(compressed, compression=zipfile.ZIP_DEFLATED)
    with pytest.raises(BundleError, match="bundle_invalid"):
        parse_bundle(compressed, tmp_path / "candidate-a")
    assert not (tmp_path / "candidate-a").exists()

    trailing = tmp_path / "trailing.zip"
    _write_bundle(trailing)
    trailing.write_bytes(trailing.read_bytes() + b"trailing")
    with pytest.raises(BundleError, match="bundle_invalid"):
        parse_bundle(trailing, tmp_path / "candidate-b")
    assert not (tmp_path / "candidate-b").exists()
