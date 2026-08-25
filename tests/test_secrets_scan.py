from __future__ import annotations

from pathlib import Path, PurePosixPath
import os

import pytest

from project_knowledge.models import ProjectManifest
from project_knowledge.secrets_scan import (
    SecretExceptionError,
    load_secret_exceptions,
    scan_payload,
    scan_repository,
)


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


@pytest.mark.parametrize(
    ("detector", "payload"),
    [
        ("telegram_token", b"123456789:" + b"a" * 35),
        (
            "private_key",
            b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 48,
        ),
        ("aws_access_key", b"AK" + b"IA" + b"A" * 16),
        ("github_token", b"gh" + b"p_" + b"a" * 36),
        ("slack_token", b"xox" + b"b-" + b"1" * 12 + b"-" + b"a" * 24),
        ("bearer_token", b"authorization: Bearer " + b"a" * 32),
        ("credentialed_url", b"postgresql://app:" + b"p" * 20 + b"@db.example/app"),
    ],
)
def test_structured_findings_are_named_redacted_and_non_bypassable(
    detector: str, payload: bytes
) -> None:
    path = PurePosixPath("src/settings.bin")

    findings = scan_payload(path, b"safe\n" + payload + b"\n")

    selected = next(item for item in findings if item.detector == detector)
    assert selected.path == path
    assert selected.line == 2
    assert selected.fingerprint.startswith("sha256:")
    assert len(selected.fingerprint) == len("sha256:") + 64
    assert selected.bypassable is False
    serialized = repr(selected)
    assert payload.decode("utf-8", errors="ignore") not in serialized


@pytest.mark.parametrize(
    ("path", "reference"),
    [
        ("src/settings.ts", "process.env.SERVICE_API_KEY"),
        ("src/settings.ts", "import.meta.env.SERVICE_API_KEY"),
        ("src/settings.ts", "os.environ['SERVICE_API_KEY']"),
        ("src/settings.ts", "os.getenv('SERVICE_API_KEY')"),
        ("src/settings.ts", "getenv('SERVICE_API_KEY')"),
        ("src/settings.ts", "${SERVICE_API_KEY}"),
        ("src/settings.ts", '"${SERVICE_API_KEY}"'),
        ("src/settings.yaml", "process.env.SERVICE_API_KEY"),
        ("src/settings.yaml", "import.meta.env.SERVICE_API_KEY"),
    ],
)
def test_generic_assignment_ignores_environment_references(
    path: str, reference: str
) -> None:
    payload = f"api_key = {reference}\n".encode()

    assert not any(
        finding.detector == "generic_secret_assignment"
        for finding in scan_payload(PurePosixPath(path), payload)
    )


@pytest.mark.parametrize(
    ("path", "assignment"),
    [
        ("src/settings.ts", 'api_key = "correct-horse-battery-staple"'),
        ("src/settings.py", "password = 'correct-horse-battery-staple'"),
        ("src/settings.yaml", "client_secret: correct-horse-battery-staple"),
    ],
)
def test_generic_assignment_reports_long_literals(path: str, assignment: str) -> None:
    finding = next(
        item
        for item in scan_payload(PurePosixPath(path), (assignment + "\n").encode())
        if item.detector == "generic_secret_assignment"
    )

    assert finding.line == 1
    assert finding.bypassable is True
    assert "correct-horse" not in repr(finding)


def test_scanner_finds_structured_bytes_in_non_utf8_file() -> None:
    findings = scan_payload(
        PurePosixPath("src/blob.bin"),
        b"\xff\xfe\nAK" + b"IA" + b"A" * 16,
    )

    assert [item.detector for item in findings] == ["aws_access_key"]


def test_repository_scan_uses_only_manifest_safe_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text(
        'api_key = "correct-horse-battery-staple"\n', encoding="utf-8"
    )
    (tmp_path / "src/auth-token.txt").write_text(
        "AK" + "IA" + "A" * 16, encoding="utf-8"
    )

    findings = scan_repository(tmp_path, manifest())

    assert len(findings) == 1
    assert findings[0].path == PurePosixPath("src/app.py")


def write_exceptions(path: Path, finding_fingerprint: str, *, detector: str = "generic_secret_assignment") -> None:
    path.write_text(
        "schema_version: 1\n"
        "exceptions:\n"
        "  - path: src/settings.ts\n"
        f"    detector: {detector}\n"
        f"    fingerprint: {finding_fingerprint}\n"
        "    reason: reviewed public test fixture\n",
        encoding="utf-8",
    )


