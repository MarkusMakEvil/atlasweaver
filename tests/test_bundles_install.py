from __future__ import annotations

from pathlib import Path, PurePosixPath

from project_knowledge.bundles import PackRequest, install_local_bundle, pack_bundle
from project_knowledge.operation_state import load_operation_state
from tests.support import write_manifest_v2
from tests.test_bundles_pack import _owned_repository


def test_pack_installs_through_validator_and_is_idempotent(tmp_path: Path) -> None:
    source = _owned_repository(tmp_path)
    packed = pack_bundle(PackRequest(source, tmp_path / "bundle.zip"))
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "fixture.py").write_bytes((source / "fixture.py").read_bytes())
    write_manifest_v2(clone, include_roots=(PurePosixPath("fixture.py"),))

    first = install_local_bundle(clone, packed.path)
    second = install_local_bundle(clone, packed.path)

    assert first.status == "installed"
    assert second.status == "already_current"
    assert first.generation_digest == second.generation_digest == packed.artifact.generation_digest
    assert first.build_epoch == second.build_epoch == packed.artifact.build_epoch
    assert load_operation_state(clone).last_success.operation == "artifact_install"
