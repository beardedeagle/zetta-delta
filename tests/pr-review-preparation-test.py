#!/usr/bin/env python3
"""Offline Git fixtures for preparation, source preservation and owned cleanup."""

import copy
import fcntl
import importlib.machinery
import io
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/pr-review/scripts"
sys.path.insert(0, str(SCRIPTS))
import preparation
import workflow

REGISTRY = {"version": 1, "thread_limit": 15, "metered_max_spawns": 2,
            "providers": [{"provider_id": "fixture", "lane": "fixture", "billing": "flat", "limit": 2}],
            "models": [{"provider_id": "fixture", "model_id": "model", "family": "family"},
                       {"provider_id": "fixture", "model_id": "vet", "family": "vet-family"},
                       {"provider_id": "fixture", "model_id": "orchestrator", "family": "orchestrator-family"}]}
MODEL = {"provider_id": "fixture", "model_id": "model"}


class GitFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="review-preparation-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.parent = self.base / "checkout"
        self.parent.mkdir()
        self.directory = self.base / "run"
        self.directory.mkdir(mode=0o700)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@localhost")
        (self.parent / "original.txt").write_text("original\n" * 10)
        (self.parent / "ignored.txt").write_text("tracked\n")
        (self.parent / ".gitignore").write_text("*.ignored\n")
        self.commit("base")
        self.common = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "feature")
        (self.parent / "feature.txt").write_text("feature\n")
        self.commit("feature")
        self.head = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "main")
        (self.parent / "base-only.txt").write_text("advanced base\n")
        self.commit("advance base")
        self.left = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.left)
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.git("checkout", "-q", "feature")
        self.local: dict = {"repository": str(self.parent), "intake": {"classification": "local"},
                      "comparison": {"mode": "range-three-dot", "left": "main", "right": "feature", "untracked": []}}

    def git(self, *args, root=None, input=None):
        result = subprocess.run(["rtk", "proxy", "git", "-C", str(root or self.parent), *args],
                                input=input, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def prepare(self, mode="range-three-dot", **request):
        target = copy.deepcopy(self.local)
        target["comparison"].update(mode=mode, **request)
        return preparation.prepare(self.directory, "run-fixture", "local", target, self.parent)

    def resource(self, frozen):
        return self.directory / frozen["preparation"]["resource"]

    def native_clone(self, identifier: str = "native-one", project: Path | None = None) -> tuple[Path, Path, str]:
        """Create Delta's split checkout/store layout in the local fixture.

        Args:
            identifier (str): Disposable native checkout identifier.
            project (Path | None): Owning project, or the source fixture by default.

        Returns:
            tuple[Path, Path, str]: Checkout, Git store and parent pin reference.
        """
        project = project or self.parent
        child = project / ".delta/worktrees" / identifier / "project"
        store = project / ".delta/clones" / identifier / "project.git"
        store.parent.mkdir(parents=True)
        self.git("clone", "-q", "--bare", str(project), str(store))
        child.mkdir(parents=True)
        (child / ".git").write_text("gitdir: " + str(store) + "\n")
        self.git("config", "core.bare", "false", root=store)
        self.git("config", "core.worktree", str(child), root=store)
        self.git("checkout", "-q", "HEAD", root=child)
        pin = f"refs/delta/{identifier}/project/{self.head}"
        self.git("update-ref", pin, self.head, root=project)
        return child, store, pin

    def indexes(self, available=preparation.INDEX_TOOLS):
        helper_path = self.base / "index-helper.py"
        helper_path.write_text("# trusted fixture maintainer\n")
        queue = self.base / "zg.queue"
        helper, _ = preparation.index_helper(bundled=True)
        def ensure(root, names, deadline):
            for name in names:
                cache = root / {"tgrep": ".tgrep", "codegraph": ".codegraph", "zg": ".zvec-grep"}[name]
                cache.mkdir()
                (cache / "index").write_text("fixture cache\n")
            (root / ".git/info/exclude").write_text(".tgrep/\n.codegraph/\n.zvec-grep/\n")
            (root / ".git/codex-context-indexes.json").write_text(json.dumps({name: {"fingerprint": name + "-stamp"} for name in names}))
        # Preparation sees only the public API; native details stay in the real maintainer.
        api = SimpleNamespace(**{name: getattr(helper, name) for name in
                                 ("plan_indexes", "setup_indexes", "idle_indexes", "unavailable_indexes", "sealed_snapshot", "IndexPending")})
        for mock in (patch.object(helper, "ensure", side_effect=ensure),
                     patch.object(helper, "fingerprint", side_effect=lambda root, deadline, name: name + "-stamp"),
                     patch.object(helper, "maintenance_dir", side_effect=lambda root, deadline: root / ".git"),
                     patch.object(helper, "zg_queue", return_value=queue),
                     patch.object(preparation, "index_helper", return_value=(api, helper_path)),
                     patch.object(helper.shutil, "which", side_effect=lambda name: "/fixture/" + name if name in available else None)):
            mock.start()
            self.addCleanup(mock.stop)
        return helper


class PreparationTests(GitFixture):
    def test_setup_and_finalize_keep_registered_native_administration_and_copy_guards(self):
        first = self.prepare()
        child, _, _ = self.native_clone()
        native = preparation.capture_runtime_copy(self.directory, "run-fixture", "local", first, child, ())
        copies = (native,)
        staged = preparation.prepare(self.directory, "run-fixture", "later", self.local, self.parent, copies)
        project = self.base / "extra-project"
        self.git("clone", "-q", str(self.parent), str(project))
        record = preparation.capture_baseline(self.directory, "extra-project", project, copies)
        baselines = {"extra-project": record}
        self.indexes(("tgrep",))
        request = preparation.setup_request(self.directory, "run-fixture", "later", staged, ["tgrep"],
                                             baselines=baselines, native_copies=copies)
        self.assertEqual(request["tools"], ["tgrep"])
        final = preparation.finalize(self.directory, "run-fixture", "later", staged, None, [], None,
                                     baselines=baselines, native_copies=copies)
        self.assertEqual(preparation.check(self.directory, "run-fixture", "later", final, baselines, native_copies=copies)["status"], "unchanged")
        (project / "feature.txt").write_text("unowned copy mutation\n")
        with self.assertRaises(preparation.NeedsDecision):
            preparation.setup_request(self.directory, "run-fixture", "later", staged, ["tgrep"],
                                      baselines=baselines, native_copies=copies)
        (project / "feature.txt").write_text("feature\n")
        (self.parent / "feature.txt").write_text("user mutation\n")
        with self.assertRaises(preparation.NeedsDecision):
            preparation.finalize(self.directory, "run-fixture", "later", staged, None, [], None,
                                 baselines=baselines, native_copies=copies)

    def test_external_native_copy_requires_registered_store_and_worktree_binding(self):
        frozen = self.prepare()
        unrelated = self.base / "unrelated"
        self.git("clone", "-q", str(self.parent), str(unrelated))
        fake = self.base / "fake-worktree"
        fake.mkdir()
        (fake / ".git").write_text("gitdir: " + str(self.parent / ".git") + "\n")
        for root in (unrelated, fake):
            with self.subTest(root=root), self.assertRaises((ValueError, preparation.NeedsDecision)):
                preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, root, (),
                                                 parent_root=str(self.parent))
        project = self.base / "native-project"
        self.git("clone", "-q", str(self.parent), str(project))
        self.git("config", "user.name", "Fixture", root=project)
        self.git("config", "user.email", "fixture@localhost", root=project)
        (project / "native-only.txt").write_text("different native project commit\n")
        self.git("add", "native-only.txt", root=project)
        self.git("commit", "-qm", "native project", root=project)
        record = preparation.capture_baseline(self.directory, "native-project", project, ())
        parents = {str(self.parent): json.loads((self.resource(frozen) / "parent-baseline.json").read_text()),
                   str(project): preparation.read_baseline(self.directory, record)}
        external = self.base / "valid-linked"
        self.git("worktree", "add", "-q", "--detach", str(external), "HEAD", root=project)
        proof = preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, external, (), parents,
                                                parent_root=str(project))
        self.assertEqual(proof["common_dir"], str(project / ".git"))
        self.assertNotEqual(proof["head"], frozen["head_sha"])
        self.assertEqual(preparation.check(self.directory, "run-fixture", "local", frozen, {"native-project": record},
                                          native_copies=(proof,))["status"], "unchanged")

    def test_nested_git_metadata_is_bound_for_gitlinks_and_untracked_checkouts(self):
        module = self.parent / "module"
        self.git("clone", "-q", str(self.parent), str(module))
        for tracked in (False, True):
            with self.subTest(tracked=tracked):
                if tracked:
                    self.git("update-index", "--add", "--cacheinfo", "160000", self.head, "module")
                before = preparation.baseline(self.parent)
                self.git("config", "review.fixture", "changed", root=module)
                self.assertNotEqual(preparation.baseline(self.parent), before)
                self.git("config", "--unset", "review.fixture", root=module)
                self.assertEqual(preparation.baseline(self.parent), before)
                self.git("update-ref", "refs/heads/new-ref", self.head, root=module)
                self.assertNotEqual(preparation.baseline(self.parent), before)
                self.git("update-ref", "-d", "refs/heads/new-ref", root=module)
                self.assertEqual(preparation.baseline(self.parent), before)

    def test_nested_redirected_git_store_is_rejected_before_reading_it(self):
        external = self.base / "external-store"
        self.git("clone", "-q", str(self.parent), str(external))
        module = self.parent / "module"
        module.mkdir()
        (module / ".git").write_text("gitdir: " + str(external / ".git") + "\n")
        real_git = preparation.git
        def guarded(root, *args, **kwargs):
            self.assertNotEqual(root, module, "redirected nested metadata must not be read")
            return real_git(root, *args, **kwargs)
        with patch.object(preparation, "git", side_effect=guarded), self.assertRaises((ValueError, preparation.NeedsDecision)):
            preparation.baseline(self.parent)

    def test_absorbed_module_metadata_and_control_redirects_preserve_ownership(self):
        module = self.parent / "module"
        store = self.parent / ".git/modules/module"
        store.parent.mkdir()
        self.git("clone", "-q", "--separate-git-dir", str(store), str(self.parent), str(module))
        before = preparation.baseline(self.parent)
        config = (store / "config").read_bytes()
        self.git("config", "review.fixture", "changed", root=module)
        self.assertNotEqual(preparation.baseline(self.parent), before)
        (store / "config").write_bytes(config)
        self.assertEqual(preparation.baseline(self.parent), before)
        external = self.base / "external-store"
        self.git("clone", "-q", str(self.parent), str(external))
        real_git = preparation.git
        def guarded(root, *args, **kwargs):
            self.assertNotEqual(root, module, "unowned/special nested metadata must not be read")
            return real_git(root, *args, **kwargs)
        (store / "commondir").write_text(str(external / ".git") + "\n")
        with patch.object(preparation, "git", side_effect=guarded), self.assertRaises(ValueError):
            preparation.baseline(self.parent)
        (store / "commondir").unlink()
        (store / "config").unlink()
        os.mkfifo(store / "config")
        with patch.object(preparation, "git", side_effect=guarded), self.assertRaises(preparation.NeedsDecision):
            preparation.baseline(self.parent)

    def test_known_codegraph_socket_is_fingerprinted_without_cache_exemptions(self):
        temporary = tempfile.TemporaryDirectory(prefix="review-socket-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        short = Path(temporary.name).resolve() / "checkout"
        self.parent.rename(short)
        self.parent = short
        self.local["repository"] = str(short)
        cache = short / ".codegraph"
        cache.mkdir()
        path = cache / "socket"
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(path))
        path.chmod(0o600)
        module = short / "module"
        self.git("clone", "-q", str(short), str(module))
        (module / ".codegraph").mkdir()
        nested_server = socket.socket(socket.AF_UNIX)
        self.addCleanup(nested_server.close)
        nested_path = module / ".codegraph/socket"
        nested_server.bind(str(nested_path))
        nested_path.chmod(0o600)
        before = preparation.baseline(short)
        self.assertEqual(before["files"][".codegraph/socket"][0], "socket")
        self.assertEqual(before["files"]["module/.codegraph/socket"][0], "socket")
        self.assertEqual(preparation.baseline(short), before)
        frozen = self.prepare()
        self.assertEqual(preparation.check(self.directory, "run-fixture", "local", frozen, {})["status"], "unchanged")
        (cache / "unexpected-index-write").write_text("unexpected writer\n")
        self.assertIn("parent", preparation.check(self.directory, "run-fixture", "local", frozen, {})["components"])
        (cache / "unexpected-index-write").unlink()
        with patch.object(preparation.os, "getuid", return_value=os.getuid() + 1), self.assertRaises(preparation.NeedsDecision):
            preparation.baseline(short)
        path.chmod(0o666)
        with self.assertRaises(preparation.NeedsDecision):
            preparation.baseline(short)
        path.chmod(0o600)
        server.close()
        path.unlink()
        replacement = socket.socket(socket.AF_UNIX)
        self.addCleanup(replacement.close)
        replacement.bind(str(path))
        path.chmod(0o600)
        self.assertNotEqual(preparation.baseline(short), before)
        self.assertIn("parent", preparation.check(self.directory, "run-fixture", "local", frozen, {})["components"])
        path.rename(cache / "unknown-socket")
        with self.assertRaises(preparation.NeedsDecision):
            preparation.baseline(short)
        (cache / "unknown-socket").unlink()
        os.mkfifo(path)
        with self.assertRaises(preparation.NeedsDecision):
            preparation.baseline(short)

    def test_native_sibling_clone_uses_project_ownership_without_rebinding_parent(self):
        frozen = self.prepare()
        project = self.base / "native-project"
        self.git("clone", "-q", str(self.parent), str(project))
        parent, _, _ = self.native_clone("native-parent", project)
        records = {name: preparation.capture_baseline(self.directory, name, root, ())
                   for name, root in (("native-project", project), ("native-parent", parent))}
        parents = {frozen["parent_root"]: json.loads((self.resource(frozen) / "parent-baseline.json").read_text())}
        parents.update({record["root"]: preparation.read_baseline(self.directory, record) for record in records.values()})
        child, store, pin = self.native_clone("native-child", project)
        proof = preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, child, (),
                                                parents, str(parent))
        self.assertEqual(proof["parent_root"], str(parent))
        self.assertEqual(proof["project_root"], str(project))
        self.assertEqual(proof["clone_store"]["ref"], pin)
        check = lambda: preparation.check(self.directory, "run-fixture", "local", frozen, records, native_copies=(proof,))
        self.assertEqual(check(), {"status": "unchanged", "components": []})
        for root, name in ((store, "unexpected.txt"), (parent, "feature.txt"), (project, ".delta/user.txt"),
                           (self.parent, "feature.txt")):
            with self.subTest(root=root):
                path = root / name
                original = path.read_bytes() if path.exists() else None
                path.write_text("unexpected write\n")
                self.assertNotEqual(check()["status"], "unchanged")
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)
                self.assertEqual(check()["status"], "unchanged")
        self.git("update-ref", pin, self.left, root=project)
        self.assertNotEqual(check()["status"], "unchanged")
        self.git("update-ref", pin, self.head, root=project)
        self.assertEqual(check()["status"], "unchanged")

    def test_native_clone_bookkeeping_protects_store_pin_and_unrelated_content(self):
        frozen = self.prepare()
        child, store, pin = self.native_clone()
        proof = preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, child, ())
        copies = (proof,)
        check = lambda: preparation.check(self.directory, "run-fixture", "local", frozen, {}, native_copies=copies)
        self.assertEqual(check(), {"status": "unchanged", "components": []})
        for mutation in ("store-content", "store-mode", "moved-pin", "missing-pin", "unrelated-ref", "unrelated-file"):
            with self.subTest(mutation=mutation):
                before = store.stat().st_mode & 0o7777
                if mutation == "store-content":
                    (store / "unexpected.txt").write_text("native store changed\n")
                elif mutation == "store-mode":
                    store.chmod(before ^ 0o020)
                elif mutation == "moved-pin":
                    self.git("update-ref", pin, self.left)
                elif mutation == "missing-pin":
                    self.git("update-ref", "-d", pin)
                elif mutation == "unrelated-ref":
                    self.git("update-ref", "refs/heads/unrelated", self.head)
                else:
                    (self.parent / ".delta/unrelated.txt").write_text("user content\n")
                self.assertNotEqual(check()["status"], "unchanged")
                if mutation == "store-content":
                    (store / "unexpected.txt").unlink()
                elif mutation == "store-mode":
                    store.chmod(before)
                elif mutation in ("moved-pin", "missing-pin"):
                    self.git("update-ref", pin, self.head)
                elif mutation == "unrelated-ref":
                    self.git("update-ref", "-d", "refs/heads/unrelated")
                else:
                    (self.parent / ".delta/unrelated.txt").unlink()
                self.assertEqual(check()["status"], "unchanged")
        store.rename(store.with_name("redirected.git"))
        store.symlink_to(store.with_name("redirected.git"), target_is_directory=True)
        self.assertNotEqual(check()["status"], "unchanged")

    def test_native_clone_cannot_exempt_a_preexisting_git_store(self):
        child, store, _ = self.native_clone()
        shutil.rmtree(child)
        frozen = self.prepare()
        child.mkdir(parents=True)
        (child / ".git").write_text("gitdir: " + str(store) + "\n")
        self.git("checkout", "-q", "--force", "HEAD", root=child)
        with self.assertRaisesRegex(ValueError, "pre-existing user content"):
            preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, child, ())

    def test_static_search_and_nonwriting_git_preserve_sealed_source(self):
        frozen = self.prepare()
        source = Path(frozen["source_root"])
        os.utime(source / "feature.txt", (1, 1))
        result = subprocess.run(["rtk", "proxy", "tgrep", "--no-index", "-F", "--", "feature", str(source)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("feature.txt", result.stdout)
        self.assertEqual(preparation.git(source, "status", "--porcelain"), b"")
        self.assertEqual(preparation.check(self.directory, "run-fixture", "local", frozen, {})["status"], "unchanged")
        cache = source / ".tgrep"
        cache.mkdir()
        (cache / "index.bin").write_bytes(b"late startup cache")
        self.assertIn("source", preparation.check(self.directory, "run-fixture", "local", frozen, {})["components"])

    def test_native_bookkeeping_is_exact_and_keeps_parent_source_and_copy_guards(self):
        namespace = self.parent / ".delta"
        namespace.mkdir()
        protected = namespace / "user-work.txt"
        protected.write_text("private user work\n")
        frozen = self.prepare()
        child = namespace / "worktrees" / "native-one"
        self.git("worktree", "add", "-q", "-b", "native-one", str(child), "HEAD")
        self.assertEqual(preparation.check(self.directory, "run-fixture", "local", frozen, {})["components"], ["parent"])
        first = preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, child, ())
        copies = (first,)
        second_root = namespace / "worktrees" / "native-two"
        self.git("worktree", "add", "-q", "-b", "native-two", str(second_root), "HEAD")
        second = preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, second_root, copies)
        copies = (*copies, second)
        def check():
            return preparation.check(self.directory, "run-fixture", "local", frozen, {}, native_copies=copies)
        self.assertEqual(check(), {"status": "unchanged", "components": []})
        child.chmod(first["mode"] ^ 0o020)
        self.assertIn("native:" + str(child), check()["components"])
        child.chmod(first["mode"])
        another = preparation.prepare(self.directory, "run-fixture", "later", self.local, self.parent, copies)
        self.assertEqual(preparation.check(self.directory, "run-fixture", "later", another, {}, native_copies=copies)["status"], "unchanged")
        protected.write_text("changed user work\n")
        self.assertIn("parent", check()["components"])
        protected.write_text("private user work\n")
        foreign = namespace / "unrecorded.txt"
        foreign.write_text("not owned by a native receipt\n")
        self.assertIn("parent", check()["components"])
        foreign.unlink()
        self.git("branch", "unrelated-user-ref", "HEAD")
        self.assertIn("parent", check()["components"])
        self.git("branch", "-D", "unrelated-user-ref")
        self.git("update-ref", "refs/heads/main", self.head)
        self.assertIn("parent", check()["components"])
        self.git("update-ref", "refs/heads/main", self.left)
        self.git("update-ref", "refs/heads/native-one", self.left)
        self.assertIn("parent", check()["components"])
        self.assertIn("native:" + str(child), check()["components"])
        self.git("update-ref", "refs/heads/native-one", self.head)
        source = Path(frozen["source_root"]) / "feature.txt"
        source.write_text("unexpected source write\n")
        self.assertIn("source", check()["components"])
        source.write_text("feature\n")
        (child / "feature.txt").write_text("unexpected child write\n")
        self.assertIn("native:" + str(child), check()["components"])
        (child / "feature.txt").write_text("feature\n")
        self.assertEqual(check()["status"], "unchanged")
        self.git("worktree", "remove", "--force", str(child))
        self.assertEqual(check()["status"], "unknown")
        self.assertIn("native:" + str(child), check()["components"])

    def test_native_copy_cannot_hide_existing_user_content_or_redirected_roots(self):
        child = self.parent / ".delta" / "existing"
        self.git("worktree", "add", "-q", "--detach", str(child), "HEAD")
        frozen = self.prepare()
        with self.assertRaisesRegex(ValueError, "separate from protected parent/source"):
            preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, self.parent, ())
        with self.assertRaisesRegex(ValueError, "pre-existing user content"):
            preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, child, ())
        alias = self.base / "alias"
        alias.symlink_to(child, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "canonical"):
            preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, alias, ())

    def test_native_project_can_differ_from_the_reviewed_repository_without_bypassing_its_baseline(self):
        frozen = self.prepare()
        project = self.base / "native-project"
        self.git("clone", "-q", str(self.parent), str(project))
        record = preparation.capture_baseline(self.directory, "native-parent", project, ())
        original = preparation.read_baseline(self.directory, record)
        other = preparation.prepare(self.directory, "run-fixture", "other", self.local, self.parent)
        run = {"run_id": "run-fixture", "targets": {
            "local": {"frozen": frozen, "setup": None, "resources_removed": False, "baselines": {}},
            "other": {"frozen": other, "setup": None, "resources_removed": False, "baselines": {"native-parent": record}}}}
        parents = workflow.parent_ledgers(self.directory, run)
        self.assertEqual(parents[str(project)], original)
        child = project / ".delta" / "worktrees" / "native"
        self.git("worktree", "add", "-q", "-b", "native", str(child), "HEAD", root=project)
        with self.assertRaises(preparation.NeedsDecision) as caught:
            preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, child, ())
        self.assertEqual(caught.exception.code, "runtime_parent")
        proof = preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, child, (),
                                                parents)
        copies = (proof,)
        check = lambda: preparation.check(self.directory, "run-fixture", "local", frozen, {"native-parent": record}, native_copies=copies)
        self.assertEqual(check()["status"], "unchanged")
        self.assertEqual(proof["parent_root"], str(project))
        external = self.base / "external-native"
        self.git("worktree", "add", "-q", "--detach", str(external), "HEAD", root=project)
        with self.assertRaises(preparation.NeedsDecision) as caught:
            preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, external, copies, parents)
        self.assertEqual(caught.exception.code, "runtime_parent")
        external_proof = preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, external, copies, parents,
                                                        parent_root=str(project))
        copies = (*copies, external_proof)
        self.assertEqual(check()["status"], "unchanged")
        (project / "feature.txt").write_text("unexpected merge-back\n")
        self.assertIn("copy:native-parent", check()["components"])
        (project / "feature.txt").write_text("feature\n")
        artifact = self.directory / record["artifact"]
        saved = artifact.read_bytes()
        artifact.write_text("{}")
        self.assertEqual(check()["status"], "unknown")
        artifact.write_bytes(saved)
        redirected = self.directory / "redirected-ledger"
        redirected.write_bytes(saved)
        artifact.unlink()
        artifact.symlink_to(redirected)
        self.assertEqual(check()["status"], "unknown")

    def test_native_bookkeeping_cannot_exempt_writes_to_a_frozen_source(self):
        frozen = self.prepare()
        source = Path(frozen["source_root"])
        ledger = preparation.baseline(source)
        child = source / ".delta" / "native"
        self.git("worktree", "add", "-q", "-b", "native", str(child), "HEAD", root=source)
        proof = preparation.capture_runtime_copy(self.directory, "run-fixture", "local", frozen, child, (), {str(source): ledger})
        observed = preparation.check(self.directory, "run-fixture", "local", frozen, {}, native_copies=(proof,))
        self.assertIn("source", observed["components"])
        self.assertEqual(observed["status"], "changed")

    def test_cleanup_preserves_unmarked_debris_and_removes_only_known_owned_snapshots(self):
        frozen = self.prepare()
        marker = self.resource(frozen) / "owner.json"
        original = marker.read_bytes()
        marker.unlink()
        with self.assertRaisesRegex(ValueError, "ownership evidence is missing"):
            preparation.cleanup(self.directory, "run-fixture", "local", setup_resource=self.resource(frozen).name)
        marker.write_bytes(original)
        orphan = self.directory / ("snapshot-" + "f" * 32)
        orphan.mkdir()
        sentinel = orphan / "user-data.txt"
        sentinel.write_text("unowned contents\n")
        self.assertEqual(preparation.cleanup(self.directory, "run-fixture", "local"), [self.resource(frozen).name])
        self.assertEqual(sentinel.read_text(), "unowned contents\n")

    def test_prepare_pr_reread_binds_captured_host_and_rejects_wrong_url(self):
        url = "https://github.example.test/fixture/project/pull/12"
        target = {"repository": "fixture/project", "intake": {"number": 12, "url": url,
                  "base_sha": self.left, "head_sha": self.head}}
        live = {"number": 12, "state": "open", "html_url": url,
                "base": {"sha": self.left}, "head": {"sha": self.head}}
        with patch.object(preparation.intake, "api", return_value=live) as api:
            self.assertEqual(preparation.comparison(target, self.parent)[-1], live)
            api.assert_called_once_with("repos/fixture/project/pulls/12", hostname="github.example.test")
        with (patch.object(preparation.intake, "api", return_value=dict(live, html_url="https://github.com/fixture/project/pull/12")),
              self.assertRaisesRegex(ValueError, "captured target")):
            preparation.comparison(target, self.parent)

    def test_catalog_maps_both_type_change_sections_without_losing_patch_bytes(self):
        roster = self.base / "roster.md"
        roster.write_text("## Profiles\n\n| Spawn as | Provider ID | Model | Lane | Family | Billing | Worktree | Roles | Use for |\n"
                          "|---|---|---|---|---|---|---|---|---|\n"
                          "| fixture | fixture | model | fixture | family | flat | isolated | reviewer | Review |\n")
        name = 'odd "file\tname\n.txt'
        path = self.parent / name
        (self.parent / "original.txt").rename(path)
        self.commit("unusual path")
        for symlink in (True, False):
            with self.subTest(symlink=symlink):
                previous = self.git("rev-parse", "HEAD")
                path.unlink()
                if symlink:
                    path.symlink_to("feature.txt")
                else:
                    path.write_text("replacement\n")
                self.commit("type change")
                frozen = self.prepare("range-two-dot", left=previous, right="HEAD")
                patch_file = self.resource(frozen) / "review.patch"
                patch_data = patch_file.read_bytes()
                catalog = workflow.contract.catalog(frozen, roster, REGISTRY)
                parts = list(catalog["units"].values())
                self.assertEqual({tuple(unit["paths"]) for unit in parts}, {(name,)})
                self.assertEqual(sum(unit["kind"] == "metadata" for unit in parts), 2)
                self.assertEqual(b"".join(patch_data[unit["offset"]:unit["offset"] + unit["bytes"]] for unit in parts), patch_data)
                workflow.contract.validate_catalog(catalog, frozen, REGISTRY)
                self.assertEqual(catalog, workflow.contract.catalog(frozen, roster, REGISTRY))
                for damaged in (patch_data[:patch_data.index(b"diff --git ", 1)], patch_data + patch_data):
                    patch_file.write_bytes(damaged)
                    with self.assertRaisesRegex(ValueError, "patch sections disagree"):
                        workflow.contract.catalog(frozen, roster, REGISTRY)
                patch_file.write_bytes(patch_data)
        path.unlink()
        path.symlink_to("feature.txt")
        self.git("add", "--", name)
        path.unlink()
        path.write_text("replacement\n")
        frozen = self.prepare("working", left=None, right="HEAD")
        catalog = workflow.contract.catalog(frozen, roster, REGISTRY)
        for artifact in ("review.patch", "staged.patch", "unstaged.patch"):
            patch_data = (self.resource(frozen) / artifact).read_bytes()
            parts = [unit for unit in catalog["units"].values() if unit["artifact"] == artifact]
            self.assertEqual(b"".join(patch_data[unit["offset"]:unit["offset"] + unit["bytes"]] for unit in parts), patch_data)
            self.assertEqual(sum(unit["kind"] == "metadata" for unit in parts), 0 if artifact == "review.patch" else 2)

    def test_two_dot_three_dot_and_default_branch_preserve_semantics(self):
        for mode, expected in (("range-two-dot", self.left), ("range-three-dot", self.common),
                               ("branch-merge-base", self.common)):
            with self.subTest(mode=mode):
                frozen = self.prepare(mode, left=None if mode == "branch-merge-base" else "main")
                self.assertEqual(frozen["diff_base_sha"], expected)
                self.assertEqual(frozen["head_sha"], self.head)
                self.assertEqual(self.git("rev-parse", "HEAD", root=Path(frozen["source_root"])), self.head)
                self.assertIn("feature.txt", frozen["inventory"])
                self.assertEqual("base-only.txt" in frozen["inventory"], mode == "range-two-dot")
        self.git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        with self.assertRaisesRegex(preparation.NeedsDecision, "supply the branch base"):
            self.prepare("branch-merge-base", left=None)

    def test_legacy_installed_maintainer_cannot_claim_the_new_lifecycle_contract(self):
        legacy = self.base / "home/.local/share/zetta-delta/ensure-context-indexes.py"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("INDEX_DIRS = ()\nBUDGET_SECONDS = 1\n"
                          "ZG_LIFECYCLE_VERSION = 1\n"
                          "ensure = fingerprint = zg_queue = lambda *args: None\n")
        with patch.object(preparation.Path, "home", return_value=self.base / "home"):
            with self.assertRaises(preparation.NeedsDecision) as caught:
                preparation.index_helper()
            staged = self.prepare()
            frozen = preparation.finalize(self.directory, "run-fixture", "local", staged, None, [], None)
            self.assertEqual(preparation.check(self.directory, "run-fixture", "local", frozen, {})["status"], "unchanged")
            self.assertEqual(preparation.cleanup(self.directory, "run-fixture", "local"), [self.resource(frozen).name])
        self.assertEqual(caught.exception.code, "index_maintainer")

    def test_version_two_maintainer_cannot_claim_foreground_writer_protection(self):
        helper, _ = preparation.index_helper(bundled=True)
        for version in (2, 3):
            with self.subTest(version=version), patch.object(helper, "INDEX_LIFECYCLE_VERSION", version), \
                 patch.object(preparation.importlib.util, "module_from_spec", return_value=helper), \
                 patch.object(importlib.machinery.SourceFileLoader, "exec_module"), \
                 self.assertRaises(preparation.NeedsDecision) as caught:
                preparation.index_helper(bundled=True)
            self.assertEqual(caught.exception.code, "index_maintainer")

    def test_foreground_crash_holds_finalization_and_cleanup_until_writer_exit(self):
        helper, helper_path = preparation.index_helper(bundled=True)
        staged = self.prepare()
        source = Path(staged["source_root"])
        ready, stop = self.base / "writer-ready", self.base / "writer-stop"
        writer = ("import os,pathlib,sys,time\n"
                  "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                  "deadline=time.monotonic()+15\n"
                  "while not pathlib.Path(sys.argv[2]).exists() and time.monotonic()<deadline: time.sleep(0.02)\n")
        driver = ("import importlib.util,pathlib,sys\n"
                  "spec=importlib.util.spec_from_file_location('fixture_hook',sys.argv[1])\n"
                  "h=importlib.util.module_from_spec(spec); spec.loader.exec_module(h)\n"
                  "h.fingerprint=lambda *args:'fixture'\n"
                  "h.shutil.which=lambda *args:'/fixture/tgrep'\n"
                  "h.maintain_tgrep=lambda root,changed,deadline,pass_fds=(): h.run([sys.executable,'-c'," + repr(writer) +
                  "," + repr(str(ready)) + "," + repr(str(stop)) + "],root,deadline,pass_fds=pass_fds)\n"
                  "h.setup_indexes(pathlib.Path(sys.argv[2]),['tgrep'])\n")
        with patch.object(preparation, "index_helper", return_value=(helper, helper_path)), \
             patch.object(helper.shutil, "which", return_value="/fixture/tgrep"):
            request = preparation.setup_request(self.directory, "run-fixture", "local", staged, ["tgrep"])
            child = subprocess.Popen([sys.executable, "-B", "-c", driver, str(helper_path), str(source)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            writer_pid = None
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                writer_pid = int(ready.read_text())
                child.kill()
                child.wait(timeout=5)
                for operation in (lambda: preparation.finalize(self.directory, "run-fixture", "local", staged, request, [], None),
                                  lambda: preparation.cleanup(self.directory, "run-fixture", "local", request, self.resource(staged).name)):
                    with self.assertRaises(preparation.NeedsDecision) as caught:
                        operation()
                    self.assertEqual(caught.exception.code, "index_setup_pending")
                    self.assertTrue(source.exists())
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
                while helper.lock_held(source / ".git/codex-context-indexes.lock") and time.monotonic() < deadline:
                    time.sleep(0.02)
            self.assertEqual(preparation.cleanup(self.directory, "run-fixture", "local", request, self.resource(staged).name),
                             [self.resource(staged).name])
            self.assertFalse(source.exists())

    def test_maintainer_requires_a_usable_module_loader(self):
        for spec in (None, SimpleNamespace(loader=None)):
            with self.subTest(spec=spec):
                with (
                    patch.object(preparation.importlib.util, "spec_from_file_location", return_value=spec),
                    self.assertRaises(preparation.NeedsDecision) as caught,
                ):
                    preparation.index_helper(bundled=True)
                self.assertEqual(caught.exception.code, "index_maintainer")

    def test_native_queue_handoff_holds_until_the_background_terminal_receipt(self):
        helper, path = preparation.index_helper(bundled=True)
        staged = self.prepare()
        source = Path(staged["source_root"])
        state_path = source / ".git/codex-context-indexes.json"
        helper.save_state(source / ".git", state_path, {"zg": {"fingerprint": "stamp"}})
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.base / "cache")}), \
             patch.object(preparation, "index_helper", return_value=(helper, path)), \
             patch.object(helper.shutil, "which", return_value="/fixture/zg"), \
             patch.object(helper, "ignore_indexes"), \
             patch.object(helper, "maintenance_dir", return_value=source / ".git"), \
             patch.object(helper, "scope", return_value=([], {"zg": 1, "codegraph": 1})), \
             patch.object(helper, "zg_preflight"), \
             patch.object(helper, "zg_index_usable", return_value=False), \
             patch.object(helper, "fingerprint", return_value="stamp"):
            request = preparation.setup_request(self.directory, "run-fixture", "local", staged, ["zg"])
            with helper.safe_open(helper.zg_first_build_lock()) as machine:
                fcntl.flock(machine, fcntl.LOCK_EX)
                helper.ensure(source, ["zg"], time.monotonic() + 5)
            self.assertEqual(json.loads(state_path.read_text())["zg"]["pending"], "queued")
            leases = []
            def launch(argv, **kwargs):
                if "--zg-queued" in argv:
                    self.assertIn(str(source), helper.zg_queue().read_text().splitlines())
                    helper.ensure(source, ["zg"], time.monotonic() + 5, queued=True)
                else:
                    self.assertIn("--zg-build", argv)
                    self.assertEqual(helper.zg_queue().read_text(), "")
                    state = json.loads(state_path.read_text())["zg"]
                    self.assertEqual(state["pending"], "starting")
                    lease = os.dup(kwargs["pass_fds"][0])
                    leases.append(lease)
                    info = os.fstat(lease)
                    self.assertEqual(state["lease"], [info.st_dev, info.st_ino])
                return SimpleNamespace(pid=1)
            with patch.object(helper.subprocess, "Popen", side_effect=launch) as dispatched:
                helper.start_queued_zg()
                self.assertEqual(dispatched.call_count, 2)
            # The launched child retains the inherited lease before executing.
            for operation in (lambda: preparation.finalize(self.directory, "run-fixture", "local", staged, request, [], None),
                              lambda: preparation.cleanup(self.directory, "run-fixture", "local", request, self.resource(staged).name)):
                with self.assertRaises(preparation.NeedsDecision) as caught:
                    operation()
                self.assertEqual(caught.exception.code, "index_setup_pending")
            for lease in leases:
                os.close(lease)
            with patch.object(helper, "maintain_zg"):
                helper.build_zg_in_background(source, "stamp")
            self.assertEqual(json.loads(state_path.read_text())["zg"], {"fingerprint": "stamp"})
            frozen = preparation.finalize(self.directory, "run-fixture", "local", staged, request, [], None)
            self.assertEqual(preparation.check(self.directory, "run-fixture", "local", frozen, {})["status"], "unchanged")
            self.assertEqual(preparation.cleanup(self.directory, "run-fixture", "local", request, self.resource(frozen).name),
                             [self.resource(frozen).name])

    def test_dirty_source_and_its_git_metadata_are_never_changed(self):
        (self.parent / "original.txt").write_text("staged\n")
        self.git("add", "original.txt")
        (self.parent / "original.txt").write_text("unstaged\n")
        (self.parent / "user.txt").write_text("untracked user work\n")
        (self.parent / "cache.ignored").write_text("ignored user work\n")
        (self.parent / ".git/FETCH_HEAD").write_text("pre-existing fetch state\n")
        before = preparation.baseline(self.parent)
        objects = preparation.files(self.parent / ".git/objects")
        frozen = self.prepare()
        self.assertEqual(preparation.baseline(self.parent), before)
        self.assertEqual(preparation.files(self.parent / ".git/objects"), objects)
        source = Path(frozen["source_root"])
        self.assertEqual((source / "original.txt").read_text(), "original\n" * 10)
        self.assertFalse((source / "user.txt").exists())
        self.assertFalse((source / ".git/objects/info/alternates").exists())
        self.assertEqual(self.git("rev-parse", "--git-common-dir", root=source), ".git")
        self.assertEqual(preparation.check(self.directory, "run-fixture", "local", frozen, {}),
                         {"status": "unchanged", "components": []})

    def test_complete_patch_renames_binary_and_unusual_filenames_share_inventory(self):
        self.git("mv", "original.txt", "renamed.txt")
        unusual = "a tab\tand newline\n.txt"
        (self.parent / unusual).write_text("unusual name\n")
        (self.parent / "binary.dat").write_bytes(b"\x00binary\xff\n")
        self.commit("rename and binary")
        frozen = self.prepare()
        resource = self.resource(frozen)
        ledger = json.loads((resource / "inventory.json").read_text())
        self.assertEqual(ledger["diff_base_sha"], frozen["diff_base_sha"])
        self.assertEqual(ledger["head_sha"], frozen["head_sha"])
        rename = next(row for row in ledger["changes"] if row["status"].startswith("R"))
        self.assertEqual(rename["paths"], ["original.txt", "renamed.txt"])
        binary = next(row for row in ledger["changes"] if row["paths"] == ["binary.dat"])
        self.assertTrue(binary["binary"])
        self.assertIsNone(binary["additions"])
        self.assertIn(unusual, frozen["inventory"])
        applied = preparation.test_copy(self.directory, "run-fixture", "local", frozen)
        self.git("checkout", "--detach", frozen["diff_base_sha"], root=applied)
        result = subprocess.run(["rtk", "proxy", "git", "-C", str(applied), "apply", "--index", str(resource / "review.patch")],
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("write-tree", root=applied), self.git("rev-parse", frozen["head_sha"] + "^{tree}", root=applied))

    def test_working_snapshot_preserves_staged_only_changes_and_exact_scoped_bytes(self):
        original = (self.parent / "original.txt").read_bytes()
        (self.parent / "original.txt").write_text("staged only\n")
        self.git("add", "original.txt")
        (self.parent / "original.txt").write_bytes(original)
        (self.parent / "feature.txt").write_text("working change\n")
        self.git("update-index", "--assume-unchanged", "feature.txt")
        (self.parent / "selected.txt").write_bytes(b"selected\r\n")
        (self.parent / "selected.ignored").write_text("explicit ignored source\n")
        (self.parent / "omitted.txt").write_text("keep private\n")
        (self.parent / "link").symlink_to("selected.txt")
        before = preparation.baseline(self.parent)
        frozen = self.prepare("working", left=None, right="HEAD", untracked=["selected.txt", "selected.ignored", "link"])
        source = Path(frozen["source_root"])
        resource = self.resource(frozen)
        self.assertEqual(preparation.baseline(self.parent), before)
        self.assertEqual((source / "selected.txt").read_bytes(), b"selected\r\n")
        self.assertEqual((source / "original.txt").read_bytes(), original)
        self.assertEqual(os.readlink(source / "link"), "selected.txt")
        self.assertFalse((source / "omitted.txt").exists())
        self.assertEqual((source / "feature.txt").read_text(), "working change\n")
        self.assertIn("original.txt", frozen["inventory"])
        self.assertIn(b"staged only", (resource / "staged.patch").read_bytes())
        self.assertIn(b"staged only", (resource / "unstaged.patch").read_bytes())
        self.assertEqual(frozen["preparation"]["baseline_head_sha"], self.head)
        self.assertFalse(self.git("status", "--porcelain", root=source))

    def test_sparse_missing_paths_are_preserved_and_attributes_do_not_execute_filters(self):
        self.git("update-index", "--skip-worktree", "ignored.txt")
        (self.parent / "ignored.txt").unlink()
        (self.parent / ".gitattributes").write_text("*.txt text eol=lf filter=unsafe\n")
        (self.parent / "selected.txt").write_bytes(b"raw\r\n")
        frozen = self.prepare("working", left=None, right="HEAD", untracked=[".gitattributes", "selected.txt"])
        source = Path(frozen["source_root"])
        self.assertEqual((source / "ignored.txt").read_text(), "tracked\n")
        self.assertEqual((source / "selected.txt").read_bytes(), b"raw\r\n")
        self.assertFalse((self.parent / "ignored.txt").exists())

    def test_pr_intake_movement_and_fork_head_binding(self):
        pull = {"number": 12, "base_sha": self.left, "head_sha": self.head,
                "url": "https://github.com/fixture/project/pull/12"}
        target = {"repository": "fixture/project", "intake": pull}
        live = {"number": 12, "state": "open", "html_url": "https://github.com/fixture/project/pull/12",
                "base": {"sha": self.left, "ref": "main", "repo": {"full_name": "fixture/project"}},
                "head": {"sha": self.head, "ref": "feature", "repo": {"full_name": "fork/project"}}}
        with patch.object(preparation.intake, "api", return_value=live):
            frozen = preparation.prepare(self.directory, "run-fixture", "fixture/project#12", target, self.parent)
        self.assertEqual(frozen["diff_base_sha"], self.common)
        self.assertEqual(json.loads((self.resource(frozen) / "pr.json").read_text())["head"]["repository"], "fork/project")
        live["head"]["sha"] = self.left
        with (
            patch.object(preparation.intake, "api", return_value=live),
            self.assertRaisesRegex(preparation.NeedsDecision, "moved since intake"),
        ):
            preparation.prepare(self.directory, "run-fixture", "fixture/project#12", target, self.parent)

    def test_working_gitlinks_preserve_uninitialized_modules_and_hold_dirty_content(self):
        self.git("update-index", "--add", "--cacheinfo", "160000", self.head, "module")
        self.git("commit", "-qm", "record gitlink")
        for initialized in (False, True):
            with self.subTest(initialized=initialized):
                if initialized:
                    (self.parent / "module").mkdir()
                frozen = self.prepare("working", left=None, right="HEAD")
                tree = self.git("ls-tree", frozen["head_sha"], "module", root=Path(frozen["source_root"]))
                self.assertEqual(tree, "160000 commit " + self.head + "\tmodule")
        module = self.parent / "module"
        self.git("init", "-q", "-b", "main", root=module)
        self.git("config", "user.name", "Fixture", root=module)
        self.git("config", "user.email", "fixture@localhost", root=module)
        (module / "code.txt").write_text("module source\n")
        self.git("add", "code.txt", root=module)
        self.git("commit", "-qm", "module", root=module)
        frozen = self.prepare("working", left=None, right="HEAD")
        self.assertIn(self.git("rev-parse", "HEAD", root=module),
                      self.git("ls-tree", frozen["head_sha"], "module", root=Path(frozen["source_root"])))
        (module / "code.txt").write_text("dirty module\n")
        before = preparation.baseline(self.parent)
        with self.assertRaises(preparation.NeedsDecision) as caught:
            self.prepare("working", left=None, right="HEAD")
        self.assertEqual(caught.exception.code, "working_submodule")
        self.assertEqual(preparation.baseline(self.parent), before)

    def test_missing_pr_history_fetches_only_in_disposable_repository_and_checks_oid(self):
        remote = self.base / "remote"
        self.git("clone", "-q", "--no-local", str(self.parent), str(remote))
        self.git("update-ref", "refs/heads/main", self.left, root=remote)
        self.git("update-ref", "refs/pull/12/head", self.head, root=remote)
        source = self.directory / "fetch-fixture"
        preparation.repository(source, self.parent, [])
        live = {"number": 12, "html_url": "https://github.com/fixture/project/pull/12",
                "base": {"ref": "main", "repo": {"full_name": "fixture/project", "clone_url": "https://github.com/fixture/project.git"}}}
        before = preparation.baseline(self.parent)
        real_git = preparation.git
        def fetch(root, *args, **kwargs):
            if args and args[0] == "rev-parse" and root == self.parent:
                raise ValueError("fixture missing object")
            args = tuple(str(remote) if arg == "https://github.com/fixture/project.git" else arg for arg in args)
            return real_git(root, *args, **kwargs)
        with patch.object(preparation, "git", side_effect=fetch):
            preparation.fetch_pr(source, self.parent, self.left, self.head, live)
            with self.assertRaisesRegex(preparation.NeedsDecision, "differs from intake"):
                preparation.fetch_pr(source, self.parent, self.left, self.left, live)
        self.assertEqual(preparation.baseline(self.parent), before)

    def test_integrity_detects_parent_ignored_content_snapshot_index_and_artifact_changes(self):
        frozen = self.prepare()
        resource = self.resource(frozen)
        (self.parent / "new.ignored").write_text("unexpected\n")
        check = preparation.check(self.directory, "run-fixture", "local", frozen, {})
        self.assertIn("parent", check["components"])
        (self.parent / "new.ignored").unlink()
        self.git("update-index", "--assume-unchanged", "feature.txt", root=Path(frozen["source_root"]))
        (resource / "review.patch").write_bytes(b"tampered\n")
        check = preparation.check(self.directory, "run-fixture", "local", frozen, {})
        self.assertEqual(set(check["components"]), {"source", "artifact:review.patch"})

    def test_participating_copy_baseline_and_test_copy_have_separate_ownership(self):
        frozen = self.prepare()
        source_before = preparation.baseline(Path(frozen["source_root"]))
        test_root = preparation.test_copy(self.directory, "run-fixture", "local", frozen)
        self.assertFalse((test_root / ".git/objects/info/alternates").exists())
        (test_root / "generated.txt").write_text("test output\n")
        self.assertEqual(preparation.baseline(Path(frozen["source_root"])), source_before)
        self.assertEqual(preparation.check(self.directory, "run-fixture", "local", frozen, {})["status"], "unchanged")
        copies = {"runtime-copy": {"root": str(self.parent), "fingerprint": preparation.fingerprint(preparation.baseline(self.parent))}}
        (self.parent / "child-output.txt").write_text("unexpected copy change\n")
        self.assertIn("copy:runtime-copy", preparation.check(self.directory, "run-fixture", "local", frozen, copies)["components"])

    def test_command_local_scans_reuse_content_and_detect_changes_on_the_next_command(self):
        frozen = self.prepare()
        observations = {}
        copies = {"parent-copy": {"root": str(self.parent), "fingerprint": frozen["parent_fingerprint"]}}
        with patch.object(preparation, "baseline", wraps=preparation.baseline) as baseline:
            for _ in range(2):
                self.assertEqual(preparation.check(self.directory, "run-fixture", "local", frozen, copies,
                                                   observations=observations)["status"], "unchanged")
            self.assertEqual(sum(call.args[0] == self.parent for call in baseline.call_args_list), 1)
        changed = self.parent / "ignored.txt"
        before = changed.stat()
        changed.write_text("changed\n")
        os.utime(changed, ns=(before.st_atime_ns, before.st_mtime_ns))
        observation = preparation.check(self.directory, "run-fixture", "local", frozen, copies)
        self.assertEqual(observation["status"], "changed")
        self.assertIn("parent", observation["components"])
        self.assertIn("copy:parent-copy", observation["components"])

    def test_missing_registered_copy_reports_its_id_and_preserves_the_hold(self):
        frozen = self.prepare()
        root = self.base / "runtime-copy"
        shutil.copytree(Path(frozen["source_root"]), root)
        copies = {"runtime-copy": {"root": str(root), "fingerprint": preparation.fingerprint(preparation.baseline(root))}}
        self.assertEqual(preparation.check(self.directory, "run-fixture", "local", frozen, copies)["status"], "unchanged")
        shutil.rmtree(root)
        self.assertEqual(preparation.check(self.directory, "run-fixture", "local", frozen, copies),
                         {"status": "unknown", "components": ["copy:runtime-copy"]})

    def test_cleanup_preserves_parent_other_runs_journal_and_refuses_symlink_replacement(self):
        frozen = self.prepare()
        journal = self.directory / "run.json"
        journal.write_text("journal stays\n")
        other = self.directory / ("snapshot-" + "f" * 32)
        other.mkdir()
        preparation.save(other / "owner.json", {"run_id": "run-other", "target": "local", "resource": other.name})
        before = preparation.baseline(self.parent)
        removed = preparation.cleanup(self.directory, "run-fixture", "local")
        self.assertEqual(removed, [self.resource(frozen).name])
        self.assertTrue(other.exists())
        self.assertEqual(journal.read_text(), "journal stays\n")
        self.assertEqual(preparation.baseline(self.parent), before)
        other.rename(self.directory / "saved-other")
        other.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "replaced resource"):
            preparation.cleanup(self.directory, "run-fixture", "local")
        self.assertEqual(preparation.baseline(self.parent), before)

    def test_preparation_race_and_unmerged_index_hold_without_cleaning_user_work(self):
        real_artifacts = preparation.artifacts
        def change_parent(*args, **kwargs):
            result = real_artifacts(*args, **kwargs)
            (self.parent / "new-user-work.txt").write_text("preserve me\n")
            return result
        with (
            patch.object(preparation, "artifacts", side_effect=change_parent),
            self.assertRaisesRegex(preparation.NeedsDecision, "changed during preparation"),
        ):
            self.prepare()
        self.assertEqual((self.parent / "new-user-work.txt").read_text(), "preserve me\n")
        self.assertEqual(list(self.directory.glob("snapshot-*")), [])
        blob = self.git("rev-parse", "HEAD:original.txt")
        self.git("update-index", "--index-info", input=f"0 {'0' * 40}\toriginal.txt\n100644 {blob} 1\toriginal.txt\n")
        before = preparation.baseline(self.parent)
        with self.assertRaisesRegex(preparation.NeedsDecision, "unmerged index"):
            self.prepare("working", left=None, right="HEAD")
        self.assertEqual(preparation.baseline(self.parent), before)