def test_exception_accepts_only_exact_contextual_finding(tmp_path: Path) -> None:
    finding = next(
        item
        for item in scan_payload(
            PurePosixPath("src/settings.ts"),
            b'api_key = "correct-horse-battery-staple"\n',
        )
        if item.detector == "generic_secret_assignment"
    )
    write_exceptions(tmp_path / ".graphify-secret-exceptions.yaml", finding.fingerprint)

    accepted = load_secret_exceptions(tmp_path, (finding,))

    assert accepted == frozenset({finding.fingerprint})


def test_contextual_fingerprint_binds_the_complete_assignment_expression() -> None:
    path = PurePosixPath("src/settings.ts")
    original = next(
        item
        for item in scan_payload(
            path,
            b'api_key = "correct-horse-battery-staple"\n',
        )
        if item.detector == "generic_secret_assignment"
    )
    extended = next(
        item
        for item in scan_payload(
            path,
            b'api_key = "correct-horse-battery-staple" + "new-production-secret-value"\n',
        )
        if item.detector == "generic_secret_assignment"
    )

    assert extended.fingerprint != original.fingerprint


def test_contextual_fingerprint_binds_key_and_occurrence() -> None:
    findings = [
        item
        for item in scan_payload(
            PurePosixPath("src/settings.ts"),
            b'api_key = "same-reviewed-placeholder"\n'
            b'password = "same-reviewed-placeholder"\n'
            b'api_key = "same-reviewed-placeholder"\n',
        )
        if item.detector == "generic_secret_assignment"
    ]

    assert len(findings) == 3
    assert len({item.fingerprint for item in findings}) == 3


def test_exception_rejects_non_bypassable_structured_finding(tmp_path: Path) -> None:
    finding = scan_payload(
        PurePosixPath("src/settings.ts"), b"AK" + b"IA" + b"A" * 16
    )[0]
    write_exceptions(
        tmp_path / ".graphify-secret-exceptions.yaml",
        finding.fingerprint,
        detector=finding.detector,
    )

    with pytest.raises(SecretExceptionError, match="cannot be excepted"):
        load_secret_exceptions(tmp_path, (finding,))


@pytest.mark.parametrize("change", ["fingerprint", "path", "duplicate"])
def test_exception_rejects_stale_unknown_or_duplicate_entry(
    tmp_path: Path, change: str
) -> None:
    finding = next(
        item
        for item in scan_payload(
            PurePosixPath("src/settings.ts"),
            b'api_key = "correct-horse-battery-staple"\n',
        )
        if item.detector == "generic_secret_assignment"
    )
    exception_path = tmp_path / ".graphify-secret-exceptions.yaml"
    write_exceptions(exception_path, finding.fingerprint)
    text = exception_path.read_text(encoding="utf-8")
    if change == "fingerprint":
        text = text.replace(finding.fingerprint, "sha256:" + "0" * 64)
    elif change == "path":
        text = text.replace("src/settings.ts", "src/other.ts")
    else:
        text = text + "  - path: src/settings.ts\n" + text.split("  - path: src/settings.ts\n", 1)[1]
    exception_path.write_text(text, encoding="utf-8")

    with pytest.raises(SecretExceptionError):
        load_secret_exceptions(tmp_path, (finding,))


def test_exception_load_rejects_file_swapped_to_symlink_after_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finding = next(
        item
        for item in scan_payload(
            PurePosixPath("src/settings.ts"),
            b'api_key = "correct-horse-battery-staple"\n',
        )
        if item.detector == "generic_secret_assignment"
    )
    exception_path = tmp_path / ".graphify-secret-exceptions.yaml"
    write_exceptions(exception_path, finding.fingerprint)
    external = tmp_path / "external.yaml"
    external.write_bytes(exception_path.read_bytes())
    displaced = tmp_path / "displaced.yaml"
    real_stat = os.stat
    swapped = False

    def swap_after_stat(path: object, *args: object, **kwargs: object):
        nonlocal swapped
        result = real_stat(path, *args, **kwargs)
        if (Path(path) == exception_path or path == exception_path.name) and not swapped:
            swapped = True
            exception_path.rename(displaced)
            os.symlink(external, exception_path)
        return result

    monkeypatch.setattr(os, "stat", swap_after_stat)

    with pytest.raises(SecretExceptionError):
        load_secret_exceptions(tmp_path, (finding,))
    assert swapped
