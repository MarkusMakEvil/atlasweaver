from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import stat

import pytest

from project_knowledge.models import ProjectManifest
from project_knowledge.receipt import (
    ReceiptError,
    load_staging_receipt,
    verify_staged_input,
    write_staging_receipt,
)
from project_knowledge.staging import StagedInput


def manifest() -> ProjectManifest:
    return ProjectManifest(
        schema_version=1,
        project_id="demo",
        display_name="Demo",
        include_roots=(PurePosixPath("src"),),
        output_dir=PurePosixPath("graphify-out"),
        obsidian_namespace=PurePosixPath("Projects/demo/Generated"),
        excludes=(),
        track_html=False,
        graphify_version="0.9.48",
    )


def write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def digest(files: list[tuple[str, bytes]]) -> str:
    import hashlib

    value = hashlib.sha256()
    for path, payload in sorted(files):
        value.update(path.encode("utf-8") + b"\0" + payload + b"\0")
    return value.hexdigest()


def staged(root: Path) -> StagedInput:
    files = [("src/a.py", b"a\n"), ("src/nested/z.py", b"z\n")]
    for relative, payload in files:
        write(root / relative, payload)
    return StagedInput(
        root=root,
        source_digest=digest(files),
        files=tuple(PurePosixPath(path) for path, _ in files),
    )


def test_receipt_round_trip_is_private_deterministic_and_path_free(tmp_path: Path) -> None:
    value = staged(tmp_path / "private-stage")
    receipt_path = tmp_path / "receipt.json"

    written = write_staging_receipt(receipt_path, value, manifest())
    loaded = load_staging_receipt(receipt_path, manifest())

    assert loaded == written
    assert loaded.project_id == "demo"
    assert loaded.graphify_version == "0.9.48"
    assert loaded.source_digest == value.source_digest
    assert loaded.files == value.files
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    raw = receipt_path.read_text(encoding="utf-8")
    assert raw == json.dumps(
        {
            "schema_version": 1,
            "project_id": "demo",
            "graphify_version": "0.9.48",
            "source_digest": value.source_digest,
            "files": ["src/a.py", "src/nested/z.py"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    assert str(tmp_path) not in raw


def test_receipt_exclusive_create_preserves_existing_file(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text("owned by caller\n", encoding="utf-8")

    with pytest.raises(ReceiptError, match="already exists"):
        write_staging_receipt(receipt_path, staged(tmp_path / "stage"), manifest())

    assert receipt_path.read_text(encoding="utf-8") == "owned by caller\n"


def test_receipt_rejects_duplicate_keys_and_project_mismatch(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        '{"schema_version":1,"project_id":"demo","project_id":"other",'
        '"graphify_version":"0.9.48","source_digest":"' + "1" * 64 + '","files":[]}',
        encoding="utf-8",
    )

    with pytest.raises(ReceiptError, match="duplicate JSON key"):
        load_staging_receipt(receipt_path, manifest())


def test_verify_staged_input_recomputes_exact_snapshot(tmp_path: Path) -> None:
    value = staged(tmp_path / "stage")
    receipt_path = tmp_path / "receipt.json"
    receipt = write_staging_receipt(receipt_path, value, manifest())

    verified = verify_staged_input(value.root, receipt)

    assert verified == value


@pytest.mark.parametrize("mutation", ["changed", "extra", "missing"])
def test_verify_staged_input_rejects_snapshot_drift(
    tmp_path: Path, mutation: str
) -> None:
    value = staged(tmp_path / "stage")
    receipt = write_staging_receipt(tmp_path / "receipt.json", value, manifest())
    if mutation == "changed":
        write(value.root / "src/a.py", b"changed\n")
    elif mutation == "extra":
        write(value.root / "src/extra.py", b"extra\n")
    else:
        (value.root / "src/a.py").unlink()

    with pytest.raises(ReceiptError, match="staged input does not match receipt"):
        verify_staged_input(value.root, receipt)


def test_verify_staged_input_rejects_symlink(tmp_path: Path) -> None:
    value = staged(tmp_path / "stage")
    receipt = write_staging_receipt(tmp_path / "receipt.json", value, manifest())
    external = tmp_path / "external.py"
    external.write_text("private\n", encoding="utf-8")
    (value.root / "src/a.py").unlink()
    os.symlink(external, value.root / "src/a.py")

    with pytest.raises(ReceiptError, match="regular files"):
        verify_staged_input(value.root, receipt)


def test_receipt_load_rejects_file_swapped_to_symlink_after_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = staged(tmp_path / "stage")
    receipt_path = tmp_path / "receipt.json"
    write_staging_receipt(receipt_path, value, manifest())
    external = tmp_path / "external.json"
    external.write_bytes(receipt_path.read_bytes())
    displaced = tmp_path / "displaced.json"
    real_stat = os.stat
    swapped = False

    def swap_after_stat(path: object, *args: object, **kwargs: object):
        nonlocal swapped
        result = real_stat(path, *args, **kwargs)
        if (Path(path) == receipt_path or path == receipt_path.name) and not swapped:
            swapped = True
            receipt_path.rename(displaced)
            os.symlink(external, receipt_path)
        return result

    monkeypatch.setattr(os, "stat", swap_after_stat)

    with pytest.raises(ReceiptError):
        load_staging_receipt(receipt_path, manifest())
    assert swapped
