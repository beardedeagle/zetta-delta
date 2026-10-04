#!/usr/bin/env python3
"""Run: python3 ~/.codex/hooks/test_ensure_context_indexes.py"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HOOK = Path(__file__).with_name("ensure-context-indexes.py")


class HookTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="context-hook-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        # Keep cache-held maintenance files, including the machine-wide zg lock, out of the real cache.
        # Keep the user's index-roots file out of the tests too.
        cache = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.base / "cache"),
                                        "XDG_CONFIG_HOME": str(self.base / "config")})
        cache.start()
        self.addCleanup(cache.stop)
        self.root = self.base / "repo"
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "sub").mkdir()
        (self.root / "sample.py").write_text("def first(): return 1\n")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        fake = self.bin / "tgrep"
        fake.write_text("#!" + sys.executable + "\n" + r'''import json, os, pathlib, sys, time
root = pathlib.Path(sys.argv[2])
index = root / ".tgrep"
if sys.argv[1] == "status":
    if (index / "ready").exists():
        print("Index status for " + str(root) + "\n  Hidden coverage: complete")
    else:
        print("No index found")
else:
    with open(root / ".git" / "builds", "a") as f: f.write("build\n")
    if os.environ.get("FAIL_INDEX"): sys.exit(1)
    time.sleep(float(os.environ.get("SLOW_INDEX", "0")))
    index.mkdir(exist_ok=True)
    (index / "ready").write_text("ok")
''')
        fake.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        self.payload = {"hook_event_name": "PreToolUse", "cwd": str(self.base),
                        "tool_name": "exec_command", "tool_input": {
                            "cmd": "rtk proxy tgrep -F -- first .", "workdir": str(self.root / "sub")}}

    def invoke(self, payload=None, env=None):
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload or self.payload),
                              text=True, capture_output=True, env=env or self.env, timeout=8)

    def builds(self):
        p = self.root / ".git" / "builds"
        return p.read_text().count("build") if p.exists() else 0

    def test_readiness_idempotence_edits_corruption_and_ignored_files(self):
        first = self.invoke()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.builds(), 1, first.stderr)
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.builds(), 1)
        (self.root / "sample.py").write_text("def second(): return 22\n")
        self.invoke()
        self.assertEqual(self.builds(), 2)
        (self.root / ".tgrep" / "ready").unlink()
        self.invoke()
        self.assertEqual(self.builds(), 3)
        exclude = self.root / ".git/info/exclude"
        exclude.write_text(exclude.read_text() + "\nignored.txt\n")
        self.invoke()
        before = self.builds()
        (self.root / "ignored.txt").write_text("ignored mutation")
        self.invoke()
        self.assertEqual(self.builds(), before)
        self.assertIn("/.tgrep/", exclude.read_text())

    def test_nonrepo_and_ambiguous_shell_noop(self):
        p = {**self.payload, "tool_input": {"cmd": "tgrep -F -- first .", "workdir": str(self.base)}}
        self.assertEqual(self.invoke(p).returncode, 0)
        p["tool_input"]["cmd"] = "echo 'tgrep -F -- first .'"
        p["tool_input"]["workdir"] = str(self.root)
        self.invoke(p)
        self.assertEqual(self.builds(), 0)

    def test_concurrent_hooks_build_once(self):
        env = dict(self.env, SLOW_INDEX="0.6")
        first = subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        first.stdin.write(json.dumps(self.payload))
        first.stdin.close()
        first.stdin = None
        time.sleep(0.15)
        second = self.invoke(env=env)
        first.communicate(timeout=8)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(self.builds(), 1)

    def test_failure_allows_fallback_and_cools_down(self):
        failed = self.invoke(env=dict(self.env, FAIL_INDEX="1"))
        self.assertEqual(failed.returncode, 0)
        self.assertIn("fallback", failed.stderr)
        self.invoke()
        self.assertEqual(self.builds(), 1)

    def test_zg_failures_explain_config_mismatch_without_repeating_stderr(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for detail, expected in (
            ("Code: ZVEC_GREP.ENGINE.CONFIG.INVALID\n"
             "  detail: defaults.disableMetalTensor is not supported\n",
             "zg rejects defaults.disableMetalTensor; verify the installed zg build includes local configuration support"),
            ("Code: ZVEC_GREP.ENGINE.CONFIG.INVALID\n",
             "zg rejected global configuration; verify the installed zg build against ~/.zvec-grep/config.json"),
            ("unrecognized failure\n", "zg index exited 1"),
        ):
            for changed in (False, True):
                with self.subTest(detail=detail, changed=changed), \
                     patch.object(module.Path, "home", return_value=self.base), \
                     patch.object(module, "zvec_model", return_value=("local/cached", self.base / "cache")), \
                     patch.object(module, "run", return_value=subprocess.CompletedProcess(
                         [], 1, "FAKE_PRIVATE_OUTPUT", detail + "FAKE_PRIVATE_VALUE")) as run:
                    with self.assertRaises(RuntimeError) as raised:
                        module.maintain_zg(self.root, changed, time.monotonic() + 1)
                    self.assertEqual(str(raised.exception), expected)
                    if "CONFIG.INVALID" in detail:
                        self.assertEqual(run.call_count, 1, "invalid config must not trigger another attempt")

    def test_zg_installation_drift_stops_before_model_selection(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        checker = self.base / ".local/share/zvec-grep/verify-install.py"
        checker.parent.mkdir(parents=True)
        checker.touch()
        with patch.object(module.Path, "home", return_value=self.base), \
             patch.object(module, "zvec_model") as model, \
             patch.object(module, "run", return_value=subprocess.CompletedProcess(
                 [], 1, "FAKE_PRIVATE_OUTPUT", "FAKE_PRIVATE_ERROR")) as run:
            with self.assertRaises(RuntimeError) as raised:
                module.maintain_zg(self.root, True, time.monotonic() + 1)
            self.assertEqual(str(raised.exception), "zg local patch verification failed; "
                             "run ~/.local/share/zvec-grep/verify-install.py")
            self.assertEqual(run.call_args.args[0], [sys.executable, checker])
            self.assertFalse(run.call_args.kwargs["check"])
            model.assert_not_called()
            run.return_value.returncode = 0
            model.return_value = ("local/cached", self.base / "cache")
            module.maintain_zg(self.root, False, time.monotonic() + 1)
            model.assert_called_once()

    def test_zg_uses_and_fingerprints_git_excluded_ignore_file(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rules = self.root / ".ignore"
        exclude = self.root / ".git/info/exclude"
        exclude.write_text(exclude.read_text() + "\n.ignore\n")
        before = module.fingerprint(self.root, time.monotonic() + 2)
        with patch.object(module.Path, "home", return_value=self.base), \
             patch.object(module, "zvec_model", return_value=("local/cached", self.base / "cache")), \
             patch.object(module, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            module.maintain_zg(self.root, True, time.monotonic() + 1)
            self.assertNotIn("--ignore-file", run.call_args_list[0].args[0])
            rules.write_text(".env*\n")
            run.reset_mock()
            module.maintain_zg(self.root, True, time.monotonic() + 1)
            self.assertEqual(run.call_args_list[0].args[0][-2:], ["--ignore-file", rules])
        added = module.fingerprint(self.root, time.monotonic() + 2)
        self.assertNotEqual(before, added, "Git-excluded .ignore must affect freshness")
        rules.write_text(".env*\nprivate/\n")
        self.assertNotEqual(added, module.fingerprint(self.root, time.monotonic() + 2))
        rules.unlink()
        self.assertEqual(before, module.fingerprint(self.root, time.monotonic() + 2))

    def test_query_waits_for_inflight_initialization(self):
        env = dict(self.env, SLOW_INDEX="0.6")
        first = subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            first.stdin.write(json.dumps(self.payload))
            first.stdin.close()
            first.stdin = None
            deadline = time.monotonic() + 4
            while not self.builds() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.builds(), 1, "first initializer did not start")
            second = self.invoke(env=env)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertTrue((self.root / ".tgrep/ready").exists(),
                            "query hook returned before the running initializer was ready")
            self.assertEqual(self.builds(), 1, "waiting query rebuilt an unchanged index")
        finally:
            first.communicate(timeout=8)

    def test_complete_json_does_not_wait_for_eof(self):
        p = subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
        try:
            p.stdin.write(json.dumps(self.payload))
            p.stdin.flush()
            self.assertEqual(p.wait(timeout=4), 0)
            self.assertEqual(self.builds(), 1)
        finally:
            if p.poll() is None: p.kill()
            p.communicate()

    def test_local_embedding_policy_and_timeout_cleanup(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        index = self.root / ".zvec-grep"
        index.mkdir()
        (index / "manifest.json").write_text(json.dumps({"indexPolicy": "enabled", "rootPaths": [],
                    "embedding": {"provider": "qwen", "model": "text-embedding-v4"}}))
        with self.assertRaisesRegex(RuntimeError, "local embedding"):
            module.zvec_model(self.root, time.monotonic() + 2)
        childpid = self.root / ".git/child-pid"
        marker = self.root / ".git/escaped-deadline"
        script = ("import subprocess,sys,time; "
                  "p=subprocess.Popen([sys.executable,'-c'," + repr("import time; from pathlib import Path; time.sleep(0.6); Path(" + repr(str(marker)) + ").write_text('escaped')") + "]); "
                  "open(" + repr(str(childpid)) + ",'w').write(str(p.pid)); time.sleep(10)")
        with self.assertRaises(subprocess.TimeoutExpired):
            module.run([sys.executable, "-c", script], self.root, time.monotonic() + 0.2)
        time.sleep(0.6)
        self.assertTrue(childpid.exists())
        self.assertFalse(marker.exists(), "indexer descendant survived deadline")
        with patch.object(module, "run", return_value=subprocess.CompletedProcess([], 0,
                "Server status for repo\n  Indexing:   complete\n  Hidden coverage: complete", "")) as mocked:
            self.assertIs(module.maintain_tgrep(self.root, True, time.monotonic() + 1), False)
            self.assertEqual(mocked.call_count, 1, "do not rebuild under a running tgrep server")
        with patch.object(module, "maintain_tgrep", return_value=False) as maintain:
            module.ensure(self.root, ["tgrep"], time.monotonic() + 2)
            module.ensure(self.root, ["tgrep"], time.monotonic() + 2)
            self.assertTrue(all(call.args[1] for call in maintain.call_args_list))
            self.assertEqual(len(maintain.call_args_list), 2)

    def test_repo_git_helpers_and_inherited_git_environment_are_not_executed(self):
        marker = self.base / "fsmonitor-ran"
        executable = self.base / "fsmonitor"
        executable.write_text("#!/bin/sh\ntouch " + str(marker) + "\n")
        executable.chmod(0o755)
        subprocess.run(["git", "-C", str(self.root), "add", "sample.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "core.fsmonitor", str(executable)], check=True)
        result = self.invoke(env=dict(self.env, GIT_DIR=str(self.base / "not-a-repo")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.builds(), 1, result.stderr)
        self.assertFalse(marker.exists())

    def test_symlinked_maintenance_files_do_not_clobber_targets(self):
        target = self.base / "preserve"
        target.write_text("unchanged")
        for relative in ("codex-context-indexes.lock", "codex-context-indexes.json", "info/exclude"):
            with self.subTest(path=relative):
                link = self.root / ".git" / relative
                if link.exists(): link.unlink()
                link.symlink_to(target)
                result = self.invoke()
                self.assertEqual(result.returncode, 0)
                self.assertEqual(target.read_text(), "unchanged")
                self.assertEqual(self.builds(), 0)
                link.unlink()

    def test_git_config_cannot_redirect_selected_repository(self):
        sibling = self.base / "sibling"
        subprocess.run(["git", "init", "-q", str(sibling)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "core.worktree", str(sibling)], check=True)
        result = self.invoke()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.builds(), 0)
        self.assertFalse((sibling / ".tgrep").exists())
        # A redirect to an ancestor also passes a naive containment-only check.
        subprocess.run(["git", "-C", str(self.root), "config", "core.worktree", str(self.base)], check=True)
        self.invoke()
        self.assertFalse((self.base / ".tgrep").exists())

    def test_corrupt_state_fifo_does_not_hang(self):
        state = self.root / ".git/codex-context-indexes.json"
        os.mkfifo(state)
        result = self.invoke()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.builds(), 1, result.stderr)
        self.assertTrue(state.is_file())

    def test_whole_hook_deadline_fails_open(self):
        script = ("import importlib.util; s=importlib.util.spec_from_file_location('hook'," + repr(str(HOOK)) + "); "
                  "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.BUDGET_SECONDS=1; m.main()")
        start = time.monotonic()
        result = subprocess.run([sys.executable, "-c", script], input=json.dumps(self.payload), text=True,
                                capture_output=True, env=dict(self.env, SLOW_INDEX="10"), timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertIn("hook budget exhausted", result.stderr)
        self.assertLess(time.monotonic() - start, 2)
        self.assertFalse((self.root / ".tgrep/ready").exists())

    def test_selected_target_and_worktree_root(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        selected = module.selection({"hook_event_name": "PreToolUse", "cwd": str(self.base),
                    "tool_name": "mcp__codegraph__codegraph_explore", "tool_input": {"projectPath": str(self.root / "sub")}})
        self.assertEqual(selected, (self.root, ["codegraph"]))
        self.assertIsNone(module.selection({"hook_event_name": "SessionStart", "cwd": str(self.base)}))
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "--allow-empty", "-qm", "fixture"], check=True)
        worktree = self.base / "worktree"
        subprocess.run(["git", "-C", str(self.root), "worktree", "add", "-qb", "fixture", str(worktree)], check=True)
        (worktree / "nested").mkdir()
        selected = module.selection({"hook_event_name": "SessionStart", "cwd": str(worktree / "nested")})
        self.assertEqual(selected[0], worktree)

    def test_allowlisted_nongit_roots(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for relative in ("projects/app/sub", "projects/broken", "scripts/tool", "elsewhere"):
            (self.base / relative).mkdir(parents=True)
        (self.base / "projects/broken/.git").mkdir()  # a Git marker git_root rejected stays rejected

        def pick(relative):
            return module.selection({"hook_event_name": "SessionStart", "cwd": str(self.base / relative)})
        with patch.dict(os.environ, {"HOME": str(self.base)}):
            # No index-roots file: Git repositories only, even under ~/projects and ~/scripts.
            for relative in ("projects/app/sub", "scripts/tool"):
                self.assertIsNone(pick(relative), relative)
            config = self.base / "config/zetta-delta/index-roots"
            config.parent.mkdir(parents=True)
            config.write_text(f"# opted-in folders\n\n{self.base / 'scripts'}\n~/projects/*\nrelative/ignored\n")
            self.assertEqual(pick("projects/app/sub"), (self.base / "projects/app", list(module.TOOLS)))
            self.assertEqual(pick("scripts/tool")[0], self.base / "scripts")
            for refused in ("projects", "projects/broken", "elsewhere"):
                self.assertIsNone(pick(refused), refused)

    def test_nongit_root_keeps_state_outside_and_honors_gitignore(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = self.base / "projects/app"
        (app / "results").mkdir(parents=True)
        (app / "main.py").write_text("x = 1\n")
        (app / ".gitignore").write_text("results/\n")
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.base / "cache")}):
            self.assertTrue(module.maintenance_dir(app, time.monotonic() + 2).is_relative_to(self.base / "cache"))
        before = module.fingerprint(app, time.monotonic() + 5)
        (app / "results/run.txt").write_text("ignored output")
        self.assertEqual(before, module.fingerprint(app, time.monotonic() + 5))
        (app / "main.py").write_text("x = 2\n")
        self.assertNotEqual(before, module.fingerprint(app, time.monotonic() + 5))
        with patch.object(module, "run", return_value=subprocess.CompletedProcess([], 0, "No index", "")) as run:
            with self.assertRaises(RuntimeError):  # the fake status never reports coverage
                module.maintain_tgrep(app, True, time.monotonic() + 1)
            self.assertIn("--no-require-git", run.call_args_list[1].args[0])
        with patch.object(module, "zg_preflight", return_value=("local/cached", self.base / "cache")), \
             patch.object(module, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            module.maintain_zg(app, True, time.monotonic() + 1)
            self.assertEqual(run.call_args_list[0].args[0][-2:], ["--ignore-file", app / ".gitignore"])

    def test_first_zg_build_runs_detached_then_refreshes_inline(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        state_dir = self.root / ".git"
        with patch.object(module.subprocess, "Popen") as popen:
            module.start_background_zg(self.root, state_dir, "stamp")
        self.assertIn("--zg-build", popen.call_args.args[0])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        with patch.object(module.shutil, "which", return_value="/usr/bin/true"), \
             patch.object(module, "zg_preflight") as preflight, \
             patch.object(module, "start_background_zg", return_value="started") as start, \
             patch.object(module, "maintain_zg") as maintain:
            module.ensure(self.root, ["zg"], time.monotonic() + 5)
            preflight.assert_called_once()
            start.assert_called_once()
            maintain.assert_not_called()
            with module.safe_open(state_dir / "zg-build.lock") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                module.ensure(self.root, ["zg"], time.monotonic() + 5)
                start.assert_called_once()  # a running build is not started twice
            maintain.side_effect = lambda *args: (self.root / ".zvec-grep").mkdir(exist_ok=True)
            module.build_zg_in_background(self.root, "stamp")
            maintain.assert_called_once()
            module.ensure(self.root, ["zg"], time.monotonic() + 5)
            start.assert_called_once()
            self.assertEqual(maintain.call_count, 2, "a built index refreshes inline")

    def test_first_zg_builds_run_one_at_a_time_on_this_machine(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        state = self.root / ".git/codex-context-indexes.json"
        with patch.object(module.shutil, "which", return_value="/usr/bin/true"), \
             patch.object(module, "zg_preflight"), \
             patch.object(module, "start_background_zg", return_value="started") as start, \
             patch.object(module, "maintain_zg") as maintain, \
             patch.object(module, "note") as note:
            with module.safe_open(module.zg_first_build_lock()) as other_build:
                fcntl.flock(other_build, fcntl.LOCK_EX | fcntl.LOCK_NB)
                module.ensure(self.root, ["zg"], time.monotonic() + 5)
                start.assert_not_called()
                self.assertIn("another one running on this machine", note.call_args.args[0])
                with self.assertRaisesRegex(RuntimeError, "another zg first build"):
                    module.build_zg_in_background(self.root, "stamp")  # a hook that raced past the check
                maintain.assert_not_called()
            self.assertNotIn("zg", json.loads(state.read_text()), "a deferred build sets no backoff")
            module.ensure(self.root, ["zg"], time.monotonic() + 5)
            start.assert_called_once()

    def test_zg_runs_with_two_embedding_contexts_unless_set(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        log = self.base / "zg.log"
        fake = self.bin / "zg"
        fake.write_text("#!" + sys.executable + "\nimport os, sys\nwith open(" + repr(str(log)) + ", 'a') as f:\n"
                        "    f.write(sys.argv[1] + ' ' + os.environ.get('ZVEC_GREP_LLAMA_CONTEXT_PARALLELISM', '-') + '\\n')\n")
        fake.chmod(0o755)
        key = "ZVEC_GREP_LLAMA_CONTEXT_PARALLELISM"
        with patch.dict(os.environ, {"PATH": self.env["PATH"]}), \
             patch.object(module, "zg_preflight", return_value=("local/cached", self.base / "cache")):
            os.environ.pop(key, None)
            module.maintain_zg(self.root, True, time.monotonic() + 5)
            self.assertEqual(log.read_text(), "index 2\nstatus 2\n")
            log.unlink()
            with patch.dict(os.environ, {key: "4"}):
                module.maintain_zg(self.root, True, time.monotonic() + 5)
            self.assertEqual(log.read_text(), "index 4\nstatus 4\n", "a value the user set wins")

    def test_codegraph_full_build_over_the_limit_is_never_started(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real, calls = module.run, []

        def fake(args, *rest, **kwargs):
            if args[0] != "codegraph":
                return real(args, *rest, **kwargs)
            calls.append(args[1])
            return subprocess.CompletedProcess(args, 0, '{"initialized": false}', "")
        with patch.object(module, "run", side_effect=fake):
            with patch.dict(module.FIRST_BUILD_MAX_FILES, {"codegraph": 0}):
                with self.assertRaisesRegex(RuntimeError, "full build skipped: 1 files"):
                    module.maintain_codegraph(self.root, True, time.monotonic() + 5)
            self.assertNotIn("init", calls)
            with self.assertRaisesRegex(RuntimeError, "not ready"):  # the fake never reports success
                module.maintain_codegraph(self.root, True, time.monotonic() + 5)
            self.assertIn("init", calls)

    def test_background_timeout_drops_only_its_own_partial_index_and_stops(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        index = self.root / ".zvec-grep"
        index.mkdir()
        with patch.object(module, "maintain_zg", side_effect=subprocess.TimeoutExpired("zg", 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                module.build_zg_in_background(self.root, "stamp")
        self.assertTrue(index.exists(), "an index the build did not create is kept")
        index.rmdir()

        def killed(*args):
            index.mkdir()
            raise subprocess.TimeoutExpired("zg", 1)
        with patch.object(module, "maintain_zg", side_effect=killed):
            with self.assertRaises(subprocess.TimeoutExpired):
                module.build_zg_in_background(self.root, "stamp")
        self.assertFalse(index.exists())
        with patch.object(module.shutil, "which", return_value="/usr/bin/true"), \
             patch.object(module, "zg_preflight"), \
             patch.object(module, "start_background_zg") as start, \
             patch.object(module, "note") as note:
            module.ensure(self.root, ["zg"], time.monotonic() + 5)
        start.assert_not_called()
        self.assertIn("zg skipped", note.call_args.args[0])

    def test_nested_repositories_count_for_freshness_not_for_zg_size(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        nested = self.root / "vendor/lib"
        nested.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        (nested / "code.py").write_text("y = 1\n")
        (self.root / ".hidden").mkdir()
        (self.root / ".hidden/h.py").write_text("z = 1\n")
        files, sizes = module.scope(self.root, time.monotonic() + 5)
        self.assertIn("vendor/lib/code.py", files)
        # zg reads sample.py only: it skips hidden paths and nested repositories; codegraph reads all three.
        self.assertEqual(sizes, {"codegraph": 3, "zg": 1})
        before = module.fingerprint(self.root, time.monotonic() + 5)
        (nested / "code.py").write_text("y = 22\n")
        self.assertNotEqual(before, module.fingerprint(self.root, time.monotonic() + 5))
        with patch.dict(module.FIRST_BUILD_MAX_FILES, {"zg": 0}), \
             patch.object(module.shutil, "which", return_value="/usr/bin/true"), \
             patch.object(module, "zg_preflight"), \
             patch.object(module, "start_background_zg") as start, \
             patch.object(module, "note") as note:
            module.ensure(self.root, ["zg"], time.monotonic() + 5)
        start.assert_not_called()
        self.assertIn("(1 files)", note.call_args.args[0])

    def test_delta_checkouts_get_a_git_excluded_sembleignore(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        patcher = patch.object(module, "semble_clear_orphans")  # never touch the real Semble cache
        patcher.start()
        self.addCleanup(patcher.stop)
        ignore, exclude = self.root / ".sembleignore", self.root / ".git/info/exclude"
        status = lambda: subprocess.run(["git", "-C", self.root, "status", "--porcelain", "-uall"],
                                        capture_output=True, text=True, check=True).stdout
        module.ignore_indexes(self.root, time.monotonic() + 5)
        self.assertFalse(ignore.exists())  # no .delta/, nothing to do
        self.assertNotIn("/.sembleignore", exclude.read_text().splitlines())
        (self.root / ".delta/worktrees").mkdir(parents=True)
        for _ in range(2):  # idempotent
            module.ignore_indexes(self.root, time.monotonic() + 5)
            self.assertEqual(ignore.read_text(), ".delta/\n")
            self.assertEqual(exclude.read_text().splitlines().count("/.sembleignore"), 1)
        self.assertNotIn(".sembleignore", status())
        ignore.write_text("build/")  # an existing untracked file keeps its lines
        module.ignore_indexes(self.root, time.monotonic() + 5)
        self.assertEqual(ignore.read_text(), "build/\n.delta/\n")
        ignore.unlink()
        ignore.symlink_to(self.base / "elsewhere")  # refused, without stopping maintenance
        with patch.object(module, "note") as note:
            module.ignore_indexes(self.root, time.monotonic() + 5)
        self.assertIn("semble: ", note.call_args.args[0])
        self.assertFalse((self.base / "elsewhere").exists())
        ignore.unlink()
        ignore.write_text("dist/\n")
        subprocess.run(["git", "-C", self.root, "add", "-f", ".sembleignore"], check=True)
        with patch.object(module, "note") as note:
            module.ignore_indexes(self.root, time.monotonic() + 5)
        self.assertEqual(ignore.read_text(), "dist/\n")  # committed files are never edited
        self.assertIn("committed .sembleignore lacks .delta/", note.call_args.args[0])

    def test_roots_with_delta_checkouts_clear_semble_orphans(self):
        spec = importlib.util.spec_from_file_location("index_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls, fake = self.base / "semble-calls", self.bin / "semble"
        fake.write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\n')
        fake.chmod(0o755)
        with patch.dict(os.environ, PATH=self.env["PATH"]):
            module.ignore_indexes(self.root, time.monotonic() + 5)
            self.assertFalse(calls.exists())  # no .delta/, no cleanup
            (self.root / ".delta").mkdir()
            module.ignore_indexes(self.root, time.monotonic() + 5)
            self.assertEqual(calls.read_text(), "clear orphans\n")
            fake.write_text("#!/bin/sh\nexit 3\n")
            with patch.object(module, "note") as note:
                module.ignore_indexes(self.root, time.monotonic() + 5)  # a failure is noted, not raised
            self.assertEqual(note.call_args.args[0], "semble: semble exited 3")


if __name__ == "__main__":
    unittest.main()
