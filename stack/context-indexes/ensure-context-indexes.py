#!/usr/bin/env python3
"""Bounded, fail-open Codex SessionStart / search PreToolUse index maintenance.

Only selected Git worktrees and the non-Git folders listed in $XDG_CONFIG_HOME/zetta-delta/index-roots (default
~/.config) are indexed. No shell evaluation, model downloads, remote embeddings, persistent servers, or watchers.
Native tools own indexes.
"""

import fcntl
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TextIO

BUDGET_SECONDS = 55
BACKGROUND_SECONDS = 1800  # a first zg build; refreshes of an existing index fit BUDGET_SECONDS
# Full builds above these sizes are skipped with a note, never started: measured here, codegraph
# indexes ~230 files/s inside BUDGET_SECONDS and zg ~3 files/s inside BACKGROUND_SECONDS.
FIRST_BUILD_MAX_FILES = {"codegraph": 5000, "zg": 4000}
INDEX_DIRS = (".tgrep", ".codegraph", ".zvec-grep")
TOOLS = ("tgrep", "codegraph", "zg")
INDEX_LIFECYCLE_VERSION = 4
# Match Caveman's gitsafe helper; automatic repository inspection must not run repo hooks.
GIT_OVERRIDES = {
    "core.fsmonitor": "false", "core.hooksPath": "/dev/null", "core.sshCommand": "",
    "core.askPass": "", "core.editor": "true", "core.pager": "cat",
    "core.alternateRefsCommand": "", "diff.external": "", "credential.helper": "",
    "protocol.ext.allow": "never", "uploadpack.packObjectsHook": "",
}


def note(message):
    print("context-indexes: " + message + "; use the router fallback if needed.", file=sys.stderr)


def read_payload():
    # Codex may keep stdin open after its JSON object; never wait for EOF.
    deadline = time.monotonic() + 1
    data = b""
    while len(data) < 1024 * 1024:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([sys.stdin], [], [], remaining)[0]:
            return {}
        chunk = os.read(sys.stdin.fileno(), 65536)
        if not chunk:
            return {}
        data += chunk
        try:
            value, _ = json.JSONDecoder().raw_decode(data.decode().lstrip())
            return value if isinstance(value, dict) else {}
        except (ValueError, UnicodeDecodeError):
            pass
    return {}


