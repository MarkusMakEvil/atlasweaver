"""Deterministic, least-authority CLI for the project knowledge workflow."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Callable, Sequence

from .adapter import AdapterError, adapt_candidate
from .artifacts import ArtifactValidationError, promote_graph, validate_candidate
from .atlas import (
    AtlasOwnershipError,
    generated_root,
    load_prepared_atlas_candidate,
    prepare_atlas_candidate,
    promote_atlas,
    validate_atlas_candidate,
)
from .graphify import (
    GraphifyError,
    SubprocessCommandRunner,
    probe_graphify,
)
from .health import assess_health, inspect_project_state, safe_input_snapshot
from .manifest import ManifestError, load_manifest
from .models import ProjectManifest
from .privacy import PrivacyError
from .receipt import ReceiptError, load_staging_receipt, verify_staged_input
from .secrets_scan import (
    SecretExceptionError,
    load_secret_exceptions,
    scan_repository,
)
from .staging import StagedInput, StagingError, stage_input_with_receipt


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CliFailure(Exception):
    code: str
    message: str
    exit_code: int = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="project-knowledge")
    sub = parser.add_subparsers(dest="command", required=True)
    for add_command in (
        add_detect,
        add_preflight,
        add_stage,
        add_adapt,
        add_scan_secrets,
        add_validate,
        add_promote,
        add_atlas_prepare,
        add_atlas_promote,
        add_health,
    ):
        add_command(sub)
    return parser


def _command(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
) -> argparse.ArgumentParser:
    parser = sub.add_parser(name)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def add_detect(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    _command(sub, "detect")


def add_preflight(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = _command(sub, "preflight")
    parser.add_argument("--graphify-binary", type=Path, default=Path("graphify"))
    parser.add_argument("--assistant-skill", type=Path)


def add_stage(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = _command(sub, "stage")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)


def add_adapt(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = _command(sub, "adapt")
    parser.add_argument("--staged-input", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--raw-candidate", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)


def add_scan_secrets(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    _command(sub, "scan-secrets")


def _add_graph_candidate_command(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], name: str
) -> None:
    parser = _command(sub, name)
    parser.add_argument("--candidate", type=Path, required=True)


def add_validate(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    _add_graph_candidate_command(sub, "validate")


def add_promote(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    _add_graph_candidate_command(sub, "promote")


def add_atlas_prepare(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = _command(sub, "atlas-prepare")
    parser.add_argument("--candidate", type=Path, required=True)


def add_atlas_promote(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = _command(sub, "atlas-promote")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--atlas", type=Path, required=True)


def add_health(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = _command(sub, "health")
    parser.add_argument("--atlas", type=Path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        repo = _real_repo(arguments.repo)
        manifest = _manifest(repo)
        document = _dispatch(arguments, repo, manifest)
    except CliFailure as error:
        return _emit_error(arguments, error)
    except ManifestError:
        return _emit_error(
            arguments, CliFailure("invalid_manifest", "project manifest is invalid")
        )
    except PrivacyError:
        return _emit_error(
            arguments, CliFailure("privacy_invalid", "privacy exclusions are invalid")
        )
    except SecretExceptionError:
        return _emit_error(
            arguments,
            CliFailure("secret_policy_invalid", "secret exception policy is invalid"),
        )
    except StagingError:
        return _emit_error(
            arguments, CliFailure("staging_failed", "safe input staging failed")
        )
    except ReceiptError:
        return _emit_error(
            arguments, CliFailure("staging_failed", "safe input receipt validation failed")
        )
    except AdapterError:
        return _emit_error(
            arguments, CliFailure("adaptation_failed", "Graphify candidate adaptation failed")
        )
    except ArtifactValidationError:
        return _emit_error(
            arguments,
            CliFailure("artifact_invalid", "graph candidate validation failed"),
        )
    except AtlasOwnershipError:
        return _emit_error(
            arguments,
            CliFailure("atlas_invalid", "atlas candidate or namespace is invalid"),
        )
    except GraphifyError:
        return _emit_error(
            arguments,
            CliFailure("graphify_contract_failed", "Graphify preflight failed"),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _emit_error(
            arguments,
            CliFailure("operation_failed", "project knowledge operation failed"),
        )
    _emit(document, as_json=arguments.as_json)
    return 0


def _dispatch(
    arguments: argparse.Namespace, repo: Path, manifest: ProjectManifest
) -> dict[str, object]:
    handlers: dict[str, Callable[[argparse.Namespace, Path, ProjectManifest], dict[str, object]]] = {
        "detect": _detect,
        "preflight": _preflight,
        "stage": _stage,
        "adapt": _adapt,
        "scan-secrets": _scan_secrets,
        "validate": _validate,
        "promote": _promote,
        "atlas-prepare": _atlas_prepare,
        "atlas-promote": _atlas_promote,
        "health": _health,
    }
    return handlers[arguments.command](arguments, repo, manifest)


def _detect(
    arguments: argparse.Namespace, repo: Path, manifest: ProjectManifest
) -> dict[str, object]:
    del arguments
    health = assess_health(inspect_project_state(repo, manifest))
    return _success(
        "detect",
        "detected",
        project_id=manifest.project_id,
        graph_status=health.status,
        source_matches=health.source_matches,
    )


def _preflight(
    arguments: argparse.Namespace, repo: Path, manifest: ProjectManifest
) -> dict[str, object]:
    snapshot = safe_input_snapshot(repo, manifest)
    capabilities = probe_graphify(
        arguments.graphify_binary,
        manifest.graphify_version,
        SubprocessCommandRunner(),
        assistant_skill=arguments.assistant_skill,
    )
    return _success(
        "preflight",
        "ready",
        project_id=manifest.project_id,
        graphify_version=capabilities.version,
        safe_file_count=len(snapshot.files),
        obsidian_export=capabilities.supports_obsidian_export,
    )


def _stage(
    arguments: argparse.Namespace, repo: Path, manifest: ProjectManifest
) -> dict[str, object]:
    staged = stage_input_with_receipt(
        repo, manifest, arguments.destination, arguments.receipt
    )
    return _success(
        "stage",
        "staged",
        project_id=manifest.project_id,
        source_digest=staged.source_digest,
        file_count=len(staged.files),
    )


def _adapt(
    arguments: argparse.Namespace, repo: Path, manifest: ProjectManifest
) -> dict[str, object]:
    receipt = load_staging_receipt(arguments.receipt, manifest)
    staged = verify_staged_input(arguments.staged_input, receipt)
    _require_current_input(repo, manifest, staged)
    adapted = adapt_candidate(
        arguments.raw_candidate,
        arguments.destination,
        staged,
        manifest,
        post_write_check=lambda: _require_current_input(repo, manifest, staged),
    )
    return _success(
        "adapt",
        "adapted",
        project_id=manifest.project_id,
        source_digest=adapted.source_digest,
        node_count=adapted.node_count,
        edge_count=adapted.edge_count,
        skipped_count=adapted.skipped_count,
    )


def _scan_secrets(
    arguments: argparse.Namespace, repo: Path, manifest: ProjectManifest
) -> dict[str, object]:
    del arguments
    findings = scan_repository(repo, manifest)
    accepted = load_secret_exceptions(repo, findings)
    unaccepted = [
        finding for finding in findings if finding.fingerprint not in accepted
    ]
    return _success(
        "scan-secrets",
        "scanned",
        finding_count=len(unaccepted),
        excepted_count=len(findings) - len(unaccepted),
        findings=[
            {
                "detector": finding.detector,
                "path": finding.path.as_posix(),
                "line": finding.line,
                "fingerprint": finding.fingerprint,
            }
            for finding in unaccepted
        ],
    )


def _validate(
    arguments: argparse.Namespace, repo: Path, manifest: ProjectManifest
) -> dict[str, object]:
    staged = _current_input(repo, manifest)
    with tempfile.TemporaryDirectory(prefix="project-knowledge-validate-") as temporary:
        clone = _clone_graph_candidate(arguments.candidate, Path(temporary) / "candidate")
        validated = validate_candidate(clone, staged, manifest)
        _require_current_input(repo, manifest, staged)
        return _success(
            "validate",
            "valid",
            project_id=manifest.project_id,
            graph_digest=validated.graph_digest,
            node_count=validated.node_count,
            edge_count=validated.edge_count,
        )


def _promote(
    arguments: argparse.Namespace, repo: Path, manifest: ProjectManifest
) -> dict[str, object]:
    staged = _current_input(repo, manifest)
    with tempfile.TemporaryDirectory(prefix="project-knowledge-promote-") as temporary:
        clone = _clone_graph_candidate(arguments.candidate, Path(temporary) / "candidate")
        validated = validate_candidate(clone, staged, manifest)
        _require_current_input(repo, manifest, staged)
        promoted = promote_graph(validated, repo)
        return _success(
            "promote",
            "promoted",
            project_id=manifest.project_id,
            graph_digest=promoted.digest,
        )


def _atlas_prepare(
    arguments: argparse.Namespace, repo: Path, manifest: ProjectManifest
) -> dict[str, object]:
    del repo
    prepared = prepare_atlas_candidate(
        arguments.candidate, manifest, manifest.graphify_version
    )
    return _success(
        "atlas-prepare",
        "prepared",
        project_id=manifest.project_id,
        file_count=len(prepared.files),
        generator_version=prepared.generator_version,
    )


def _atlas_promote(
    arguments: argparse.Namespace, repo: Path, manifest: ProjectManifest
) -> dict[str, object]:
    del repo
    prepared = load_prepared_atlas_candidate(arguments.candidate, manifest)
    validated = validate_atlas_candidate(prepared, manifest)
    promoted = promote_atlas(validated, arguments.atlas, manifest)
    return _success(
        "atlas-promote",
        "promoted",
        project_id=manifest.project_id,
        atlas_digest=promoted.digest,
        changed=promoted.changed,
    )


def _health(
    arguments: argparse.Namespace, repo: Path, manifest: ProjectManifest
) -> dict[str, object]:
    atlas_available = _atlas_available(arguments.atlas, manifest)
    registry_matches = _registry_matches(repo, manifest)
    state = inspect_project_state(
        repo,
        manifest,
        atlas_available=atlas_available,
        registry_matches=registry_matches,
    )
    health = assess_health(state)
    return {"schema_version": SCHEMA_VERSION, "command": "health", **health.to_dict()}


def _current_input(repo: Path, manifest: ProjectManifest) -> StagedInput:
    snapshot = safe_input_snapshot(repo, manifest)
    return StagedInput(
        root=repo,
        source_digest=snapshot.source_digest,
        files=snapshot.files,
    )


def _require_current_input(
    repo: Path, manifest: ProjectManifest, expected: StagedInput
) -> None:
    current = safe_input_snapshot(repo, manifest)
    if current.source_digest != expected.source_digest or current.files != expected.files:
        raise StagingError("safe source changed during candidate validation")


def _clone_graph_candidate(source: Path, destination: Path) -> Path:
    """Capture root-level regular candidate files without following links."""
    try:
        root = _real_directory(source, "graph candidate")
    except StagingError as error:
        raise ArtifactValidationError("graph candidate must be a real directory") from error
    source_fd = _open_directory(root)
    destination.mkdir(mode=0o700, exist_ok=False)
    try:
        with os.scandir(os.dup(source_fd)) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            before = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                raise ArtifactValidationError(
                    "graph candidate entries must be regular files"
                )
            descriptor = os.open(
                name, os.O_RDONLY | _nofollow_flag(), dir_fd=source_fd
            )
            try:
                opened = os.fstat(descriptor)
                if _identity(before) != _identity(opened):
                    raise ArtifactValidationError("graph candidate changed during capture")
                chunks: list[bytes] = []
                while chunk := os.read(descriptor, 1024 * 1024):
                    chunks.append(chunk)
                after = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
                if _identity(opened) != _identity(after):
                    raise ArtifactValidationError("graph candidate changed during capture")
            finally:
                os.close(descriptor)
            target = destination / name
            with target.open("xb") as output:
                os.chmod(target, 0o600)
                output.write(b"".join(chunks))
        return destination.resolve()
    finally:
        os.close(source_fd)


def _atlas_available(atlas: Path | None, manifest: ProjectManifest) -> bool:
    if atlas is None:
        return False
    try:
        root = _real_directory(atlas, "atlas")
        generated = generated_root(root, manifest)
        _real_directory(generated, "generated atlas namespace")
        load_prepared_atlas_candidate(generated, manifest)
    except (OSError, AtlasOwnershipError, CliFailure, StagingError):
        return False
    return True


def _registry_matches(repo: Path, manifest: ProjectManifest) -> bool:
    try:
        ownership = json.loads((repo / "graphify-out/.project-knowledge-ownership.json").read_text())
        registry = Path(os.environ.get("HOME", "")) / ".graphify/global-manifest.json"
        document = json.loads(registry.read_text())
        entry = document["repos"][manifest.project_id]
        return document.get("version") == 1 and entry.get("source_hash") == str(ownership["graph_digest"])[:16]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _manifest(repo: Path) -> ProjectManifest:
    return load_manifest(repo / ".graphify-project.yaml", repo)


def _real_repo(path: Path) -> Path:
    try:
        info = path.lstat()
    except OSError as error:
        raise CliFailure("invalid_repo", "repository must be a real directory") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CliFailure("invalid_repo", "repository must be a real directory")
    return path.absolute()


def _real_directory(path: Path, description: str) -> Path:
    absolute = path.absolute()
    try:
        info = absolute.lstat()
    except OSError as error:
        raise StagingError(f"{description} is unavailable") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise StagingError(f"{description} must be a real directory")
    descriptor = _open_directory(absolute)
    os.close(descriptor)
    return absolute


def _open_directory(path: Path) -> int:
    return os.open(path, _directory_flags())


def _directory_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    except AttributeError as error:
        raise StagingError("safe directory traversal is unavailable") from error


def _nofollow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as error:
        raise StagingError("safe file traversal is unavailable") from error


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev


def _success(command: str, status: str, **fields: object) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "status": status,
        **fields,
    }


def _emit(document: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(document, sort_keys=True, separators=(",", ":")))
        return
    command = str(document["command"])
    status = str(document["status"])
    print(f"{command}: {status}")
    for key in sorted(document):
        if key not in {"schema_version", "command", "status"}:
            value = document[key]
            if isinstance(value, list):
                value = ",".join(str(item) for item in value)
            print(f"{key}: {value}")


def _emit_error(arguments: argparse.Namespace, error: CliFailure) -> int:
    command = getattr(arguments, "command", "unknown")
    document = {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "status": "error",
        "error": {"code": error.code, "message": error.message},
    }
    if getattr(arguments, "as_json", False):
        print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    else:
        print(f"{error.code}: {error.message}", file=sys.stderr)
    return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
