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


def read_installation_manifest(directory: Path, current: dict) -> dict:
    """Read a required installation receipt and verify its complete installed bytes.

    Args:
        directory (Path): Release scripts directory containing the receipt.
        current (dict): Identity freshly computed from that directory's modules.

    Returns:
        dict: Installation manifest authenticated against all modules and helpers.

    Raises:
        OSError: The manifest or an installed helper cannot be read.
        ValueError: The manifest is invalid or differs from the installed bytes.
    """
    receipt = directory / MANIFEST
    try:
        if receipt.is_symlink():
            raise ValueError("redirected manifest")
        with os.fdopen(os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "r") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("manifest must be a regular file")
            manifest = json.load(source)
        if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int
                or manifest["version"] != 1):
            raise ValueError("unsupported installation manifest")
        recorded = manifest["runtime"]
        validate_identity(recorded)
        installed = manifest["installed_files"]
        if (not isinstance(installed, dict) or set(installed) != {*MODULES, *HELPERS}
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in installed.values())):
            raise ValueError("invalid installed file receipt")
        actual = {**current["files"], **{name: file_hash(directory / name) for name in HELPERS}}
        if recorded != current or installed != actual:
            raise ValueError("code differs")
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("installed review runtime manifest does not match its code") from error
    return manifest


def identity() -> dict:
    """Return this release's identity and verify its installed modules and helpers.

    Returns:
        dict: Portable identity of the current review code and journal schema.

    Raises:
        OSError: Code or a required installation manifest cannot be read.
        ValueError: The installed manifest does not bind the current code/helpers.
    """
    directory = Path(__file__).resolve().parent
    current = code_identity(directory)
    receipt = directory / MANIFEST
    managed = directory.name == "scripts" and directory.parent.parent.name == "releases"
    if managed or receipt.exists() or receipt.is_symlink():
        read_installation_manifest(directory, current)
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


def validate_storage_ancestors(path: Path, *, include_self: bool = False) -> None:
    """Reject ancestor directories that permit another user to replace storage.

    Args:
        path (Path): Canonical managed directory or existing creation parent.
        include_self (bool): Also validate an existing parent of new storage.

    Raises:
        OSError: An ancestor's metadata cannot be read.
        ValueError: An ancestor is foreign-owned or writable without sticky protection.
    """
    for ancestor in (path, *path.parents) if include_self else path.parents:
        info = ancestor.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
            raise ValueError("runtime storage ancestor must be trusted and protect against external replacement")


def owned_directory(path: Path) -> None:
    """Require private owned storage beneath trusted, replacement-safe ancestors.

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
    validate_storage_ancestors(path)


def make_owned_directory(path: Path) -> None:
    """Create missing managed directory components with private permissions.

    Args:
        path (Path): Absolute runtime or backup directory to create or validate.

    Raises:
        OSError: A component cannot be created or inspected.
        ValueError: Existing or newly created storage fails ownership validation.
    """
    missing = []
    existing = path
    while not existing.exists():
        missing.append(existing)
        existing = existing.parent
    if missing:
        validate_storage_ancestors(existing, include_self=True)
    else:
        owned_directory(existing)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        owned_directory(directory)


def validate_installation_storage(destination: Path, backup_root: Path, *, force: bool) -> None:
    """Validate current storage and replacement authorization before using it.

    Args:
        destination (Path): Absolute shared review directory, containing runs.
        backup_root (Path): Absolute backup root for the replaced scripts entry.
        force (bool): Whether an existing scripts entry may be replaced.

    Raises:
        OSError: Existing storage metadata cannot be read.
        ValueError: Ownership, redirection, overlap or replacement checks fail.
    """
    for path in (destination, backup_root):
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError("runtime and backup directories must be absolute and nonredirected")
        existing = next(parent for parent in (path, *path.parents) if parent.exists())
        if existing == path:
            owned_directory(existing)
        else:
            validate_storage_ancestors(existing, include_self=True)
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
        owned_directory(target.parent)
        owned_directory(target)
    elif scripts.exists():
        owned_directory(scripts)
    if (scripts.exists() or scripts.is_symlink()) and not force:
        raise ValueError("runtime scripts already exist; rerun with --force to preserve and replace them")


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


def remove_retired_directory(path: Path) -> None:
    """Remove an owned legacy copy after its backup and replacement are active.

    Args:
        path (Path): Retired local scripts directory, never the completed backup.

    Raises:
        OSError: Directory access, permission repair or removal fails.
        PermissionError: A read-only directory is owned by another user.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        for _, _, _, directory in os.fwalk(".", dir_fd=descriptor, follow_symlinks=False):
            info = os.fstat(directory)
            if not info.st_mode & stat.S_IWUSR:
                if info.st_uid != os.getuid():
                    raise PermissionError("cannot make a foreign-owned retired runtime directory writable")
                os.fchmod(directory, stat.S_IMODE(info.st_mode) | stat.S_IWUSR)
    finally:
        os.close(descriptor)
    shutil.rmtree(path)


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
        dict: Runtime identity, changed status, release, optional backup path and
            cleanup_pending details if the activated install retains an old copy.

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
    validate_installation_storage(destination, backup_root, force=force)
    scripts = destination / "scripts"
    releases = destination / "releases"
    if dry_run:
        return {"runtime": expected, "changed": True, "dry_run": True, "destination": str(scripts)}
    make_owned_directory(destination)
    descriptor = os.open(destination / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode) or os.fstat(lock.fileno()).st_uid != os.getuid():
            raise ValueError("runtime installation lock is not owned")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        validate_installation_storage(destination, backup_root, force=force)
        if scripts.is_symlink():
            current = scripts.resolve()
            try:
                manifest = read_installation_manifest(current, code_identity(current))
                if manifest["runtime"] == expected and manifest["installed_files"] == expected_files:
                    return {"runtime": expected, "changed": False, "release": str(current), "backup": None}
            except (OSError, ValueError):
                # A broken old receipt must not prevent an explicitly forced repair.
                pass
        make_owned_directory(releases)
        with tempfile.TemporaryDirectory(prefix=".pending-", dir=releases) as temporary:
            pending = Path(temporary)
            staged = pending / "scripts"
            staged.mkdir(mode=0o700)
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
        previous = None
        try:
            if scripts.exists() or scripts.is_symlink():
                backup = backup_root / str(scripts).lstrip(os.sep)
                make_owned_directory(backup.parent)
                if backup.exists() or backup.is_symlink():
                    raise ValueError("runtime backup already exists; use a fresh backup root")
                if scripts.is_symlink():
                    backup.symlink_to(scripts.resolve(), target_is_directory=True)
                else:
                    # The configured backup root may be on another filesystem.
                    # Complete its copy before moving the active local directory.
                    with tempfile.TemporaryDirectory(prefix=".scripts-backup-", dir=backup.parent) as temporary:
                        copied = Path(temporary) / "scripts"
                        shutil.copytree(scripts, copied, symlinks=True)
                        copied.rename(backup)
                    previous = destination / (".scripts-previous-" + uuid.uuid4().hex)
                    scripts.rename(previous)
            replacement.replace(scripts)
        except BaseException:
            if previous is not None and previous.exists() and not scripts.exists():
                previous.rename(scripts)
            raise
        finally:
            replacement.unlink(missing_ok=True)
        result = {"runtime": expected, "changed": True, "release": str(release / "scripts"),
                  "backup": str(backup) if backup is not None else None}
        if previous is not None:
            try:
                remove_retired_directory(previous)
            except OSError as error:
                result["cleanup_pending"] = {"path": str(previous), "error": str(error)}
        return result


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