def run(args, root, deadline, check=True, extra_env=None, pass_fds: tuple[int, ...] = ()):
    """Run a native tool and reap its process group on interruption or timeout.

    Args:
        args (list[str | Path]): Native command and arguments.
        root (Path): Working directory.
        deadline (float): Absolute monotonic execution deadline.
        check (bool): Reject a nonzero command status.
        extra_env (dict[str, str] | None): Additional environment settings.
        pass_fds (tuple[int, ...]): Lifecycle leases retained by native writers.

    Returns:
        subprocess.CompletedProcess[str]: Captured status and output.

    Raises:
        OSError: The command cannot launch or its process group cannot be reaped.
        TimeoutError: The deadline already expired.
        subprocess.TimeoutExpired: The command exceeded the remaining deadline.
        RuntimeError: A checked command exited unsuccessfully.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("hook budget exhausted")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(extra_env or {})
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_COUNT=str(len(GIT_OVERRIDES)))
    for i, (key, value) in enumerate(GIT_OVERRIDES.items()):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    if args[0] == "git":
        flags = [part for key, value in GIT_OVERRIDES.items() for part in ("-c", key + "=" + value)]
        args = ["git", "--no-pager", *flags, *args[1:]]
    # Propagate safe Git config to Git invocations inside indexer children too.
    # A separate process group lets the deadline reap indexer worker children too.
    child = subprocess.Popen([str(x) for x in args], cwd=root, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                             text=True, start_new_session=True, pass_fds=pass_fds)
    try:
        out, err = child.communicate(timeout=remaining)
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.communicate()
        raise
    result = subprocess.CompletedProcess(args, child.returncode, out, err)
    if check and child.returncode:
        raise RuntimeError(f"{Path(args[0]).name} exited {child.returncode}")
    return result


def git_root(path):
    path = Path(path).expanduser().resolve()
    if path.is_file():
        path = path.parent
    if not path.is_dir():
        return None
    result = run(["git", "-C", path, "rev-parse", "--show-toplevel"], path,
                 time.monotonic() + 2, check=False)
    if result.returncode:
        return None
    root = Path(result.stdout.strip()).resolve()
    marker = next((p for p in (path, *path.parents) if (p / ".git").exists()), None)
    return root if (root == marker and path.is_relative_to(root) and root != Path.home().resolve()
                    and root != Path(root.anchor)) else None


def extra_roots():
    """Read the non-Git folders the user opted in to indexing.

    The file $XDG_CONFIG_HOME/zetta-delta/index-roots (default ~/.config) lists one absolute or ~ path per line:
    ``~/scripts`` makes that folder one root, and ``~/projects/*`` makes each folder inside it its own root. Blank
    lines and lines starting with ``#`` are skipped, and so are relative paths. Without the file, only Git
    repositories are indexed.

    Returns:
        list[tuple[Path, int]]: (folder, depth) pairs; depth 0 is the folder itself, 1 each folder inside it.
    """
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "zetta-delta" / "index-roots"
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    roots = []
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        depth = 1 if entry.endswith("/*") else 0
        base = Path(entry[:-2] if depth else entry).expanduser()
        if base.is_absolute():
            roots.append((base.resolve(), depth))
    return roots


def allowlisted_root(path):
    # Non-Git folders the user opted in through the index-roots file.
    path = Path(path).expanduser().resolve()
    for base, depth in extra_roots():
        if path.is_relative_to(base) and len(path.relative_to(base).parts) >= depth:
            root = base.joinpath(*path.relative_to(base).parts[:depth])
            # A Git checkout that git_root rejected (for example a redirected worktree) stays rejected.
            return root if root.is_dir() and not (root / ".git").exists() else None
    return None


def project_root(path):
    return git_root(path) or allowlisted_root(path)


def sealed_snapshot(root: Path) -> bool:
    """Authenticate a review seal using private artifacts outside its checkout.

    Args:
        root (Path): Canonical candidate checkout root.

    Returns:
        bool: Whether this owned review snapshot has matching durable seal evidence.

    Raises:
        ValueError: Seal JSON or ownership/bindings are invalid.
        RuntimeError: Seal paths, permissions or file ownership are unsafe.
        OSError: Finalization evidence exists but is incomplete or unreadable.
    """
    resource = root.parent
    if root.name != "source" or not re.fullmatch(r"snapshot-[0-9a-f]{32}", resource.name):
        return False
    names = ("index-seal.json", "owner.json", "final-source-baseline.json", "index-result.json")
    if not any((resource / name).exists() or (resource / name).is_symlink() for name in (names[0], *names[2:])):
        return False
    info = resource.lstat()
    if (root.resolve() != root or resource.resolve() != resource or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise RuntimeError("review seal requires a canonical private owned resource")
    hashes, values = {}, {}
    for name in names:
        descriptor = os.open(resource / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise RuntimeError("review seal artifacts must be private owned regular files")
            if name in names[:2]:
                raw = source.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("review seal JSON exceeds its bounded size")
                values[name] = json.loads(raw)
                hashes[name] = hashlib.sha256(raw).hexdigest()
            else:
                hashes[name] = hashlib.file_digest(source, "sha256").hexdigest()
    owner, seal = values["owner.json"], values["index-seal.json"]
    if (not isinstance(owner, dict) or set(owner) != {"run_id", "target", "resource"}
            or owner["resource"] != resource.name
            or any(not isinstance(owner[key], str) or not owner[key].strip() for key in ("run_id", "target"))):
        raise ValueError("review seal resource ownership does not match")
    expected = {name: hashes[name] for name in names[1:]}
    if (not isinstance(seal, dict) or set(seal) != {"version", "source_root", "artifacts"}
            or type(seal["version"]) is not int or seal["version"] != 1
            or seal["source_root"] != str(root) or seal["artifacts"] != expected):
        raise ValueError("review seal artifact bindings do not match")
    return True


def selection(payload: dict) -> tuple[Path, list[str]] | None:
    cwd = Path(payload.get("cwd") or os.getcwd()).expanduser().resolve()
    event = payload.get("hook_event_name", "")
    if event == "SessionStart":
        root = project_root(cwd)
        return (root, list(TOOLS)) if root and not sealed_snapshot(root) else None
    if event != "PreToolUse":
        return None
    name = str(payload.get("tool_name", ""))
    inputs = payload.get("tool_input") or {}
    if not isinstance(inputs, dict):
        return None
    target, names = cwd, []
    if "codegraph" in name:
        target = inputs.get("projectPath") or inputs.get("repo") or cwd
        names = ["codegraph"]
    elif "zvec" in name and ("search" in name or "query" in name):
        target = inputs.get("root") or inputs.get("workspaceRoot") or cwd
        names = ["zg"]
    elif name.rsplit(".", 1)[-1] in ("exec_command", "shell_command", "shell", "Bash"):
        command = inputs.get("cmd") or inputs.get("command") or ""
        if not isinstance(command, str) or any(c in command for c in ("$", "`", "\n")):
            return None
        tokens = shlex.split(command, posix=True)
        target = Path(inputs.get("workdir") or inputs.get("cwd") or cwd)
        if not target.is_absolute():
            target = cwd / target
        if len(tokens) >= 4 and tokens[0] == "cd" and tokens[2] == "&&":
            target = target / Path(tokens[1]).expanduser()
            tokens = tokens[3:]
        if any(t in (";", "&&", "||", "|", ">", ">>", "<", "&") for t in tokens):
            return None
        if tokens and Path(tokens[0]).name == "rtk":
            tokens = tokens[1:]
            if tokens and tokens[0] == "proxy":
                tokens = tokens[1:]
        if not tokens:
            return None
        tool, args = Path(tokens[0]).name, tokens[1:]
        if "--help" in args or "-h" in args:
            return None
        if tool == "tgrep" and "--" in args and "--no-index" not in args:
            paths = args[args.index("--") + 2:]
            if len(paths) > 1:
                return None
            if paths:
                target = target / Path(paths[0]).expanduser()
            names = ["tgrep"]
        elif tool == "codegraph" and args and args[0] in (
                "explore", "query", "node", "context", "callers", "callees", "impact", "affected", "files"):
            for i, arg in enumerate(args):
                if arg in ("-p", "--path") and i + 1 < len(args):
                    target = target / Path(args[i + 1]).expanduser()
                elif arg.startswith("--path="):
                    target = target / Path(arg.split("=", 1)[1]).expanduser()
            names = ["codegraph"]
        elif tool == "zg" and args and args[0] == "query" and "--rg" not in args:
            names = ["zg"]
    if not names or not isinstance(target, (str, Path)) or "://" in str(target):
        return None
    target = Path(target).expanduser()
    root = project_root(target if target.is_absolute() else cwd / target)
    return (root, names) if root and not sealed_snapshot(root) else None


def load_json(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor) as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError("index configuration must be a bounded regular JSON file")
        value = json.loads(f.read(1024 * 1024))
        if not isinstance(value, dict):
            raise ValueError("index configuration must be a JSON object")  # noqa: TRY004 - validation errors retain the ValueError contract.
        return value


def safe_open(path):
    if path.parent.resolve() != path.parent.absolute():
        raise RuntimeError("refusing a symlinked index-maintenance directory")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise RuntimeError("refusing non-regular or hard-linked index-maintenance file")
    return os.fdopen(descriptor, "r+")


def ignore_indexes(root, deadline):
    path = Path(run(["git", "rev-parse", "--git-path", "info/exclude"], root, deadline).stdout.strip())
    if not path.is_absolute():
        path = root / path
    if path.parent.is_symlink():
        raise RuntimeError("refusing symlinked Git info directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    delta = (root / ".delta").is_dir()
    # Worktrees can share info/exclude while holding distinct index locks.
    with safe_open(path) as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.seek(0)
        old = f.read()
        wanted = [f"/{d}/" for d in INDEX_DIRS] + (["/.sembleignore"] if delta else [])
        missing = [line for line in wanted if line not in old.splitlines()]
        if missing:
            f.write("\n" + "\n".join(missing) + "\n")
    if delta:
        for step in (semble_skip_delta, semble_clear_orphans):
            try:
                step(root, deadline)
            except (OSError, RuntimeError) as error:
                note("semble: " + str(error).splitlines()[0][:180])


def semble_skip_delta(root, deadline):
    # Delta keeps thread checkouts in .delta/. Semble honors .gitignore and .sembleignore, not
    # info/exclude, so without this it indexes every checkout copy. Never edit a committed file.
    path = root / ".sembleignore"
    listed = lambda text: any(line.strip() in (".delta", ".delta/", "/.delta", "/.delta/")
                              for line in text.splitlines())
    if not run(["git", "ls-files", "--error-unmatch", "--", ".sembleignore"], root, deadline,
               check=False).returncode:
        if not (path.is_file() and listed(path.read_text(errors="ignore"))):
            note("semble: the committed .sembleignore lacks .delta/; add it by hand to keep "
                 "Delta checkouts out of Semble")
        return
    with safe_open(path) as f:
        old = f.read()
        if not listed(old):
            f.write(("\n" if old and not old.endswith("\n") else "") + ".delta/\n")


def semble_cache_folder():
    # Where Semble keeps its indexes: SEMBLE_CACHE_LOCATION when absolute, else the platform cache folder.
    override = os.environ.get("SEMBLE_CACHE_LOCATION", "")
    if override and Path(override).is_absolute():
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/semble"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "semble"


def semble_clear_orphans(root, deadline):
    # Semble never evicts its cache, so each Delta checkout it searched leaves an entry (~50 MB) after
    # Delta deletes the checkout. Drop only those: entries for a folder under this root's .delta/ that
    # is gone. Semble's own `clear orphans` would also drop caches of folders on unmounted drives.
    # An entry counts only when its key is the sha256 of its root_path, as Semble checks; any other
    # layout is left alone.
    checkouts = root / ".delta"
    for metadata in sorted(semble_cache_folder().glob("*/index*/metadata.json")):
        if time.monotonic() >= deadline:
            raise TimeoutError("semble cache scan exceeded hook budget")
        entry = metadata.parent.parent
        try:
            source = json.loads(metadata.read_text(encoding="utf-8")).get("root_path")
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(source, str) or not source:
            continue
        source = Path(source).expanduser().resolve()
        if (hashlib.sha256(str(source).encode()).hexdigest() == entry.name and source.is_relative_to(checkouts)
                and not source.exists() and entry.is_dir() and not entry.is_symlink()):
            shutil.rmtree(entry)


