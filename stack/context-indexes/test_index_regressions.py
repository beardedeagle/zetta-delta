"""Index lifecycle regressions, using isolated Git roots and fake native tools."""
import fcntl
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
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
        assert spec is not None and spec.loader is not None
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

    def test_setup_plan_preserves_source_and_uses_native_budget(self):
        (self.root / ".delta").mkdir()
        before = set(self.root.rglob("*"))
        with patch.object(self.hook.shutil, "which", side_effect=lambda name: "/fixture/tgrep" if name == "tgrep" else None), \
             patch.object(self.hook, "ensure") as ensure:
            plan = self.hook.plan_indexes(self.root, ["tgrep", "zg"])
            self.assertEqual(plan["available"], ["tgrep"])
            self.assertIn(".sembleignore", plan["cache_paths"])
            self.assertEqual(set(self.root.rglob("*")), before)
            ensure.assert_not_called()
            with patch.object(self.hook.time, "monotonic", return_value=100):
                self.hook.setup_indexes(self.root, plan["available"])
            ensure.assert_called_once_with(self.root, ["tgrep"], 100 + self.hook.BUDGET_SECONDS)
        (self.root / ".sembleignore").symlink_to(self.base / "missing")
        self.assertNotIn(".sembleignore", self.hook.plan_indexes(self.root, ["tgrep"])["cache_paths"])
        for selection in ([], ["tgrep", "tgrep"], ["other"]):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                self.hook.plan_indexes(self.root, selection)

    def test_idle_api_holds_both_native_locks_and_releases_on_exception(self):
        locks = [self.root / ".git" / name for name in ("codex-context-indexes.lock", "zg-build.lock")]
        with (
            self.assertRaisesRegex(RuntimeError, "caller interrupted"),
            self.hook.idle_indexes(self.root, []),
        ):
            self.assertTrue(all(self.hook.lock_held(path) for path in locks))
            raise RuntimeError("caller interrupted")
        self.assertFalse(any(self.hook.lock_held(path) for path in locks))

    def test_idle_api_rejects_corrupt_and_redirected_lifecycle_evidence(self):
        state = self.root / ".git/codex-context-indexes.json"
        for payload in ("{", "[]"):
            state.write_text(payload)
            with (
                self.subTest(payload=payload),
                self.assertRaises(ValueError),
                self.hook.idle_indexes(self.root, []),
            ):
                self.fail("corrupt evidence cannot imply idle")
        state.write_text('{"zg": {"fingerprint": "old", "pending": "starting"}}')
        with (
            self.assertRaises(self.hook.IndexPending),
            self.hook.idle_indexes(self.root, []),
        ):
            self.fail("pending work cannot be cleared by an older receipt")
        external = self.base / "external.json"
        state.rename(external)
        state.symlink_to(external)
        with (
            self.assertRaisesRegex(ValueError, "redirected"),
            self.hook.idle_indexes(self.root, []),
        ):
            self.fail("redirected state cannot imply idle")
        state.unlink()
        state.write_text('{"zg": {"fingerprint": "old"}}')
        queue = self.hook.zg_queue()
        queue.symlink_to(external)
        with (
            self.assertRaisesRegex(ValueError, "redirected"),
            self.hook.idle_indexes(self.root, ["zg"]),
        ):
            self.fail("redirected queue cannot imply idle")
        self.assertEqual(json.loads(external.read_text())["zg"]["pending"], "starting")

    def test_readiness_api_reports_stale_missing_and_unreadable_indexes(self):
        state_dir = self.root / ".git"
        self.hook.save_state(state_dir, state_dir / "codex-context-indexes.json",
                             {"tgrep": {"fingerprint": "old"}, "zg": {"retry_after": 123}})
        with self.hook.idle_indexes(self.root, ["tgrep", "zg"]):
            with patch.object(self.hook, "fingerprint", return_value="current"):
                self.assertEqual(self.hook.unavailable_indexes(self.root, ["tgrep", "zg"], ["tgrep"]), ["tgrep", "zg"])
            with patch.object(self.hook, "fingerprint", return_value="old"):
                self.assertEqual(self.hook.unavailable_indexes(self.root, ["tgrep", "zg"], ["tgrep"]), ["zg"])
            with (
                patch.object(self.hook, "fingerprint", side_effect=TimeoutError),
                self.assertRaisesRegex(RuntimeError, "cannot verify tgrep"),
            ):
                self.hook.unavailable_indexes(self.root, ["tgrep"], ["tgrep"])

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

    def test_nested_git_exclusions_reindex_a_ready_broader_zg_corpus(self):
        nested = self.root / "nested"
        nested.mkdir()
        self.git(nested, "init", "-q")
        (nested / "tracked.py").write_text("tracked = 1\n")
        self.git(nested, "add", "tracked.py")
        (nested / "cache.py").write_text("cache = 1\n")
        generated = nested / "generated"
        generated.mkdir()
        (generated / "source.py").write_text("generated = 1\n")
        index = self.root / ".zvec-grep"
        index.mkdir()
        manifest = index / "manifest.json"
        manifest.write_text(json.dumps({"rootPaths": [{"absolutePath": str(self.root), "include": ["nested/**"]}]}))
        self.hook.ignore_indexes(self.root, time.monotonic() + 10)
        inventory = self.hook.write_git_inventory(self.root, time.monotonic() + 10)
        manifest.write_text(json.dumps({"rootPaths": [{"absolutePath": str(self.root),
                                                        "include": ["nested/**"], "ignoreFiles": [str(inventory)]}]}))
        exclude = nested / ".git/info/exclude"
        original = exclude.read_text()
        indexes = []
        original_run = self.hook.run
        # Source bytes stay unchanged. Keep their metadata fixed too, since
        # macOS bookkeeping can change ctime while the fixture reads them.
        fixed = {path: path.lstat() for path in (self.root / "main.py", nested / "tracked.py",
                                                nested / "cache.py", generated / "source.py",
                                                self.root / ".git/info/exclude")}
        original_lstat = Path.lstat

        def source_stat(path):
            return fixed[path] if path in fixed else original_lstat(path)

        def fake_native(argv, root, deadline, **kwargs):
            if argv[0] != "/usr/bin/sandbox-exec":
                return original_run(argv, root, deadline, **kwargs)
            native = argv[3:]
            self.assertEqual(native[0], "zg")
            if native[1] == "index":
                indexes.append(native)
            else:
                self.assertEqual(native[1], "status")
            return subprocess.CompletedProcess(argv, 0, "Workspace index is ready\n", "")

        def maintain():
            self.hook.ensure(self.root, ["zg"], time.monotonic() + 10)
            return self.hook.fingerprint(self.root, time.monotonic() + 10, "zg")

        with patch.object(self.hook, "zg_preflight", return_value=("fixture-model", str(self.base / "models"))), \
             patch.object(self.hook, "zg_index_usable", return_value=True), \
             patch.object(self.hook, "run", side_effect=fake_native), \
             patch.object(Path, "lstat", source_stat):
            first = maintain()
            self.assertEqual(len(indexes), 1)
            self.assertEqual(maintain(), first)
            self.assertEqual(len(indexes), 1)
            exclude.write_text(original + "\ncache.py\n")
            ignored_file = maintain()
            self.assertNotEqual(ignored_file, first)
            self.assertEqual(len(indexes), 2)
            self.assertIn("nested/cache.py", json.loads(inventory.read_text())["ignored"])
            exclude.write_text(original + "\ncache.py\ngenerated/\n")
            ignored_directory = maintain()
            self.assertNotEqual(ignored_directory, ignored_file)
            self.assertEqual(len(indexes), 3)
            self.assertIn("nested/generated/", json.loads(inventory.read_text())["ignored"])
            exclude.write_text(original + "\ncache.py\ngenerated/\nnonexistent-file\n")
            self.assertEqual(maintain(), ignored_directory)
            self.assertEqual(len(indexes), 3)
            exclude.write_text(original)
            self.assertNotEqual(maintain(), ignored_directory)
            self.assertEqual(len(indexes), 4)
            self.assertNotIn("nested/cache.py", json.loads(inventory.read_text())["ignored"])
        self.assertIn("nested/tracked.py", json.loads(inventory.read_text())["tracked"])
        for invocation in indexes:
            self.assertIn(inventory, invocation)

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

    def test_module_imports_on_active_python_without_running_main(self):
        code = ("import importlib.util; spec=importlib.util.spec_from_file_location('hook'," + repr(str(HOOK)) +
                "); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5, check=False)
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

    def test_failed_queue_dispatch_retains_work_and_duplicate_children_cannot_claim_it(self):
        state_dir = self.root / ".git"
        state = {"zg": {"fingerprint": "old"}, "tgrep": {"fingerprint": "keep"}}
        self.hook.pending_zg(state_dir, state, "queued")
        self.hook.queue_zg(self.root)
        with patch.object(self.hook.subprocess, "Popen", side_effect=OSError("fixture dispatch failure")), \
             patch.object(self.hook, "note"):
            self.hook.start_queued_zg()
        self.assertEqual(self.hook.zg_queue().read_text(), str(self.root) + "\n")
        self.assertEqual(json.loads((state_dir / "codex-context-indexes.json").read_text())["zg"]["pending"], "queued")
        self.assertTrue(self.hook.claim_zg_queue(self.root, state_dir, state))
        self.assertFalse(self.hook.claim_zg_queue(self.root, state_dir, state))
        self.assertEqual(state["tgrep"], {"fingerprint": "keep"})
        with patch.object(self.hook, "ignore_indexes") as ignore:
            self.hook.ensure(self.root, ["zg"], time.monotonic() + 5, queued=True)
        ignore.assert_not_called()

    def test_launch_failure_and_queued_preflight_failure_are_terminal(self):
        state_dir = self.root / ".git"
        state_path = state_dir / "codex-context-indexes.json"
        with (
            patch.object(self.hook.subprocess, "Popen", side_effect=OSError("fixture launch failure")),
            self.assertRaises(OSError),
        ):
            self.hook.start_background_zg(self.root, state_dir, "stamp", {})
        self.assertEqual(set(json.loads(state_path.read_text())["zg"]), {"retry_after"})
        state = json.loads(state_path.read_text())
        self.hook.pending_zg(state_dir, state, "queued")
        self.hook.queue_zg(self.root)
        with patch.object(self.hook, "zg_preflight", side_effect=RuntimeError("fixture preflight failure")), \
             patch.object(self.hook, "note"), patch.object(self.hook, "start_background_zg") as start:
            self.hook.ensure(self.root, ["zg"], time.monotonic() + 5, queued=True)
        start.assert_not_called()
        self.assertEqual(self.hook.zg_queue().read_text(), "")
        self.assertEqual(set(json.loads(state_path.read_text())["zg"]), {"retry_after"})

    def test_pending_startup_does_not_spawn_twice_and_background_failure_clears_it(self):
        state_dir = self.root / ".git"
        state_path = state_dir / "codex-context-indexes.json"
        state = {"zg": {"fingerprint": "old"}}
        self.hook.pending_zg(state_dir, state, "starting")
        with patch.object(self.hook, "start_background_zg") as start, patch.object(self.hook, "note"):
            self.hook.ensure(self.root, ["zg"], time.monotonic() + 5)
        start.assert_not_called()
        self.assertEqual(json.loads(state_path.read_text())["zg"]["pending"], "starting")
        with (
            patch.object(self.hook, "maintain_zg", side_effect=RuntimeError("fixture build failure")),
            patch.object(self.hook, "start_queued_zg"),
            self.assertRaises(RuntimeError),
        ):
            self.hook.build_zg_in_background(self.root, "stamp")
        self.assertEqual(set(json.loads(state_path.read_text())["zg"]), {"retry_after"})

    def test_inherited_leases_hold_through_launcher_death_and_a_stopped_native_writer(self):
        ready = self.base / "writer-ready"
        stop = self.base / "writer-stop"
        writer = ("import os,pathlib,sys,time\n"
                  "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                  "deadline=time.monotonic()+15\n"
                  "while not pathlib.Path(sys.argv[2]).exists() and time.monotonic()<deadline: time.sleep(0.02)\n")
        driver = ("import importlib.util,pathlib,sys\n"
                  "spec=importlib.util.spec_from_file_location('fixture_hook',sys.argv[1])\n"
                  "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
                  "module.maintain_zg=lambda root,changed,deadline,pass_fds=(): module.run("
                  "[sys.executable,'-c'," + repr(writer) + "," + repr(str(ready)) + "," + repr(str(stop)) +
                  "],root,deadline,pass_fds=pass_fds)\n"
                  "module.build_zg_in_background(pathlib.Path(sys.argv[2]),'stamp',int(sys.argv[3]))\n")
        popen = subprocess.Popen
        children = []
        def launch(args, **kwargs):
            child = popen([sys.executable, "-c", driver, str(HOOK), str(self.root), args[-1]], **kwargs)
            children.append(child)
            return child
        state_dir = self.root / ".git"
        writer_pid = None
        try:
            with patch.object(self.hook.subprocess, "Popen", side_effect=launch):
                self.hook.start_background_zg(self.root, state_dir, "stamp", {})
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "native writer must start before the crash is injected")
            writer_pid = int(ready.read_text())
            self.assertTrue(self.hook.lock_held(state_dir / "zg-build.lock"))
            children[0].kill()
            children[0].wait(timeout=5)
            os.kill(writer_pid, signal.SIGSTOP)
            with self.assertRaises(self.hook.IndexPending), self.hook.idle_indexes(self.root, ["zg"]):
                self.fail("a stopped writer still owns its resources")
            self.assertEqual(json.loads((state_dir / "codex-context-indexes.json").read_text())["zg"]["pending"], "starting")
        finally:
            if writer_pid is not None:
                try:
                    os.kill(writer_pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            stop.touch()
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
            deadline = time.monotonic() + 5
            while self.hook.lock_held(state_dir / "zg-build.lock") and time.monotonic() < deadline:
                time.sleep(0.02)
        with self.hook.idle_indexes(self.root, ["zg"]):
            receipt = json.loads((state_dir / "codex-context-indexes.json").read_text())["zg"]
            self.assertNotIn("pending", receipt)
            self.assertIn("retry_after", receipt)
        self.assertFalse(self.hook.lock_held(self.hook.zg_first_build_lock()))

    def test_foreground_native_writers_retain_ownership_after_supervisor_death(self):
        for name in ("tgrep", "codegraph", "zg"):
            with self.subTest(tool=name):
                ready, stop, heartbeat = [self.base / (name + suffix) for suffix in ("-ready", "-stop", "-heartbeat")]
                executable = self.bin / name
                executable.write_text("#!" + sys.executable + "\nimport json,os,pathlib,sys,time\n"
                    "if sys.argv[1]=='status':\n"
                    " print(json.dumps({'initialized':False}) if pathlib.Path(sys.argv[0]).name=='codegraph' else 'Not ready')\n"
                    " sys.exit(1 if pathlib.Path(sys.argv[0]).name=='zg' else 0)\n"
                    "pathlib.Path(" + repr(str(ready)) + ").write_text(str(os.getpid()))\n"
                    "deadline=time.monotonic()+15\n"
                    "while not pathlib.Path(" + repr(str(stop)) + ").exists() and time.monotonic()<deadline:\n"
                    " pathlib.Path(" + repr(str(heartbeat)) + ").write_text(str(time.monotonic_ns()))\n"
                    " time.sleep(0.02)\n")
                executable.chmod(0o755)
                driver = ("import importlib.util,pathlib,sys\n"
                    "spec=importlib.util.spec_from_file_location('fixture_hook',sys.argv[1])\n"
                    "h=importlib.util.module_from_spec(spec); spec.loader.exec_module(h)\n"
                    "h.fingerprint=lambda *args:'fixture'\n"
                    "h.zg_preflight=lambda *args:('fixture-model','/fixture/models')\n"
                    "h.zg_index_usable=lambda *args:True\n"
                    "native_run=h.run\n"
                    "h.run=lambda args,*a,**kw:native_run(args[3:] if args[0]=='/usr/bin/sandbox-exec' else args,*a,**kw)\n"
                    "h.setup_indexes(pathlib.Path(sys.argv[2]),[sys.argv[3]])\n")
                child = subprocess.Popen([sys.executable, "-B", "-c", driver, str(HOOK), str(self.root), name],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                writer_pid = None
                lock_path = self.root / ".git/codex-context-indexes.lock"
                state_path = self.root / ".git/codex-context-indexes.json"
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(ready.exists(), "native writer must start before the crash")
                    writer_pid = int(ready.read_text())
                    child.kill()
                    child.wait(timeout=5)
                    before = heartbeat.read_text()
                    deadline = time.monotonic() + 2
                    while heartbeat.read_text() == before and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertNotEqual(heartbeat.read_text(), before, "writer executes after supervisor death")
                    os.kill(writer_pid, signal.SIGSTOP)
                    with self.assertRaises(self.hook.IndexPending), self.hook.idle_indexes(self.root, [name]):
                        self.fail("a live or stopped foreground writer still owns its snapshot")
                    self.assertEqual(json.loads(state_path.read_text())["foreground"]["tool"], name)
                finally:
                    stop.touch()
                    if writer_pid is not None:
                        try:
                            os.killpg(writer_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)
                    deadline = time.monotonic() + 5
                    while self.hook.lock_held(lock_path) and time.monotonic() < deadline:
                        time.sleep(0.02)
                with self.hook.idle_indexes(self.root, [name]):
                    receipt = json.loads(state_path.read_text())
                    self.assertNotIn("foreground", receipt)
                    self.assertEqual(set(receipt[name]), {"retry_after"})

    def test_foreground_reconciliation_rejects_missing_or_replaced_ownership(self):
        state_path = self.root / ".git/codex-context-indexes.json"
        for receipt in ({"tool": "tgrep"}, {"tool": "tgrep", "lease": [0, 0]}):
            state_path.write_text(json.dumps({"foreground": receipt}))
            with self.subTest(receipt=receipt), self.assertRaises((ValueError, self.hook.IndexPending)), self.hook.idle_indexes(self.root, []):
                self.fail("unproven foreground ownership must hold")
            self.assertEqual(json.loads(state_path.read_text())["foreground"], receipt)

    def test_idle_reconciles_only_the_matching_released_startup_lease(self):
        state_dir = self.root / ".git"
        with self.hook.safe_open(state_dir / "zg-build.lock") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(lock.fileno())
            self.hook.pending_zg(state_dir, {}, "starting", [info.st_dev, info.st_ino])
            with self.assertRaises(self.hook.IndexPending), self.hook.idle_indexes(self.root, ["zg"]):
                self.fail("startup must be held before a child executes")
        with self.hook.idle_indexes(self.root, ["zg"]):
            self.assertNotIn("pending", json.loads((state_dir / "codex-context-indexes.json").read_text())["zg"])
        self.hook.pending_zg(state_dir, {}, "starting", [info.st_dev, info.st_ino + 1])
        with self.assertRaises(self.hook.IndexPending), self.hook.idle_indexes(self.root, ["zg"]):
            self.fail("a replaced lease cannot establish that the old actor exited")

    def test_queue_prunes_deleted_roots_and_demand_resumes_after_machine_owner_exits(self):
        state_dir = self.root / ".git"
        self.hook.pending_zg(state_dir, {}, "queued")
        self.hook.queue_zg(self.root)
        self.hook.queue_zg(self.base / "deleted")
        with patch.object(self.hook.subprocess, "Popen"):
            self.hook.start_queued_zg()
        self.assertEqual(self.hook.zg_queue().read_text(), str(self.root) + "\n")
        with patch.object(self.hook, "zg_preflight"), patch.object(self.hook, "zg_index_usable", return_value=False), \
                patch.object(self.hook, "start_background_zg", return_value="fixture resumed") as start:
            self.hook.ensure(self.root, ["zg"], time.monotonic() + 5)
        start.assert_called_once()
        self.assertEqual(self.hook.zg_queue().read_text(), "")

    def test_unknown_partial_index_is_kept_after_background_timeout(self):
        index = self.root / ".zvec-grep"
        index.mkdir()
        sentinel = index / "unknown-owner-data"
        sentinel.write_text("preserve")
        with (
            patch.object(self.hook, "maintain_zg", side_effect=subprocess.TimeoutExpired("fake-zg", 1)),
            patch.object(self.hook, "start_queued_zg"),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            self.hook.build_zg_in_background(self.root, "fixture")
        self.assertEqual(sentinel.read_text(), "preserve")

    def test_replacement_index_is_not_owned_by_the_background_build(self):
        index = self.root / ".zvec-grep"
        sentinel = index / "replacement-owner"

        def replaced(*args, **kwargs):
            if index.exists():
                shutil.rmtree(index)
            index.mkdir()
            sentinel.write_text("preserve replacement")
            raise subprocess.TimeoutExpired("fake-zg", 1)

        with (
            patch.object(self.hook, "maintain_zg", side_effect=replaced),
            patch.object(self.hook, "start_queued_zg"),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
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
