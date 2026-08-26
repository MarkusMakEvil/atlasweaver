from __future__ import annotations

import json
from pathlib import Path
import stat
import struct
import zipfile
import zlib

import pytest

from project_knowledge.bundles import BundleError, PackRequest, pack_bundle
from project_knowledge.lifecycle import RefreshOptions, refresh_project
from project_knowledge.operation_state import load_operation_state
from tests.test_lifecycle import (
    Official0948FixtureRunner,
    _fixture_graphify,
    _source_repository,
)


def _owned_repository(tmp_path: Path) -> Path:
    repo, manifest = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    refresh_project(
        repo,
        manifest,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        ambient={},
        graphify_binary=executable,
    )
    return repo


def _stored_zip(entries: list[tuple[str, bytes]]) -> bytes:
    local = struct.Struct("<4s5H3L2H")
    central = struct.Struct("<4s6H3L5H2L")
    eocd = struct.Struct("<4s4H2LH")
    body = bytearray()
    directory = bytearray()
    for name, payload in entries:
        encoded = name.encode("ascii")
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        offset = len(body)
        body.extend(local.pack(
            b"PK\x03\x04", 20, 0, 0, 0, 33, crc,
            len(payload), len(payload), len(encoded), 0,
        ))
        body.extend(encoded)
        body.extend(payload)
        directory.extend(central.pack(
            b"PK\x01\x02", (3 << 8) | 20, 20, 0, 0, 0, 33, crc,
            len(payload), len(payload), len(encoded), 0, 0, 0, 0,
            (stat.S_IFREG | 0o600) << 16, offset,
        ))
        directory.extend(encoded)
    offset = len(body)
    body.extend(directory)
    body.extend(eocd.pack(
        b"PK\x05\x06", 0, 0, len(entries), len(entries),
        len(directory), offset, 0,
    ))
    return bytes(body)


def test_pack_is_byte_deterministic_closed_and_records_state(tmp_path: Path) -> None:
    repo = _owned_repository(tmp_path)
    first = pack_bundle(PackRequest(repo, tmp_path / "first.zip"))
    second = pack_bundle(PackRequest(repo, tmp_path / "second.zip"))

    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.sha256 == second.sha256
    with zipfile.ZipFile(first.path) as archive:
        assert archive.namelist() == [
            "artifact.json",
            "graphify-out/GRAPH_EVIDENCE.json",
            "graphify-out/GRAPH_REPORT.md",
            "graphify-out/graph.json",
        ]
        assert all(item.compress_type == zipfile.ZIP_STORED for item in archive.infolist())
        entries = [(name, archive.read(name)) for name in archive.namelist()]
        artifact = json.loads(entries[0][1])
    assert first.path.read_bytes() == _stored_zip(entries)
    assert list(artifact) == sorted(artifact)
    assert artifact["generation_digest"] == first.artifact.generation_digest
    assert artifact["build_epoch"] == first.artifact.build_epoch == 1
    assert artifact["transport"] == {"channel": None, "provider": "none"}
    assert load_operation_state(repo).last_success.operation == "artifact_pack"


def test_pack_refuses_existing_output_and_stale_source(tmp_path: Path) -> None:
    repo = _owned_repository(tmp_path)
    output = tmp_path / "bundle.zip"
    output.write_bytes(b"caller-owned")
    with pytest.raises(BundleError):
        pack_bundle(PackRequest(repo, output))
    assert output.read_bytes() == b"caller-owned"

    output.unlink()
    (repo / "fixture.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(BundleError) as raised:
        pack_bundle(PackRequest(repo, output))
    assert raised.value.code == "bundle_stale"
    assert not output.exists()