def git_files(root, deadline, check=True):
    # Paths Git's ignore rules keep, minus index folders; a nested repository is one "dir/" entry.
    if (root / ".git").exists():
        result = run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                     root, deadline, check=check)
    else:
        # No repository: apply Git's ignore rules through an empty throwaway Git directory.
        with tempfile.TemporaryDirectory(prefix="context-indexes-") as empty:
            run(["git", "init", "-q", "--bare", empty], root, deadline)
            result = run(["git", "--git-dir", empty, "--work-tree", root, "ls-files", "-z", "--others",
                          "--exclude-standard"], root, deadline)
    if result.returncode:
        return []
    return [name for name in result.stdout.split("\0") if name and name.split("/", 1)[0] not in INDEX_DIRS]


def scope(root: Path, deadline: float, tool: str | None = None):
    # tgrep and codegraph descend into nested repositories and hidden folders; zg skips both.
    # Returns every path to fingerprint and, per tool, how many files a full build reads.
    top = [name.rstrip("/") + "/" if (root / name).is_dir() and not (root / name).is_symlink() else name
           for name in git_files(root, deadline)]
    files, pending = list(top), [name for name in top if name.endswith("/")]
    while pending:
        nested = pending.pop()
        for name in git_files(root / nested, deadline, check=False):
            child = root / nested / name
            if child.is_dir() and not child.is_symlink():
                name = name.rstrip("/") + "/"
            files.append(nested + name)
            if name.endswith("/"):
                pending.append(nested + name)
    zg_files = [name for name in top if not name.endswith("/")
                and not any(part.startswith(".") for part in name.split("/"))]
    # Stored includes can opt into hidden paths or nested repositories. Admission
    # must then use the full eligible corpus as a conservative upper bound.
    try:
        manifest = load_json(root / ".zvec-grep/manifest.json")
        roots = manifest.get("rootPaths", [])
        broader = not isinstance(roots, list) or any(
            entry.get("hidden") or entry.get("include") or entry.get("globs") or entry.get("insensitiveGlobs")
            for entry in roots if isinstance(entry, dict))
    except (OSError, ValueError):
        # zg preflight owns configuration errors; code-tool freshness remains
        # independent. Until it is readable, admit against the largest corpus.
        broader = True
    if broader:
        zg_files = [name for name in files if not name.endswith("/")]
    return (zg_files if tool == "zg" else files), {
        "codegraph": sum(not name.endswith("/") for name in files),
        "zg": len(zg_files)}


def fingerprint(root: Path, deadline: float, tool: str | None = None) -> str:
    # ponytail: O(file count) metadata scan, use a watcher only if this is measurably costly.
    files = scope(root, deadline, tool)[0]
    extras = []
    if (root / ".git").exists():
        extras = [run(["git", "rev-parse", "--git-path", "info/exclude"], root, deadline).stdout.strip()]
    digest = hashlib.sha256()
    global_ignore = run(["git", "config", "--path", "--get", "core.excludesFile"],
                        root, deadline, check=False).stdout.strip()
    default_ignore = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git/ignore"
    for extra in (*extras, global_ignore or str(default_ignore), ".gitignore", ".ignore"):
        path = root / Path(extra).expanduser()
        try:
            info = path.lstat()
            digest.update(repr((str(path), info.st_mtime_ns, info.st_ctime_ns, info.st_size)).encode())
        except FileNotFoundError:
            pass
    for name in sorted(set(files)):
        if time.monotonic() >= deadline:
            raise TimeoutError("file scan exceeded hook budget")
        try:
            stat = (root / name).lstat()
            stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_mode)
        except FileNotFoundError:
            stamp = None
        digest.update(repr((name, stamp)).encode(errors="surrogateescape"))
    return digest.hexdigest()


def maintain_tgrep(root, changed, deadline, pass_fds: tuple[int, ...] = ()):
    """Maintain exact-search coverage while retaining the caller's lifecycle leases.

    Args:
        root (Path): Validated index root.
        changed (bool): Whether source coverage changed.
        deadline (float): Absolute monotonic execution deadline.
        pass_fds (tuple[int, ...]): Locks inherited by native maintenance commands.

    Returns:
        bool | None: False when a server owns refresh, otherwise successful completion.

    Raises:
        OSError: A native command cannot launch.
        RuntimeError: Native status fails or coverage remains incomplete.
        TimeoutError: The deadline expired before launch.
        subprocess.TimeoutExpired: A native command exceeded the deadline.
    """
    status = run(["tgrep", "status", root], root, deadline, pass_fds=pass_fds).stdout
    if status.startswith("Server status"):
        # Rebuilding the disk index does not refresh an already-running server.
        if (changed or "Indexing:   complete" not in status or "Hidden coverage: complete" not in status
                or "Reconcile pending: yes" in status or "Reconcile overdue: yes" in status):
            note("tgrep server owns refresh; use tgrep --no-index for current edits")
        return False  # Status does not prove that a running server consumed these edits.
    if changed or "Hidden coverage: complete" not in status:
        args = ["tgrep", "index", root]
        for directory in (".git", *INDEX_DIRS):
            args += ["--exclude", directory]
        if not (root / ".git").exists():
            args.append("--no-require-git")  # honor .gitignore outside a repository
        run(args, root, deadline, pass_fds=pass_fds)
        status = run(["tgrep", "status", root], root, deadline, pass_fds=pass_fds).stdout
        if "Hidden coverage: complete" not in status:
            raise RuntimeError("tgrep index coverage is not ready")


