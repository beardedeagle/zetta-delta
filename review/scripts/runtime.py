"""Identify review code and install complete, independently pinned releases."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SCHEMA_VERSION = 1
MODULES = ("handoff.py", "intake.py", "preparation.py", "publication.py",
           "review_contract.py", "runtime.py", "workflow.py")
HELPERS = ("identity.py", "ensure-context-indexes.py")
MANIFEST = "runtime-manifest.json"


def file_hash(path: Path) -> str:
    """Hash a regular code file without accepting a redirected file.

    Args:
        path (Path): Expected regular source or installed file.

    Returns:
        str: SHA-256 content digest.

    Raises:
        OSError: The file cannot be read.
        ValueError: The path is redirected or is not a regular file.
    """
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("runtime code must be a regular file")
        return hashlib.file_digest(stream, "sha256").hexdigest()


def code_identity(directory: Path) -> dict:
    """Identify only owned review modules, independently of install location.

    Args:
        directory (Path): Directory containing every owned review module.

    Returns:
        dict: Schema, canonical file hashes and their aggregate fingerprint.

    Raises:
        OSError: An owned module cannot be read.
        ValueError: An owned module is not a regular file.
    """
    files = {name: file_hash(directory / name) for name in MODULES}
    value = {"schema_version": SCHEMA_VERSION, "files": files}
    return dict(value, fingerprint=hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest())


def validate_identity(value: dict) -> None:
    """Validate a stored runtime identity before comparing or replaying it.

    Args:
        value (dict): Identity supplied by a manifest or run record.

    Raises:
        ValueError: Fields, schema or content fingerprint are inconsistent.
    """
    if (not isinstance(value, dict) or set(value) != {"schema_version", "files", "fingerprint"}
            or type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION
            or not isinstance(value["files"], dict) or set(value["files"]) != set(MODULES)
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in value["files"].values())):
        raise ValueError("invalid or unsupported review runtime identity")
    payload = {"schema_version": value["schema_version"], "files": value["files"]}
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if value["fingerprint"] != expected:
        raise ValueError("review runtime identity fingerprint is inconsistent")


def identity() -> dict:
    """Return this release's identity, checking its installation receipt if present.

    Returns:
        dict: Portable identity of the current review code and journal schema.

    Raises:
        OSError: Code or an installed manifest cannot be read.
        ValueError: The installed manifest does not bind the current code.
    """
    directory = Path(__file__).resolve().parent
    current = code_identity(directory)
    receipt = directory / MANIFEST
    if receipt.exists() or receipt.is_symlink():
        try:
            if receipt.is_symlink():
                raise ValueError("redirected manifest")
            recorded = json.loads(receipt.read_text())["runtime"]
            validate_identity(recorded)
            if recorded != current:
                raise ValueError("code differs")
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("installed review runtime manifest does not match its code") from error
    return current


def verify_recorded(expected: dict) -> None:
    """Require the runtime recorded by a mutating run command to be current.

    Args:
        expected (dict): Identity previously recorded in the run journal.

    Raises:
        OSError: Current runtime code cannot be read.
        ValueError: The identity is malformed or the runtime differs.
    """
    validate_identity(expected)
    if expected != identity():
        raise ValueError("review runtime differs from the run; record an explicit runtime adoption before mutation")


def install_sources() -> dict[str, Path]:
    """Locate runtime modules and independently bound helper copies to install.

    Returns:
        dict[str, Path]: Installed filenames mapped to their source files.
    """
    scripts = Path(__file__).resolve().parent
    root = scripts.parents[1]
    sources = {name: scripts / name for name in (*MODULES, *HELPERS)}
    if not sources["identity.py"].exists():
        sources["identity.py"] = root / "skills/orchestrate/scripts/identity.py"
    if not sources["ensure-context-indexes.py"].exists():
        sources["ensure-context-indexes.py"] = root / "stack/context-indexes/ensure-context-indexes.py"
    return sources


def owned_directory(path: Path) -> None:
    """Refuse redirected, foreign-owned or externally writable runtime storage.

    Args:
        path (Path): Existing directory managed by this installer.

    Raises:
        OSError: Directory metadata cannot be read.
        ValueError: The directory is not safe for owned code or backups.
    """
    info = path.lstat()
    if (path.resolve() != path or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError("runtime storage must be an owned, nonredirected directory without external write access")


def source_revision(sources: dict[str, Path]) -> str | None:
    """Read optional source provenance; installed content hashes remain authoritative.

    Args:
        sources (dict[str, Path]): Exact files selected for the release.

    Returns:
        str | None: Git revision of a matching source checkout, otherwise None.
    """
    root = sources["runtime.py"].resolve().parents[2]
    try:
        location = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
        if location.returncode or Path(location.stdout.strip()).resolve() != root:
            return None
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True, check=False)
        value = commit.stdout.strip()
        return value if commit.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) else None
    except OSError:
        return None


def install(destination: Path, backup_root: Path, *, sources: dict[str, Path] | None = None,
            force: bool = False, dry_run: bool = False) -> dict:
    """Stage a verified release and switch future commands without mixing files.

    Args:
        destination (Path): Absolute shared review directory, containing runs.
        backup_root (Path): Absolute backup root for the replaced scripts entry.
        sources (dict[str, Path] | None): Source files; defaults to this bundle.
        force (bool): Permit replacement of an existing scripts entry.
        dry_run (bool): Validate and describe the operation without writes.

    Returns:
        dict: Runtime identity, changed status, release and optional backup path.

    Raises:
        OSError: Source, staging, backup or replacement fails.
        ValueError: Ownership, code consistency or replacement authorization fails.
        BlockingIOError: Another installer holds the release-switch lock.
    """
    sources = sources if sources is not None else install_sources()
    if set(sources) != {*MODULES, *HELPERS}:
        raise ValueError("runtime installation requires the exact module and helper set")
    expected_files = {name: file_hash(path) for name, path in sorted(sources.items())}
    expected = code_identity(sources["runtime.py"].parent)
    if any(expected["files"][name] != expected_files[name] for name in MODULES):
        raise ValueError("runtime source changed during installation preflight")
    for path in (destination, backup_root):
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError("runtime and backup directories must be absolute and nonredirected")
        existing = next(parent for parent in (path, *path.parents) if parent.exists())
        owned_directory(existing)
    if destination.is_relative_to(backup_root) or backup_root.is_relative_to(destination):
        raise ValueError("runtime and backup directories must not overlap")
    scripts = destination / "scripts"
    releases = destination / "releases"
    if releases.exists() or releases.is_symlink():
        owned_directory(releases)
    if scripts.is_symlink():
        target = scripts.resolve()
        if (scripts.lstat().st_uid != os.getuid() or not target.is_relative_to(releases)
                or len(target.relative_to(releases).parts) != 2 or target.name != "scripts"):
            raise ValueError("runtime scripts link must name an owned release")
        owned_directory(target)
    elif scripts.exists():
        owned_directory(scripts)
    present = scripts.exists() or scripts.is_symlink()
    if present and not force:
        raise ValueError("runtime scripts already exist; rerun with --force to preserve and replace them")
    if dry_run:
        return {"runtime": expected, "changed": True, "dry_run": True, "destination": str(scripts)}
    destination.mkdir(parents=True, exist_ok=True)
    owned_directory(destination)
    descriptor = os.open(destination / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode) or os.fstat(lock.fileno()).st_uid != os.getuid():
            raise ValueError("runtime installation lock is not owned")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if scripts.is_symlink():
            current = scripts.resolve()
            receipt = current / MANIFEST
            try:
                if (receipt.is_file() and not receipt.is_symlink()
                        and json.loads(receipt.read_text()).get("runtime") == expected
                        and all(file_hash(current / name) == digest for name, digest in expected_files.items())):
                    return {"runtime": expected, "changed": False, "release": str(current), "backup": None}
            except (OSError, ValueError, AttributeError):
                # A broken old receipt must not prevent an explicitly forced repair.
                pass
        releases.mkdir(exist_ok=True)
        owned_directory(releases)
        with tempfile.TemporaryDirectory(prefix=".pending-", dir=releases) as temporary:
            pending = Path(temporary)
            staged = pending / "scripts"
            staged.mkdir()
            for name, source in sources.items():
                shutil.copy2(source, staged / name)
                (staged / name).chmod(0o644)
            if (any(file_hash(staged / name) != digest for name, digest in expected_files.items())
                    or any(file_hash(sources[name]) != digest for name, digest in expected_files.items())):
                raise ValueError("runtime source changed during installation")
            receipt = {"version": 1, "runtime": expected, "installed_files": expected_files,
                       "source_revision": source_revision(sources)}
            (staged / MANIFEST).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            (staged / MANIFEST).chmod(0o644)
            release = releases / (expected["fingerprint"][:16] + "-" + uuid.uuid4().hex)
            pending.rename(release)
        replacement = destination / (".scripts-" + uuid.uuid4().hex)
        replacement.symlink_to(release / "scripts", target_is_directory=True)
        backup = None
        moved = False
        try:
            if scripts.exists() or scripts.is_symlink():
                backup = backup_root / str(scripts).lstrip(os.sep)
                backup.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists() or backup.is_symlink():
                    raise ValueError("runtime backup already exists; use a fresh backup root")
                if scripts.is_symlink():
                    backup.symlink_to(scripts.resolve(), target_is_directory=True)
                else:
                    scripts.rename(backup)
                    moved = True
            replacement.replace(scripts)
        except BaseException:
            if moved and backup is not None and not scripts.exists():
                backup.rename(scripts)
            raise
        finally:
            replacement.unlink(missing_ok=True)
        return {"runtime": expected, "changed": True, "release": str(release / "scripts"),
                "backup": str(backup) if backup is not None else None}


def main() -> int:
    """Run the standalone identity or runtime-only installation command.

    Returns:
        int: Zero on success, one for an invalid or failed installation.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("identity")
    installer = subparsers.add_parser("install")
    installer.add_argument("--destination", type=Path, required=True)
    installer.add_argument("--backup-root", type=Path, required=True)
    installer.add_argument("--force", action="store_true")
    installer.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = identity() if args.command == "identity" else install(args.destination, args.backup_root, force=args.force, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print("review runtime: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