class SealedSnapshotTests(GitFixture):
    def setUp(self):
        super().setUp()
        self.helper, self.helper_path = preparation.index_helper(bundled=True)
        self.staged = self.prepare()
        self.source = Path(self.staged["source_root"])
        for mock in (patch.object(preparation, "index_helper", return_value=(self.helper, self.helper_path)),
                     patch.object(self.helper, "fingerprint", return_value="fixture-stamp"),
                     patch.object(self.helper.shutil, "which", side_effect=lambda name: "/fixture/tgrep" if name == "tgrep" else None),
                     patch.object(self.helper, "zg_queue", return_value=self.base / "zg.queue")):
            mock.start()
            self.addCleanup(mock.stop)

    def finalize(self, request=None, fallback=(), evidence=None):
        return preparation.finalize(self.directory, "run-fixture", "local", self.staged, request, list(fallback), evidence)

    def test_sealing_suppresses_native_hook_writers_and_preserves_setup_and_test_copies(self):
        session = {"cwd": str(self.source), "hook_event_name": "SessionStart"}
        self.assertEqual(self.helper.selection(session), (self.source, list(self.helper.TOOLS)))
        request = preparation.setup_request(self.directory, "run-fixture", "local", self.staged, ["tgrep"])
        def build(root, changed, deadline, **kwargs):
            (root / ".tgrep").mkdir()
            (root / ".tgrep/index").write_text("fixture cache\n")
        with patch.object(self.helper, "maintain_tgrep", side_effect=build) as writer:
            preparation.run_setup(self.staged, request)
        writer.assert_called_once()
        frozen = self.finalize(request)
        self.assertEqual(self.finalize(request), frozen)
        for payload in (session,
                        dict(session, hook_event_name="PreToolUse", tool_name="mcp__codegraph__codegraph_explore", tool_input={"projectPath": str(self.source)}),
                        dict(session, hook_event_name="PreToolUse", tool_name="mcp__zvec__search", tool_input={"root": str(self.source)}),
                        dict(session, hook_event_name="PreToolUse", tool_name="exec_command", tool_input={"cmd": "rtk tgrep -- x " + str(self.source)})):
            with self.subTest(payload=payload):
                self.assertIsNone(self.helper.selection(payload))
        with patch.object(self.helper, "maintenance_dir", side_effect=AssertionError("sealed sources must not start maintenance")):
            self.helper.ensure(self.source, list(self.helper.TOOLS), time.monotonic() + 5)
            self.helper.ensure(self.source, ["zg"], time.monotonic() + 5, queued=True)
            self.helper.build_zg_in_background(self.source, "fixture-stamp")
            self.helper.setup_indexes(self.source, ["tgrep"])
        self.assertEqual(preparation.check(self.directory, "run-fixture", "local", frozen, {})["status"], "unchanged")
        test_root = preparation.test_copy(self.directory, "run-fixture", "local", frozen)
        self.assertEqual(self.helper.selection(dict(session, cwd=str(test_root))), (test_root, list(self.helper.TOOLS)))
        (self.source / ".tgrep/index").write_text("unexpected writer\n")
        self.assertIn("source", preparation.check(self.directory, "run-fixture", "local", frozen, {})["components"])

    def test_finalization_wins_a_hook_waiting_for_the_foreground_lock(self):
        wait = self.helper.wait_for_lock
        finalized = []
        def finalize_before_lock(lock, deadline):
            finalized.append(self.finalize())
            wait(lock, deadline)
        with patch.object(self.helper, "wait_for_lock", side_effect=finalize_before_lock), \
             patch.object(self.helper, "ignore_indexes") as writer:
            self.helper.ensure(self.source, ["tgrep"], time.monotonic() + 5)
        writer.assert_not_called()
        self.assertEqual(len(finalized), 1)
        self.assertEqual(preparation.check(self.directory, "run-fixture", "local", finalized[0], {})["status"], "unchanged")

    def test_sealed_background_build_releases_its_inherited_lease_without_writing(self):
        self.finalize()
        with self.helper.safe_open(self.source / ".git/zg-build.lock") as lock, \
             patch.object(self.helper, "maintain_zg") as writer:
            fcntl.flock(lock, fcntl.LOCK_EX)
            lease = os.dup(lock.fileno())
            try:
                self.helper.build_zg_in_background(self.source, "fixture-stamp", lease)
                with self.assertRaises(OSError):
                    os.fstat(lease)
            finally:
                try:
                    os.close(lease)
                except OSError:
                    pass
        writer.assert_not_called()
        self.assertFalse((self.source / ".zvec-grep").exists())

    def test_interrupted_seal_holds_writers_and_allows_identical_finalization_retry(self):
        save = preparation.save
        def interrupted(path, data):
            if path.name == "index-seal.json":
                raise OSError("interrupted seal save")
            save(path, data)
        with patch.object(preparation, "save", side_effect=interrupted), self.assertRaisesRegex(OSError, "interrupted"):
            self.finalize()
        with patch.object(self.helper, "maintenance_dir", side_effect=AssertionError("incomplete seals cannot admit writers")), \
             self.assertRaises(OSError):
            self.helper.ensure(self.source, ["tgrep"], time.monotonic() + 5)
        frozen = self.finalize()
        self.assertTrue(self.helper.sealed_snapshot(self.source))
        self.assertEqual(self.finalize(), frozen)

    def test_static_fallback_is_sealed_without_requiring_unavailable_indexes(self):
        request = preparation.setup_request(self.directory, "run-fixture", "local", self.staged, ["zg"])
        with self.assertRaises(preparation.NeedsDecision) as caught:
            self.finalize(request)
        self.assertEqual(caught.exception.code, "index_fallback")
        frozen = self.finalize(request, ["zg"], "complete patch and static caller evidence cover the change")
        self.assertTrue(self.helper.sealed_snapshot(self.source))
        self.assertEqual(preparation.check(self.directory, "run-fixture", "local", frozen, {})["status"], "unchanged")

    def test_redirected_or_tampered_seal_evidence_never_admits_maintenance(self):
        self.finalize()
        resource = self.resource(self.staged)
        seal = resource / "index-seal.json"
        original = seal.read_bytes()
        for data in ({}, dict(json.loads(original), source_root=str(self.parent))):
            seal.write_text(json.dumps(data))
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.helper.ensure(self.source, ["tgrep"], time.monotonic() + 5)
        seal.write_bytes(original)
        for name in ("owner.json", "final-source-baseline.json", "index-result.json"):
            path = resource / name
            backup = self.base / ("saved-" + name)
            content = path.read_bytes()
            path.write_bytes(content + b" ")
            with self.subTest(name=name, attack="tamper"), self.assertRaises(ValueError):
                self.helper.ensure(self.source, ["tgrep"], time.monotonic() + 5)
            path.write_bytes(content)
            path.rename(backup)
            for attack in ("symlink", "hardlink", "fifo"):
                if attack == "symlink":
                    path.symlink_to(backup)
                elif attack == "hardlink":
                    os.link(backup, path)
                else:
                    os.mkfifo(path, 0o600)
                with self.subTest(name=name, attack=attack), self.assertRaises((OSError, RuntimeError)):
                    self.helper.ensure(self.source, ["tgrep"], time.monotonic() + 5)
                path.unlink()
            backup.rename(path)
        resource.chmod(0o755)
        with self.assertRaises(RuntimeError):
            self.helper.ensure(self.source, ["tgrep"], time.monotonic() + 5)
        resource.chmod(0o700)
        with patch.object(self.helper.os, "getuid", return_value=os.getuid() + 1), self.assertRaises(RuntimeError):
            self.helper.ensure(self.source, ["tgrep"], time.monotonic() + 5)
        self.assertTrue(self.helper.sealed_snapshot(self.source))

    def test_in_checkout_seal_cannot_disable_maintenance(self):
        (self.source / "index-seal.json").write_text('{"version": 1}')
        (self.source / ".git/index-seal.json").write_text('{"version": 1}')
        self.assertFalse(self.helper.sealed_snapshot(self.source))
        self.assertEqual(self.helper.selection({"cwd": str(self.source), "hook_event_name": "SessionStart"}),
                         (self.source, list(self.helper.TOOLS)))