def maintain_codegraph(root, changed, deadline, pass_fds: tuple[int, ...] = ()):
    """Maintain the structural index while retaining the caller's lifecycle leases.

    Args:
        root (Path): Validated index root.
        changed (bool): Whether source coverage changed.
        deadline (float): Absolute monotonic execution deadline.
        pass_fds (tuple[int, ...]): Locks inherited by native maintenance commands.

    Raises:
        OSError: A native command cannot launch.
        ValueError: Native status is malformed.
        RuntimeError: Native indexing fails or exceeds the first-build scope limit.
        TimeoutError: The deadline expired before launch.
        subprocess.TimeoutExpired: A native command exceeded the deadline.
    """
    def status():
        data = json.loads(run(["codegraph", "status", root, "--json"], root, deadline, pass_fds=pass_fds).stdout)
        if Path(data.get("projectPath", root)).resolve() != root:
            return {"initialized": False}
        return data
    data = status()
    if not data.get("initialized") or data.get("index", {}).get("reindexRecommended"):
        # A full build killed at the deadline restarts from scratch next run, so never start one that cannot finish.
        count, limit = scope(root, deadline)[1]["codegraph"], FIRST_BUILD_MAX_FILES["codegraph"]
        if count > limit:
            raise RuntimeError(f"full build skipped: {count} files exceeds the hook's {limit}-file limit; "
                               "build it by hand if wanted")
    if not data.get("initialized"):
        run(["codegraph", "init", root, "--yes"], root, deadline, pass_fds=pass_fds)
    elif data.get("index", {}).get("reindexRecommended"):
        run(["codegraph", "index", root, "--quiet"], root, deadline, pass_fds=pass_fds)
    elif changed or any(data.get("pendingChanges", {}).values()) or data.get("index", {}).get("state") != "complete":
        run(["codegraph", "sync", root, "--quiet"], root, deadline, pass_fds=pass_fds)
    else:
        return
    data = status()
    if not data.get("initialized") or data.get("index", {}).get("state") != "complete":
        raise RuntimeError("codegraph index is not ready")


def zvec_model(root, deadline):
    config = load_json(Path.home() / ".zvec-grep/config.json")
    manifest = load_json(root / ".zvec-grep/manifest.json")
    if manifest:
        if manifest.get("indexPolicy") == "disabled":
            raise RuntimeError("zg indexing is disabled for this workspace")
        for entry in manifest.get("rootPaths", []):
            if (not Path(entry["absolutePath"]).resolve().is_relative_to(root)
                    or entry.get("noIgnore") or entry.get("follow")):
                raise RuntimeError("zg stored corpus needs explicit review")
        schema = manifest.get("embedding") or {}
        model = schema.get("provider", "") + "/" + schema.get("model", "")
    else:
        model = os.environ.get("ZVEC_GREP_EMBEDDING") or config.get("defaults", {}).get("embedding", "")
    if not model.startswith("local/"):
        raise RuntimeError("zg needs an already configured local embedding model")
    if not Path("/usr/bin/sandbox-exec").exists():
        raise RuntimeError("zg automatic indexing needs the local network guard")
    cache = Path(config.get("defaults", {}).get("modelCacheDir") or os.environ.get("ZVEC_GREP_MODEL_CACHE")
                 or str(Path(os.environ.get("ZVEC_GREP_HOME", str(Path.home() / ".zvec-grep"))) / "models")).expanduser().resolve()
    executable = shutil.which("zg")
    if executable is None:
        raise RuntimeError("zg is not installed")
    catalog = Path(executable).resolve().parents[1] / "engine/models/catalog.js"
    # Read the installed catalog, so model names/sizes are never guessed or downloaded.
    script = ("const m=await import(process.argv[1]); "
              "const s=await import(new URL('../pipeline/indexing/scanner/index.js',process.argv[1])); "
              "console.log(JSON.stringify({...m.EMBEDDING_MODEL_CATALOG[process.argv[2]],"
              "gitInventoryVersion:s.ZETTA_DELTA_GIT_INVENTORY_VERSION}))")
    entry = json.loads(run(["node", "--input-type=module", "-e", script, catalog.as_uri(), model], root, deadline).stdout)
    if entry.get("gitInventoryVersion") != 1:
        raise RuntimeError("zg needs the reviewed exact Git exclusion patch before automatic indexing")
    if entry.get("backend") != "llama-cpp" or not entry.get("cacheFile"):
        raise RuntimeError("zg cached model backend is not validated for automatic indexing")
    artifact = cache / entry["cacheFile"]
    if not artifact.is_file() or artifact.stat().st_size != entry["artifacts"][0]["size"]:
        raise RuntimeError("zg local embedding model is not fully cached")
    with artifact.open("rb") as f:
        if f.read(4) != b"GGUF":
            raise RuntimeError("zg cached model is not GGUF")
    return model, cache


def zg_preflight(root, deadline):
    checker = Path.home() / ".local/share/zvec-grep/verify-install.py"
    if checker.is_file() and run([sys.executable, checker], root, deadline, check=False).returncode:
        raise RuntimeError("zg local patch verification failed; "
                           "run ~/.local/share/zvec-grep/verify-install.py")
    return zvec_model(root, deadline)


def write_git_inventory(root: Path, deadline: float) -> Path:
    """Persist literal effective Git exclusions, retaining tracked exceptions.

    Git collapses wholly ignored directories, so generated dependency trees do
    not require a leaf scan. Nested repositories have their own ignore rules.
    """
    tracked, ignored, pending = set(), set(), [("", root)]
    while pending:
        prefix, folder = pending.pop()
        if (folder / ".git").exists():
            names = run(["git", "ls-files", "-z", "--cached"], folder, deadline).stdout.split("\0")
            excluded = run(["git", "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"],
                           folder, deadline).stdout.split("\0")
        else:
            with tempfile.TemporaryDirectory(prefix="context-git-inventory-") as empty:
                run(["git", "init", "-q", "--bare", empty], folder, deadline)
                names = []
                excluded = run(["git", "--git-dir", empty, "--work-tree", folder, "ls-files", "-z",
                                "--others", "--ignored", "--exclude-standard", "--directory"],
                               folder, deadline).stdout.split("\0")
        tracked.update(prefix + name for name in names if name and not (folder / name).is_dir()
                       and name.split("/", 1)[0] not in INDEX_DIRS)
        ignored.update(prefix + name for name in excluded if name)
        ignored.update(prefix + name + "/" for name in INDEX_DIRS)
        for name in git_files(folder, deadline):
            child = folder / name
            if child.is_dir() and not child.is_symlink():
                pending.append((prefix + name.rstrip("/") + "/", child))
    # Maintenance-owned directories are always excluded, even if committed.
    state_dir = maintenance_dir(root, deadline)
    path = state_dir / "codex-zg-git-inventory.json"
    save_state(state_dir, path, {"zettaDeltaGitInventory": 1, "ignored": sorted(ignored), "tracked": sorted(tracked)})
    return path


