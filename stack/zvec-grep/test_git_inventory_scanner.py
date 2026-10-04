"""Optional pure scanner integration; never creates a store or loads a model.

Set ZETTA_ZG_SCANNER_SOURCE to the pinned upstream scanner TS (before our
patch), ZETTA_ZG_DIST to inspected dist dependencies, and ZETTA_NODE to a
direct Node >=24 interpreter. No package manager or network runs here.
"""
import json
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

PATCH = Path(__file__).parents[1] / "patches/zvec-grep/0003-fix-preserve-exact-effective-Git-exclusions.patch"


class GitInventoryScanner(unittest.TestCase):
    def setUp(self):
        source = os.environ.get("ZETTA_ZG_SCANNER_SOURCE")
        dist = os.environ.get("ZETTA_ZG_DIST")
        self.node = os.environ.get("ZETTA_NODE")
        if not all((source, dist, self.node)):
            self.skipTest("pure scanner fixture/interpreter paths were not supplied")
        temporary = tempfile.TemporaryDirectory(prefix="zg-exact-scanner-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        environment = patch.dict(os.environ, {"HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.base / "config"), "XDG_CACHE_HOME": str(self.base / "cache"),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.copy = self.base / "upstream"
        scanner = self.copy / "src/engine/pipeline/indexing/scanner/index.ts"
        scanner.parent.mkdir(parents=True)
        shutil.copy2(source, scanner)
        if PATCH.exists():
            subprocess.run(["git", "apply", "--check", str(PATCH)], cwd=self.copy, check=True,
                           capture_output=True, text=True)
            subprocess.run(["git", "apply", str(PATCH)], cwd=self.copy, check=True,
                           capture_output=True, text=True)
        self.scanner = scanner
        installed = Path(dist).resolve()
        pending = [installed / "engine/pipeline/indexing/scanner/index.js"]
        seen = set()
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            destination = self.copy / "src" / path.relative_to(installed)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            for dependency in re.findall(r'(?:from\s+|import\s*)["\x27]([^"\x27]+)["\x27]', path.read_text()):
                if dependency.startswith("."):
                    pending.append((path.parent / dependency).resolve())
        (self.copy / "package.json").write_text('{"type":"module"}')
        self.inventory = self.base / "git-inventory.json"

    def scan(self, ignored, tracked, extra_ignore=None):
        self.inventory.write_text(json.dumps({"zettaDeltaGitInventory": 1, "ignored": ignored, "tracked": tracked}))
        files = [str(self.inventory)] + ([str(extra_ignore)] if extra_ignore else [])
        code = """import fs from 'node:fs/promises'; import {syncBuiltinESMExports} from 'node:module';
        const visited=[]; const original=fs.readdir;
        fs.readdir=async (...args)=>{visited.push(args[0]); return original(...args)};
        syncBuiltinESMExports(); const {scanRootPaths}=await import(process.argv[1]);
        const result=await scanRootPaths('fixture',[{absolutePath:process.argv[2],recursive:true,
          ignoreFiles:JSON.parse(process.argv[3])}]);
        console.log(JSON.stringify({files:result.files.map(f=>f.relativePath).sort(),visited}));"""
        env = dict(os.environ, HOME=str(self.base / "home"), XDG_CACHE_HOME=str(self.base / "cache"),
                   XDG_CONFIG_HOME=str(self.base / "config"), PATH=str(self.base / "empty-path"))
        result = subprocess.run([self.node, "--input-type=module", "-e", code, self.scanner.as_uri(),
                                 str(self.root), json.dumps(files)], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.visited = output["visited"]
        return output["files"]

    def test_literal_exclusions_and_tracked_gitignore_exceptions(self):
        ignored = ["private.txt", "back\\slash.txt", "line\nbreak.txt", "star*.txt", "[literal].txt", "end .txt"]
        for name in ["main.py", "tracked.py", *ignored]:
            (self.root / name).write_text("synthetic source\n")
        (self.root / "tracked-dir").mkdir()
        (self.root / "tracked-dir/keep.py").write_text("synthetic source\n")
        (self.root / "back").mkdir()
        (self.root / "back/slash.txt").write_text("different path\n")
        (self.root / ".gitignore").write_text("tracked.py\ntracked-dir/\n")
        self.assertEqual(self.scan(ignored, ["tracked.py", "tracked-dir/keep.py"]),
                         ["back/slash.txt", "main.py", "tracked-dir/keep.py", "tracked.py"])

    def test_explicit_ignore_filters_still_apply_to_tracked_files(self):
        (self.root / "tracked.py").write_text("synthetic source\n")
        (self.root / ".gitignore").write_text("tracked.py\n")
        explicit = self.base / "explicit.ignore"
        explicit.write_text("tracked.py\n")
        self.assertEqual(self.scan([], ["tracked.py"], explicit), [])

    def test_literal_directory_exclusions_preserve_tracked_descendants(self):
        for name in ("ignored/deep/file.py", "mixed/keep.py", "mixed/private.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic source\n")
        (self.root / ".gitignore").write_text("mixed/\n")
        self.assertEqual(self.scan(["ignored/", "mixed/"], ["mixed/keep.py"]), ["mixed/keep.py"])
        self.assertNotIn(str(self.root / "ignored"), self.visited, "fully ignored directories must be pruned")

    def test_generated_effective_git_inventory_matches_scanned_corpus(self):
        hook_path = Path(__file__).parents[1] / "context-indexes/ensure-context-indexes.py"
        spec = importlib.util.spec_from_file_location("inventory_hook", hook_path)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)

        def git(*args):
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                            "-C", str(self.root), *args], check=True, capture_output=True, text=True)

        git("init", "-q")
        for name in ("main.py", "tracked.py"):
            (self.root / name).write_text("synthetic source\n")
        git("add", "main.py", "tracked.py")
        (self.root / ".gitignore").write_text("tracked.py\n")
        (self.root / ".git/info/exclude").write_text("local-private.py\ntracked.py\n")
        global_ignore = self.home / "global-ignore"
        global_ignore.write_text("*.txt\ntracked.py\nhuge/\n")
        git("config", "core.excludesFile", str(global_ignore))
        for name in ("local-private.py", "back\\slash.txt", "line\nbreak.txt", "star*.txt", "[literal].txt",
                     "huge/deep/file.py", "explicit.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic source\n")
        explicit = self.root / ".ignore"
        explicit.write_text("explicit.py\n")
        inventory_path = hook.write_git_inventory(self.root, time.monotonic() + 5)
        inventory = json.loads(inventory_path.read_text())
        self.assertIn("huge/", inventory["ignored"])
        self.assertNotIn("huge/deep/file.py", inventory["ignored"])
        actual = self.scan(inventory["ignored"], inventory["tracked"], explicit)
        self.assertEqual(actual, ["main.py", "tracked.py"])
        self.assertNotIn(str(self.root / "huge"), self.visited)
        # Native default/explicit filters may narrow Git's eligible corpus.
        self.assertEqual(hook.scope(self.root, time.monotonic() + 5)[1]["zg"], 3)


if __name__ == "__main__":
    unittest.main()
