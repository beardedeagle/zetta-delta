#!/usr/bin/env python3
"""Bounded, fail-open Codex SessionStart / search PreToolUse index maintenance.

Only selected Git worktrees and the non-Git folders listed in $XDG_CONFIG_HOME/zetta-delta/index-roots (default
~/.config) are indexed. No shell evaluation, model downloads, remote embeddings, persistent servers, or watchers.
Native tools own indexes.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import tempfile

BUDGET_SECONDS = 55
BACKGROUND_SECONDS = 1800  # a first zg build; refreshes of an existing index fit BUDGET_SECONDS
# Full builds above these sizes are skipped with a note, never started: measured here, codegraph
# indexes ~230 files/s inside BUDGET_SECONDS and zg ~3 files/s inside BACKGROUND_SECONDS.
FIRST_BUILD_MAX_FILES = {"codegraph": 5000, "zg": 4000}
INDEX_DIRS = (".tgrep", ".codegraph", ".zvec-grep")
TOOLS = ("tgrep", "codegraph", "zg")
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


def run(args, root, deadline, check=True, extra_env=None):
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
                             text=True, start_new_session=True)
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


def selection(payload):
    cwd = Path(payload.get("cwd") or os.getcwd()).expanduser().resolve()
    event = payload.get("hook_event_name", "")
    if event == "SessionStart":
        root = project_root(cwd)
        return (root, list(TOOLS)) if root else None
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
    return (root, names) if root else None


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
            raise ValueError("index configuration must be a JSON object")
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


def maintain_tgrep(root, changed, deadline):
    status = run(["tgrep", "status", root], root, deadline).stdout
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
        run(args, root, deadline)
        status = run(["tgrep", "status", root], root, deadline).stdout
        if "Hidden coverage: complete" not in status:
            raise RuntimeError("tgrep index coverage is not ready")


def maintain_codegraph(root, changed, deadline):
    def status():
        data = json.loads(run(["codegraph", "status", root, "--json"], root, deadline).stdout)
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
        run(["codegraph", "init", root, "--yes"], root, deadline)
    elif data.get("index", {}).get("reindexRecommended"):
        run(["codegraph", "index", root, "--quiet"], root, deadline)
    elif changed or any(data.get("pendingChanges", {}).values()) or data.get("index", {}).get("state") != "complete":
        run(["codegraph", "sync", root, "--quiet"], root, deadline)
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
    catalog = Path(shutil.which("zg")).resolve().parents[1] / "engine/models/catalog.js"
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


def maintain_zg(root, changed, deadline):
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
        result = run(guard + ["zg", *args], root, deadline, check=False, extra_env=contexts)
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


def start_queued_zg():
    # Run the maintainer once for each waiting root: the first to take the machine lock starts its
    # build, and the others queue again behind it.
    try:
        with safe_open(zg_queue()) as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            roots = f.read().splitlines()
            f.seek(0)
            f.truncate()
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


def lock_held(path):
    with safe_open(path) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def start_background_zg(root, state_dir, stamp):
    # A first zg build outlasts the hook budget, and a build killed at the deadline leaves
    # residue zg cannot reopen, so it runs once, detached, with its own longer budget.
    log = os.open(state_dir / "zg-build.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--zg-build", str(root), stamp],
                         cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    finally:
        os.close(log)
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


def build_zg_in_background(root, stamp):
    deadline = time.monotonic() + BACKGROUND_SECONDS
    if project_root(root) != root:
        raise RuntimeError("background zg build target is not an index root")
    state_dir = maintenance_dir(root, deadline)
    index = root / ".zvec-grep"
    with safe_open(state_dir / "zg-build.lock") as build_lock, safe_open(zg_first_build_lock()) as machine_lock:
        fcntl.flock(build_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another build already holds it
        try:
            fcntl.flock(machine_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Two hooks raced past the check in ensure: wait in the queue for the running build.
            queue_zg(root)
            raise RuntimeError("another zg first build is running on this machine; this one is queued") from None
        owner = None
        try:
            index.mkdir(mode=0o700)
        except FileExistsError:
            pass  # Existing directories, including partial stores, have unknown ownership.
        else:
            token = os.urandom(16).hex()
            marker = index / ".codex-build-owner"
            with safe_open(marker) as f:
                f.write(token)
            info = index.stat()
            owner = (info.st_dev, info.st_ino, token)
        outcome = {"retry_after": time.time() + 600}  # a failed build backs off before the next attempt
        try:
            maintain_zg(root, True, deadline)
            outcome = {"fingerprint": stamp}  # edits during the build differ and refresh inline
        except (TimeoutError, subprocess.TimeoutExpired):
            # The killed build left residue zg cannot reopen: drop only an index this build created,
            # and stop retrying until someone builds it by hand.
            try:
                if owner and index.is_dir() and not index.is_symlink():
                    info = index.stat()
                    marker = index / ".codex-build-owner"
                    if ((info.st_dev, info.st_ino) == owner[:2] and not marker.is_symlink()
                            and marker.is_file() and marker.stat().st_size == len(owner[2])
                            and marker.read_text() == owner[2]
                            and not zg_index_usable(root, time.monotonic() + 2)):
                        shutil.rmtree(index, ignore_errors=True)
            except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
                note("zg timeout residue was preserved because ownership/readiness could not be confirmed")
            outcome = {"gave_up": stamp}
            raise
        finally:
            try:
                with safe_open(state_dir / "codex-context-indexes.lock") as lock:
                    wait_for_lock(lock, time.monotonic() + BUDGET_SECONDS)
                    state_path = state_dir / "codex-context-indexes.json"
                    state = load_state(state_path)
                    state["zg"] = outcome
                    save_state(state_dir, state_path, state)
            finally:
                fcntl.flock(machine_lock, fcntl.LOCK_UN)
                start_queued_zg()


def ensure(root, names, deadline):
    state_dir = maintenance_dir(root, deadline)
    with safe_open(state_dir / "codex-context-indexes.lock") as lock:
        # SessionStart runs asynchronously. A query must wait for its initializer
        # instead of immediately querying the missing or partially written index.
        wait_for_lock(lock, deadline)
        state_path = state_dir / "codex-context-indexes.json"
        if state_path.is_symlink() or any((root / d).is_symlink() for d in INDEX_DIRS):
            raise RuntimeError("refusing symlinked index state or directory")
        if (root / ".git").exists():
            ignore_indexes(root, deadline)
        stamps = {}
        state = load_state(state_path)
        for name in names:
            if not shutil.which(name):
                note(name + " is not installed")
                continue
            previous = state.get(name, {})
            if not isinstance(previous, dict):
                previous = {}
            if previous.get("retry_after", 0) > time.time():
                note(name + " maintenance is cooling down after an earlier failure")
                continue
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
                        note(f"zg skipped: a first build here ({count} files) outlasts its "
                             f"{BACKGROUND_SECONDS // 60}-minute background budget; build it by hand if wanted")
                    elif lock_held(zg_first_build_lock()):
                        queue_zg(root)
                        note("zg first build is queued behind another one running on this machine; "
                             "zg is unavailable here until it finishes")
                    else:
                        note(start_background_zg(root, state_dir, stamp))
                    continue
                ready = {"tgrep": maintain_tgrep, "codegraph": maintain_codegraph, "zg": maintain_zg}[name](
                    root, previous.get("fingerprint") != stamp, deadline)
                if ready is not False:
                    state[name] = {"fingerprint": stamp}
            except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.TimeoutExpired) as error:
                state[name] = {"retry_after": time.time() + 60}
                message = "hook budget exhausted" if isinstance(error, subprocess.TimeoutExpired) else str(error)
                note(name + ": " + message.splitlines()[0][:180])
        save_state(state_dir, state_path, state)


def deadline_expired(_signal, _frame):
    raise TimeoutError("hook budget exhausted")


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--zg-build":
        try:
            build_zg_in_background(Path(sys.argv[2]), sys.argv[3])
        except Exception as error:
            note("zg background build: " + str(error).splitlines()[0][:180])
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
            ensure(*selected, deadline)
    except Exception as error:
        note(str(error).splitlines()[0][:180])
    finally:
        signal.alarm(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
