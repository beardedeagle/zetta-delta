"""Prepare independent review repositories; detect writes without restoring files."""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

import intake

INDEX_TOOLS = ("tgrep", "codegraph", "zg")
DIFF_OPTIONS = ("--no-ext-diff", "--no-textconv", "--no-color", "--find-renames=50%", "-l1000",
                "--ignore-submodules=none", "--diff-algorithm=myers", "--no-indent-heuristic")
PATCH_OPTIONS = ("--patch", "--binary", "--full-index", "--unified=3")
GIT_METADATA = ("index", "HEAD", "FETCH_HEAD", "config", "config.worktree", "packed-refs", "info/attributes", "objects/info/alternates")


class NeedsDecision(ValueError):
    """Carry a precise preparation hold without choosing missing semantics."""

    def __init__(self, code: str, reason: str, observation: dict | None = None):
        """Initialize the hold.

        Args:
            code (str): Machine-readable decision identifier.
            reason (str): Evidence or decision required to continue.
            observation (dict | None): Actual integrity failure to persist before holding.
        """
        super().__init__(reason)
        self.code = code
        self.observation = observation


def git(root: Path, *args: str, input: bytes | None = None, auth: bool = False) -> bytes:
    """Run unfiltered Git with isolated configuration and no optional source writes.

    Args:
        root (Path): Explicit repository or initialization directory.
        *args (str): Literal Git arguments, never shell code.
        input (bytes | None): Optional standard input.
        auth (bool): Retain host credential configuration for HTTPS fetch only.

    Returns:
        bytes: Complete command output.

    Raises:
        ValueError: Git failed; credentials and arbitrary stderr are not exposed.
        OSError: The executable could not be started.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="file:https",
               GIT_CONFIG_NOSYSTEM="1", LC_ALL="C")
    if not auth:
        env["GIT_CONFIG_GLOBAL"] = os.devnull
    command = ["rtk", "proxy", "git", "-c", "core.hooksPath=" + os.devnull,
               "-c", "core.autocrlf=false", "-c", "core.attributesFile=" + os.devnull,
               "-c", "core.fsmonitor=false", "-C", str(root), *args]
    result = subprocess.run(command, input=input, capture_output=True, env=env, check=False)
    if result.returncode:
        raise ValueError(f"Git {args[0]} failed in {root} (exit {result.returncode})")
    return result.stdout


def oid(value: str) -> str:
    """Validate a canonical Git object ID.

    Args:
        value (str): SHA-1 or SHA-256 object ID.

    Returns:
        str: Validated ID.

    Raises:
        ValueError: The ID is not canonical hexadecimal.
    """
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        raise ValueError("invalid canonical Git object ID")
    return value


def relative_path(value: str) -> str:
    """Validate a literal repository path, including unusual Git filenames.

    Args:
        value (str): Repository-relative path.

    Returns:
        str: Unmodified path.

    Raises:
        ValueError: The path escapes the repository or addresses Git metadata.
    """
    if (not isinstance(value, str) or not value or "\0" in value or value.startswith("/")
            or any(part in ("", ".", "..") or part.casefold() == ".git" for part in value.split("/"))):
        raise ValueError("invalid repository-relative path")
    return value


def resolve(root: Path, ref: str) -> str:
    """Resolve a caller-selected revision to a commit without changing refs.

    Args:
        root (Path): Source repository.
        ref (str): Explicit revision expression.

    Returns:
        str: Canonical commit ID.

    Raises:
        NeedsDecision: The requested commit is unavailable or ambiguous.
        ValueError: The expression is empty or contains control characters.
    """
    if not isinstance(ref, str) or not ref.strip() or any(c in ref for c in "\0\r\n"):
        raise ValueError("invalid comparison revision")
    try:
        return oid(git(root, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip())
    except ValueError as error:
        raise NeedsDecision("comparison_revision", f"resolve an available, unambiguous commit for {ref!r}") from error


def digest(path: Path) -> str:
    """Hash a regular file without loading it into memory.

    Args:
        path (Path): File to read.

    Returns:
        str: SHA-256 content digest.

    Raises:
        OSError: The file cannot be read.
    """
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def files(root: Path, exclude: tuple[str, ...] = (), *, nested_git: list[Path] | None = None) -> dict:
    """Inventory all content, including ignored files, without following symlinks.

    Args:
        root (Path): Existing directory; Git metadata is inventoried separately.
        exclude (tuple[str, ...]): Explicit new setup cache paths to skip while active.
        nested_git (list[Path] | None): Collect initialized nested checkouts for
            the Git metadata owner; never traverse their administrative stores.

    Returns:
        dict: Sorted paths bound to kind, permissions and content digest/link target.

    Raises:
        OSError: Content disappeared or could not be read.
        NeedsDecision: An unowned/unknown socket, device or FIFO cannot be safely
            fingerprinted. Known CodeGraph sockets retain their exact identity.
    """
    result = {}
    pending = [root]
    while pending:
        for path in sorted(pending.pop().iterdir()):
            if path.name.casefold() == ".git" and (path.name == ".git" or
                    ((path.parent / ".git").exists() and path.samefile(path.parent / ".git"))):
                if nested_git is not None and path.parent != root:
                    nested_git.append(path.parent)
                continue
            if str(path.relative_to(root)) in exclude:
                continue
            info = path.lstat()
            key = str(path.relative_to(root))
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                result[key] = ["link", mode, os.readlink(path)]
            elif stat.S_ISREG(info.st_mode):
                result[key] = ["file", mode, digest(path)]
            elif stat.S_ISDIR(info.st_mode):
                result[key] = ["directory", mode]
                pending.append(path)
            elif stat.S_ISSOCK(info.st_mode) and path.name == "socket" and path.parent.name == ".codegraph":
                checkout_root = path.parent.parent
                if checkout_root != root and (nested_git is None or checkout_root not in nested_git):
                    raise NeedsDecision("source_file_type", "CodeGraph socket is outside a recorded checkout")
                owner = path.parent.lstat()
                if (info.st_uid != os.getuid() or owner.st_uid != os.getuid()
                        or not stat.S_ISDIR(owner.st_mode) or (info.st_mode | owner.st_mode) & 0o022):
                    raise NeedsDecision("source_file_type", "CodeGraph socket ownership or permissions are unsafe")
                if checkout_root == root:
                    try:
                        checkout = Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel").rstrip(b"\n")))
                    except ValueError as error:
                        raise NeedsDecision("source_file_type", "CodeGraph socket requires a verified checkout") from error
                    if checkout != root:
                        raise NeedsDecision("source_file_type", "CodeGraph socket is outside its checkout")
                result[key] = ["socket", mode, info.st_uid, info.st_dev, info.st_ino, info.st_ctime_ns]
            else:
                raise NeedsDecision("source_file_type", f"cannot fingerprint special file {path}")
    return dict(sorted(result.items()))


def fingerprint(value: dict) -> str:
    """Hash a canonical JSON content ledger.

    Args:
        value (dict): Content/metadata ledger, containing no raw file contents.

    Returns:
        str: Stable SHA-256 digest.
    """
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def project_inventory(root: Path, exclusions: dict[str, str] | None = None,
                      destination: Path | None = None) -> dict:
    """Inventory current bytes without following links or opening special files.

    Args:
        root (Path): Canonical existing project directory.
        exclusions (dict[str, str] | None): Explicit relative subtrees and reasons.
        destination (Path | None): New private copy root, or None for observation.

    Returns:
        dict: Included entries, explicit exclusions and unsupported-entry gaps.

    Raises:
        OSError: A source entry cannot be read safely.
        ValueError: A source entry changes while being copied.
    """
    exclusions = exclusions or {}
    result: dict = {"entries": {}, "excluded": {}, "gaps": {}}
    directories = []
    if root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("project inventory requires a canonical directory")
    original = root.stat()

    def excluded(relative: str) -> str | None:
        if any(part.casefold() == ".git" for part in relative.split("/")):
            return "Git metadata"
        return next((reason for path, reason in exclusions.items()
                     if relative == path or relative.startswith(path + "/")), None)

    def fail(error: OSError) -> None:
        raise error

    for directory, dirs, files_, descriptor in os.fwalk(root, follow_symlinks=False, onerror=fail):
        relative_dir = Path(directory).relative_to(root)
        for name in sorted(dirs + files_):
            relative = (relative_dir / name).as_posix()
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            reason = excluded(relative)
            if reason:
                result["excluded"][relative] = reason
                if name in dirs:
                    dirs.remove(name)
                continue
            target = destination / relative if destination is not None else None
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                if name in dirs:
                    dirs.remove(name)
                link = os.readlink(name, dir_fd=descriptor)
                try:
                    resolved = (root / relative).resolve()
                    safe = (not os.path.isabs(link) and resolved.is_relative_to(root)
                            and excluded(resolved.relative_to(root).as_posix()) is None)
                except (OSError, RuntimeError):
                    safe = False
                row = {"kind": "symlink", "mode": mode, "target": link}
                if not safe:
                    result["gaps"][relative] = dict(row, reason="absolute, escaping, cyclic or excluded link; not copied")
                    continue
                result["entries"][relative] = row
                if target is not None:
                    target.symlink_to(link)
            elif stat.S_ISDIR(info.st_mode):
                result["entries"][relative] = {"kind": "directory", "mode": mode}
                if target is not None:
                    target.mkdir(mode=0o700)
                    directories.append((target, mode))
            elif stat.S_ISREG(info.st_mode):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                with os.fdopen(fd, "rb") as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
                        raise ValueError("project file changed during capture")
                    checksum = hashlib.sha256()
                    with ExitStack() as stack:
                        output = stack.enter_context(target.open("xb")) if target is not None else None
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            checksum.update(chunk)
                            if output is not None:
                                output.write(chunk)
                    after = os.fstat(source.fileno())
                    attributes = ("st_dev", "st_ino", "st_size", "st_mode", "st_mtime_ns", "st_ctime_ns")
                    if any(getattr(before, attr) != getattr(after, attr) for attr in attributes):
                        raise ValueError("project file changed during capture")
                result["entries"][relative] = {"kind": "file", "mode": mode, "size": before.st_size,
                                                "sha256": checksum.hexdigest()}
                if target is not None:
                    target.chmod(mode)
            else:
                result["gaps"][relative] = {"kind": "special", "mode": mode, "device": info.st_dev,
                                            "inode": info.st_ino, "type": stat.S_IFMT(info.st_mode),
                                            "reason": "nonregular filesystem entry; not copied"}
        dirs.sort()
    for path, mode in reversed(directories):
        path.chmod(mode)
    current = root.stat()
    if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
        raise ValueError("project root moved during capture")
    return result


def cache_identity(root: Path) -> dict:
    """Identify an existing disposable CodeGraph sidecar without opening SQLite.

    Args:
        root (Path): Canonical protected project root.

    Returns:
        dict: Stable identities for the root, cache directory, database and SHM;
            empty when any entry is absent, redirected or unsafe.
    """
    result = {}
    try:
        for name in (".", ".codegraph", ".codegraph/codegraph.db", ".codegraph/codegraph.db-shm"):
            path = root / name
            info = path.lstat()
            directory = name in (".", ".codegraph")
            expected_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            if not expected_kind or path.resolve() != path or info.st_uid != os.getuid() or info.st_mode & 0o022:
                return {}
            result[name] = [info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode), info.st_nlink]
            if not directory:
                result[name].append(info.st_size)
        descriptor = os.open(root / ".codegraph/codegraph.db", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as database:
            info = os.fstat(database.fileno())
            actual = [info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode), info.st_nlink, info.st_size]
            if actual != result[".codegraph/codegraph.db"] or database.read(16) != b"SQLite format 3\0":
                return {}
    except OSError:
        return {}
    return result


def recover_cache(before: dict, current: dict, root: str) -> tuple[dict, list[dict]]:
    """Compare original ledgers while auditing one identity-stable SHM change.

    Args:
        before (dict): Immutable baseline with explicit cache-recovery identities.
        current (dict): Complete current ledger, including database/WAL and Git.
        root (str): Canonical protected root for the audit receipt.

    Returns:
        tuple[dict, list[dict]]: Comparison ledger and exact before/after hashes.
            Every other byte and metadata field retains its original comparison.
    """
    name = ".codegraph/codegraph.db-shm"
    old, new = before["files"].get(name), current["files"].get(name)
    if (not before.get("cache_identity") or before["cache_identity"] != current.get("cache_identity")
            or not old or not new or old[:2] != new[:2] or old[0] != "file" or old == new):
        return current, []
    normalized = dict(current, files=dict(current["files"]))
    normalized["files"][name] = old
    return normalized, [{"root": root, "path": name, "before": old[2], "after": new[2]}]


def project_baseline(root: Path, exclude: tuple[str, ...] = (), *, native_copies: tuple[dict, ...] = (),
                     cache_recovery: bool = False) -> dict:
    """Protect filesystem content and any existing Git administration separately.

    Args:
        root (Path): Canonical project or frozen filesystem root.
        exclude (tuple[str, ...]): Owned index cache paths during setup only.
        native_copies (tuple[dict, ...]): Exact registered native administration.
        cache_recovery (bool): Capture identities for explicitly excluded CodeGraph
            SHM metadata; default keeps legacy and reviewed-cache baselines strict.

    Returns:
        dict: Baseline with the existing file/Git ledger shape and root identity.

    Raises:
        OSError: Source content or administration is unreadable.
        ValueError: Source or nested Git ownership is invalid.
    """
    copies = [row for row in native_copies if Path(row["root"]).is_relative_to(root)]
    native_roots = [str(Path(row["root"]).relative_to(root)) for row in copies]
    native_roots += [str(Path(row["git_dir"]).relative_to(root)) for row in native_copies
                     if "clone_store" in row and Path(row["git_dir"]).is_relative_to(root)]
    inventory = project_inventory(root, dict.fromkeys((*exclude, *native_roots), "owned runtime/cache path"))
    ledger = {}
    for name, row in {**inventory["entries"], **inventory["gaps"]}.items():
        kind, mode = row["kind"], row["mode"]
        ledger[name] = (["file", mode, row["sha256"]] if kind == "file" else
                        ["link", mode, row["target"]] if kind == "symlink" else
                        ["directory", mode] if kind == "directory" else ["special", row])
    for row in copies:
        for path, expected in row["created_dirs"].items():
            absolute = Path(row.get("project_root", row["parent_root"])) / path
            if absolute.is_relative_to(root) and ledger.get(str(absolute.relative_to(root))) == expected:
                del ledger[str(absolute.relative_to(root))]
    if (root / ".git").is_symlink():
        raise ValueError("project Git metadata was redirected")
    metadata = git_metadata(root, native_copies, allow_unborn=True) if (root / ".git").exists() else {}
    stores = {Path(common_dir(root))} if metadata else set()
    projects = [root]
    for name, reason in inventory["excluded"].items():
        checkout = (root / name).parent
        if reason != "Git metadata" or checkout == root:
            continue
        captured = nested_git_metadata(checkout, stores, projects, native_copies, allow_unborn=True)
        metadata.setdefault("nested", {})[str(checkout.relative_to(root))] = captured
        stores.add(Path(captured["common_dir"]))
        projects.append(checkout)
    info = root.stat()
    result = {"files": dict(sorted(ledger.items())), "git": metadata,
              "root_identity": [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]}
    if cache_recovery and (identities := cache_identity(root)):
        result["cache_identity"] = identities
    return result


def prepare_project(directory: Path, run_id: str, key: str, target: dict, parent: Path,
                    native_copies: tuple[dict, ...] = ()) -> dict:
    """Capture a filesystem target in the existing private resource ownership.

    Args:
        directory (Path): Private run directory outside the project.
        run_id (str): Owning run ID.
        key (str): Target ID derived from its canonical root.
        target (dict): Validated project request and exclusion rationale.
        parent (Path): Recorded canonical project root.
        native_copies (tuple[dict, ...]): Registered native bookkeeping.

    Returns:
        dict: Filesystem identity, complete manifest and owned artifact hashes.

    Raises:
        ValueError: Source, ownership or copy identity is invalid.
        NeedsDecision: The original project changes during capture.
        OSError: A required entry cannot be copied safely.
    """
    if parent != Path(target["project"]["source_root"]) or parent.resolve(strict=True) != parent:
        raise ValueError("project source differs from its recorded canonical root")
    if parent == directory or parent in directory.parents or directory in parent.parents:
        raise ValueError("run and project directories must be separate")
    exclusions = {row["path"]: row["reason"] for row in target["project"]["exclusions"]}
    cache_recovery = any(name in exclusions for name in (".codegraph", ".codegraph/codegraph.db-shm"))
    before = project_baseline(parent, native_copies=native_copies, cache_recovery=cache_recovery)
    resource = directory / ("snapshot-" + uuid.uuid4().hex)
    resource.mkdir(mode=0o700)
    save(resource / "owner.json", {"run_id": run_id, "target": key, "resource": resource.name})
    try:
        source = resource / "source"
        source.mkdir(mode=0o700)
        manifest = project_inventory(parent, exclusions, source)
        after = project_baseline(parent, native_copies=native_copies, cache_recovery=cache_recovery)
        if before != recover_cache(before, after, str(parent))[0] or manifest != project_inventory(parent, exclusions):
            raise NeedsDecision("source_integrity", "project changed during capture; preserve it and create a new run")
        copied = project_inventory(source)
        if copied["entries"] != manifest["entries"] or copied["gaps"] or copied["excluded"]:
            raise ValueError("project snapshot does not match the captured manifest")
        save(resource / "manifest.json", manifest)
        save(resource / "parent-baseline.json", before)
        source_baseline = project_baseline(source)
        save(resource / "source-baseline.json", source_baseline)
        return {"mode": "project", "source_root": str(source), "source_fingerprint": fingerprint(source_baseline),
                "parent_root": str(parent), "parent_fingerprint": fingerprint(before), "manifest": manifest,
                "manifest_fingerprint": fingerprint(manifest), "inventory": sorted(manifest["entries"]),
                "preparation": {"resource": resource.name, "artifacts": {
                    name: digest(resource / name) for name in ("owner.json", "manifest.json", "parent-baseline.json", "source-baseline.json")}}}
    except BaseException:
        remove_resource(owned(directory, resource.name, run_id, key))
        raise


def remove_resource(path: Path) -> None:
    """Remove an authenticated private copy, including read-only directories.

    Args:
        path (Path): Run-owned resource or test copy, validated by its caller.

    Raises:
        ValueError: The resource root was redirected.
        OSError: Private directories cannot be made removable or deleted.
    """
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("refuse removal of a redirected resource")
    for _, _, _, descriptor in os.fwalk(path, follow_symlinks=False):
        os.fchmod(descriptor, stat.S_IMODE(os.fstat(descriptor).st_mode) | 0o700)
    shutil.rmtree(path)


def validate_project_frozen(data: dict, target: dict) -> None:
    """Validate filesystem identity during replay without inventing Git commits.

    Args:
        data (dict): Generated filesystem preparation/finalization record.
        target (dict): Original canonical project request.

    Raises:
        ValueError: Identity, manifest or artifact bindings are inconsistent.
    """
    required = {"mode", "source_root", "source_fingerprint", "parent_root", "parent_fingerprint",
                "manifest", "manifest_fingerprint", "inventory", "preparation"}
    if set(data) != required or data["mode"] != "project" or data["parent_root"] != target["project"]["source_root"]:
        raise ValueError("project preparation differs from its requested filesystem target")
    for name in ("source_fingerprint", "parent_fingerprint", "manifest_fingerprint"):
        if not isinstance(data[name], str) or not re.fullmatch(r"[0-9a-f]{64}", data[name]):
            raise ValueError("project fingerprint must be SHA-256")
    manifest = data["manifest"]
    if (not isinstance(manifest, dict) or set(manifest) != {"entries", "excluded", "gaps"}
            or any(not isinstance(row, dict) for row in manifest.values())
            or fingerprint(manifest) != data["manifest_fingerprint"]
            or data["inventory"] != sorted(manifest["entries"])):
        raise ValueError("invalid complete project manifest")
    for name, row in manifest["entries"].items():
        relative_path(name)
        if not isinstance(row, dict) or type(row.get("mode")) is not int or not 0 <= row["mode"] <= 0o7777:
            raise ValueError("invalid project entry permissions")
        if row.get("kind") == "file":
            if (set(row) != {"kind", "mode", "size", "sha256"} or type(row["size"]) is not int or row["size"] < 0
                    or not isinstance(row["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])):
                raise ValueError("invalid project file identity")
        elif row.get("kind") == "directory":
            if set(row) != {"kind", "mode"}:
                raise ValueError("invalid project directory identity")
        elif row.get("kind") == "symlink":
            if set(row) != {"kind", "mode", "target"} or not isinstance(row["target"], str) or not row["target"] or "\0" in row["target"] or os.path.isabs(row["target"]):
                raise ValueError("invalid project link identity")
        else:
            raise ValueError("unsupported included project entry")
    proof = data["preparation"]
    if (not isinstance(proof, dict) or set(proof) != {"resource", "artifacts"}
            or not re.fullmatch(r"snapshot-[0-9a-f]{32}", proof["resource"])
            or not isinstance(proof["artifacts"], dict)
            or not {"owner.json", "manifest.json", "parent-baseline.json", "source-baseline.json"} <= proof["artifacts"].keys()
            or Path(data["source_root"]).parent.name != proof["resource"]
            or Path(data["source_root"]).name != "source" or not Path(data["source_root"]).is_absolute()):
        raise ValueError("invalid owned project preparation")
    for name, checksum in proof["artifacts"].items():
        if Path(name).name != name or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("invalid project artifact identity")


def common_dir(root: Path) -> str:
    """Identify the actual shared Git store for a canonical checkout.

    Args:
        root (Path): Canonical checkout root.

    Returns:
        str: Absolute Git common-directory path.

    Raises:
        ValueError: Git cannot identify its store.
    """
    path = Path(os.fsdecode(git(root, "rev-parse", "--git-common-dir").rstrip(b"\n")))
    return str((path if path.is_absolute() else root / path).resolve())


def clone_reference(root: Path, parent: Path, store: Path, head: str) -> str | None:
    """Identify the pin for Delta's exact split checkout/store layout.

    Args:
        root (Path): Canonical native checkout.
        parent (Path): Protected native project root.
        store (Path): Actual native Git common directory.
        head (str): Pinned native commit ID.

    Returns:
        str | None: Exact parent reference, or None for another checkout layout.
    """
    if not root.is_relative_to(parent):
        return None
    parts = root.relative_to(parent).parts
    if (len(parts) != 4 or parts[:2] != (".delta", "worktrees")
            or store != parent / ".delta/clones" / parts[2] / (parts[3] + ".git")):
        return None
    return f"refs/delta/{parts[2]}/{parts[3]}/{head}"


def clone_store_baseline(copy: dict) -> dict:
    """Fingerprint an owned native Git store and its exact protected-parent pin.

    Args:
        copy (dict): Generated native-copy evidence with clone-store ownership.

    Returns:
        dict: Store content, permissions and current parent pin binding.

    Raises:
        ValueError: Store, checkout or parent Git metadata was redirected.
        OSError: Store content or permissions are unreadable.
        NeedsDecision: Store content has an unsupported file type.
    """
    root, store = (Path(copy[name]) for name in ("root", "git_dir"))
    parent = Path(copy.get("project_root", copy["parent_root"]))
    proof = copy["clone_store"]
    if (store.resolve() != store or copy["common_dir"] != str(store)
            or common_dir(root) != str(store)
            or clone_reference(root, parent, store, copy["head"]) != proof["ref"]
            or common_dir(parent) != proof["ref_store"]):
        raise ValueError("native clone store or pin binding was redirected")
    return {"files": files(store), "mode": stat.S_IMODE(store.stat().st_mode),
            "ref": git(parent, "rev-parse", "--verify", "--end-of-options", proof["ref"]).decode().strip(),
            "ref_store": proof["ref_store"]}


def git_metadata(root: Path, native_copies: tuple[dict, ...] = (), *, allow_unborn: bool = False) -> dict:
    """Capture Git state shared by outer and owned nested checkouts.

    Args:
        root (Path): Canonical, ownership-validated checkout.
        native_copies (tuple[dict, ...]): Exact registered native ref exclusions.
        allow_unborn (bool): Protect an uncommitted project without requiring HEAD.

    Returns:
        dict: Commit, refs and index/config/control-file content hashes.

    Raises:
        ValueError: Git state or a control path was redirected or unreadable.
        OSError: Metadata content could not be read.
    """
    refs = git(root, "for-each-ref", "--format=%(refname)%00%(objectname)")
    shared = common_dir(root) if any(copy["ref"] or "clone_store" in copy for copy in native_copies) else None
    native_refs = {os.fsencode(copy["ref"]): os.fsencode(copy["head"]) for copy in native_copies
                   if copy["ref"] and copy["common_dir"] == shared}
    native_refs.update({os.fsencode(copy["clone_store"]["ref"]): os.fsencode(copy["head"])
                        for copy in native_copies if "clone_store" in copy and copy["clone_store"]["ref_store"] == shared})
    refs = b"".join(line for line in refs.splitlines(keepends=True)
                    if native_refs.get(line.partition(b"\0")[0]) != line.partition(b"\0")[2].rstrip(b"\n"))
    try:
        head = resolve(root, "HEAD")
    except NeedsDecision:
        if not allow_unborn:
            raise
        branch = git(root, "symbolic-ref", "HEAD").decode().strip()
        if not branch.startswith("refs/heads/") or git(root, "for-each-ref", branch):
            raise ValueError("project HEAD is neither a commit nor an unborn branch") from None
        head = None
    metadata = {"head": head, "refs": hashlib.sha256(refs).hexdigest()}
    if any(copy["root"] == str(root) for copy in native_copies):
        metadata["native_root"] = [stat.S_IMODE(root.stat().st_mode), shared or common_dir(root),
                                   str(Path(os.fsdecode(git(root, "rev-parse", "--absolute-git-dir").rstrip(b"\n"))).resolve()),
                                   (root / ".git").is_symlink()]
    for name in GIT_METADATA:
        path = Path(os.fsdecode(git(root, "rev-parse", "--git-path", name).rstrip(b"\n")))
        if not path.is_absolute():
            path = root / path
        metadata[name] = digest(path) if path.exists() else None
    for name in ("gitdir", "commondir"):
        path = root / ".git" / name if (root / ".git").is_dir() else Path(os.fsdecode(git(root, "rev-parse", "--absolute-git-dir").rstrip(b"\n"))) / name
        if path.is_symlink():
            raise ValueError("Git worktree control path was redirected")
        if path.exists():
            metadata[name] = digest(path)
    return metadata


def nested_git_metadata(root: Path, stores: set[Path], projects: list[Path], native_copies: tuple[dict, ...],
                        *, allow_unborn: bool = False) -> dict:
    """Bind nested Git administration only inside known ownership layouts.

    Args:
        root (Path): Initialized nested checkout found without following links.
        stores (set[Path]): Already owned parent/module Git common directories.
        projects (list[Path]): Already validated containing checkout roots.
        native_copies (tuple[dict, ...]): Exact registered native administration.
        allow_unborn (bool): Permit an uncommitted filesystem-review project.

    Returns:
        dict: Store ownership, marker permissions/content and full Git state.

    Raises:
        ValueError: Metadata redirects outside local/module/native ownership.
        OSError: A marker or store cannot be read.
        NeedsDecision: Nested administration contains an unsupported file type.
    """
    marker = root / ".git"
    info = marker.lstat()
    if stat.S_ISDIR(info.st_mode):
        directory = marker
        binding = ["directory", stat.S_IMODE(info.st_mode), info.st_uid]
    elif stat.S_ISREG(info.st_mode):
        raw = marker.read_bytes().rstrip(b"\n")
        if not raw.startswith(b"gitdir: ") or not raw[8:] or b"\0" in raw:
            raise ValueError("invalid nested Git marker")
        directory = Path(os.path.abspath(root / os.fsdecode(raw[8:])))
        binding = ["file", stat.S_IMODE(info.st_mode), info.st_uid, digest(marker)]
    else:
        raise ValueError("nested Git marker was redirected")
    if directory.resolve() != directory or not directory.is_dir():
        raise ValueError("nested Git directory was redirected")
    linked = directory.parent in {store / "worktrees" for store in stores}
    if not (directory == marker or any(directory.is_relative_to(store / "modules") for store in stores)
            or linked or any(clone_reference(root, project, directory, "") for project in projects)):
        raise ValueError("nested Git store is outside known checkout ownership")
    control = directory / "commondir"
    if control.is_symlink() or (control.exists() and not control.is_file()):
        raise ValueError("nested Git common directory was redirected")
    common = Path(os.path.abspath(directory / os.fsdecode(control.read_bytes().rstrip(b"\n")))) if control.exists() else directory
    if (common.resolve() != common or not common.is_dir()
            or not (common == directory or common in stores or any(common.is_relative_to(store / "modules") for store in stores))):
        raise ValueError("nested Git common store is outside known checkout ownership")
    if linked:
        locator = directory / "gitdir"
        if locator.is_symlink() or not locator.is_file() or Path(os.fsdecode(locator.read_bytes().rstrip(b"\n"))) != marker:
            raise ValueError("nested linked worktree ownership was redirected")
    for store in {directory, common}:
        for name in (*GIT_METADATA, "gitdir", "commondir"):
            path = store / name
            if path.exists() and not path.is_file():
                raise NeedsDecision("source_file_type", "nested Git metadata contains an unsupported file type")
    if (Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel").rstrip(b"\n"))) != root
            or common_dir(root) != str(common)):
        raise ValueError("nested Git checkout ownership changed")
    return {"git_dir": str(directory), "common_dir": str(common), "marker": binding,
            "git": git_metadata(root, native_copies, allow_unborn=allow_unborn)}


def baseline(root: Path, exclude: tuple[str, ...] = (), *, native_copies: tuple[dict, ...] = ()) -> dict:
    """Capture work content and Git state without refreshing the source index.

    Args:
        root (Path): Absolute checkout root with no symlink parents.
        exclude (tuple[str, ...]): Explicit setup cache paths, never user source paths.
        native_copies (tuple[dict, ...]): Generated native-copy ownership evidence;
            only exact new checkout roots, containers and pinned refs are normalized.

    Returns:
        dict: File ledger and hashed index/config/ref metadata.

    Raises:
        ValueError: The directory is not the requested checkout root.
        NeedsDecision: HEAD or a file type cannot be resolved safely.
        OSError: Source content is unreadable.
    """
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("baseline requires an absolute root without symlink parents")
    if Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel").rstrip(b"\n"))) != root:
        raise ValueError("baseline must name the checkout root")
    metadata = git_metadata(root, native_copies)
    copies = [copy for copy in native_copies if Path(copy["root"]).is_relative_to(root)]
    native_roots = tuple(str(Path(copy["root"]).relative_to(root)) for copy in copies
                         if Path(copy["root"]).is_relative_to(root))
    native_roots += tuple(str(Path(copy["git_dir"]).relative_to(root)) for copy in native_copies
                          if "clone_store" in copy and Path(copy["git_dir"]).is_relative_to(root))
    nested = []
    ledger = files(root, exclude + native_roots, nested_git=nested)
    for copy in copies:
        for path, expected in copy["created_dirs"].items():
            absolute = Path(copy.get("project_root", copy["parent_root"])) / path
            if absolute.is_relative_to(root):
                name = str(absolute.relative_to(root))
                if ledger.get(name) == expected:
                    del ledger[name]
    if nested:
        stores = {Path(common_dir(root))}
        projects = [root]
        metadata["nested"] = {}
        for checkout in sorted(nested, key=lambda path: len(path.parts)):
            captured = nested_git_metadata(checkout, stores, projects, native_copies)
            metadata["nested"][str(checkout.relative_to(root))] = captured
            stores.add(Path(captured["common_dir"]))
            projects.append(checkout)
    return {"files": ledger, "git": metadata}


def capture_runtime_copy(directory: Path, run_id: str, key: str, frozen: dict, root: Path,
                         native_copies: tuple[dict, ...], parents: dict[str, dict] | None = None,
                         parent_root: str | None = None) -> dict:
    """Capture a declared native checkout without exempting existing user work.

    Args:
        directory (Path): Private run directory containing owned preparation.
        run_id (str): Owning run ID.
        key (str): Target ID receiving the native start receipt.
        frozen (dict): Frozen parent/source and immutable artifact hashes.
        root (Path): Actual canonical checkout from native handle metadata.
        native_copies (tuple[dict, ...]): Previously captured native copies.
        parents (dict[str, dict] | None): All protected parent ledgers for this run,
            including additional native project roots registered before launch.
        parent_root (str | None): Declared actual native dispatch parent. A
            separately registered project may own sibling checkout bookkeeping.

    Returns:
        dict: Copy baseline and exact newly created administrative exceptions,
            retaining the dispatch parent and any separate owning project root.

    Raises:
        ValueError: The root redirects, overlaps protected content or is not a checkout.
        OSError: Ownership or original parent evidence cannot be read.
        NeedsDecision: The native checkout cannot be fingerprinted safely.
    """
    if root.resolve() != root or not root.is_absolute() or (root / ".git").is_symlink():
        raise ValueError("native copy requires a canonical checkout without redirected Git metadata")
    if parents is None:
        resource = owned(directory, frozen["preparation"]["resource"], run_id, key)
        parents = {frozen["parent_root"]: json.loads((resource / "parent-baseline.json").read_text())}
    store = common_dir(root)
    ancestors = sorted((Path(parent) for parent in parents if root.is_relative_to(Path(parent))),
                       key=lambda path: len(path.parts), reverse=True)
    if parent_root is not None:
        if parent_root not in parents:
            raise NeedsDecision("runtime_parent", "register the actual native parent with baseline before launch")
        parent = Path(parent_root)
    elif ancestors:
        parent = ancestors[0]
    else:
        raise NeedsDecision("runtime_parent", "supply runtime_parent_root from native metadata and register its baseline before launch; do not infer a parent from shared Git refs")
    if str(root) in parents or root == Path(frozen["source_root"]):
        raise ValueError("native copy must be separate from protected parent/source")
    head = resolve(root, "HEAD")
    project = next((candidate for candidate in ancestors if parent.is_relative_to(candidate)
                    and clone_reference(root, candidate, Path(store), head)), parent)
    original = parents[str(project)]
    prior_dirs = {name: value for copy in native_copies
                  if copy.get("project_root", copy["parent_root"]) == str(project)
                  for name, value in copy["created_dirs"].items()}
    created = {}
    clone_ref = clone_reference(root, project, Path(store), head)
    git_dir = Path(os.fsdecode(git(root, "rev-parse", "--absolute-git-dir").rstrip(b"\n")))
    if git_dir.resolve() != git_dir:
        raise ValueError("native Git directory was redirected")
    if root.is_relative_to(project):
        relative = root.relative_to(project)
        if relative.parts[0] != ".delta" or len(relative.parts) < 2:
            raise ValueError("nested native copies must use the Delta checkout namespace")
        roots = (root, Path(store)) if clone_ref else (root,)
        for owned_root in roots:
            prefix = str(owned_root.relative_to(project))
            if any(path == prefix or path.startswith(prefix + "/") for path in original["files"]):
                raise ValueError("native copy cannot exclude pre-existing user content")
            for path in owned_root.parents:
                if path == project:
                    break
                name = str(path.relative_to(project))
                if name not in original["files"]:
                    created[name] = prior_dirs.get(name, ["directory", stat.S_IMODE(path.stat().st_mode)])
    if not clone_ref and not (root.is_relative_to(project) and git_dir == root / ".git" and store == str(git_dir)):
        binding = git_dir / "gitdir"
        if (store != common_dir(project) or git_dir.parent != Path(store) / "worktrees"
                or binding.is_symlink() or not binding.is_file()
                or Path(os.fsdecode(binding.read_bytes().rstrip(b"\n"))) != root / ".git"):
            raise NeedsDecision("runtime_copy", "native checkout requires a registered project's own clone store or linked-worktree binding; register its actual project before launch")
    if any(root == Path(copy["root"]) or root.is_relative_to(Path(copy["root"]))
           or Path(copy["root"]).is_relative_to(root) for copy in native_copies):
        raise ValueError("new native copies cannot reuse or overlap an existing runtime checkout")
    symbolic = git(root, "rev-parse", "--symbolic-full-name", "HEAD").decode().strip()
    ref = symbolic if symbolic.startswith("refs/heads/") and store == common_dir(parent) else None
    copy = {"root": str(root), "parent_root": str(parent), "common_dir": store,
            "git_dir": str(git_dir),
            "mode": stat.S_IMODE(root.stat().st_mode),
            "ref": ref, "head": head, "created_dirs": created}
    if project != parent:
        copy["project_root"] = str(project)
    if clone_ref:
        copy["clone_store"] = {"ref": clone_ref, "ref_store": common_dir(project)}
        captured_store = clone_store_baseline(copy)
        if captured_store["ref"] != head:
            raise NeedsDecision("runtime_copy", "native parent pin does not match the checkout commit")
        copy["clone_store"]["fingerprint"] = fingerprint(captured_store)
    captured = baseline(root, native_copies=(*native_copies, copy))
    if (captured["git"]["head"] != copy["head"]
            or captured["git"]["native_root"] != [copy["mode"], store, copy["git_dir"], False]):
        raise NeedsDecision("runtime_copy", "native checkout changed while its ownership evidence was captured")
    copy["fingerprint"] = fingerprint(captured)
    return copy


def capture_baseline(directory: Path, key: str, root: Path, native_copies: tuple[dict, ...], *, filesystem: bool = False,
                     cache_recovery: bool = False) -> dict:
    """Keep a compact baseline reference and private ledger for native bookkeeping.

    Args:
        directory (Path): Private run directory outside the captured checkout.
        key (str): Stable participating-copy ID.
        root (Path): Canonical Git checkout root.
        native_copies (tuple[dict, ...]): Existing validated administrative exceptions.
        filesystem (bool): Protect a project parent without requiring a Git commit.
        cache_recovery (bool): Retain identities for an explicitly excluded SHM file.

    Returns:
        dict: Root/fingerprint and generated ledger filename/checksum.

    Raises:
        ValueError: Capturing the ledger would write into its protected checkout.
        OSError: The private ledger cannot be read or written.
        NeedsDecision: The checkout cannot be fingerprinted.
    """
    if directory == root or root in directory.parents:
        raise ValueError("run and baseline checkout must be separate")
    ledger = (project_baseline(root, native_copies=native_copies, cache_recovery=cache_recovery) if filesystem
              else baseline(root, native_copies=native_copies))
    path = directory / ("baseline-" + uuid.uuid4().hex + ".json")
    save(path, ledger)
    result = {"id": key, "root": str(root), "fingerprint": fingerprint(ledger),
              "artifact": path.name, "artifact_sha256": digest(path)}
    if filesystem:
        result["filesystem"] = True
    return result


def read_baseline(directory: Path, record: dict) -> dict:
    """Read generated baseline evidence without following replaced artifacts.

    Args:
        directory (Path): Private run directory.
        record (dict): Recorded root, fingerprint and artifact checksum.

    Returns:
        dict: Validated original file/Git ledger.

    Raises:
        NeedsDecision: A legacy baseline has no original bookkeeping ledger.
        ValueError: The immutable artifact was replaced or changed.
        OSError: Original evidence cannot be read.
    """
    if "artifact" not in record:
        raise NeedsDecision("runtime_parent", "this legacy copy baseline has no original native-parent ledger; prepare a new run")
    name = record["artifact"]
    if not isinstance(name, str) or not re.fullmatch(r"baseline-[0-9a-f]{32}\.json", name):
        raise ValueError("invalid baseline artifact")
    path = directory / name
    if path.is_symlink() or digest(path) != record["artifact_sha256"]:
        raise ValueError("baseline artifact was replaced or changed")
    ledger = json.loads(path.read_text())
    if fingerprint(ledger) != record["fingerprint"]:
        raise ValueError("baseline artifact disagrees with its recorded fingerprint")
    return ledger


def save(path: Path, value: dict | list) -> None:
    """Save a private preparation artifact inside a newly owned resource.

    Args:
        path (Path): New destination.
        value (dict | list): JSON artifact.

    Raises:
        OSError: The destination exists or cannot be written.
    """
    with path.open("x", encoding="utf-8") as output:
        os.chmod(path, 0o600)
        json.dump(value, output, indent=2, ensure_ascii=True)
        output.write("\n")


def repository(path: Path, parent: Path, commits: list[str]) -> None:
    """Fetch pinned history into an independent repository with empty templates.

    Args:
        path (Path): New run-owned destination.
        parent (Path): Read-only local object source.
        commits (list[str]): Pinned commits to transfer.

    Raises:
        OSError: The destination cannot be created.
        ValueError: Initialization or history transfer failed.
    """
    path.mkdir(mode=0o700)
    format_name = git(parent, "rev-parse", "--show-object-format").decode().strip()
    if format_name not in ("sha1", "sha256"):
        raise ValueError("unsupported Git object format")
    git(path, "init", "--quiet", "--template=", "--object-format=" + format_name)
    # Review the stored bytes; project attributes must not transform frozen content.
    (path / ".git/info").mkdir(exist_ok=True)
    (path / ".git/info/attributes").write_text("* -text -ident -filter -working-tree-encoding\n")
    if commits:
        git(path, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--", str(parent), *commits)


def comparison(target: dict, parent: Path) -> tuple[str, str, str, str, dict | None]:
    """Resolve existing review comparison semantics; never infer an unknown base.

    Args:
        target (dict): Intake target or explicit local comparison request.
        parent (Path): Read-only source checkout.

    Returns:
        tuple[str, str, str, str, dict | None]: Mode, left/right commits, right
            expression, and live PR metadata when applicable.

    Raises:
        NeedsDecision: Default base, commits, or stable PR metadata are unavailable.
        ValueError: The request is malformed.
    """
    request = target.get("comparison")
    if request is None:
        pull = target["intake"]
        hostname, endpoint = intake.pr_endpoint(target["repository"], pull["number"], pull["url"])
        live = intake.api(endpoint, hostname=hostname)
        if live.get("html_url") != pull["url"]:
            raise ValueError("live PR does not match the captured target")
        if (live.get("state") != "open" or live.get("number") != pull["number"]
                or any(intake.sha(live[side]["sha"], side) != pull[field]
                       for side, field in (("base", "base_sha"), ("head", "head_sha")))):
            raise NeedsDecision("comparison_changed", "PR closed or base/head moved since intake; create a new run")
        return "pr-merge-base", pull["base_sha"], pull["head_sha"], pull["head_sha"], live
    mode = request["mode"]
    right = resolve(parent, request["right"])
    left_ref = request.get("left")
    if mode == "branch-merge-base" and left_ref is None:
        try:
            left_ref = git(parent, "symbolic-ref", "refs/remotes/origin/HEAD").decode().strip()
        except ValueError as error:
            raise NeedsDecision("comparison_base", "supply the branch base; origin/HEAD does not identify the default branch") from error
    left = right if mode == "working" else resolve(parent, left_ref)
    return mode, left, right, request["right"], None


def fetch_pr(source: Path, parent: Path, left: str, right: str, live: dict) -> None:
    """Reuse local objects, fetching missing PR refs only in the disposable repo.

    Args:
        source (Path): Independent run-owned repository.
        parent (Path): Read-only local object source.
        left (str): Intake base commit.
        right (str): Intake head commit.
        live (dict): Verified live metadata, including fork identity.

    Raises:
        NeedsDecision: A missing ref cannot be fetched or no longer matches intake.
        ValueError: GitHub returned an unsafe repository URL/ref.
    """
    for side, commit in (("base", left), ("head", right)):
        try:
            resolve(parent, commit)
            git(source, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--", str(parent), commit)
            continue
        except (ValueError, NeedsDecision):
            pass
        repo = live["base"]["repo"]
        url = repo["clone_url"]
        parsed = urlsplit(url)
        host = urlsplit(live["html_url"]).hostname
        if (parsed.scheme != "https" or not host or parsed.hostname != host or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.port
                or parsed.path != "/" + repo["full_name"] + ".git"):
            raise ValueError("unsafe PR repository URL")
        ref = "refs/heads/" + live["base"]["ref"] if side == "base" else f"refs/pull/{live['number']}/head"
        git(source, "check-ref-format", ref)
        try:
            git(source, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--", url, ref, auth=True)
            if resolve(source, "FETCH_HEAD") != commit:
                raise NeedsDecision("comparison_changed", "fetched PR ref differs from intake; create a new run")
        except ValueError as error:
            if isinstance(error, NeedsDecision):
                raise
            raise NeedsDecision("comparison_objects", "required PR history is unavailable; preserve the incomplete review") from error


def working_snapshot(source: Path, parent: Path, head: str, untracked: list[str]) -> tuple[str, str]:
    """Freeze staged, unstaged and explicitly selected untracked content privately.

    Args:
        source (Path): Independent repository containing baseline HEAD history.
        parent (Path): Untouched source checkout.
        head (str): Resolved baseline HEAD.
        untracked (list[str]): Exact untracked file paths authorized for review.

    Returns:
        tuple[str, str]: Private snapshot commit and preserved staged tree ID.

    Raises:
        NeedsDecision: The index is unmerged or submodule working content is dirty.
        ValueError: An untracked selection is invalid or object transfer fails.
        OSError: A source file cannot be read.
    """
    entries = git(parent, "ls-files", "--stage", "-z").split(b"\0")[:-1]
    tracked = {}
    missing = []
    for entry in entries:
        metadata, name = entry.split(b"\t", 1)
        mode, blob, stage = metadata.decode().split()
        if stage != "0":
            raise NeedsDecision("working_index", "resolve the unmerged index before freezing working changes")
        path = relative_path(os.fsdecode(name))
        tracked[path] = (mode, blob)
        if mode != "160000":
            missing.append(blob)
    # Transfer staged-only blobs too: fetching HEAD alone cannot preserve them.
    checks = git(source, "cat-file", "--batch-check", input="".join(key + "\n" for key in sorted(set(missing))).encode())
    for line in checks.splitlines():
        if line.endswith(b" missing"):
            blob = line.split()[0].decode()
            if git(source, "hash-object", "-w", "--stdin", input=git(parent, "cat-file", "blob", blob)).decode().strip() != blob:
                raise ValueError("staged object transfer mismatch")
    git(source, "read-tree", "--empty")
    git(source, "update-index", "-z", "--index-info", input=b"\0".join(entries) + (b"\0" if entries else b""))
    staged = oid(git(source, "write-tree").decode().strip())
    skipped = {os.fsdecode(entry[2:]) for entry in git(parent, "ls-files", "-v", "-z").split(b"\0")[:-1]
               if entry[:1].upper() == b"S"}
    candidates = {os.fsdecode(name) for name in git(parent, "ls-files", "--others", "-z").split(b"\0")[:-1]}
    if len(untracked) != len(set(untracked)) or not set(untracked) <= candidates:
        raise ValueError("untracked scope must name distinct, existing untracked files")
    gitlinks = []
    for name in sorted(set(tracked) | set(untracked)):
        relative_path(name)
        path = parent / name
        if any(part.is_symlink() for part in path.parents if part != parent and parent in part.parents):
            raise ValueError("source path has a symlink ancestor")
        if name in tracked and tracked[name][0] == "160000":
            mode, blob = tracked[name]
            if path.is_dir() and not path.is_symlink() and (path / ".git").exists():
                status = git(path, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none")
                if status:
                    raise NeedsDecision("working_submodule", f"submodule {name!r} contains working changes; resolve their review scope")
                blob = resolve(path, "HEAD")
            elif path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
                raise NeedsDecision("working_submodule", f"submodule {name!r} has unverified working content; resolve its review scope")
            gitlinks.append(f"{mode} {blob}\t".encode() + os.fsencode(name) + b"\0")
            continue
        if not path.exists() and not path.is_symlink():
            if name in skipped:
                mode, blob = tracked[name]
                destination = source / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                content = git(source, "cat-file", "blob", blob)
                if mode == "120000":
                    destination.symlink_to(os.fsdecode(content))
                else:
                    destination.write_bytes(content)
                    destination.chmod(0o755 if mode == "100755" else 0o644)
            continue
        info = path.lstat()
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if stat.S_ISLNK(info.st_mode):
            destination.symlink_to(os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            shutil.copyfile(path, destination, follow_symlinks=False)
            destination.chmod(0o755 if info.st_mode & stat.S_IXUSR else 0o644)
        elif stat.S_ISDIR(info.st_mode) and name in tracked:
            continue
        else:
            raise NeedsDecision("working_file_type", f"cannot freeze non-file {path}")
    git(source, "add", "--all", "--force")
    if gitlinks:
        git(source, "update-index", "-z", "--index-info", input=b"".join(gitlinks))
    tree = oid(git(source, "write-tree").decode().strip())
    commit = git(source, "-c", "user.name=Review snapshot", "-c", "user.email=review@localhost",
                 "commit-tree", tree, "-p", head, input=b"Frozen working comparison\n").decode().strip()
    return oid(commit), staged


def change_inventory(status_data: bytes, count_data: bytes) -> list[dict]:
    """Join lossless Git name-status/numstat output, including renames and binaries.

    Args:
        status_data (bytes): Complete NUL-delimited name-status output.
        count_data (bytes): Complete NUL-delimited numstat output.

    Returns:
        list[dict]: Per-change status, paths, counts and binary classification.

    Raises:
        ValueError: The outputs are malformed or disagree.
    """
    statuses = iter(status_data.split(b"\0")[:-1])
    counts = iter(count_data.split(b"\0")[:-1])
    changes = []
    try:
        for status_value in statuses:
            status_text = status_value.decode("ascii")
            names = [relative_path(os.fsdecode(next(statuses)))]
            if status_text.startswith(("R", "C")):
                names.append(relative_path(os.fsdecode(next(statuses))))
            added, removed, name = next(counts).split(b"\t", 2)
            counted = [os.fsdecode(name)] if name else [os.fsdecode(next(counts)), os.fsdecode(next(counts))]
            if counted != names:
                raise ValueError("inconsistent change inventories")
            changes.append({"status": status_text, "paths": names,
                            "additions": None if added == b"-" else int(added),
                            "deletions": None if removed == b"-" else int(removed), "binary": added == b"-"})
        if next(counts, None) is not None:
            raise ValueError("inconsistent numstat length")
    except (StopIteration, UnicodeError) as error:
        raise ValueError("malformed change inventories") from error
    return changes


def artifacts(resource: Path, source: Path, base: str, head: str, staged: str | None) -> list[str]:
    """Save complete, consistently configured patches and NUL-delimited inventories.

    Args:
        resource (Path): Run-owned artifact directory.
        source (Path): Independent repository with pinned history.
        base (str): Resolved diff base.
        head (str): Frozen target commit.
        staged (str | None): Preserved index tree for a working comparison.

    Returns:
        list[str]: Complete changed-path inventory, including both rename sides.

    Raises:
        ValueError: Git failed or returned inconsistent metadata.
        OSError: Artifacts cannot be written.
    """
    outputs = {"patch": PATCH_OPTIONS,
               "name-status.z": ("--name-status", "-z"), "numstat.z": ("--numstat", "-z"),
               "stat.txt": ("--stat", "--stat-width=120")}
    pairs = [("review", base, head)]
    if staged is not None:
        pairs += [("staged", base, staged), ("unstaged", staged, head)]
    ledger = {"diff_base_sha": base, "head_sha": head}
    inventory = set()
    for label, left, right in pairs:
        prefix = "" if label == "review" else label + "-"
        for name, flags in outputs.items():
            path = resource / (label + ".patch" if name == "patch" else prefix + name)
            path.write_bytes(git(source, "diff", *DIFF_OPTIONS, *flags, left, right, "--"))
            path.chmod(0o600)
        changes = change_inventory((resource / (prefix + "name-status.z")).read_bytes(),
                                   (resource / (prefix + "numstat.z")).read_bytes())
        ledger["changes" if label == "review" else label + "_changes"] = changes
        inventory.update(path for row in changes for path in row["paths"])
    save(resource / "inventory.json", ledger)
    return sorted(inventory)


def owned(directory: Path, resource: str, run_id: str, target: str) -> Path:
    """Verify a resource's location and exact ownership before accessing/removing it.

    Args:
        directory (Path): Private run directory.
        resource (str): Recorded snapshot directory name.
        run_id (str): Expected owning run.
        target (str): Expected owning target.

    Returns:
        Path: Validated direct child directory.

    Raises:
        ValueError: A path, marker or owner does not match.
        OSError: The marker cannot be read.
    """
    if not re.fullmatch(r"snapshot-[0-9a-f]{32}", resource) or directory.resolve() != directory:
        raise ValueError("invalid owned resource path")
    root = directory / resource
    marker = root / "owner.json"
    if root.is_symlink() or not root.is_dir() or marker.is_symlink():
        raise ValueError("owned resource or marker replaced")
    with marker.open(encoding="utf-8") as source:
        owner = json.load(source)
    if owner != {"run_id": run_id, "target": target, "resource": resource}:
        raise ValueError("resource ownership mismatch")
    return root


def prepare(directory: Path, run_id: str, key: str, target: dict, parent: Path,
            native_copies: tuple[dict, ...] = ()) -> dict:
    """Build the frozen target and baselines, preserving every source mutation.

    Args:
        directory (Path): Private run directory outside the source checkout.
        run_id (str): Owning run ID.
        key (str): Target key.
        target (dict): Validated intake/comparison target.
        parent (Path): Absolute canonical source checkout.
        native_copies (tuple[dict, ...]): Recorded native checkout bookkeeping.

    Returns:
        dict: Compact frozen target bound to independently owned artifacts.

    Raises:
        NeedsDecision: Comparison/evidence is unavailable or source changed.
        ValueError: Inputs or Git results are invalid.
        OSError: Snapshots or baselines cannot be created/read.
    """
    if "project" in target:
        if target["project"]["error"]:
            raise NeedsDecision("project_source", target["project"]["error"])
        return prepare_project(directory, run_id, key, target, parent, native_copies)
    before = baseline(parent, native_copies=native_copies)
    if parent == directory or parent in directory.parents or directory == parent or directory in parent.parents:
        raise ValueError("run and source directories must be separate")
    mode, left, right, right_ref, live = comparison(target, parent)
    resource = directory / ("snapshot-" + uuid.uuid4().hex)
    resource.mkdir(mode=0o700)
    save(resource / "owner.json", {"run_id": run_id, "target": key, "resource": resource.name})
    try:
        source = resource / "source"
        repository(source, parent, [] if live else list(dict.fromkeys((left, right))))
        if live:
            fetch_pr(source, parent, left, right, live)
        diff_base = left
        if mode in ("pr-merge-base", "range-three-dot", "branch-merge-base"):
            try:
                bases = git(source, "merge-base", "--all", left, right).decode().splitlines()
            except ValueError as error:
                raise NeedsDecision("comparison_merge_base", "required merge-base history is unavailable") from error
            if len(bases) != 1:
                raise NeedsDecision("comparison_merge_base", "comparison requires one unambiguous merge-base")
            diff_base = oid(bases[0])
        staged = None
        baseline_head = right
        if mode == "working":
            right, staged = working_snapshot(source, parent, right, target["comparison"]["untracked"])
        git(source, "checkout", "--quiet", "--detach", "--force", right)
        if resolve(source, "HEAD") != right or git(source, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("snapshot does not match the frozen commit")
        inventory = artifacts(resource, source, diff_base, right, staged)
        after = baseline(parent, native_copies=native_copies)
        if before != after or (not live and resolve(parent, right_ref) != baseline_head):
            raise NeedsDecision("source_integrity", "source changed during preparation; preserve user work and create a new run")
        source_baseline = baseline(source)
        save(resource / "parent-baseline.json", before)
        save(resource / "source-baseline.json", source_baseline)
        if live:
            save(resource / "pr.json", {side: {"sha": live[side]["sha"], "ref": live[side].get("ref"),
                                              "repository": (live[side].get("repo") or {}).get("full_name")}
                                        for side in ("base", "head")})
        artifact_hashes = {path.name: digest(path) for path in resource.iterdir() if path.is_file()}
        return {"mode": mode, "base_sha": left, "head_sha": right, "diff_base_sha": diff_base,
                "source_root": str(source), "source_fingerprint": fingerprint(source_baseline),
                "parent_root": str(parent), "parent_fingerprint": fingerprint(before), "inventory": inventory,
                "preparation": {"resource": resource.name, "artifacts": artifact_hashes,
                                "baseline_head_sha": baseline_head, "staged_tree_sha": staged}}
    except BaseException:
        remove_resource(owned(directory, resource.name, run_id, key))
        raise


def check(directory: Path, run_id: str, key: str, frozen: dict, baselines: dict,
          setup: dict | None = None, observations: dict | None = None,
          native_copies: tuple[dict, ...] = ()) -> dict:
    """Compare source, parent, artifacts and participating-copy baselines.

    Args:
        directory (Path): Private run directory.
        run_id (str): Owning run.
        key (str): Target key.
        frozen (dict): Frozen preparation result.
        baselines (dict): Additional participating-copy baseline records.
        setup (dict | None): Recorded setup intent; permit only its new cache paths.
        observations (dict | None): Command-local content scans keyed by root and
            exclusions; discard after this command's observations are recorded.
        native_copies (tuple[dict, ...]): Recorded native copies, individually checked;
            their exact administrative exceptions never exempt user/source content.

    Returns:
        dict: Actual integrity status and changed/unreadable components.
    """
    problems = []
    cache_changes = []
    scans = observations if observations is not None else {}
    def observe(root: str, exclude: tuple[str, ...] = (), *, native: bool = True, project: bool = False,
                expected: dict | None = None) -> str:
        """Reuse a full-content scan within this observation command.

        Args:
            root (str): Absolute checkout root.
            exclude (tuple[str, ...]): Explicit setup cache exclusions.
            native (bool): Normalize validated parent/copy bookkeeping; frozen
                source observations always disable these exceptions.
            project (bool): Use a filesystem baseline without requiring Git.
            expected (dict | None): Verified original ledger for audited SHM recovery.

        Returns:
            str: Content and Git metadata fingerprint.

        Raises:
            ValueError: This root's baseline could not be captured.
        """
        copies = native_copies if native else ()
        recover = project and bool((expected or {}).get("cache_identity"))
        key = (root, exclude, fingerprint({"native_copies": copies}), project, recover)
        if key not in scans:
            try:
                scans[key] = (project_baseline(Path(root), exclude, native_copies=copies, cache_recovery=recover)
                              if project else baseline(Path(root), exclude, native_copies=copies))
            except (OSError, ValueError, KeyError, TypeError):
                scans[key] = None
        if scans[key] is None:
            raise ValueError("source baseline is unreadable")
        current = scans[key]
        if recover and expected is not None:
            current, changes = recover_cache(expected, current, root)
            for change in changes:
                if change not in cache_changes:
                    cache_changes.append(change)
        return fingerprint(current)

    component = "unreadable or unowned preparation evidence"
    try:
        proof = frozen["preparation"]
        resource = owned(directory, proof["resource"], run_id, key)
        source = resource / "source"
        project = frozen["mode"] == "project"
        if (frozen["source_root"] != str(source) or source.resolve() != source or not source.is_dir()
                or (project and ((source / ".git").exists() or (source / ".git").is_symlink()))
                or (not project and ((source / ".git").is_symlink() or not (source / ".git").is_dir()
                or (source / ".git/commondir").exists()
                or (source / ".git/objects").resolve() != source / ".git/objects"
                or (source / ".git/objects/info/alternates").exists()))):
            raise ValueError("source does not belong to this resource")
        for name, expected in proof["artifacts"].items():
            if Path(name).name != name or (resource / name).is_symlink() or not (resource / name).is_file() or digest(resource / name) != expected:
                problems.append("artifact:" + name)
        if project and (json.loads((resource / "manifest.json").read_text()) != frozen["manifest"]
                        or fingerprint(frozen["manifest"]) != frozen["manifest_fingerprint"]):
            problems.append("manifest")
        for name in ("parent", "source"):
            component = name
            if name == "source" and setup:
                for cache in setup["cache_paths"]:
                    if (source / cache).is_symlink():
                        raise ValueError("index cache was redirected")
            current = observe(frozen[name + "_root"],
                              tuple(setup["cache_paths"]) if name == "source" and setup else (),
                              native=name != "source", project=project,
                              expected=json.loads((resource / (name + "-baseline.json")).read_text()) if project else None)
            if current != frozen[name + "_fingerprint"]:
                problems.append(name)
        for name, record in baselines.items():
            component = "copy:" + name
            expected = None
            if "artifact" in record:
                expected = read_baseline(directory, record)
            if observe(record["root"], project=record.get("filesystem", False), expected=expected) != record["fingerprint"]:
                problems.append("copy:" + name)
        for record in native_copies:
            component = "native:" + record["root"]
            if observe(record["root"]) != record["fingerprint"]:
                problems.append(component)
            if "clone_store" in record:
                component = "native-store:" + record["git_dir"]
                store_key = (component, (), fingerprint(record["clone_store"]))
                if store_key not in scans:
                    scans[store_key] = fingerprint(clone_store_baseline(record))
                if scans[store_key] != record["clone_store"]["fingerprint"]:
                    problems.append(component)
        return {"status": "changed" if problems else "unchanged", "components": problems,
                **({"cache_changes": cache_changes} if cache_changes else {})}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "unknown", "components": list(dict.fromkeys(problems + [component]))}


def index_helper(*, bundled: bool = False) -> tuple[ModuleType, Path]:
    """Load the existing trusted maintainer, never code from the reviewed repository.

    Args:
        bundled (bool): Use the shipped API for static preparation without requiring
            a compatible machine installation.

    Returns:
        tuple[ModuleType, Path]: Loaded maintainer and its canonical script path.

    Raises:
        NeedsDecision: The selected trusted maintainer has no compatible lifecycle
            and durable sealing API.
    """
    installed = Path.home() / ".local/share/zetta-delta/ensure-context-indexes.py"
    shipped = Path(__file__).with_name("ensure-context-indexes.py")
    if not shipped.is_file():
        shipped = Path(__file__).resolve().parents[2] / "stack/context-indexes/ensure-context-indexes.py"
    path = installed if not bundled and installed.is_file() else shipped
    if not path.is_file():
        raise NeedsDecision("index_maintainer", "the trusted index maintainer API is unavailable; repair the project bundle separately")
    path = path.resolve()
    spec = importlib.util.spec_from_file_location("review_index_maintainer", path)
    if spec is None or spec.loader is None:
        raise NeedsDecision("index_maintainer", "the trusted index maintainer cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if (not all(callable(getattr(module, name, None)) for name in
                ("plan_indexes", "setup_indexes", "idle_indexes", "unavailable_indexes", "sealed_snapshot", "IndexPending"))
            or getattr(module, "INDEX_LIFECYCLE_VERSION", None) != 4):
        raise NeedsDecision("index_maintainer", "selected index maintainer lacks the required lifecycle contract")
    return module, path


def setup_request(directory: Path, run_id: str, key: str, staged: dict, tools: list[str], *,
                  baselines: dict | None = None, native_copies: tuple[dict, ...] = ()) -> dict:
    """Bind setup to the existing maintainer and caches before any job can start.

    Args:
        directory (Path): Private run directory.
        run_id (str): Owning run ID.
        key (str): Target key.
        staged (dict): Provisional preparation evidence.
        tools (list[str]): Explicit useful index selections.
        baselines (dict | None): Recorded protected parent/participating-copy evidence.
        native_copies (tuple[dict, ...]): Registered native administrative changes;
            never applied to the frozen source.

    Returns:
        dict: Immutable setup intent, including actual executable availability.

    Raises:
        ValueError: Tools or cache paths conflict with frozen source content.
        NeedsDecision: Source integrity or maintainer availability is unresolved.
    """
    if check(directory, run_id, key, staged, baselines or {}, native_copies=native_copies)["status"] != "unchanged":
        raise NeedsDecision("source_integrity", "source changed before index setup; preserve evidence and create a new run")
    module, path = index_helper()
    resource = owned(directory, staged["preparation"]["resource"], run_id, key)
    with (resource / "source-baseline.json").open(encoding="utf-8") as source:
        original = json.load(source)["files"]
    plan = module.plan_indexes(Path(staged["source_root"]), tools)
    cache_paths = [relative_path(name) for name in plan["cache_paths"]]
    if any(path == cache or path.startswith(cache + "/") for path in original for cache in cache_paths):
        raise ValueError("index cache paths overlap frozen source content; use static review")
    return {"tools": plan["tools"], "available": plan["available"],
            "helper": str(path), "helper_fingerprint": digest(path), "cache_paths": cache_paths}


def run_setup(staged: dict, request: dict) -> None:
    """Invoke the existing bounded maintainer after the intent was journaled.

    Args:
        staged (dict): Provisional snapshot evidence.
        request (dict): Persisted setup intent.

    Raises:
        NeedsDecision: The maintainer changed or its foreground setup failed.
    """
    module, path = index_helper()
    if str(path) != request["helper"] or digest(path) != request["helper_fingerprint"]:
        raise NeedsDecision("index_maintainer", "index maintainer changed since setup intent; preserve the resource")
    try:
        module.setup_indexes(Path(staged["source_root"]), request["available"])
    except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.TimeoutExpired) as error:
        raise NeedsDecision("index_setup", "foreground index setup failed; inspect maintainer state before finalization") from error


@contextmanager
def idle_indexes(source: Path, request: dict | None) -> Generator[ModuleType]:
    """Delegate lifecycle protection to the bound maintainer API.

    Args:
        source (Path): Owned independent checkout.
        request (dict | None): Recorded setup intent, if indexes were selected.

    Yields:
        ModuleType: Bound maintainer API while its lifecycle guard is held.

    Raises:
        NeedsDecision: Maintenance is pending or its lifecycle cannot be inspected in time.
        ValueError: A lifecycle file was redirected or corrupted.
        OSError: Lifecycle evidence is unreadable.
    """
    module, path = index_helper(bundled=request is None)
    if request and (str(path) != request["helper"] or digest(path) != request["helper_fingerprint"]):
        raise NeedsDecision("index_maintainer", "index lifecycle owner changed; preserve the snapshot")
    try:
        with module.idle_indexes(source, request["available"] if request else []):
            yield module
    except module.IndexPending as error:
        raise NeedsDecision("index_setup_pending", f"{error}; retain the snapshot and retry after completion") from error
    except (TimeoutError, subprocess.TimeoutExpired) as error:
        raise NeedsDecision("index_readiness", "cannot inspect index lifecycle in time; preserve the snapshot and inspect maintainer evidence") from error
    except RuntimeError as error:
        raise ValueError(str(error)) from error


def finalize(directory: Path, run_id: str, key: str, staged: dict, request: dict | None,
             fallback: list[str], evidence: str | None, *, baselines: dict | None = None,
             native_copies: tuple[dict, ...] = ()) -> dict:
    """Seal setup under lifecycle locks and bind the automatic-maintenance hold.

    Args:
        directory (Path): Private run directory.
        run_id (str): Owning run ID.
        key (str): Target key.
        staged (dict): Immutable pre-setup comparison and baselines.
        request (dict | None): Explicit index setup intent.
        fallback (list[str]): Indexes the model chooses to replace with static evidence.
        evidence (str | None): Reason static review can cover those index failures.
        baselines (dict | None): Protected parent/participating-copy evidence.
        native_copies (tuple[dict, ...]): Registered native bookkeeping; source
            baselines always retain their full content and Git state.

    Returns:
        dict: Final frozen target, retaining original and final baseline artifacts.

    Raises:
        NeedsDecision: Index work is pending or a fallback/source decision is missing.
        ValueError: Fallback choices or evidence are invalid.
        OSError: Baseline artifacts cannot be read or saved.
    """
    resource = owned(directory, staged["preparation"]["resource"], run_id, key)
    source = resource / "source"
    with idle_indexes(source, request) as module:
        observation = check(directory, run_id, key, staged, baselines or {}, setup=request, native_copies=native_copies)
        if observation["status"] != "unchanged":
            raise NeedsDecision("source_integrity", "source/parent/artifacts changed during setup; preserve evidence and create a new run", observation)
        unavailable = []
        if request:
            try:
                unavailable = module.unavailable_indexes(source, request["tools"], request["available"])
            except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.TimeoutExpired) as error:
                raise NeedsDecision("index_readiness", f"{error}; preserve the snapshot and inspect maintainer evidence") from error
        if len(fallback) != len(set(fallback)) or not set(fallback) <= set(unavailable):
            raise ValueError("static fallback must name only failed/unavailable requested indexes")
        if set(fallback) != set(unavailable):
            raise NeedsDecision("index_fallback", "choose static evidence for failed/unavailable indexes: " + ", ".join(unavailable))
        if fallback and (not isinstance(evidence, str) or not evidence.strip() or "\0" in evidence):
            raise ValueError("static fallback requires evidence of adequate source inspection")
        current = project_baseline(source) if staged["mode"] == "project" else baseline(source)
        original_content = dict(current, files={
            path: value for path, value in current["files"].items()
            if not any(path == cache or path.startswith(cache + "/") for cache in (request or {}).get("cache_paths", []))})
        observation = check(directory, run_id, key, staged, baselines or {}, setup=request, native_copies=native_copies)
        if fingerprint(original_content) != staged["source_fingerprint"]:
            observation = {"status": "changed", "components": list(dict.fromkeys(observation["components"] + ["source"]))}
        if observation["status"] != "unchanged":
            raise NeedsDecision("source_integrity", "source/parent/artifacts changed while sealing setup; preserve evidence and create a new run", observation)
        result = json.loads(json.dumps(staged))
        result["source_fingerprint"] = fingerprint(current)
        # Idempotent retry after a failed journal save must retain the same artifacts.
        for name, data in (("final-source-baseline.json", current),
                           ("index-result.json", {"request": request, "fallback": fallback, "evidence": evidence})):
            path = resource / name
            if path.exists():
                if path.is_symlink() or json.loads(path.read_text()) != data:
                    raise ValueError("finalization artifacts differ from the prior attempt")
            else:
                save(path, data)
            result["preparation"]["artifacts"][name] = digest(path)
        seal = {"version": 1, "source_root": str(source), "artifacts": {
            name: digest(resource / name) for name in ("owner.json", "final-source-baseline.json", "index-result.json")}}
        path = resource / "index-seal.json"
        if path.exists():
            if path.is_symlink() or json.loads(path.read_text()) != seal:
                raise ValueError("index seal differs from the prior attempt")
        else:
            save(path, seal)
        if not module.sealed_snapshot(source):
            raise ValueError("index maintainer did not authenticate the sealed snapshot")
        result["preparation"]["artifacts"][path.name] = digest(path)
        return result


def test_copy(directory: Path, run_id: str, key: str, frozen: dict) -> Path:
    """Create a mutable, independently owned test repository, never a linked worktree.

    Args:
        directory (Path): Private run directory.
        run_id (str): Owning run.
        key (str): Target key.
        frozen (dict): Verified frozen preparation result.

    Returns:
        Path: Absolute test-copy root. Caller must verify execution authorization.

    Raises:
        ValueError: Ownership or Git transfer is invalid.
        OSError: Copy creation failed.
    """
    resource = owned(directory, frozen["preparation"]["resource"], run_id, key)
    path = resource / ("test-" + uuid.uuid4().hex)
    try:
        if frozen["mode"] == "project":
            path.mkdir(mode=0o700)
            setup = json.loads((resource / "index-result.json").read_text())["request"]
            caches = dict.fromkeys((setup or {}).get("cache_paths", []), "owned index cache")
            copied = project_inventory(resource / "source", caches, path)
            if copied["entries"] != frozen["manifest"]["entries"] or copied["gaps"]:
                raise ValueError("project test copy differs from the frozen manifest")
            return path
        repository(path, resource / "source", [frozen["head_sha"]])
        git(path, "checkout", "--quiet", "--detach", frozen["head_sha"])
        return path
    except BaseException:
        if path.is_dir() and not path.is_symlink():
            remove_resource(path)
        raise


def cleanup(directory: Path, run_id: str, key: str, request: dict | None = None,
            setup_resource: str | None = None) -> list[str]:
    """Remove only matching run-owned snapshots, including interrupted preparation.

    Args:
        directory (Path): Private run directory; the journal is retained.
        run_id (str): Owning run.
        key (str): Target key.
        request (dict | None): Persisted index intent, including pending startup.
        setup_resource (str | None): Resource bound to that intent; other owned
            interrupted preparations never inherit its background jobs.

    Returns:
        list[str]: Removed resource names.

    Raises:
        ValueError: A candidate resource or ownership marker was replaced.
        OSError: Ownership cannot be read or removal failed.
        NeedsDecision: Index jobs are still active, queued, starting, or unknown.
    """
    matches = []
    for path in sorted(directory.glob("snapshot-*")):
        if path.is_symlink() or (path / "owner.json").is_symlink():
            raise ValueError("refuse cleanup of replaced resource")
        if not (path / "owner.json").exists():
            if path.name == setup_resource:
                raise ValueError("recorded preparation ownership evidence is missing")
            continue
        with (path / "owner.json").open(encoding="utf-8") as source:
            owner = json.load(source)
        if owner.get("run_id") == run_id and owner.get("target") == key:
            matches.append(owned(directory, path.name, run_id, key))
    with ExitStack() as stack:
        for path in matches:
            setup = request if setup_resource in (None, path.name) else None
            if (path / "manifest.json").is_file() or (path / "source/.git").is_dir():
                if (path / "source/.git").is_symlink():
                    raise ValueError("refuse cleanup of redirected Git metadata")
                stack.enter_context(idle_indexes(path / "source", setup))
            elif setup:
                raise NeedsDecision("index_setup_pending", "index lifecycle evidence is missing; preserve the resource")
        for path in matches:
            remove_resource(path)
    return [path.name for path in matches]