def maintain_zg(root, changed, deadline, pass_fds: tuple[int, ...] = ()):
    """Maintain the offline native index while preserving any caller-owned leases.

    Args:
        root (Path): Validated index root.
        changed (bool): Whether the effective source corpus changed.
        deadline (float): Absolute monotonic execution deadline.
        pass_fds (tuple[int, ...]): Locks inherited by each native index invocation.

    Raises:
        OSError: Native files or commands are unavailable.
        RuntimeError: Configuration, corpus or native readiness is invalid.
        TimeoutError: The deadline expired before launching a command.
        subprocess.TimeoutExpired: A native command exceeded its deadline.
    """
    model, cache = zg_preflight(root, deadline)
    inventory = write_git_inventory(root, deadline)
    manifest = load_json(root / ".zvec-grep/manifest.json")
    if not any(str(inventory) in entry.get("ignoreFiles", []) for entry in manifest.get("rootPaths", [])
               if isinstance(entry, dict)):
        changed = True  # Existing indexes must adopt the effective Git corpus.
    ignore_files = [inventory]
    for entry in manifest.get("rootPaths", []):
        if isinstance(entry, dict) and Path(entry.get("absolutePath", "")).resolve() == root:
            for value in entry.get("ignoreFiles", []):
                path = Path(value) if Path(value).is_absolute() else root / value
                if path not in ignore_files:
                    ignore_files.append(path)
    for name in (".ignore",) if (root / ".git").exists() else (".gitignore", ".ignore"):
        path = root / name
        if path.is_file() and path not in ignore_files:
            ignore_files.append(path)
    guard = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)"]
    # zg's default 8 embedding contexts peak near 11 GB per build; 2 measured as fast here at 3.75 GB.
    contexts = {"ZVEC_GREP_LLAMA_CONTEXT_PARALLELISM": os.environ.get("ZVEC_GREP_LLAMA_CONTEXT_PARALLELISM") or "2"}

    def invoke(args, check=True):
        result = run(guard + ["zg", *args], root, deadline, check=False, extra_env=contexts, pass_fds=pass_fds)
        if result.returncode:
            # Classify known failures without echoing config paths, values, or raw stderr.
            if "Code: ZVEC_GREP.ENGINE.CONFIG.INVALID" in result.stderr.splitlines():
                if "defaults.disableMetalTensor is not supported" in result.stderr:
                    raise RuntimeError("zg rejects defaults.disableMetalTensor; verify the installed zg build "
                                       "includes local configuration support")
                raise RuntimeError("zg rejected global configuration; verify the installed zg build "
                                   "against ~/.zvec-grep/config.json")
            if check:
                raise RuntimeError(f"zg {args[0]} exited {result.returncode}")
        return result

    status = ["status", root, "--mode", "direct", "--check-ready"]
    if changed or invoke(status, check=False).returncode:
        args = ["index", root, "--mode", "direct", "--embedding", model, "--model-cache", cache]
        for path in ignore_files:
            args += ["--ignore-file", path]
        invoke(args)
        if invoke(status, check=False).returncode:
            raise RuntimeError("zg index is not ready")


def cache_dir():
    default = Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache")
    path = Path(os.environ.get("XDG_CACHE_HOME") or default) / "context-indexes"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def maintenance_dir(root, deadline):
    if (root / ".git").exists():
        return Path(run(["git", "rev-parse", "--absolute-git-dir"], root, deadline).stdout.strip())
    # Non-Git roots keep lock and state in the user cache, never inside the project.
    path = cache_dir() / hashlib.sha256(str(root).encode()).hexdigest()[:24]
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def zg_first_build_lock():
    # One first zg build at a time on this machine: each new Delta checkout is a new root, and
    # concurrent first builds share one GPU while each peaks near 11 GB.
    return cache_dir() / "zg-first-build.lock"


def zg_queue():
    # Roots whose first zg build waits for the machine lock, one per line.
    return cache_dir() / "zg-first-build.queue"


def queue_zg(root):
    with safe_open(zg_queue()) as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        if str(root) not in f.read().splitlines():
            f.write(str(root) + "\n")


def start_queued_zg() -> None:
    """Dispatch waiting roots, retaining queue evidence until each child claims it.

    The first child taking the machine lock builds; the others queue again.
    Dispatch failures retain their roots for the existing queue/retry path.
    """
    try:
        with safe_open(zg_queue()) as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            roots = []
            for line in f.read().splitlines():
                path = Path(line)
                if not path.is_absolute():
                    continue
                try:
                    if stat.S_ISDIR(path.stat().st_mode):
                        roots.append(line)
                except FileNotFoundError:
                    pass
                except OSError:
                    roots.append(line)  # Unreadable is not evidence that a root was deleted.
            f.seek(0)
            f.truncate()
            f.write("".join(line + "\n" for line in roots))
            # A queued child removes its root only after recording startup under the root lock.
        for line in roots:
            if not (Path(line).is_absolute() and Path(line).is_dir()):
                continue
            log = os.open(cache_dir() / "zg-queue.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--zg-queued", line],
                                 cwd=line, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            finally:
                os.close(log)
    except (OSError, RuntimeError) as error:
        note("zg queue: " + str(error).splitlines()[0][:180])


def wait_for_lock(lock, deadline):
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for this worktree's index maintenance")
            time.sleep(min(0.05, remaining))


def load_state(state_path):
    if state_path.is_symlink():
        raise RuntimeError("refusing symlinked index state")
    try:
        return load_json(state_path)
    except (ValueError, OSError):
        return {}


def save_state(state_dir, state_path, state):
    with tempfile.NamedTemporaryFile(mode="w", prefix="codex-context-indexes-", dir=state_dir, delete=False) as f:
        temp = Path(f.name)
        f.write(json.dumps(state) + "\n")
    try:
        if state_path.is_symlink():
            raise RuntimeError("refusing symlinked index state")
        temp.replace(state_path)
    finally:
        temp.unlink(missing_ok=True)


def pending_zg(state_dir: Path, state: dict, stage: str, lease: list[int] | None = None) -> None:
    """Persist pending work before enqueue/launch under the caller's root lock.

    Args:
        state_dir (Path): Existing maintenance directory.
        state (dict): Locked state, updated in place while retaining older receipts.
        stage (str): Queued or starting work.
        lease (list[int] | None): Device/inode of the continuously held build lock.

    Raises:
        OSError: State cannot be persisted; no launch may follow.
        RuntimeError: The state file was redirected.
    """
    previous = state.get("zg", {})
    state["zg"] = {**(previous if isinstance(previous, dict) else {}), "pending": stage}
    for name in ("lease", "outcome", "owner"):
        state["zg"].pop(name, None)
    if lease is not None:
        state["zg"]["lease"] = lease
    save_state(state_dir, state_dir / "codex-context-indexes.json", state)


def claim_zg_queue(root: Path, state_dir: Path, state: dict) -> bool:
    """Transfer a queued root to durable startup before removing its queue entry.

    Args:
        root (Path): Queued index root.
        state_dir (Path): Maintenance directory; caller holds its foreground lock.
        state (dict): Locked completion state, updated in place.

    Returns:
        bool: Whether this process claimed the root; duplicate children do no work.

    Raises:
        OSError: Queue/state persistence failed; pending evidence is retained.
        RuntimeError: A lifecycle file was redirected or is not regular.
    """
    with safe_open(zg_queue()) as queue:
        fcntl.flock(queue, fcntl.LOCK_EX)
        roots = queue.read().splitlines()
        if str(root) not in roots:
            return False
        pending_zg(state_dir, state, "queued")
        queue.seek(0)
        queue.truncate()
        queue.write("".join(line + "\n" for line in roots if line != str(root)))
    return True