class IndexPreparationTests(GitFixture):
    def setUp(self):
        super().setUp()
        self.helper = self.indexes()
        self.staged = self.prepare()
        self.source = Path(self.staged["source_root"])
        self.request = preparation.setup_request(self.directory, "run-fixture", "local", self.staged, ["zg"])

    def finalize(self, fallback=(), evidence=None):
        return preparation.finalize(self.directory, "run-fixture", "local", self.staged, self.request, list(fallback), evidence)

    def cleanup(self):
        return preparation.cleanup(self.directory, "run-fixture", "local", self.request, self.resource(self.staged).name)

    def test_startup_without_receipt_holds_finalization_and_cleanup(self):
        for operation in (self.finalize, self.cleanup):
            with self.assertRaises(preparation.NeedsDecision) as caught:
                operation()
            self.assertEqual(caught.exception.code, "index_setup_pending")
        self.assertTrue(self.source.exists())
        self.assertFalse((self.resource(self.staged) / "final-source-baseline.json").exists())

    def test_pending_startup_holds_despite_an_old_receipt_and_empty_queue(self):
        state = self.source / ".git/codex-context-indexes.json"
        state.write_text(json.dumps({"zg": {"fingerprint": "zg-stamp", "pending": "starting"}}))
        for operation in (self.finalize, self.cleanup,
                          lambda: preparation.cleanup(self.directory, "run-fixture", "local")):
            with self.assertRaises(preparation.NeedsDecision) as caught:
                operation()
            self.assertEqual(caught.exception.code, "index_setup_pending")
        self.assertTrue(self.source.exists())

    def test_native_locks_and_queue_hold_even_with_old_completed_state(self):
        preparation.run_setup(self.staged, self.request)
        for name in ("codex-context-indexes.lock", "zg-build.lock"):
            with self.subTest(lock=name), (self.source / ".git" / name).open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                for operation in (self.finalize, self.cleanup):
                    with self.assertRaises(preparation.NeedsDecision) as caught:
                        operation()
                    self.assertEqual(caught.exception.code, "index_setup_pending")
        self.helper.zg_queue().write_text(str(self.source) + "\n")
        for operation in (self.finalize, self.cleanup):
            with self.assertRaises(preparation.NeedsDecision) as caught:
                operation()
            self.assertEqual(caught.exception.code, "index_setup_pending")
        self.assertTrue(self.source.exists())

    def test_completed_setup_retains_original_evidence_and_seals_cache_baseline(self):
        original = (self.resource(self.staged) / "source-baseline.json").read_bytes()
        preparation.run_setup(self.staged, self.request)
        frozen = self.finalize()
        self.assertEqual(self.finalize(), frozen, "failed journal saves must allow identical finalization retries")
        self.assertEqual((self.resource(self.staged) / "source-baseline.json").read_bytes(), original)
        self.assertEqual(frozen["head_sha"], self.staged["head_sha"])
        final = json.loads((self.resource(frozen) / "final-source-baseline.json").read_text())
        self.assertIn(".zvec-grep/index", final["files"])
        self.assertEqual(preparation.check(self.directory, "run-fixture", "local", frozen, {})["status"], "unchanged")
        (self.source / ".zvec-grep/index").write_text("late index write\n")
        self.assertIn("source", preparation.check(self.directory, "run-fixture", "local", frozen, {})["components"])

    def test_setup_never_accepts_code_parent_or_patch_changes(self):
        preparation.run_setup(self.staged, self.request)
        for path, component in ((self.source / "feature.txt", "source"), (self.parent / "feature.txt", "parent"),
                                (self.resource(self.staged) / "review.patch", "artifact:review.patch")):
            with self.subTest(component=component):
                original = path.read_bytes()
                path.write_bytes(b"unexpected change\n")
                with self.assertRaises(preparation.NeedsDecision) as caught:
                    self.finalize()
                self.assertEqual(caught.exception.code, "source_integrity")
                assert caught.exception.observation is not None
                self.assertIn(component, caught.exception.observation["components"])
                self.assertEqual(path.read_bytes(), b"unexpected change\n")
                path.write_bytes(original)

    def test_failed_unavailable_indexes_require_explicit_static_evidence(self):
        self.request.update(tools=["tgrep", "zg"], available=["tgrep"])
        (self.source / ".git/codex-context-indexes.json").write_text(json.dumps({"tgrep": {"retry_after": 123}}))
        with self.assertRaises(preparation.NeedsDecision) as caught:
            self.finalize()
        self.assertEqual(caught.exception.code, "index_fallback")
        with self.assertRaisesRegex(ValueError, "requires evidence"):
            self.finalize(["tgrep", "zg"])
        with self.assertRaisesRegex(ValueError, "only failed/unavailable"):
            self.finalize(["codegraph"], "static inspection")
        frozen = self.finalize(["tgrep", "zg"], "complete patch and targeted static callers cover this change")
        result = json.loads((self.resource(frozen) / "index-result.json").read_text())
        self.assertEqual(result["fallback"], ["tgrep", "zg"])
        self.assertEqual(self.cleanup(), [self.resource(frozen).name])

    def test_unreadable_freshness_and_late_source_race_hold(self):
        preparation.run_setup(self.staged, self.request)
        with patch.object(self.helper, "fingerprint", side_effect=TimeoutError):
            with self.assertRaises(preparation.NeedsDecision) as caught:
                self.finalize()
            self.assertEqual(caught.exception.code, "index_readiness")
        with patch.object(self.helper, "maintenance_dir", side_effect=subprocess.TimeoutExpired("git", 1)):
            for operation in (self.finalize, self.cleanup):
                with self.assertRaises(preparation.NeedsDecision) as caught:
                    operation()
                self.assertEqual(caught.exception.code, "index_readiness")
        real_baseline = preparation.baseline
        def change_at_seal(root, exclude=(), **kwargs):
            if root == self.source and not exclude:
                (root / "feature.txt").write_text("late change\n")
            return real_baseline(root, exclude, **kwargs)
        with patch.object(preparation, "baseline", side_effect=change_at_seal):
            with self.assertRaises(preparation.NeedsDecision) as caught:
                self.finalize()
            assert caught.exception.observation is not None
            self.assertIn("source", caught.exception.observation["components"])
        self.assertFalse((self.resource(self.staged) / "final-source-baseline.json").exists())

    def test_cleanup_does_not_apply_setup_intent_to_other_owned_preparations(self):
        other = self.prepare()
        shutil.rmtree(Path(other["source_root"]) / ".git")
        preparation.run_setup(self.staged, self.request)
        self.assertCountEqual(self.cleanup(), [self.resource(self.staged).name, self.resource(other).name])

    def test_redirected_lifecycle_and_cache_paths_preserve_external_files(self):
        preparation.run_setup(self.staged, self.request)
        state = self.source / ".git/codex-context-indexes.json"
        external = self.base / "external-state.json"
        state.rename(external)
        state.symlink_to(external)
        for operation in (self.finalize, self.cleanup):
            with self.assertRaisesRegex(ValueError, "redirected"):
                operation()
        state.unlink()
        state.write_bytes(external.read_bytes())
        lock = self.source / ".git/codex-context-indexes.lock"
        lock.unlink()
        lock.symlink_to(external)
        for operation in (self.finalize, self.cleanup):
            with self.assertRaises(OSError):
                operation()
        lock.unlink()
        os.link(external, lock)
        for operation in (self.finalize, self.cleanup):
            with self.assertRaises(ValueError):
                operation()
        lock.unlink()
        cache = self.source / ".zvec-grep"
        cache.rename(self.base / "external-cache")
        cache.symlink_to(self.base / "external-cache", target_is_directory=True)
        with self.assertRaises(preparation.NeedsDecision) as caught:
            self.finalize()
        assert caught.exception.observation is not None
        self.assertEqual(caught.exception.observation["status"], "unknown")
        self.cleanup()
        self.assertTrue(external.exists())
        self.assertEqual((self.base / "external-cache/index").read_text(), "fixture cache\n")


