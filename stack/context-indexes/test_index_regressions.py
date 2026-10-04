"""Index lifecycle regressions, using isolated Git roots and fake native tools."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HOOK = Path(__file__).with_name("ensure-context-indexes.py")


class IndexRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="index-regressions-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.env = patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.base / "config"),
            "XDG_CACHE_HOME": str(self.base / "cache"), "SEMBLE_CACHE_LOCATION": str(self.base / "semble"),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        spec = importlib.util.spec_from_file_location("index_regression_hook", HOOK)
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.git(self.root, "init", "-q")
        (self.root / "main.py").write_text("x = 1\n")
        fake = self.bin / "zg"
        fake.write_text("#!" + sys.executable + "\nimport pathlib,sys\n"
                        "root=pathlib.Path(sys.argv[2])\n"
                        "ready=(root/'.zvec-grep/fixture-ready').exists()\n"
                        "print('Workspace index is ready' if ready else 'Workspace index is not created')\n"
                        "sys.exit(0 if ready else 1)\n")
        fake.chmod(0o755)

    def git(self, root, *args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                               "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                               "-C", str(root), *args], capture_output=True, text=True, check=True)

    def test_tracked_gitlink_edits_refresh_code_tools_but_not_zg(self):
        nested = self.root / "module"
        nested.mkdir()
        self.git(nested, "init", "-q")
        child = nested / "child.py"
        child.write_text("x = 1\n")
        self.git(nested, "add", "child.py")
        self.git(nested, "commit", "-qm", "fixture")
        self.git(self.root, "add", "main.py", "module")
        before = self.hook.fingerprint(self.root, time.monotonic() + 5)
        files, counts = self.hook.scope(self.root, time.monotonic() + 5)
        self.assertIn("module/child.py", files)
        self.assertEqual(counts, {"codegraph": 2, "zg": 1})
        child.write_text("x = 222\n")
        self.assertNotEqual(before, self.hook.fingerprint(self.root, time.monotonic() + 5))

    def test_empty_index_directory_obeys_first_build_admission(self):
        (self.root / ".zvec-grep").mkdir()
        with patch.object(self.hook, "zg_preflight"), \
             patch.dict(self.hook.FIRST_BUILD_MAX_FILES, {"zg": 0}), \
             patch.object(self.hook, "maintain_zg") as inline, \
             patch.object(self.hook, "start_background_zg") as background:
            self.hook.ensure(self.root, ["zg"], time.monotonic() + 5)
        inline.assert_not_called()
        background.assert_not_called()

    def test_recursively_tracked_gitlink_edits_are_in_the_code_corpus(self):
        middle = self.root / "module"
        deep = middle / "child"
        deep.mkdir(parents=True)
        self.git(middle, "init", "-q")
        self.git(deep, "init", "-q")
        source = deep / "deep.py"
        source.write_text("x = 1\n")
        self.git(deep, "add", "deep.py")
        self.git(deep, "commit", "-qm", "fixture")
        (middle / "middle.py").write_text("x = 1\n")
        self.git(middle, "add", "child", "middle.py")
        self.git(middle, "commit", "-qm", "fixture")
        self.git(self.root, "add", "main.py", "module")
        before = self.hook.fingerprint(self.root, time.monotonic() + 5)
        files, counts = self.hook.scope(self.root, time.monotonic() + 5)
        self.assertIn("module/child/deep.py", files)
        self.assertEqual(counts, {"codegraph": 3, "zg": 1})
        source.write_text("x = 333\n")
        self.assertNotEqual(before, self.hook.fingerprint(self.root, time.monotonic() + 5))

    def test_stored_broader_zg_filters_use_a_conservative_corpus(self):
        (self.root / ".hidden.py").write_text("hidden source\n")
        child = self.root / "nested"
        child.mkdir()
        self.git(child, "init", "-q")
        (child / "child.py").write_text("nested source\n")
        index = self.root / ".zvec-grep"
        index.mkdir()
        (index / "manifest.json").write_text(json.dumps({"rootPaths": [{"hidden": True}]}))
        self.assertEqual(self.hook.scope(self.root, time.monotonic() + 5)[1]["zg"], 3)
        self.assertIn("nested/child.py", self.hook.scope(self.root, time.monotonic() + 5, "zg")[0])

    def test_native_readiness_distinguishes_stale_and_unreadable_stores(self):
        index = self.root / ".zvec-grep"
        index.mkdir()
        (index / "manifest.json").write_text('{"embedding":{"provider":"local"}}')
        for name in ("files.zvec", "index.zvec"):
            (index / name).mkdir()
        for code, output, expected in ((0, "Workspace index is ready", True),
                                       (1, "Workspace index needs an update", True),
                                       (1, "Workspace index is not created", False)):
            with self.subTest(output=output), patch.object(self.hook, "run", return_value=
                    subprocess.CompletedProcess([], code, output, "")):
                self.assertEqual(self.hook.zg_index_usable(self.root, time.monotonic() + 5), expected)

    def test_module_imports_on_system_python_without_running_main(self):
        system_python = Path("/usr/bin/python3")
        if not system_python.exists():
            self.skipTest("system Python is unavailable")
        code = ("import importlib.util; spec=importlib.util.spec_from_file_location('hook'," + repr(str(HOOK)) +
                "); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)")
        result = subprocess.run([str(system_python), "-c", code], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_corrupt_zg_manifest_does_not_block_code_tool_fingerprints(self):
        index = self.root / ".zvec-grep"
        index.mkdir()
        (index / "manifest.json").write_text("{broken")
        self.assertTrue(self.hook.fingerprint(self.root, time.monotonic() + 5, "codegraph"))

    def test_empty_index_directory_uses_background_budget(self):
        (self.root / ".zvec-grep").mkdir()
        with patch.object(self.hook, "zg_preflight"), \
             patch.object(self.hook, "maintain_zg") as inline, \
             patch.object(self.hook, "start_background_zg", return_value="fixture background") as background:
            self.hook.ensure(self.root, ["zg"], time.monotonic() + 5)
        inline.assert_not_called()
        background.assert_called_once()

    def test_unknown_partial_index_is_kept_after_background_timeout(self):
        index = self.root / ".zvec-grep"
        index.mkdir()
        sentinel = index / "unknown-owner-data"
        sentinel.write_text("preserve")
        with patch.object(self.hook, "maintain_zg", side_effect=subprocess.TimeoutExpired("fake-zg", 1)), \
             patch.object(self.hook, "start_queued_zg"):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.hook.build_zg_in_background(self.root, "fixture")
        self.assertEqual(sentinel.read_text(), "preserve")

    def test_replacement_index_is_not_owned_by_the_background_build(self):
        index = self.root / ".zvec-grep"
        sentinel = index / "replacement-owner"

        def replaced(*args):
            if index.exists():
                shutil.rmtree(index)
            index.mkdir()
            sentinel.write_text("preserve replacement")
            raise subprocess.TimeoutExpired("fake-zg", 1)

        with patch.object(self.hook, "maintain_zg", side_effect=replaced), \
             patch.object(self.hook, "start_queued_zg"):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.hook.build_zg_in_background(self.root, "fixture")
        self.assertTrue(sentinel.exists(), "an index with another owner must survive cleanup")

    def test_zg_receives_literal_git_inventory_and_tracked_exceptions(self):
        ignored = ["private.txt", "back\\slash.txt", "line\nbreak.txt", "star*.txt", "[literal].txt"]
        for name in ["tracked.py", *ignored]:
            (self.root / name).write_text("synthetic source\n")
        self.git(self.root, "add", "main.py", "tracked.py")
        (self.root / ".gitignore").write_text("tracked.py\n")
        (self.root / ".git/info/exclude").write_text("private.txt\ntracked.py\n")
        global_ignore = self.home / "global-ignore"
        global_ignore.write_text("*.txt\ntracked.py\n")
        self.git(self.root, "config", "core.excludesFile", str(global_ignore))
        real_run = self.hook.run
        commands = []

        def fake_native(args, *rest, **kwargs):
            if args[0] != "/usr/bin/sandbox-exec":
                return real_run(args, *rest, **kwargs)
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(self.hook, "zg_preflight", return_value=("local/cached", self.base / "models")), \
             patch.object(self.hook, "run", side_effect=fake_native):
            self.hook.maintain_zg(self.root, True, time.monotonic() + 5)
        arguments = commands[0]
        self.assertIn("--ignore-file", arguments, "Git exclusions must reach the native scanner")
        inventory = json.loads(Path(arguments[arguments.index("--ignore-file") + 1]).read_text())
        self.assertEqual(inventory["zettaDeltaGitInventory"], 1)
        self.assertEqual(set(inventory["ignored"]) & set(ignored), set(ignored))
        self.assertIn("tracked.py", inventory["tracked"])
        self.assertNotIn("tracked.py", inventory["ignored"])
        self.assertEqual(self.hook.scope(self.root, time.monotonic() + 5)[1]["zg"], 2)


if __name__ == "__main__":
    unittest.main()