def lock_held(path):
    with safe_open(path) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def start_background_zg(root: Path, state_dir: Path, stamp: str, state: dict) -> str:
    """Record startup before detaching a first build with its existing longer budget.

    Args:
        root (Path): Index root.
        state_dir (Path): Maintenance directory; caller holds its foreground lock.
        stamp (str): Source fingerprint bound to the build.
        state (dict): Locked completion state, updated in place.

    Returns:
        str: Build-started status.

    Raises:
        OSError: Log, state or process creation failed.
        RuntimeError: State or log was redirected.
    """
    with safe_open(state_dir / "zg-build.lock") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(lease.fileno())
        pending_zg(state_dir, state, "starting", [info.st_dev, info.st_ino])
        started = False
        try:
            log = os.open(state_dir / "zg-build.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--zg-build", str(root), stamp,
                                  str(lease.fileno())], cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True, pass_fds=(lease.fileno(),))
                started = True
            finally:
                os.close(log)
        except OSError:
            if not started:
                state["zg"] = {"retry_after": time.time() + 60}
                save_state(state_dir, state_dir / "codex-context-indexes.json", state)
            raise
        # Close, never unlock: the inherited open-file description still owns the lease.
    return "zg first build started in the background; zg is unavailable until it finishes"


def zg_index_usable(root: Path, deadline: float) -> bool:
    """Check a native store can be read; ordinary source edits remain inline refreshes."""
    index = root / ".zvec-grep"
    manifest = load_json(index / "manifest.json")
    if not manifest.get("embedding") or not all((index / name).exists() for name in ("files.zvec", "index.zvec")):
        return False
    result = run(["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)",
                  "zg", "status", root, "--mode", "direct", "--check-ready", "--no-color"],
                 root, deadline, check=False)
    # The pinned native CLI reports source differences separately from an
    # uncreated/unreadable store. A stale but readable store needs only refresh.
    return result.returncode == 0 or "Workspace index needs an update" in result.stdout


def reconcile_zg(root: Path, state_dir: Path, state: dict, lock: TextIO) -> None:
    """Reconcile pending work while the caller owns both root lifecycle locks.

    Args:
        root (Path): Validated index root.
        state_dir (Path): Maintenance directory protected by the foreground lock.
        state (dict): Mutable lifecycle evidence, persisted after reconciliation.
        lock (TextIO): Exclusively acquired build lock, proving all lease owners exited.

    Raises:
        IndexPending: Pending startup lacks matching continuous-ownership evidence.
        ValueError: The saved terminal outcome is invalid.
        OSError: Lifecycle evidence cannot be saved.
    """
    receipt = state.get("zg", {})
    if not isinstance(receipt, dict) or "pending" not in receipt or receipt["pending"] == "queued":
        return
    info = os.fstat(lock.fileno())
    if receipt.get("lease") != [info.st_dev, info.st_ino]:
        raise IndexPending("zg startup lacks matching lock ownership; inspect native work before reconciliation")
    outcome = receipt.get("outcome", {"retry_after": time.time() + 60})
    if not isinstance(outcome, dict) or not outcome.keys() & {"fingerprint", "retry_after", "gave_up", "skipped"} or "pending" in outcome:
        raise ValueError("invalid zg terminal outcome")
    owner = receipt.get("owner")
    if owner is not None and (not isinstance(owner, list) or len(owner) != 3
                              or any(type(value) is not int for value in owner[:2])
                              or not isinstance(owner[2], str) or len(owner[2]) != 32):
        raise ValueError("invalid zg index ownership evidence")
    index = root / ".zvec-grep"
    try:
        if owner and "fingerprint" not in outcome and index.is_dir() and not index.is_symlink():
            info = index.stat()
            marker = index / ".codex-build-owner"
            if ((info.st_dev, info.st_ino) == tuple(owner[:2]) and not marker.is_symlink()
                    and marker.is_file() and marker.stat().st_size == len(owner[2])
                    and marker.read_text() == owner[2]
                    and not zg_index_usable(root, time.monotonic() + 2)):
                shutil.rmtree(index)
    except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
        note("zg failure residue was preserved because ownership/readiness could not be confirmed")
    state["zg"] = outcome
    save_state(state_dir, state_dir / "codex-context-indexes.json", state)