class WorkflowPreparationTests(GitFixture):
    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "workflow.py"), "--run", str(self.directory), *args],
                              text=True, capture_output=True, check=False)

    def create(self, *comparison):
        self.directory.rmdir()
        for name, value in (("registry", REGISTRY), ("model", {"provider_id": "fixture", "model_id": "orchestrator"})):
            (self.base / (name + ".json")).write_text(json.dumps(value))
        created = self.cli("create", "--method", "pr-review", "--thread-id", "fixture-thread",
                           "--registry", str(self.base / "registry.json"), "--orchestrator", str(self.base / "model.json"),
                           "--source", str(self.parent), *comparison)
        self.assertEqual(created.returncode, 0, created.stderr)
        return json.loads(created.stdout)

    def record(self, operation, data):
        _saved, run = workflow.load_run(self.directory / "run.json")
        path = self.base / "event.json"
        path.write_text(json.dumps({"target": "local", "operation": operation, "data": data}))
        return self.cli("record", "--event", str(path), "--revision", str(run["revision"]))

    def operation(self, command, *args):
        _, run = workflow.load_run(self.directory / "run.json")
        return self.cli(command, "--target", "local", "--revision", str(run["revision"]), *args)

    def prepare_cli(self):
        result = self.operation("prepare")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["targets"][0]["phase"], "setup")
        result = self.operation("finalize")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["targets"][0]["frozen"]

    def in_process(self, command, *args):
        _, run = workflow.load_run(self.directory / "run.json")
        output = io.StringIO()
        argv = ["workflow.py", "--run", str(self.directory), command, "--target", "local", "--revision", str(run["revision"]), *args]
        with patch.object(sys, "argv", argv), redirect_stdout(output):
            result = workflow.main()
        return result, json.loads(output.getvalue())

    def catalog_plan(self):
        roster = self.base / "roster.md"
        roster.write_text("# Roster\n\n## Profiles\n\n| Spawn as | Provider ID | Model | Lane | Family | Billing | Worktree | Roles | Use for |\n"
                          "|---|---|---|---|---|---|---|---|---|\n| fixture | fixture | model | fixture | family | flat | isolated | reviewer | Review |\n"
                          "| vet | fixture | vet | fixture | vet-family | flat | isolated | reviewer | Vet |\n")
        result = self.operation("catalog", "--roster", str(roster))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = json.loads(result.stdout)["targets"][0]["catalog"]
        proposed = {"required_perspectives": ["family"], "boundary_evidence": "coherent whole patch", "selection_evidence": "one fixture profile",
                    "profiles": {"fixture": {"decision": "selected", "primary": True, "capability": 1, "evidence": "fixture"},
                                 "vet": {"decision": "selected", "primary": False, "capability": 1, "evidence": "independent vet"}},
                    "coverage": {key: {"risk": "noncode", "families": ["family"], "shortfall": None} for key in saved["units"]},
                    "partitioned": False, "interactions": {}, "context": "fixture intent and rules",
                    "routing": {"finder": "fixture", "vetter": "vet", "stand_in": None, "stand_in_capability": None}}
        result = self.record("plan", proposed)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return saved

    def test_setup_intent_is_persisted_before_execution_and_planning_requires_finalization(self):
        self.create("--range", "main...feature")
        self.assertEqual(self.operation("prepare").returncode, 0)
        result = self.record("plan", {"required_perspectives": ["family"], "boundary_evidence": "coherent change", "selection_evidence": "fixture"})
        self.assertEqual(result.returncode, 1, "setup must not admit review planning")
        helper = self.indexes()
        ensure = helper.ensure
        def assert_persisted(root, names, deadline):
            saved, run = workflow.load_run(self.directory / "run.json")
            self.assertEqual(saved["events"][-1]["operation"], "index-setup")
            self.assertEqual(run["targets"]["local"]["index_setup"]["tools"], names)
            ensure(root, names, deadline)
        with patch.object(helper, "ensure", side_effect=assert_persisted):
            result, output = self.in_process("setup", "--tools", "tgrep")
        self.assertEqual(result, 0, output)
        self.assertEqual(output["targets"][0]["phase"], "setup")
        result, output = self.in_process("finalize")
        self.assertEqual(result, 0, output)
        self.assertEqual(output["targets"][0]["phase"], "prepared")

    def test_finalization_race_records_sticky_integrity_evidence(self):
        self.create("--range", "main...feature")
        self.assertEqual(self.operation("prepare").returncode, 0)
        real_finalize = preparation.finalize
        def late_write(*args, **kwargs):
            (self.parent / "feature.txt").write_text("preserve late user edit\n")
            return real_finalize(*args, **kwargs)
        with patch.object(preparation, "finalize", side_effect=late_write):
            result, output = self.in_process("finalize")
        self.assertEqual(result, 2, output)
        saved, run = workflow.load_run(self.directory / "run.json")
        self.assertEqual(saved["events"][-1]["operation"], "integrity")
        self.assertIn("parent", run["targets"]["local"]["integrity"][-1]["components"])
        self.assertIsNone(run["targets"]["local"]["frozen"])
        (self.parent / "feature.txt").write_text("feature\n")
        self.assertEqual(self.operation("finalize").returncode, 2)

    def test_interrupted_setup_retains_intent_and_cannot_bypass_missing_receipts(self):
        self.create("--range", "main...feature")
        self.assertEqual(self.operation("prepare").returncode, 0)
        helper = self.indexes()
        with patch.object(helper, "ensure", side_effect=RuntimeError):
            result, output = self.in_process("setup", "--tools", "zg")
        self.assertEqual((result, output["code"]), (2, "index_setup"))
        _, run = workflow.load_run(self.directory / "run.json")
        self.assertEqual(run["targets"]["local"]["index_setup"]["available"], ["zg"])
        for command, args in (("finalize", ("--static-fallback", "zg", "--evidence", "static inspection")), ("cleanup", ())):
            result, output = self.in_process(command, *args)
            self.assertEqual((result, output["code"]), (2, "index_setup_pending"))
        self.assertEqual(self.operation("setup", "--tools", "tgrep").returncode, 1)
        self.assertEqual(self.in_process("setup", "--tools", "zg")[0], 0)
        _, resumed = workflow.load_run(self.directory / "run.json")
        self.assertEqual(resumed["targets"]["local"]["index_setup"], run["targets"]["local"]["index_setup"])
        self.assertEqual(self.in_process("finalize")[0], 0)

    def test_diff_command_matches_the_sealed_patch_under_hostile_git_configuration(self):
        self.create("--range", "main...feature")
        self.assertEqual(self.operation("prepare").returncode, 0)
        self.assertEqual(self.operation("finalize").returncode, 0)
        _, run = workflow.load_run(self.directory / "run.json")
        frozen = run["targets"]["local"]["frozen"]
        expected = (self.resource(frozen) / "review.patch").read_bytes()
        config = self.base / "hostile-git-config"
        config.write_text("[diff]\n\tnoprefix = true\n")
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_COUNT": "1",
                                   "GIT_CONFIG_KEY_0": "diff.noprefix", "GIT_CONFIG_VALUE_0": "true"}):
            result = self.cli("diff", "--target", "local")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.encode(), expected)
        _, unchanged = workflow.load_run(self.directory / "run.json")
        self.assertEqual(unchanged["revision"], run["revision"])
        (self.parent / "feature.txt").write_text("preserve new user edit\n")
        self.assertEqual(self.cli("diff", "--target", "local").returncode, 2)
        saved, changed = workflow.load_run(self.directory / "run.json")
        self.assertGreater(changed["revision"], run["revision"])
        self.assertEqual(saved["events"][-1]["data"]["components"], ["parent"])

    def test_cli_preparation_authorized_test_copy_check_hold_and_cleanup(self):
        self.create("--range", "main...feature")
        frozen = self.prepare_cli()
        before = (self.directory / "run.json").read_bytes()
        result = self.operation("test-copy")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["code"], "test_authorization")
        self.assertEqual((self.directory / "run.json").read_bytes(), before)
        result = self.record("authorization", {"submission": False, "test_execution": True, "metered_spawns": 0, "evidence": "fixture explicit authorization"})
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.operation("test-copy")
        self.assertEqual(result.returncode, 0, result.stderr)
        test_root = Path(json.loads(result.stdout)["targets"][0]["test_copies"][0])
        (test_root / "generated.txt").write_text("authorized test output\n")
        self.assertEqual(self.cli("check").returncode, 0)
        (self.parent / "user-work.txt").write_text("preserve unexpected change\n")
        result = self.cli("check")
        self.assertEqual(result.returncode, 2, result.stderr)
        _, run = workflow.load_run(self.directory / "run.json")
        self.assertEqual(run["targets"]["local"]["integrity"][-1]["status"], "changed")
        self.assertEqual(workflow.status(run)["targets"][0]["next"]["code"], "source_integrity")
        (self.parent / "user-work.txt").unlink()
        self.assertEqual(self.cli("check").returncode, 2, "a restored file must not erase recorded write evidence")
        _, run = workflow.load_run(self.directory / "run.json")
        result = self.cli("cleanup", "--target", "local", "--revision", str(run["revision"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.resource(frozen).exists())
        self.assertTrue((self.directory / "run.json").exists())

    def test_cli_dispatch_records_check_actual_source_instead_of_accepting_assertions(self):
        self.create("--branch", "feature", "--base", "main")
        self.prepare_cli()
        catalog = self.catalog_plan()
        _, run = workflow.load_run(self.directory / "run.json")
        frozen = run["targets"]["local"]["frozen"]
        data = {"id": "review", "kind": "discovery", "profile": "fixture", "role": "reviewer", "roles": ["reviewer"],
                "model": MODEL, "scope": frozen["inventory"], "required": True, "reason": "fixture boundary", "units": list(catalog["units"]), "interactions": [],
                "workload": {"input_budget_bytes": 100000, "capacity_bytes": None, "evidence": "bounded unknown capacity", "rescope": "split on incomplete evidence"},
                "test_plan": {"commands": [], "root": None, "evidence": "static review"}}
        (Path(frozen["source_root"]) / "feature.txt").write_text("unexpected reviewer edit\n")
        result = self.record("assignment", data)
        self.assertEqual(result.returncode, 2, result.stderr)
        _, run = workflow.load_run(self.directory / "run.json")
        self.assertEqual(run["targets"]["local"]["assignments"], {})
        self.assertEqual(run["targets"]["local"]["integrity"][-1]["status"], "changed")
        self.assertEqual((Path(frozen["source_root"]) / "feature.txt").read_text(), "unexpected reviewer edit\n")

    def test_generated_catalog_preserves_working_hunks_renames_binary_and_staged_only_evidence(self):
        renamed = "renamed\n$(touch should-not-exist).txt"
        self.git("mv", "original.txt", renamed)
        (self.parent / "binary.bin").write_bytes(b"\0\xffbinary\n")
        self.git("add", "binary.bin")
        (self.parent / "feature.txt").write_text("staged-only\n")
        self.git("add", "feature.txt")
        (self.parent / "feature.txt").write_text("feature\n")
        before = preparation.baseline(self.parent)
        self.create("--working")
        frozen = self.prepare_cli()
        catalog = self.catalog_plan()
        resource = Path(frozen["source_root"]).parent
        for label in ("review", "staged", "unstaged"):
            artifact = label + ".patch"
            patch_data = (resource / artifact).read_bytes()
            parts = sorted((unit for unit in catalog["units"].values() if unit["artifact"] == artifact), key=lambda unit: unit["offset"])
            self.assertEqual(b"".join(patch_data[unit["offset"]:unit["offset"] + unit["bytes"]] for unit in parts), patch_data)
        self.assertTrue(any(renamed in unit["paths"] and unit["kind"] == "metadata" for unit in catalog["units"].values()))
        self.assertTrue(any(unit["paths"] == ["binary.bin"] and unit["kind"] == "metadata" for unit in catalog["units"].values()))
        self.assertFalse(any("feature.txt" in unit["paths"] and unit["artifact"] == "review.patch" for unit in catalog["units"].values()))
        self.assertTrue(any("feature.txt" in unit["paths"] and unit["artifact"] == "staged.patch" for unit in catalog["units"].values()))
        self.assertEqual(preparation.baseline(self.parent), before)
        self.assertFalse((self.parent / "should-not-exist").exists())

    def test_cli_catalog_task_and_report_contract_are_self_contained_and_source_checked(self):
        self.create("--range", "main...feature", "--admission-mode", "native")
        frozen = self.prepare_cli()
        catalog = self.catalog_plan()
        unit = {"id": "review", "kind": "discovery", "profile": "fixture", "role": "reviewer", "roles": ["reviewer"],
                "model": MODEL, "scope": frozen["inventory"], "required": True, "reason": "whole change",
                "units": list(catalog["units"]), "interactions": [],
                "workload": {"input_budget_bytes": 100000, "capacity_bytes": None, "evidence": "bounded unknown capacity", "rescope": "split on evidence growth"},
                "test_plan": {"commands": [], "root": None, "evidence": "static inspection adequate"}}
        result = self.record("assignment", unit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        project = self.base / "native-project"
        self.git("clone", "-q", str(self.parent), str(project))
        parent, _, _ = self.native_clone("native-parent", project)
        for name, root in (("native-project", project), ("native-parent", parent)):
            result = self.operation("baseline", "--id", name, "--root", str(root))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        _, run = workflow.load_run(self.directory / "run.json")
        captured = subprocess.run([sys.executable, str(SCRIPTS / "workflow.py"), "--run", str(self.directory),
                                   "native-parent", "--revision", str(run["revision"])],
                                  cwd=parent, text=True, capture_output=True, check=False)
        self.assertEqual(captured.returncode, 0, captured.stdout + captured.stderr)
        started = {"assignment_id": "review", "runtime_handle": "native-handle", "runtime_identity": MODEL,
                   "runtime_profile": "fixture", "runtime_evidence": "separate runtime route metadata"}
        self.assertEqual(self.record("started", started).returncode, 2)
        task = self.operation("task", "--assignment", "review")
        self.assertEqual(task.returncode, 0, task.stdout + task.stderr)
        approval = workflow.load_run(self.directory / "run.json")[1]["targets"]["local"]["assignments"]["review"]["task_ready"]
        self.assertIsNone(approval["capacity"]["parent_active"])
        self.assertIsNone(approval["capacity"]["process_active"])
        self.assertEqual(approval["capacity"]["runtime_parent_root"], str(parent))
        task_text = json.loads(task.stdout)["task"]
        task_command = shlex.split(task_text.splitlines()[1])
        before = (self.directory / "run.json").read_bytes()
        pending = subprocess.run(task_command, text=True, capture_output=True, check=False)
        self.assertEqual(pending.returncode, 2, pending.stdout + pending.stderr)
        self.assertEqual(json.loads(pending.stdout)["code"], "runtime_registration")
        self.assertEqual((self.directory / "run.json").read_bytes(), before)
        self.assertEqual(json.loads(task.stdout)["revision"], workflow.load_run(self.directory / "run.json")[1]["revision"])
        identity = workflow.model_record(MODEL, REGISTRY)
        result = self.record("started", started)
        self.assertEqual(json.loads(result.stdout)["code"], "runtime_copy")
        child, _, _ = self.native_clone("native-handle", project)
        before_registration = (self.directory / "run.json").read_bytes()
        for command in (("check",), ("task", "--target", "local", "--assignment", "review", "--revision",
                                     str(workflow.load_run(self.directory / "run.json")[1]["revision"]))):
            with self.subTest(command=command):
                held = self.cli(*command)
                self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
                self.assertEqual(json.loads(held.stdout)["code"], "runtime_registration")
                self.assertEqual((self.directory / "run.json").read_bytes(), before_registration)
        started["runtime_root"] = str(child)
        result = self.record("started", started)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        proof = workflow.load_run(self.directory / "run.json")[1]["targets"]["local"]["assignments"]["review"]["runtime_copy"]
        self.assertIn("clone_store", proof)
        self.assertEqual(proof["parent_root"], str(parent))
        self.assertEqual(proof["project_root"], str(project))
        released = subprocess.run(task_command, text=True, capture_output=True, check=False)
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        self.assertIn("PRIMARY ASSIGNMENT:", released.stdout)
        self.assertIn("FILE REPORT HANDOFF:", released.stdout)
        payload = {}
        for section in ("report_template", "target", "categories", "diff_command"):
            evidence = subprocess.run([*task_command, "--section", section, "--length", "32768"], text=True, capture_output=True, check=False)
            self.assertEqual(evidence.returncode, 0, evidence.stdout + evidence.stderr)
            heading, body = evidence.stdout.split("\n", 1)
            self.assertIn("next_offset=done", heading)
            payload[section] = json.loads(body)
        self.assertEqual(payload["report_template"]["assignment_id"], "review")
        self.assertEqual(payload["target"], frozen)
        self.assertEqual(set(payload["categories"]), set(workflow.CATEGORIES))
        diff = subprocess.run(["/bin/sh", "-c", payload["diff_command"]], capture_output=True, check=False)
        self.assertEqual(diff.returncode, 0, diff.stderr)
        self.assertTrue(diff.stdout.endswith(b"exit=0\n"), diff.stdout)
        self.assertEqual(diff.stdout[:-7], (Path(frozen["source_root"]).parent / "review.patch").read_bytes())
        report = {"assignment_id": "review", "status": "done", "target": frozen, "reviewer": identity,
                  "checked": frozen["inventory"], "checked_units": list(catalog["units"]), "checked_interactions": [],
                  "categories": {name: {"status": "checked", "evidence": "fixture trace"} for name in workflow.CATEGORIES},
                  "findings": [], "verdicts": [], "verification": {"results": [], "not_run": "static trace adequate"},
                  "coverage_gaps": [], "pre_existing": [], "learned": [], "source_status": "unchanged", "evidence": "frozen code inspected"}
        result = self.record("report", report)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.record("complete", {"coverage_evidence": "all generated units and categories reconciled"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["targets"][0]["next"]["status"], "complete")
        replayed = workflow.load_run(self.directory / "run.json")[1]
        self.assertEqual(replayed["targets"]["local"]["assignments"]["review"]["runtime_copy"]["root"], str(child))

    def test_legacy_freeze_during_setup_reports_missing_preparation_explicitly(self):
        self.create("--range", "main...feature")
        self.assertEqual(self.operation("prepare").returncode, 0)
        run = workflow.load_run(self.directory / "run.json")[1]
        legacy = dict(run["targets"]["local"]["setup"])
        del legacy["preparation"]
        result = self.record("freeze", legacy)
        self.assertEqual(result.returncode, 1)
        self.assertIn("finalization requires owned preparation evidence", result.stderr)
        self.assertEqual(workflow.load_run(self.directory / "run.json")[1]["targets"]["local"]["phase"], "setup")

    def test_catalog_rejects_changed_patch_and_cannot_be_submitted_as_manual_evidence(self):
        self.create("--range", "main...feature")
        frozen = self.prepare_cli()
        result = self.record("catalog", {})
        self.assertEqual(result.returncode, 1)
        self.assertIn("dedicated command", result.stderr)
        roster = self.base / "roster.md"
        roster.write_text("unused because integrity holds first")
        (Path(frozen["source_root"]).parent / "review.patch").write_bytes(b"truncated")
        result = self.operation("catalog", "--roster", str(roster))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["code"], "source_integrity")

    def test_empty_comparison_catalog_does_not_invent_hunks(self):
        self.create("--working")
        frozen = self.prepare_cli()
        catalog = self.catalog_plan()
        self.assertEqual(frozen["inventory"], [])
        self.assertEqual(catalog["units"], {})


if __name__ == "__main__":
    unittest.main()
