from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from project_knowledge.doctor import doctor_project
from project_knowledge.locking import TransactionLockError, open_repository_access
from project_knowledge.manifest import ManifestError
from project_knowledge.models import ArtifactIntent
from tests.support import manifest_v2, write_manifest_v2
from tests.test_graphify_adapter import ProbeRunner, _REQUIRED_HELP


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    entries: list[tuple[str, str, bytes]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append((relative, "link", os.readlink(path).encode()))
        elif path.is_dir():
            entries.append((relative, "dir", b""))
        else:
            entries.append((relative, "file", path.read_bytes()))
    return tuple(entries)


def _configured_repository(root: Path, *, source: str = "safe\n") -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src/app.py").write_text(source, encoding="utf-8")
    write_manifest_v2(root)
    return root


def _graphify_fixture(tmp_path: Path) -> Path:
    executable = tmp_path / "fixture-graphify"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    return executable


def _capability_runner(executable: Path) -> ProbeRunner:
    runner = ProbeRunner()
    runner.answer((str(executable.resolve()), "--version"), stdout="graphify 0.9.48\n")
    runner.answer((str(executable.resolve()), "--help"), stdout=_REQUIRED_HELP)
    return runner


def test_doctor_is_repository_read_only_path_free_and_versioned(
    tmp_path: Path,
) -> None:
    repo = _configured_repository(tmp_path / "repo")
    executable = _graphify_fixture(tmp_path)
    runner = _capability_runner(executable)
    before = _tree_snapshot(repo)

    result = doctor_project(
        repo,
        graphify_binary=executable,
        runner=runner,
        package_version="9.8.7-test",
    )
    document = result.to_dict()

    assert document["package"] == {
        "version": "9.8.7-test",
        "manifest_schema": 2,
        "ownership_schema": 2,
        "evidence_schema": 1,
        "query_schema": 1,
    }
    assert document["projection"]["safe_file_count"] == 1
    assert str(repo) not in json.dumps(document)
    assert _tree_snapshot(repo) == before
    assert not (repo / ".project-knowledge").exists()


def test_doctor_reports_valid_artifact_provider_as_configured(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactIntent(
        provider="github-release",
        host="github.com",
        repository="acme/widgets",
        repository_id=123456789,
        channel="main",
        source_ref="refs/heads/main",
        signer_workflow="acme/widgets/.github/workflows/release.yml",
        signer_digest="a" * 40,
    )
    repo = _configured_repository(tmp_path / "repo")
    write_manifest_v2(repo, artifacts=artifacts)
    executable = _graphify_fixture(tmp_path)

    result = doctor_project(
        repo,
        graphify_binary=executable,
        runner=_capability_runner(executable),
        package_version="test",
    ).to_dict()

    assert result["health"]["features"]["artifacts"] == {
        "status": "configured",
        "issues": [],
    }
    assert "artifact_provider_invalid" not in result["health"]["warnings"]
    assert "artifact_provider_invalid" not in [
        item["code"] for item in result["diagnostics"]
    ]


def test_doctor_reports_recovery_required_without_touching_corrupt_journal(
    tmp_path: Path,
) -> None:
    repo = _configured_repository(tmp_path / "repo")
    state = repo / ".project-knowledge"
    state.mkdir(mode=0o700)
    journal = state / "init-transaction.json"
    journal.write_text("corrupt caller bytes\n", encoding="utf-8")
    runner = ProbeRunner()

    result = doctor_project(repo, runner=runner, package_version="test")

    assert [item.code for item in result.diagnostics] == ["init_recovery_required"]
    assert journal.read_text(encoding="utf-8") == "corrupt caller bytes\n"
    assert runner.calls == []


def test_doctor_expected_identity_mismatch_is_read_only_and_never_probes(
    tmp_path: Path,
) -> None:
    repo = _configured_repository(tmp_path / "repo")
    with open_repository_access(repo) as loaded:
        expected = loaded.identity
    repo.rename(tmp_path / "original")
    replacement = _configured_repository(repo, source="replacement\n")
    runner = ProbeRunner()
    before = _tree_snapshot(replacement)

    with pytest.raises(TransactionLockError) as raised:
        doctor_project(
            replacement,
            runner=runner,
            package_version="test",
            expected_repository_identity=expected,
        )

    assert raised.value.kind == "authority"
    assert runner.calls == []
    assert _tree_snapshot(replacement) == before
    assert not (replacement / ".project-knowledge").exists()


def test_doctor_expected_manifest_mismatch_propagates_for_fleet(
    tmp_path: Path,
) -> None:
    repo = _configured_repository(tmp_path / "repo")

    with pytest.raises(ManifestError) as raised:
        doctor_project(
            repo,
            runner=ProbeRunner(),
            package_version="test",
            expected_manifest=manifest_v2(display_name="Different"),
        )

    assert raised.value.kind == "changed"


class _ManifestMutatingProbeRunner(ProbeRunner):
    def __init__(self, repo: Path, mutate_after_call: int) -> None:
        super().__init__()
        self._repo = repo
        self._mutate_after_call = mutate_after_call

    def run(self, argv, **options):  # type: ignore[no-untyped-def]
        result = super().run(argv, **options)
        if len(self.calls) == self._mutate_after_call:
            path = self._repo / ".graphify-project.yaml"
            original = path.read_bytes()
            path.write_bytes(original.replace(b'Demo', b'Changed', 1))
            path.write_bytes(original)
        return result


@pytest.mark.parametrize("mutate_after_probe_child", range(1, 10))
def test_doctor_rechecks_pinned_manifest_before_every_probe_child(
    tmp_path: Path, mutate_after_probe_child: int
) -> None:
    repo = _configured_repository(tmp_path / "repo")
    executable = _graphify_fixture(tmp_path)
    runner = _ManifestMutatingProbeRunner(repo, mutate_after_probe_child)
    runner.answer((str(executable.resolve()), "--version"), stdout="graphify 0.9.48\n")
    runner.answer((str(executable.resolve()), "--help"), stdout=_REQUIRED_HELP)
    before = _tree_snapshot(repo)

    with pytest.raises(ManifestError) as raised:
        doctor_project(
            repo,
            graphify_binary=executable,
            runner=runner,
            package_version="test",
        )

    assert raised.value.kind == "changed"
    assert len(runner.calls) == mutate_after_probe_child
    assert _tree_snapshot(repo) == before