def build_zg_in_background(root: Path, stamp: str, lease_fd: int | None = None) -> None:
    """Build under native locks and replace pending state only with terminal evidence.

    Args:
        root (Path): Validated index root.
        stamp (str): Source fingerprint captured by the launching maintainer.
        lease_fd (int | None): Inherited build-lock descriptor, or a direct build.

    Raises:
        OSError: State/index files cannot be accessed or saved.
        ValueError: Review seal bindings or ownership are invalid.
        RuntimeError: Root validation failed or the build must queue for the machine.
        BlockingIOError: Another build holds this root's lock.
        TimeoutError: Maintenance exceeded its native budget.
        subprocess.TimeoutExpired: A native build exceeded its execution deadline.
    """
    if lease_fd is None and sealed_snapshot(root):
        return
    deadline = time.monotonic() + BACKGROUND_SECONDS
    if project_root(root) != root:
        raise RuntimeError("background zg build target is not an index root")
    state_dir = maintenance_dir(root, deadline)
    index = root / ".zvec-grep"
    if lease_fd is not None:
        info = os.fstat(lease_fd)
        current = (state_dir / "zg-build.lock").lstat()
        if (lease_fd < 3 or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise RuntimeError("background build lease was redirected")
    build_lock = os.fdopen(lease_fd, "r+") if lease_fd is not None else safe_open(state_dir / "zg-build.lock")
    outcome = {"retry_after": time.time() + 600}
    owns_machine = False
    try:
        with build_lock, safe_open(zg_first_build_lock()) as machine_lock:
            fcntl.flock(build_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if sealed_snapshot(root):
                return
            info = os.fstat(build_lock.fileno())
            lease = [info.st_dev, info.st_ino]
            try:
                fcntl.flock(machine_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                with safe_open(state_dir / "codex-context-indexes.lock") as lock:
                    wait_for_lock(lock, time.monotonic() + BUDGET_SECONDS)
                    state = load_state(state_dir / "codex-context-indexes.json")
                    pending_zg(state_dir, state, "queued")
                    queue_zg(root)
                raise RuntimeError("another zg first build is running on this machine; this one is queued") from None
            owns_machine = True
            owner = None
            try:
                index.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                token = os.urandom(16).hex()
                with safe_open(index / ".codex-build-owner") as marker:
                    marker.write(token)
                info = index.stat()
                owner = [info.st_dev, info.st_ino, token]
            with safe_open(state_dir / "codex-context-indexes.lock") as lock:
                wait_for_lock(lock, time.monotonic() + BUDGET_SECONDS)
                state = load_state(state_dir / "codex-context-indexes.json")
                pending_zg(state_dir, state, "starting", lease)
                state["zg"]["owner"] = owner
                save_state(state_dir, state_dir / "codex-context-indexes.json", state)
            try:
                maintain_zg(root, True, deadline, pass_fds=(build_lock.fileno(), machine_lock.fileno()))
                outcome = {"fingerprint": stamp}
            except (TimeoutError, subprocess.TimeoutExpired):
                outcome = {"gave_up": stamp}
                raise
            finally:
                with safe_open(state_dir / "codex-context-indexes.lock") as lock:
                    wait_for_lock(lock, time.monotonic() + BUDGET_SECONDS)
                    state = load_state(state_dir / "codex-context-indexes.json")
                    state["zg"] = {"pending": "terminal", "lease": lease, "owner": owner, "outcome": outcome}
                    save_state(state_dir, state_dir / "codex-context-indexes.json", state)
    finally:
        if owns_machine:
            with safe_open(state_dir / "codex-context-indexes.lock") as lock:
                wait_for_lock(lock, time.monotonic() + BUDGET_SECONDS)
                state = load_state(state_dir / "codex-context-indexes.json")
                with safe_open(state_dir / "zg-build.lock") as idle:
                    try:
                        fcntl.flock(idle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass  # A native writer still owns an inherited lease.
                    else:
                        reconcile_zg(root, state_dir, state, idle)
            start_queued_zg()


def reconcile_foreground(state_dir: Path, state: dict, lock: TextIO) -> None:
    """Record interrupted maintenance only after its continuous lease is released.

    Args:
        state_dir (Path): Maintenance directory protected by the acquired lock.
        state (dict): Mutable lifecycle state.
        lock (TextIO): Exclusively acquired foreground lock; all lease owners exited.

    Raises:
        IndexPending: Pending work does not match the acquired lease.
        ValueError: Pending evidence is malformed.
        OSError: The failure receipt cannot be saved.
        RuntimeError: The lifecycle file was redirected.
    """
    if "foreground" not in state:
        return
    receipt = state["foreground"]
    if (not isinstance(receipt, dict) or set(receipt) != {"tool", "lease"}
            or receipt["tool"] not in ("tgrep", "codegraph", "zg")
            or not isinstance(receipt["lease"], list) or len(receipt["lease"]) != 2
            or any(type(value) is not int for value in receipt["lease"])):
        raise ValueError("invalid foreground maintenance ownership")
    info = os.fstat(lock.fileno())
    if receipt["lease"] != [info.st_dev, info.st_ino]:
        raise IndexPending("foreground maintenance lacks matching lock ownership; inspect native work")
    state[receipt["tool"]] = {"retry_after": time.time() + 60}
    del state["foreground"]
    save_state(state_dir, state_dir / "codex-context-indexes.json", state)


def ensure(root: Path, names: list[str], deadline: float, queued: bool = False) -> None:
    """Maintain selected indexes within existing limits and account for pending jobs.

    Args:
        root (Path): Validated index root.
        names (list[str]): Selected native tools.
        deadline (float): Monotonic foreground deadline.
        queued (bool): This child must claim the durable queue entry before work.

    Raises:
        OSError: Lifecycle files cannot be accessed or persisted.
        ValueError: Review seal bindings or ownership are invalid.
        RuntimeError: Source/lifecycle paths were redirected.
        TimeoutError: The foreground lock could not be acquired before the deadline.
    """
    if sealed_snapshot(root):
        return
    state_dir = maintenance_dir(root, deadline)
    with safe_open(state_dir / "codex-context-indexes.lock") as lock:
        # SessionStart runs asynchronously. A query must wait for its initializer
        # instead of immediately querying the missing or partially written index.
        wait_for_lock(lock, deadline)
        if sealed_snapshot(root):
            return
        state_path = state_dir / "codex-context-indexes.json"
        if state_path.is_symlink() or any((root / d).is_symlink() for d in INDEX_DIRS):
            raise RuntimeError("refusing symlinked index state or directory")
        state = load_state(state_path)
        reconcile_foreground(state_dir, state, lock)
        if queued and not claim_zg_queue(root, state_dir, state):
            return
        if (root / ".git").exists():
            ignore_indexes(root, deadline)
        stamps = {}
        for name in names:
            previous = state.get(name, {})
            if not isinstance(previous, dict):
                previous = {}
            if name == "zg" and "pending" in previous and not queued:
                if previous["pending"] == "queued":
                    if lock_held(zg_first_build_lock()):
                        note("zg build is queued behind an active machine build")
                        continue
                    claim_zg_queue(root, state_dir, state)
                    state[name] = {}
                else:
                    with safe_open(state_dir / "zg-build.lock") as build_lock:
                        try:
                            fcntl.flock(build_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            reconcile_zg(root, state_dir, state, build_lock)
                        except (BlockingIOError, IndexPending):
                            note("zg build is active or lacks matching startup ownership")
                            continue
                previous = state.get(name, {})
            if not shutil.which(name):
                if name == "zg" and queued:
                    state[name] = {"retry_after": time.time() + 60}
                note(name + " is not installed")
                continue
            if previous.get("retry_after", 0) > time.time() and not queued:
                note(name + " maintenance is cooling down after an earlier failure")
                continue
            deferred = False
            try:
                corpus = "zg" if name == "zg" else "code"
                if corpus not in stamps:
                    stamps[corpus] = fingerprint(root, deadline, name)
                stamp = stamps[corpus]
                if name == "zg" and lock_held(state_dir / "zg-build.lock"):
                    note("zg first build is still running in the background; zg is unavailable until it finishes")
                    continue
                if name == "zg":
                    zg_preflight(root, deadline)  # report a broken install now, not from the child
                if name == "zg" and not zg_index_usable(root, deadline):
                    count = scope(root, deadline)[1]["zg"]
                    if "gave_up" in previous or count > FIRST_BUILD_MAX_FILES["zg"]:
                        state[name] = {"gave_up": previous["gave_up"]} if "gave_up" in previous else {"skipped": stamp}
                        note(f"zg skipped: a first build here ({count} files) outlasts its "
                             f"{BACKGROUND_SECONDS // 60}-minute background budget; build it by hand if wanted")
                    elif lock_held(zg_first_build_lock()):
                        deferred = True
                        pending_zg(state_dir, state, "queued")
                        queue_zg(root)
                        note("zg first build is queued behind another one running on this machine; "
                             "zg is unavailable here until it finishes")
                    else:
                        deferred = True
                        note(start_background_zg(root, state_dir, stamp, state))
                    continue
                info = os.fstat(lock.fileno())
                state["foreground"] = {"tool": name, "lease": [info.st_dev, info.st_ino]}
                save_state(state_dir, state_path, state)
                try:
                    ready = {"tgrep": maintain_tgrep, "codegraph": maintain_codegraph, "zg": maintain_zg}[name](
                        root, previous.get("fingerprint") != stamp, deadline, pass_fds=(lock.fileno(),))
                    if ready is not False:
                        state[name] = {"fingerprint": stamp}
                finally:
                    state.pop("foreground", None)
            except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.TimeoutExpired) as error:
                # A recorded queue/launch may outlive this foreground error.
                if not deferred:
                    state[name] = {"retry_after": time.time() + 60}
                message = "hook budget exhausted" if isinstance(error, subprocess.TimeoutExpired) else str(error)
                note(name + ": " + message.splitlines()[0][:180])
        save_state(state_dir, state_path, state)


def deadline_expired(_signal, _frame):
    raise TimeoutError("hook budget exhausted")


class IndexPending(RuntimeError):
    """Maintenance lacks terminal evidence needed for safe reuse or removal."""


def plan_indexes(root: Path, names: list[str]) -> dict:
    """Describe optional setup without starting work or changing the source.

    Args:
        root (Path): Source root to prepare.
        names (list[str]): Distinct selected native index tools.

    Returns:
        dict: Selected tools, available executables and possible new cache paths.

    Raises:
        ValueError: Selection is empty, duplicated or unsupported.
    """
    if not names or len(names) != len(set(names)) or not set(names) <= set(TOOLS):
        raise ValueError("select distinct supported index tools")
    caches = list(INDEX_DIRS)
    ignore = root / ".sembleignore"
    if (root / ".delta").is_dir() and not (ignore.exists() or ignore.is_symlink()):
        caches.append(".sembleignore")
    return {"tools": list(names), "available": [name for name in names if shutil.which(name)],
            "cache_paths": caches}


def setup_indexes(root: Path, names: list[str]) -> None:
    """Run existing maintenance within its native foreground budget.

    Args:
        root (Path): Validated source root.
        names (list[str]): Available tools from the persisted setup plan.

    Raises:
        OSError: Lifecycle files cannot be accessed or persisted.
        ValueError: Review seal bindings or ownership are invalid.
        RuntimeError: Source or lifecycle paths were redirected.
        TimeoutError: Maintenance could not acquire its foreground lock in time.
    """
    ensure(root, names, time.monotonic() + BUDGET_SECONDS)


def _lifecycle_state(state_dir: Path) -> dict:
    """Read lifecycle evidence strictly; unreadable evidence never means idle.

    Args:
        state_dir (Path): Native maintenance directory.

    Returns:
        dict: Saved state, or an empty object if no maintenance was recorded.

    Raises:
        ValueError: State was redirected, malformed or is not a bounded JSON object.
        OSError: State is unreadable.
    """
    path = state_dir / "codex-context-indexes.json"
    if path.is_symlink():
        raise ValueError("index state was redirected")
    return load_json(path)


@contextmanager
def idle_indexes(root: Path, expected: list[str]) -> Generator[None, None, None]:
    """Hold native lifecycle locks while a caller seals or removes a source copy.

    Args:
        root (Path): Validated source root.
        expected (list[str]): Available tools whose setup was requested.

    Yields:
        None: Both locks remain held until the caller exits the context.

    Raises:
        IndexPending: Maintenance is active, queued, starting or lacks a terminal receipt.
        ValueError: Lifecycle state or queue was redirected or corrupted.
        RuntimeError: A lifecycle lock is redirected, hard-linked or non-regular.
        OSError: Lifecycle evidence is unreadable.
        subprocess.TimeoutExpired: The native maintenance directory cannot be resolved in time.
    """
    state_dir = maintenance_dir(root, time.monotonic() + BUDGET_SECONDS)
    with ExitStack() as stack:
        locks = []
        for name in ("codex-context-indexes.lock", "zg-build.lock"):
            lock = stack.enter_context(safe_open(state_dir / name))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise IndexPending("index maintenance is active") from error
            locks.append(lock)
        state = _lifecycle_state(state_dir)
        reconcile_foreground(state_dir, state, locks[0])
        reconcile_zg(root, state_dir, state, locks[1])
        receipt = state.get("zg", {})
        if isinstance(receipt, dict) and "pending" in receipt:
            raise IndexPending("zg work is queued, starting, or awaiting a terminal receipt")
        if "zg" in expected:
            queue = zg_queue()
            if queue.is_symlink():
                raise ValueError("index queue was redirected")
            queued = queue.exists() and str(root) in queue.read_text().splitlines()
            if (queued or not isinstance(receipt, dict)
                    or not receipt.keys() & {"fingerprint", "retry_after", "gave_up", "skipped"}):
                raise IndexPending("zg build is queued, starting, or lacks a terminal receipt")
        yield


def unavailable_indexes(root: Path, names: list[str], available: list[str]) -> list[str]:
    """Check selected indexes against current source while idle_indexes is held.

    Args:
        root (Path): Source root protected by the caller's lifecycle guard.
        names (list[str]): Tools selected in the persisted setup plan.
        available (list[str]): Executables available when setup was requested.

    Returns:
        list[str]: Selected indexes missing a current completion fingerprint.

    Raises:
        ValueError: Lifecycle evidence is redirected, malformed or non-regular.
        OSError: Lifecycle evidence cannot be read.
        subprocess.TimeoutExpired: The native maintenance directory cannot be resolved in time.
        RuntimeError: A tool's freshness check failed; its name is in the message.
    """
    state = _lifecycle_state(maintenance_dir(root, time.monotonic() + BUDGET_SECONDS))
    unavailable = []
    for name in names:
        receipt = state.get(name, {})
        try:
            ready = (name in available and isinstance(receipt, dict)
                     and receipt.get("fingerprint") == fingerprint(root, time.monotonic() + BUDGET_SECONDS, name))
        except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"cannot verify {name} index freshness") from error
        if not ready:
            unavailable.append(name)
    return unavailable


def main():
    if len(sys.argv) in (4, 5) and sys.argv[1] == "--zg-build":
        signal.signal(signal.SIGALRM, deadline_expired)
        signal.alarm(BACKGROUND_SECONDS)
        try:
            build_zg_in_background(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]) if len(sys.argv) == 5 else None)
        except Exception as error:  # noqa: BLE001 - native hook entry points must fail open.
            note("zg background build: " + str(error).splitlines()[0][:180])
        finally:
            signal.alarm(0)
        return 0
    deadline = time.monotonic() + BUDGET_SECONDS
    signal.signal(signal.SIGALRM, deadline_expired)
    signal.alarm(BUDGET_SECONDS)
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--zg-queued":  # started by start_queued_zg
            root = Path(sys.argv[2])
            selected = (root, ["zg"]) if project_root(root) == root else None
        else:
            selected = selection(read_payload())
        if selected:
            root, names = selected
            ensure(root, names, deadline, queued=len(sys.argv) == 3 and sys.argv[1] == "--zg-queued")
    except Exception as error:  # noqa: BLE001 - native hook entry points must fail open.
        note(str(error).splitlines()[0][:180])
    finally:
        signal.alarm(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
