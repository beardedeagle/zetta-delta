#!/usr/bin/env python3
"""Offline filesystem-target, provenance and no-publication regressions."""

import copy
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "review/scripts"))
import preparation
import workflow

SPEC = importlib.util.spec_from_file_location("workflow_fixtures", ROOT / "tests/pr-review-workflow-test.py")
assert SPEC is not None and SPEC.loader is not None
FIXTURES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURES)


class ProjectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="project-review-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.enterContext(patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.base / "cache")}))
        self.source = self.base / "plain project"
        self.source.mkdir()
        (self.source / "api.py").write_text("def run():\n    return 1\n")
        self.directory = self.base / "run"
        self.directory.mkdir(mode=0o700)

    def run_record(self, roots=None, exclusions=None, method="pr-review"):
        collection = workflow.project_collection(roots or [str(self.source)], exclusions or [])
        return workflow.new_run(method, collection, FIXTURES.REGISTRY, FIXTURES.MODEL_C, "project-thread")

    def prepared(self, roots=None, exclusions=None, method="pr-review"):
        run = self.run_record(roots, exclusions, method)
        for key, target in list(run["targets"].items()):
            staged = preparation.prepare(self.directory, run["run_id"], key, target, Path(target["repository"]))
            run = workflow.apply_event(run, FIXTURES.event("prepare", staged, key))
            frozen = preparation.finalize(self.directory, run["run_id"], key, staged, None, [], None)
            run = workflow.apply_event(run, FIXTURES.event("freeze", frozen, key))
        return run

    def test_aliases_deduplicate_but_nested_and_identical_roots_remain_independent(self):
        alias = self.base / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        nested = self.source / "nested"
        nested.mkdir()
        other = self.base / "other"
        other.mkdir()
        for root in (nested, other):
            (root / "api.py").write_bytes((self.source / "api.py").read_bytes())
        run = self.run_record([str(self.source), str(alias), str(nested), str(other)])
        self.assertEqual(len(run["targets"]), 3)
        self.assertEqual(len(run["intake"]["projects"]), 4)
        self.assertIsNone(run["owner"])
        first = next(iter(run["targets"].values()))
        self.assertEqual(first["project"]["requested_roots"], [str(self.source), str(alias)])
        case_alias = self.source.with_name(self.source.name.upper())
        if case_alias.exists() and case_alias.samefile(self.source):
            run = self.run_record([str(self.source), str(case_alias)])
            self.assertEqual(len(run["targets"]), 1)
            self.assertEqual(len(run["intake"]["projects"]), 2)

    def test_failed_requested_root_stays_visible_and_prevents_overall_completion(self):
        run = self.run_record([str(self.source), str(self.base / "missing")])
        self.assertEqual(len(run["targets"]), 2)
        rows = workflow.status(run)
        self.assertFalse(rows["complete"])
        self.assertIn("project_source", {row["next"].get("code") for row in rows["targets"]})

    def test_capture_includes_ignored_binary_modes_empty_directories_and_safe_links(self):
        (self.source / ".gitignore").write_text("ignored.py\n")
        (self.source / "ignored.py").write_text("still source\n")
        (self.source / "data.bin").write_bytes(b"\x00\xff")
        (self.source / "empty").mkdir()
        (self.source / "api.py").chmod(0o755)
        (self.source / "link.py").symlink_to("api.py")
        (self.source / "outside").symlink_to("../external")
        os.mkfifo(self.source / "pipe")
        with patch.object(preparation, "git", side_effect=AssertionError("plain projects must not require Git")):
            run = self.prepared()
            key, target = next(iter(run["targets"].items()))
            frozen = target["frozen"]
            self.assertNotIn("head_sha", frozen)
            self.assertNotIn("base_sha", frozen)
            snapshot = Path(frozen["source_root"])
            self.assertEqual((snapshot / "data.bin").read_bytes(), b"\x00\xff")
            self.assertTrue((snapshot / "empty").is_dir())
            self.assertEqual(stat.S_IMODE((snapshot / "api.py").stat().st_mode), 0o755)
            self.assertEqual(os.readlink(snapshot / "link.py"), "api.py")
            self.assertIn("ignored.py", frozen["inventory"])
            self.assertFalse((snapshot / "outside").is_symlink())
            self.assertFalse((snapshot / "pipe").exists())
            self.assertEqual(set(frozen["manifest"]["gaps"]), {"outside", "pipe"})
            self.assertEqual(preparation.check(self.directory, run["run_id"], key, frozen, {})["status"], "unchanged")
            copy_root = preparation.test_copy(self.directory, run["run_id"], key, frozen)
            self.assertEqual((copy_root / "api.py").read_bytes(), (snapshot / "api.py").read_bytes())
            self.assertFalse((copy_root / ".git").exists())
            removed = preparation.cleanup(self.directory, run["run_id"], key)
            self.assertEqual(removed, [frozen["preparation"]["resource"]])
        self.assertTrue((self.source / "api.py").exists())

    def codegraph_cache(self):
        cache = self.source / ".codegraph"
        cache.mkdir(mode=0o700)
        connection = sqlite3.connect(cache / "codegraph.db")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE symbols (name TEXT)")
        connection.commit()
        return cache / "codegraph.db-shm"

    def test_disposable_indexes_are_pruned_and_never_block_new_source_checks(self):
        self.codegraph_cache()
        for name in (".tgrep", ".zvec-grep"):
            cache = self.source / name
            cache.mkdir()
            (cache / "index.bin").write_bytes(b"old cache")
        run = self.prepared()
        key, target = next(iter(run["targets"].items()))
        frozen = target["frozen"]
        self.assertEqual(target["coverage_gaps"], {})
        for name in preparation.INDEX_DIRECTORIES:
            self.assertIn(name, frozen["manifest"]["excluded"])
            self.assertFalse(any(path == name or path.startswith(name + "/") for path in frozen["inventory"]))
            cache = self.source / name
            (cache / "changed.bin").write_bytes(b"disposable index churn")
        self.assertEqual(preparation.check(self.directory, run["run_id"], key, frozen, {})["status"], "unchanged")
        (self.source / "api.py").write_text("changed source\n")
        self.assertIn("parent", preparation.check(self.directory, run["run_id"], key, frozen, {})["components"])

    def test_index_reason_does_not_hide_explicit_source_omissions(self):
        (self.source / ".codegraph").write_text("ordinary source file\n")
        for path, reason in ((path, reason) for path in ("api.py", ".codegraph")
                             for reason in (preparation.INDEX_REASON, "Git metadata")):
            with self.subTest(path=path, reason=reason):
                run = self.prepared(exclusions=[[path, reason]])
                target = next(iter(run["targets"].values()))
                self.assertTrue(any(gap["material"] and gap["reason"].startswith(path + ":")
                                    for gap in target["coverage_gaps"].values()))

    def test_explicit_descendants_of_omitted_indexes_keep_material_coverage_gaps(self):
        cache = self.source / ".codegraph"
        cache.mkdir()
        (cache / "source.py").write_text("explicitly omitted source\n")
        for path in (".codegraph/source.py", ".codegraph/absent.py"):
            with self.subTest(path=path):
                run = self.prepared(exclusions=[[path, "owner excluded this source"]])
                target = next(iter(run["targets"].values()))
                self.assertTrue(any(gap["material"] and gap["reason"].startswith(".codegraph:")
                                    for gap in target["coverage_gaps"].values()))

    def test_old_hashed_unreviewed_indexes_preserve_artifacts_and_source_guards(self):
        shm = self.codegraph_cache()
        run = self.run_record(exclusions=[[".codegraph", "generated index"]])
        key, target = next(iter(run["targets"].items()))
        original = preparation.project_baseline

        def legacy_capture(root, exclude=(), **kwargs):
            kwargs.update(exclude_indexes=False, exclude_generated=False)
            return original(root, () if root == self.source else exclude, **kwargs)

        with patch.object(preparation, "project_baseline", side_effect=legacy_capture):
            staged = preparation.prepare(self.directory, run["run_id"], key, target, self.source)
        artifact = Path(staged["source_root"]).parent / "parent-baseline.json"
        before = artifact.read_bytes()
        self.assertIn(".codegraph/codegraph.db", json.loads(before)["files"])
        self.assertIn("cache_identity", json.loads(before))
        for name in ("codegraph.db", "codegraph.db-wal", "codegraph.db-shm"):
            path = shm.with_name(name)
            path.write_bytes(path.read_bytes() + b"changed disposable bytes")
        real_fwalk = preparation.os.fwalk

        def guarded_walk(root, *args, **kwargs):
            for row in real_fwalk(root, *args, **kwargs):
                self.assertFalse(Path(row[0]).is_relative_to(self.source / ".codegraph"))
                yield row

        with patch.object(preparation.os, "fwalk", side_effect=guarded_walk):
            self.assertEqual(preparation.check(self.directory, run["run_id"], key, staged, {})["status"], "unchanged")
        self.assertEqual(artifact.read_bytes(), before)
        source_file = Path(staged["source_root"]) / "api.py"
        source_bytes = source_file.read_bytes()
        source_file.write_bytes(source_bytes + b"changed reviewed source")
        self.assertIn("source", preparation.check(self.directory, run["run_id"], key, staged, {})["components"])
        source_file.write_bytes(source_bytes)
        artifact.write_text("{}")
        self.assertNotEqual(preparation.check(self.directory, run["run_id"], key, staged, {})["status"], "unchanged")
        artifact.write_bytes(before)

    def test_old_omitted_indexes_do_not_require_hash_backfill_or_artifact_replacement(self):
        self.codegraph_cache()
        run = self.run_record(exclusions=[[".codegraph", "generated index"]])
        key, target = next(iter(run["targets"].items()))
        original = preparation.project_baseline

        def legacy_capture(root, exclude=(), **kwargs):
            kwargs.update(cache_recovery=False, exclude_indexes=False, exclude_generated=False)
            return original(root, exclude, **kwargs)

        with patch.object(preparation, "project_baseline", side_effect=legacy_capture):
            staged = preparation.prepare(self.directory, run["run_id"], key, target, self.source)
        artifact = Path(staged["source_root"]).parent / "parent-baseline.json"
        before = artifact.read_bytes()
        self.assertNotIn(".codegraph/codegraph.db", json.loads(before)["files"])
        (self.source / ".codegraph/codegraph.db").write_bytes(b"new disposable database")
        self.assertEqual(preparation.check(self.directory, run["run_id"], key, staged, {})["status"], "unchanged")
        self.assertEqual(artifact.read_bytes(), before)

    def test_manifest_listed_index_looking_source_remains_strict(self):
        cache = self.source / ".codegraph"
        cache.mkdir()
        (cache / "source.py").write_text("reviewed source\n")
        original_inventory = preparation.project_inventory
        original_baseline = preparation.project_baseline

        def legacy_inventory(root, exclusions=None, destination=None, **kwargs):
            kwargs["exclude_indexes"] = False
            return original_inventory(root, exclusions, destination, **kwargs)

        def legacy_baseline(root, exclude=(), **kwargs):
            kwargs["exclude_indexes"] = False
            return original_baseline(root, exclude, **kwargs)

        with (patch.object(preparation, "project_inventory", side_effect=legacy_inventory),
              patch.object(preparation, "project_baseline", side_effect=legacy_baseline)):
            run = self.prepared()
        key, target = next(iter(run["targets"].items()))
        frozen = target["frozen"]
        self.assertIn(".codegraph/source.py", frozen["manifest"]["entries"])
        (self.source / ".tgrep").mkdir()
        (self.source / ".tgrep/index").write_text("unreviewed generated index")
        self.assertEqual(preparation.check(self.directory, run["run_id"], key, frozen, {})["status"], "unchanged")
        for root in (self.source, Path(frozen["source_root"])):
            path = root / ".codegraph/source.py"
            before = path.read_bytes()
            path.write_bytes(before + b"changed")
            self.assertNotEqual(preparation.check(self.directory, run["run_id"], key, frozen, {})["status"], "unchanged")
            path.write_bytes(before)

    def test_disposable_nested_git_metadata_is_ignored_without_hiding_reviewed_git(self):
        before = {"files": {"api.py": ["file", 0o644, "a" * 64],
                             ".zvec-grep": ["directory", 0o700],
                             ".zvec-grep/corpus": ["directory", 0o700],
                             ".zvec-grep/corpus/source.py": ["file", 0o644, "b" * 64]},
                  "git": {"head": "c" * 40, "refs": "d" * 64,
                          "nested": {".zvec-grep/corpus": {"head": "e" * 40}}},
                  "root_identity": [1, 2, 0o700]}
        after: dict = {"files": {"api.py": before["files"]["api.py"]},
                 "git": {"head": "c" * 40, "refs": "d" * 64}, "root_identity": [1, 2, 0o700]}
        self.assertEqual(preparation.reviewed_ledger(before, ("api.py",)), preparation.reviewed_ledger(after, ("api.py",)))
        reviewed = ("api.py", ".zvec-grep/corpus/source.py")
        self.assertNotEqual(preparation.reviewed_ledger(before, reviewed), preparation.reviewed_ledger(after, reviewed))
        for field, value in (("head", "f" * 40), ("refs", "f" * 64)):
            changed = copy.deepcopy(after)
            changed["git"][field] = value
            self.assertNotEqual(preparation.reviewed_ledger(before, ("api.py",)), preparation.reviewed_ledger(changed, ("api.py",)))
        after["root_identity"][1] = 3
        self.assertNotEqual(preparation.reviewed_ledger(before, ("api.py",)), preparation.reviewed_ledger(after, ("api.py",)))

    def test_exclusions_require_rationale_and_remain_visible(self):
        generated = self.source / "generated"
        generated.mkdir()
        (generated / "out").write_text("generated\n")
        with self.assertRaises(ValueError):
            self.run_record(exclusions=[["generated", ""]])
        run = self.prepared(exclusions=[["generated", "generated build output"]])
        frozen = next(iter(run["targets"].values()))["frozen"]
        self.assertEqual(frozen["manifest"]["excluded"]["generated"], "generated build output")
        self.assertNotIn("generated/out", frozen["inventory"])

    def test_generated_and_explicit_outputs_are_pruned_before_capture_and_rechecks(self):
        omitted = ("target", "nested/node_modules", "environments/custom", "custom-out")
        for relative in omitted:
            path = self.source / relative
            path.mkdir(parents=True)
            (path / "output.bin").write_bytes(b"generated")
        (self.source / "environments/custom/pyvenv.cfg").write_text("home = /fixture/python\n")
        included = self.source / "environment-source"
        included.mkdir()
        (included / "module.py").write_text("source\n")
        real_fwalk = preparation.os.fwalk

        def guarded_walk(root, *args, **kwargs):
            for row in real_fwalk(root, *args, **kwargs):
                if Path(root) == self.source:
                    self.assertFalse(any(Path(row[0]).is_relative_to(self.source / name) for name in omitted))
                yield row

        with patch.object(preparation.os, "fwalk", side_effect=guarded_walk):
            run = self.prepared(exclusions=[["custom-out", "configured build output"]])
            key, target = next(iter(run["targets"].items()))
            frozen = target["frozen"]
            record = preparation.capture_baseline(self.directory, "owner", self.source, (), filesystem=True,
                                                   exclude=("custom-out",), exclude_generated=True)
            self.assertEqual(record["exclude"], ["custom-out"])
            self.assertTrue(record["exclude_generated"])
            self.assertTrue(set(omitted) <= set(frozen["manifest"]["excluded"]))
            self.assertIn("environment-source/module.py", frozen["inventory"])
            for relative in omitted:
                (self.source / relative / "output.bin").write_bytes(b"new generated bytes")
            self.assertEqual(preparation.check(self.directory, run["run_id"], key, frozen,
                                              {"owner": record})["status"], "unchanged")
            (included / "module.py").write_text("changed source\n")
            self.assertEqual(preparation.check(self.directory, run["run_id"], key, frozen,
                                              {"owner": record})["status"], "changed")
            (included / "module.py").write_text("source\n")
            frozen_output = Path(frozen["source_root"]) / "target"
            frozen_output.mkdir()
            (frozen_output / "late.py").write_text("unexpected frozen bytes\n")
            self.assertEqual(preparation.check(self.directory, run["run_id"], key, frozen,
                                              {"owner": record})["status"], "changed")

    def test_generated_pruning_preserves_explicit_reviewed_paths_and_ancestors(self):
        names = ("target/nested/source.py", "environment/nested/source.py")
        for name in names:
            path = self.source / name
            path.parent.mkdir(parents=True)
            path.write_text("reviewed source\n")
        (self.source / "environment/pyvenv.cfg").write_text("fixture environment\n")
        omitted = self.source / "node_modules"
        omitted.mkdir()
        (omitted / "output").write_text("disposable output\n")
        reviewed = tuple(names)
        destination = self.base / "copy"
        destination.mkdir()
        original_fwalk = preparation.os.fwalk

        def guarded_walk(root, *args, **kwargs):
            for row in original_fwalk(root, *args, **kwargs):
                self.assertFalse(Path(row[0]).is_relative_to(omitted))
                yield row

        with patch.object(preparation.os, "fwalk", side_effect=guarded_walk):
            inventory = preparation.project_inventory(self.source, destination=destination,
                                                       exclude_generated=True, reviewed=reviewed)
        ledger = preparation.files(self.source, exclude_generated=True, reviewed=reviewed)
        for name in names:
            self.assertIn(name, inventory["entries"])
            self.assertIn(name, ledger)
            self.assertEqual((destination / name).read_bytes(), (self.source / name).read_bytes())
            for ancestor in Path(name).parents:
                if ancestor != Path("."):
                    self.assertIn(ancestor.as_posix(), inventory["entries"])
                    self.assertIn(ancestor.as_posix(), ledger)
        self.assertEqual(inventory["excluded"]["node_modules"], preparation.GENERATED_REASON)
        self.assertNotIn("node_modules/output", ledger)

    def test_legacy_project_baselines_keep_generated_content_strict(self):
        output = self.source / "target"
        output.mkdir()
        (output / "file").write_text("old\n")
        before = preparation.project_baseline(self.source)
        self.assertIn("target/file", before["files"])
        self.assertNotIn("exclusion_policy", before)
        (output / "file").write_text("new\n")
        self.assertNotEqual(preparation.project_baseline(self.source), before)

    def test_explicit_legacy_policy_reproduces_original_bytes_without_replacing_artifacts(self):
        output = self.source / "target"
        output.mkdir()
        (output / "file").write_text("generated\n")
        run = self.run_record(exclusions=[["target", "build output"]])
        key, target = next(iter(run["targets"].items()))
        original = preparation.project_baseline

        def unversioned(root, exclude=(), **kwargs):
            kwargs.pop("exclude_generated", None)
            kwargs.pop("exclude_indexes", None)
            return original(root, exclude, **kwargs)

        with patch.object(preparation, "project_baseline", side_effect=unversioned):
            staged = preparation.prepare(self.directory, run["run_id"], key, target, self.source)
        artifact = Path(staged["source_root"]).parent / "parent-baseline.json"
        before = artifact.read_bytes()
        policy = {str(self.source): {"fingerprint": staged["parent_fingerprint"],
                                    "exclude": ["target"], "exclude_generated": True}}
        check = lambda candidate=policy: preparation.check(self.directory, run["run_id"], key, staged, {},
                                                           legacy_policies=candidate)
        self.assertEqual(preparation.check(self.directory, run["run_id"], key, staged, {})["status"], "changed")
        self.assertEqual(check()["status"], "unchanged")
        self.assertEqual(artifact.read_bytes(), before)
        (self.source / "api.py").write_text("changed included source\n")
        self.assertEqual(check()["status"], "changed")
        wrong = copy.deepcopy(policy)
        wrong[str(self.source)]["fingerprint"] = "0" * 64
        self.assertEqual(check(wrong)["status"], "unknown")
        wrong = {str(self.source / "missing"): next(iter(policy.values()))}
        self.assertEqual(check(wrong)["status"], "unknown")

    def test_legacy_policy_cannot_hide_originally_protected_output_or_override_modern_policy(self):
        output = self.source / "target"
        output.mkdir()
        (output / "file").write_text("protected legacy bytes\n")
        run = self.run_record(exclusions=[["target", "build output"]])
        key, target = next(iter(run["targets"].items()))
        original = preparation.project_baseline

        def strict_legacy(root, exclude=(), **kwargs):
            kwargs.pop("exclude_generated", None)
            kwargs.pop("exclude_indexes", None)
            return original(root, (), **kwargs)

        with patch.object(preparation, "project_baseline", side_effect=strict_legacy):
            staged = preparation.prepare(self.directory, run["run_id"], key, target, self.source)
        policy = {str(self.source): {"fingerprint": staged["parent_fingerprint"],
                                    "exclude": ["target"], "exclude_generated": True}}
        self.assertEqual(preparation.check(self.directory, run["run_id"], key, staged, {},
                                          legacy_policies=policy)["status"], "changed")
        modern = self.prepared(exclusions=[["target", "build output"]])
        modern_key, modern_target = next(iter(modern["targets"].items()))
        policy[str(self.source)]["fingerprint"] = modern_target["frozen"]["parent_fingerprint"]
        self.assertEqual(preparation.check(self.directory, modern["run_id"], modern_key, modern_target["frozen"], {},
                                          legacy_policies=policy)["status"], "unknown")

    def test_manifest_snapshot_and_original_mutations_are_detected(self):
        run = self.prepared()
        key, target = next(iter(run["targets"].items()))
        frozen = target["frozen"]
        for path in (Path(frozen["source_root"]) / "api.py", self.source / "api.py",
                     Path(frozen["source_root"]).parent / "manifest.json"):
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            self.assertNotEqual(preparation.check(self.directory, run["run_id"], key, frozen, {})["status"], "unchanged")
            path.write_bytes(original)
        self.assertEqual(preparation.check(self.directory, run["run_id"], key, frozen, {})["status"], "unchanged")

    def test_publication_is_rejected_before_external_reads_for_every_project(self):
        run = self.prepared()
        key, target = next(iter(run["targets"].items()))
        with patch.object(workflow.intake, "api", side_effect=AssertionError("no GitHub calls")):
            with self.assertRaisesRegex(preparation.NeedsDecision, "project"):
                workflow.publication.endpoint(target)
            for operation in ("publication-context", "publication-intent", "publication-receipt"):
                with self.subTest(operation=operation), self.assertRaisesRegex(preparation.NeedsDecision, "project"):
                    workflow.apply_event(run, FIXTURES.event(operation, {}, key))

    def test_live_repairs_do_not_change_frozen_reads_or_clear_snapshot_tampering(self):
        key, _ = self.issued()
        original = (self.source / "api.py").read_bytes()
        (self.source / "api.py").write_text("# concurrent owner repair\n")
        code, output = self.cli(self.directory, "source-read", "--target", key, "--assignment", "review", "--path", "api.py")
        self.assertEqual(code, 0, output)
        self.assertEqual(output["content"].encode(), original)
        _, run = workflow.load_run(self.directory / "run.json")
        observation = run["targets"][key]["integrity"][-1]
        self.assertEqual(observation["status"], "unchanged")
        self.assertEqual(observation["live_parent_changes"]["parent"]["status"], "changed")
        self.assertEqual((self.source / "api.py").read_text(), "# concurrent owner repair\n")
        snapshot = Path(run["targets"][key]["frozen"]["source_root"]) / "api.py"
        snapshot.write_text("# changed review input\n")
        code, errors = self.cli(self.directory, "source-read", "--target", key, "--assignment", "review", "--path", "api.py")
        self.assertEqual((code, errors["code"]), (2, "source_integrity"))
        snapshot.write_bytes(original)
        code, errors = self.cli(self.directory, "source-read", "--target", key, "--assignment", "review", "--path", "api.py")
        self.assertEqual((code, errors["code"]), (2, "source_integrity"))

    def test_unavailable_live_parent_does_not_skip_protected_snapshot_checks(self):
        key, _ = self.issued()
        _, run = workflow.load_run(self.directory / "run.json")
        snapshot = Path(run["targets"][key]["frozen"]["source_root"]) / "api.py"
        self.source.rename(self.base / "moved owner checkout")
        snapshot.write_text("# changed review input\n")
        code, errors = self.cli(self.directory, "source-read", "--target", key, "--assignment", "review", "--path", "api.py")
        self.assertEqual((code, errors["code"]), (2, "source_integrity"))
        _, run = workflow.load_run(self.directory / "run.json")
        observation = run["targets"][key]["integrity"][-1]
        self.assertEqual(observation["live_parent_changes"]["parent"]["status"], "unknown")
        self.assertIn("source", observation["components"])

    def issued(self, roots=None, cross=False):
        run = self.catalogued(roots)
        key, target = next(iter(run["targets"].items()))
        run = workflow.apply_event(run, FIXTURES.event("plan", self.proposed(run, key, cross), key))
        assignment = FIXTURES.assignment(scope=target["frozen"]["inventory"], units=list(target["catalog"]["units"]),
                                         interactions=list(run["targets"][key]["plan"]["interactions"]))
        run = workflow.apply_event(run, FIXTURES.event("assignment", assignment, key))
        baseline = preparation.capture_baseline(self.directory, "owner", self.source, (), filesystem=True,
                                                 exclude_generated=True)
        run = workflow.apply_event(run, FIXTURES.event("baseline", baseline, key))
        initial = {"method": run["method"], "intake": run["intake"], "registry": run["registry"],
                   "orchestrator": FIXTURES.MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"]}
        initial["runtime"] = workflow.runtime.identity()
        saved = {"version": 1, "initial": initial, "events": run["events"]}
        workflow.write_json(self.directory / "run.json", saved)
        workflow.issue_handoff(self.directory, saved, run, key, "review", {
            "parent_active": 0, "process_active": 0, "evidence": "offline fixture; no native launch",
            "runtime_parent_root": str(self.source)})
        saved, run = workflow.load_run(self.directory / "run.json")
        workflow.append_record(self.directory, saved, run, key, "started", FIXTURES.start())
        return key, assignment

    def read_source(self, key, path, offset=0, length=16384):
        saved, run = workflow.load_run(self.directory / "run.json")
        return workflow.source_read(self.directory, saved, run, key, "review", path, offset, length, None)

    def author_project(self, key, assignment):
        saved, run = workflow.load_run(self.directory / "run.json")
        judgments = FIXTURES.report()
        judgments.update(checked=assignment["scope"], checked_units=assignment["units"], checked_interactions=assignment["interactions"])
        for name in ("assignment_id", "target", "reviewer", "related_targets"):
            judgments.pop(name, None)
        path = self.base / "judgments.json"
        path.write_text(json.dumps(judgments))
        return workflow.author_report(self.directory, saved, run, key, "review", path)

    def test_project_completion_requires_read_receipts_for_document_tails(self):
        (self.source / "README.md").write_text("x" * 100)
        key, assignment = self.issued()
        self.read_source(key, "api.py")
        first = self.read_source(key, "README.md", 0, 80)
        self.assertEqual(first["next_offset"], 80)
        draft = self.author_project(key, assignment)
        self.assertEqual(draft["code"], "report_draft")
        self.assertTrue(any(error["code"] == "source_coverage" for error in draft["errors"]))
        self.read_source(key, "README.md", 0, 80)
        self.assertEqual(self.author_project(key, assignment)["code"], "report_draft")
        self.assertIsNone(self.read_source(key, "README.md", 80, 20)["next_offset"])
        self.assertNotIn("errors", self.author_project(key, assignment))

    def test_source_reads_are_bounded_nonexecuting_and_bind_retained_bytes(self):
        (self.source / "payload.bin").write_bytes(b"\xff\x00\xa1")
        key, assignment = self.issued()
        before = (self.directory / "run.json").read_bytes()
        for name, offset, length in (("../judgments.json", 0, 10), ("api.py", -1, 10), ("api.py", 0, 32769)):
            with self.subTest(name=name, offset=offset), self.assertRaises(ValueError):
                self.read_source(key, name, offset, length)
            self.assertEqual((self.directory / "run.json").read_bytes(), before)
        with patch.object(workflow.subprocess, "run", side_effect=AssertionError("source reads must not execute tools")):
            result = self.read_source(key, "api.py")
        self.assertEqual(result["content"], (self.source / "api.py").read_text())
        binary = self.read_source(key, "payload.bin")
        self.assertEqual((binary["encoding"], binary["content"]), ("base64", "/wCh"))
        payload = json.loads(workflow.handoff.read(self.directory, result["receipt"]))
        self.assertEqual(payload["file_sha256"], hashlib.sha256((self.source / "api.py").read_bytes()).hexdigest())
        (self.directory / result["receipt"]["artifact"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            self.author_project(key, assignment)

    def test_read_coverage_carries_only_within_the_same_native_conversation(self):
        key, assignment = self.issued()
        self.read_source(key, "api.py")
        _, run = workflow.load_run(self.directory / "run.json")
        original = run["targets"][key]
        for same_handle in (True, False):
            with self.subTest(same_handle=same_handle):
                target = copy.deepcopy(original)
                row = copy.deepcopy(target["assignments"]["review"])
                row.update(id="review-c1", source_reads=[], recovery={"assignment_id": "review", "kind": "correction"})
                if not same_handle:
                    row["runtime_handle"] = "different-conversation"
                target["assignments"][row["id"]] = row
                reply = FIXTURES.report(row["id"])
                reply.update(target=target["frozen"], reviewer=row["actual_identity"], checked=assignment["scope"],
                             checked_units=assignment["units"], checked_interactions=[], related_targets={})
                if same_handle:
                    workflow.accept_report(target, row, reply)
                    self.assertEqual(row["status"], "done")
                else:
                    with self.assertRaises(preparation.NeedsDecision) as error:
                        workflow.accept_report(target, row, reply)
                    self.assertEqual(error.exception.code, "source_coverage")

    def test_source_read_cli_records_delivery_without_changing_the_source(self):
        key, assignment = self.issued()
        original = (self.source / "api.py").read_bytes()
        argv = ["workflow.py", "--run", str(self.directory), "source-read", "--target", key,
                "--assignment", "review", "--path", "api.py", "--length", "8"]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), redirect_stdout(output):
            self.assertEqual(workflow.main(), 0)
        data = json.loads(output.getvalue())
        self.assertEqual((data["status"], data["content"], data["next_offset"]), ("read", original[:8].decode(), 8))
        self.assertEqual((self.source / "api.py").read_bytes(), original)
        self.assertEqual(data["revision"], workflow.load_run(self.directory / "run.json")[1]["revision"])
        before = (self.directory / "run.json").read_bytes()
        event = json.loads(before)["events"][-1]
        self.assertEqual(event["operation"], "source-read")
        event_path = self.base / "forged-read.json"
        event_path.write_text(json.dumps(event))
        argv = ["workflow.py", "--run", str(self.directory), "record", "--event", str(event_path),
                "--revision", str(data["revision"])]
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(workflow.main(), 1)
        self.assertEqual((self.directory / "run.json").read_bytes(), before)
        self.assertEqual(self.author_project(key, assignment)["code"], "report_draft")

    def test_related_snapshot_reads_cannot_satisfy_the_owning_file_coverage(self):
        other = self.base / "consumer"
        other.mkdir()
        (other / "api.py").write_bytes((self.source / "api.py").read_bytes())
        key, assignment = self.issued([str(self.source), str(other)], cross=True)
        saved, run = workflow.load_run(self.directory / "run.json")
        foreign = next(name for name in run["targets"] if name != key)
        read = workflow.source_read(self.directory, saved, run, key, "review", "api.py", 0, 16384, foreign)
        self.assertEqual(read["source_target"], foreign)
        self.assertEqual(self.author_project(key, assignment)["code"], "report_draft")
        self.read_source(key, "api.py")
        self.assertNotIn("errors", self.author_project(key, assignment))

    def catalogued(self, roots=None, method="pr-review"):
        run = self.prepared(roots, method=method)
        for key, target in list(run["targets"].items()):
            catalog = dict(copy.deepcopy(FIXTURES.CATALOG), units=workflow.contract.project_units(target["frozen"]))
            run = workflow.apply_event(run, FIXTURES.event("catalog", catalog, key))
        return run

    def proposed(self, run, key, cross=False):
        target = run["targets"][key]
        proposed = FIXTURES.plan()
        proposed["coverage"] = {unit: {"risk": "code", "families": ["family-a"], "shortfall": None}
                                for unit in target["catalog"]["units"]}
        proposed["cross_project"] = {"required": cross, "evidence": "explicit assessment of project contracts"}
        if cross:
            proposed["interactions"] = {"api-contract": {"question": "trace producer and consumer contracts",
                "units": list(target["catalog"]["units"]), "families": ["family-a"], "shortfall": None,
                "targets": {name: {"frozen": row["frozen"], "units": list(row["catalog"]["units"])}
                            for name, row in run["targets"].items()}}}
        return proposed

    def test_catalog_covers_real_files_and_rejects_wrong_metadata(self):
        run = self.catalogued()
        target = next(iter(run["targets"].values()))
        self.assertEqual({path for unit in target["catalog"]["units"].values() for path in unit["paths"]}, {"api.py"})
        broken = copy.deepcopy(target["catalog"])
        next(iter(broken["units"].values()))["bytes"] += 1
        with self.assertRaisesRegex(ValueError, "project"):
            workflow.contract.validate_catalog(broken, target["frozen"], run["registry"])

    def test_cross_project_plan_and_task_bind_every_snapshot(self):
        other = self.base / "consumer"
        other.mkdir()
        (other / "api.py").write_text("from producer import run\n")
        run = self.catalogued([str(self.source), str(other)])
        key = next(iter(run["targets"]))
        proposed = self.proposed(run, key, True)
        broken = copy.deepcopy(proposed)
        foreign = next(name for name in run["targets"] if name != key)
        broken["interactions"]["api-contract"]["targets"][foreign]["frozen"]["manifest_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "project"):
            workflow.apply_event(run, FIXTURES.event("plan", broken, key))
        run = workflow.apply_event(run, FIXTURES.event("plan", proposed, key))
        target = run["targets"][key]
        assignment = FIXTURES.assignment("project-discovery", scope=target["frozen"]["inventory"],
                                         units=list(target["catalog"]["units"]), interactions=["api-contract"])
        run = workflow.apply_event(run, FIXTURES.event("assignment", assignment, key))
        task = workflow.contract.task_block(run, key, assignment["id"], replay=True)
        payload = json.JSONDecoder().raw_decode(task["task"].split("\n", 1)[1])[0]
        self.assertEqual(set(payload["related_targets"]), {foreign})
        self.assertEqual(payload["related_targets"][foreign], run["targets"][foreign]["frozen"])
        self.assertNotIn("source_git_prefix", payload)
        self.assertNotIn("diff_command", payload)
        self.assertIn("Existing defects are in scope", task["task"])
        judgments = FIXTURES.report(assignment["id"])
        for field in ("assignment_id", "target", "reviewer"):
            judgments.pop(field)
        bound = workflow.handoff.assemble(task["report_template"], judgments)
        self.assertEqual(bound["related_targets"], payload["related_targets"])
        with self.assertRaisesRegex(ValueError, "omit"):
            workflow.handoff.assemble(task["report_template"], dict(judgments, related_targets={}))
        with self.assertRaisesRegex(preparation.NeedsDecision, "project"):
            workflow.apply_event(run, FIXTURES.event("cleanup", {"resources": []}, foreign))

    def test_local_completion_has_no_merge_or_approval_verdict_and_replays(self):
        run = self.catalogued()
        key, target = next(iter(run["targets"].items()))
        run = workflow.apply_event(run, FIXTURES.event("plan", self.proposed(run, key), key))
        assignment = FIXTURES.assignment(scope=target["frozen"]["inventory"], units=list(target["catalog"]["units"]))
        run = workflow.apply_event(run, FIXTURES.event("assignment", assignment, key))
        run = workflow.apply_event(run, FIXTURES.event("started", FIXTURES.start(), key))
        report = FIXTURES.report()
        report.update(target=run["targets"][key]["frozen"], checked=assignment["scope"],
                      checked_units=assignment["units"], related_targets={})
        run = workflow.apply_event(run, FIXTURES.event("report", report, key))
        run = workflow.apply_event(run, FIXTURES.event("complete", {"coverage_evidence": "all project units and categories reviewed"}, key))
        self.assertTrue(workflow.status(run)["complete"])
        self.assertIsNone(workflow.status(run)["targets"][0]["derived_event"])
        rendered = workflow.report(run)
        self.assertNotIn("merge-ready", rendered)
        self.assertNotIn("APPROVE", rendered)
        initial = {"method": run["method"], "intake": run["intake"], "registry": run["registry"],
                   "orchestrator": FIXTURES.MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"]}
        path = self.directory / "run.json"
        path.write_text(json.dumps({"version": 1, "initial": initial, "events": run["events"]}))
        self.assertEqual(workflow.load_run(path)[1], run)

    def test_unborn_and_dirty_git_projects_preserve_index_and_current_bytes(self):
        preparation.git(self.source, "init", "-q")
        (self.source / ".gitignore").write_text("ignored.py\n")
        (self.source / "ignored.py").write_text("ignored current bytes\n")
        preparation.git(self.source, "add", "api.py")
        (self.source / "api.py").write_text("unstaged current bytes\n")
        index = (self.source / ".git/index").read_bytes()
        run = self.prepared()
        frozen = next(iter(run["targets"].values()))["frozen"]
        self.assertEqual((self.source / ".git/index").read_bytes(), index)
        self.assertEqual((Path(frozen["source_root"]) / "api.py").read_text(), "unstaged current bytes\n")
        self.assertIn("ignored.py", frozen["inventory"])
        self.assertFalse((Path(frozen["source_root"]) / ".git").exists())

    def test_cross_project_findings_keep_independent_vet_and_final_judgments(self):
        other = self.base / "consumer"
        other.mkdir()
        (other / "api.py").write_text("consume producer output\n")
        run = self.catalogued([str(self.source), str(other)])
        key = next(iter(run["targets"]))
        foreign = next(name for name in run["targets"] if name != key)
        run = workflow.apply_event(run, FIXTURES.event("plan", self.proposed(run, key, True), key))
        target = run["targets"][key]
        related = workflow.contract.related_targets(target)
        references = [{"path": "api.py", "line": 1, "side": "target", "relation": "anchor", "evidence": "producer returns one"},
                      {"path": "api.py", "line": 1, "side": "target", "relation": "contract", "evidence": "consumer expects two", "target_id": foreign}]
        raw = FIXTURES.raw()
        raw.update(path="api.py", line=1, references=references)
        for kind, name, model in (("discovery", "review", FIXTURES.MODEL_A), ("vet", "vet", FIXTURES.MODEL_B)):
            assignment = FIXTURES.assignment(name, model, kind, scope=target["frozen"]["inventory"],
                units=list(target["catalog"]["units"]), interactions=["api-contract"],
                **({"finding_ids": [raw["id"]]} if kind == "vet" else {}))
            run = workflow.apply_event(run, FIXTURES.event("assignment", assignment, key))
            run = workflow.apply_event(run, FIXTURES.event("started", FIXTURES.start(name, model), key))
            report = FIXTURES.report(name, findings=[raw] if kind == "discovery" else [])
            report.update(target=target["frozen"], checked=assignment["scope"], checked_units=assignment["units"],
                          checked_interactions=assignment["interactions"], related_targets=related)
            if kind == "vet":
                report["verdicts"][0]["references"] = copy.deepcopy(references)
                broken = copy.deepcopy(report)
                broken["verdicts"][0]["references"] = references[:1]
                with self.assertRaisesRegex(ValueError, "every involved"):
                    workflow.apply_event(run, FIXTURES.event("report", broken, key))
            run = workflow.apply_event(run, FIXTURES.event("report", report, key))
        judgment = FIXTURES.adjudication()
        judgment["references"] = copy.deepcopy(references)
        wrong_root = copy.deepcopy(judgment)
        wrong_root["references"][1]["target_id"] = "project-unknown"
        with self.assertRaisesRegex(ValueError, "project"):
            workflow.apply_event(run, FIXTURES.event("adjudication", wrong_root, key))
        run = workflow.apply_event(run, FIXTURES.event("adjudication", judgment, key))
        run = workflow.apply_event(run, FIXTURES.event("complete", {"coverage_evidence": "both contract sides and all categories reviewed"}, key))
        target = run["targets"][key]
        self.assertEqual(target["findings"][raw["id"]], raw)
        self.assertEqual({target["findings"][raw["id"]]["finder"]["family"], target["vets"][raw["id"]]["vetter"]["family"],
                          target["adjudications"][raw["id"]]["adjudicator"]["family"]}, {"family-a", "family-b", "family-c"})
        self.assertIn("F-01", workflow.report(run))
        self.assertFalse(workflow.status(run)["complete"])

    def test_capture_failure_removes_only_its_owned_resource(self):
        run = self.run_record()
        key, target = next(iter(run["targets"].items()))
        unrelated = self.directory / "keep.txt"
        unrelated.write_text("keep\n")
        original = preparation.project_inventory
        def moving(root, exclusions=None, destination=None, **kwargs):
            result = original(root, exclusions, destination, **kwargs)
            if destination is not None:
                (self.source / "api.py").write_text("moved source\n")
            return result
        with patch.object(preparation, "project_inventory", side_effect=moving), self.assertRaisesRegex(preparation.NeedsDecision, "changed"):
            preparation.prepare(self.directory, run["run_id"], key, target, self.source)
        self.assertEqual(list(self.directory.iterdir()), [unrelated])
        self.assertEqual((self.source / "api.py").read_text(), "moved source\n")

    def test_read_only_directories_preserve_modes_and_can_be_cleaned(self):
        read_only = self.source / "read-only"
        read_only.mkdir()
        (read_only / "api.py").write_text("read only\n")
        read_only.chmod(0o555)
        self.addCleanup(read_only.chmod, 0o755)
        run = self.prepared()
        key, target = next(iter(run["targets"].items()))
        frozen = target["frozen"]
        self.assertEqual(stat.S_IMODE((Path(frozen["source_root"]) / "read-only").stat().st_mode), 0o555)
        preparation.cleanup(self.directory, run["run_id"], key)
        self.assertEqual(stat.S_IMODE(read_only.stat().st_mode), 0o555)

    def test_batch_project_targets_use_the_same_wave_allocation_and_plan(self):
        run = self.catalogued(method="pr-review-batch")
        key = next(iter(run["targets"]))
        self.assertEqual(workflow.status(run)["targets"][0]["next"]["code"], "batch_wave")
        run = workflow.apply_event(run, FIXTURES.event("batch-wave", FIXTURES.wave_decisions(run, {key: {"fixture-profile"}}), None))
        run = workflow.apply_event(run, FIXTURES.event("plan", self.proposed(run, key), key))
        self.assertEqual(run["targets"][key]["plan"]["routing"]["finder"], "fixture-profile")

    def test_project_git_marker_symlink_is_rejected_without_following_it(self):
        external = self.base / "external-git"
        external.mkdir()
        (self.source / ".git").symlink_to(external, target_is_directory=True)
        with (patch.object(preparation, "git", side_effect=AssertionError("do not follow redirected Git metadata")),
              self.assertRaisesRegex(ValueError, "redirect")):
            preparation.project_baseline(self.source)

    def test_cross_project_workload_counts_the_foreign_source_units(self):
        other = self.base / "consumer"
        other.mkdir()
        (other / "large.py").write_bytes(b"x" * 200000)
        run = self.catalogued([str(self.source), str(other)])
        key = next(iter(run["targets"]))
        run = workflow.apply_event(run, FIXTURES.event("plan", self.proposed(run, key, True), key))
        target = run["targets"][key]
        assignment = FIXTURES.assignment(scope=target["frozen"]["inventory"], units=list(target["catalog"]["units"]), interactions=["api-contract"])
        with self.assertRaisesRegex(preparation.NeedsDecision, "budget|resize|requires"):
            workflow.apply_event(run, FIXTURES.event("assignment", assignment, key))

    def cli(self, directory, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["workflow.py", "--run", str(directory), *arguments]), redirect_stdout(output), redirect_stderr(errors):
            code = workflow.main()
        return code, json.loads(output.getvalue() or errors.getvalue())

    def test_cli_creation_explains_missing_or_nondirectory_parent_before_intake(self):
        registry = self.base / "registry.json"
        registry.write_text(json.dumps(FIXTURES.REGISTRY))
        model = self.base / "model.json"
        model.write_text(json.dumps(FIXTURES.MODEL_C))
        arguments = ["create", "--method", "pr-review-batch", "--thread-id", "fixture", "--registry", str(registry),
                     "--orchestrator", str(model), "--project", str(self.source)]
        file_parent = self.base / "file"
        file_parent.write_text("preserve me\n")
        for parent in (self.base / "missing", file_parent):
            with self.subTest(parent=parent), patch.object(workflow, "project_collection", side_effect=AssertionError("preflight first")):
                code, result = self.cli(parent / "run", *arguments)
                self.assertEqual(code, 1, result)
                self.assertIn("run parent directory must already exist", result["reason"])
                self.assertNotIn("revision", result)
                self.assertFalse((parent / "run").exists())
        self.assertFalse((self.base / "missing").exists())
        self.assertEqual(file_parent.read_text(), "preserve me\n")
        (self.base / "missing").mkdir()
        code, state = self.cli(self.base / "missing/run", *arguments)
        self.assertEqual(code, 0, state)
        self.assertEqual(state["revision"], 0)

    def test_cli_failed_records_report_saved_revision_and_preserve_integrity(self):
        registry = self.base / "registry.json"
        registry.write_text(json.dumps(FIXTURES.REGISTRY))
        model = self.base / "model.json"
        model.write_text(json.dumps(FIXTURES.MODEL_C))
        directory = self.base / "cli run"
        code, state = self.cli(directory, "create", "--method", "pr-review-batch", "--thread-id", "fixture",
                               "--registry", str(registry), "--orchestrator", str(model), "--project", str(self.source))
        self.assertEqual(code, 0, state)
        key = state["targets"][0]["target"]
        for operation in ("prepare", "finalize"):
            code, state = self.cli(directory, operation, "--target", key, "--revision", str(state["revision"]))
            self.assertEqual(code, 0, state)
        revision = state["revision"]
        event_file = self.base / "event.json"
        event_file.write_text(json.dumps(FIXTURES.event("batch-wave", {}, key)))
        code, error = self.cli(directory, "record", "--event", str(event_file), "--revision", str(revision))
        self.assertEqual(code, 1, error)
        self.assertIn("target must be null", error["reason"])
        self.assertEqual(error["revision"], revision + 1)
        journal = directory / "run.json"
        saved = json.loads(journal.read_text())
        self.assertEqual(saved["events"][-1], FIXTURES.event("integrity", {"status": "unchanged", "components": []}, key))
        before = journal.read_bytes()
        code, stale = self.cli(directory, "record", "--event", str(event_file), "--revision", str(revision))
        self.assertEqual(code, 1, stale)
        self.assertEqual(stale["revision"], error["revision"])
        self.assertEqual(journal.read_bytes(), before)
        event_file.write_text(json.dumps(FIXTURES.event("batch-wave", {}, None)))
        code, hold = self.cli(directory, "record", "--event", str(event_file), "--revision", str(stale["revision"]))
        self.assertEqual(code, 2, hold)
        self.assertEqual(hold["code"], "batch_wave")
        self.assertEqual(hold["revision"], stale["revision"] + 1)
        _, current = workflow.load_run(journal)
        (Path(current["targets"][key]["frozen"]["source_root"]) / "api.py").write_text("changed source\n")
        code, integrity = self.cli(directory, "record", "--event", str(event_file), "--revision", str(hold["revision"]))
        self.assertEqual(code, 2, integrity)
        self.assertEqual(integrity["code"], "source_integrity")
        self.assertEqual(integrity["revision"], hold["revision"] + 1)
        saved, run = workflow.load_run(journal)
        self.assertEqual(run["revision"], integrity["revision"])
        self.assertEqual(saved["events"][-1]["data"]["status"], "changed")
        self.assertFalse(any(event["operation"] == "batch-wave" for event in saved["events"]))

    def test_cli_plain_projects_work_without_git_or_github_and_reject_mixed_scope(self):
        registry = self.base / "registry.json"
        registry.write_text(json.dumps(FIXTURES.REGISTRY))
        model = self.base / "model.json"
        model.write_text(json.dumps(FIXTURES.MODEL_C))
        directory = self.base / "cli run"
        arguments = ["create", "--method", "pr-review", "--thread-id", "fixture", "--registry", str(registry),
                     "--orchestrator", str(model), "--project", str(self.source), "--project", str(self.source)]
        with patch.object(workflow.intake, "api", side_effect=AssertionError("no GitHub")), patch.object(workflow.subprocess, "run", side_effect=AssertionError("no Git required")):
            code, state = self.cli(directory, *arguments)
            self.assertEqual(code, 0, state)
            self.assertEqual(len(state["targets"]), 1)
            self.assertEqual(len(state["project_requests"]), 2)
            key = state["targets"][0]["target"]
            for operation in ("prepare", "finalize"):
                code, state = self.cli(directory, operation, "--target", key, "--revision", str(state["revision"]))
                self.assertEqual(code, 0, state)
            for operation in ("preflight", "reconcile"):
                code, result = self.cli(directory, operation, "--target", key, "--revision", str(state["revision"]))
                self.assertEqual(code, 2, result)
                self.assertEqual(result["code"], "local_publication")
        for flags in (["--working"], ["--pr", "12"]):
            with self.subTest(flags=flags), patch.object(workflow.intake, "api", side_effect=AssertionError("no GitHub")):
                code, result = self.cli(self.base / "mixed", *arguments, *flags)
                self.assertEqual(code, 1, result)
                self.assertFalse((self.base / "mixed").exists())

    def test_project_parent_baselines_protect_plain_directories(self):
        run = self.prepared()
        key, target = next(iter(run["targets"].items()))
        record = preparation.capture_baseline(self.directory, "parent", self.source, (), filesystem=True,
                                              exclude_generated=True)
        self.assertTrue(record["filesystem"])
        self.assertEqual(preparation.check(self.directory, run["run_id"], key, target["frozen"], {"parent": record})["status"], "unchanged")
        run = workflow.apply_event(run, FIXTURES.event("baseline", record, key))
        self.assertEqual(workflow.parent_ledgers(self.directory, run)[str(self.source)], preparation.read_baseline(self.directory, record))

    def test_setup_uses_existing_lifecycle_and_test_copies_omit_generated_caches(self):
        for tool in ("tgrep", "codegraph"):
            with self.subTest(tool=tool):
                run = self.run_record()
                key, target = next(iter(run["targets"].items()))
                staged = preparation.prepare(self.directory, run["run_id"], key, target, self.source)
                module, helper_path = preparation.index_helper(bundled=True)
                cache_name = "." + tool
                with (patch.object(preparation, "index_helper", return_value=(module, helper_path)),
                      patch.object(module, "plan_indexes", return_value={"tools": [tool], "available": [tool], "cache_paths": [cache_name]}),
                      patch.object(module, "unavailable_indexes", return_value=[])):
                    request = preparation.setup_request(self.directory, run["run_id"], key, staged, [tool])
                    cache = Path(staged["source_root"]) / cache_name
                    cache.mkdir()
                    artifact = cache / ("codegraph.db" if tool == "codegraph" else "index")
                    artifact.write_text("generated fixture index\n")
                    frozen = preparation.finalize(self.directory, run["run_id"], key, staged, request, [], None)
                    self.assertTrue(module.sealed_snapshot(Path(frozen["source_root"])))
                    copied = preparation.test_copy(self.directory, run["run_id"], key, frozen)
                    self.assertFalse((copied / cache_name).exists())
                    self.assertEqual((copied / "api.py").read_bytes(), (self.source / "api.py").read_bytes())
                    self.assertEqual(preparation.check(self.directory, run["run_id"], key, frozen, {})["status"], "unchanged")
                    artifact.write_text("late index write after sealing\n")
                    self.assertEqual(preparation.check(self.directory, run["run_id"], key, frozen, {})["status"], "unchanged")
                    (Path(frozen["source_root"]) / "api.py").write_text("changed reviewed source\n")
                    self.assertIn("source", preparation.check(self.directory, run["run_id"], key, frozen, {})["components"])

    def test_static_test_copy_ignores_late_indexes_but_preserves_legacy_reviewed_index_source(self):
        cache = self.source / ".codegraph"
        cache.mkdir()
        (cache / "source.py").write_text("reviewed source\n")
        original_inventory = preparation.project_inventory
        original_baseline = preparation.project_baseline

        def legacy_inventory(root, exclusions=None, destination=None, **kwargs):
            kwargs["exclude_indexes"] = False
            return original_inventory(root, exclusions, destination, **kwargs)

        def legacy_baseline(root, exclude=(), **kwargs):
            kwargs["exclude_indexes"] = False
            return original_baseline(root, exclude, **kwargs)

        with (patch.object(preparation, "project_inventory", side_effect=legacy_inventory),
              patch.object(preparation, "project_baseline", side_effect=legacy_baseline)):
            run = self.prepared()
        key, target = next(iter(run["targets"].items()))
        frozen = target["frozen"]
        source = Path(frozen["source_root"])
        (source / ".codegraph/index").write_text("late disposable index\n")
        for name in (".tgrep", ".zvec-grep"):
            (source / name).mkdir()
            (source / name / "index").write_text("late disposable index\n")
        copied = preparation.test_copy(self.directory, run["run_id"], key, frozen)
        self.assertEqual((copied / ".codegraph/source.py").read_text(), "reviewed source\n")
        self.assertFalse((copied / ".codegraph/index").exists())
        self.assertFalse((copied / ".tgrep").exists())
        self.assertFalse((copied / ".zvec-grep").exists())
        (source / ".codegraph/source.py").write_text("changed reviewed source\n")
        with self.assertRaisesRegex(ValueError, "differs from the frozen manifest"):
            preparation.test_copy(self.directory, run["run_id"], key, frozen)

    def test_project_test_copy_omits_authenticated_new_setup_files_but_keeps_source_strict(self):
        (self.source / ".delta").mkdir()
        (self.source / ".delta/source.txt").write_text("reviewed delta configuration\n")
        run = self.run_record()
        key, target = next(iter(run["targets"].items()))
        staged = preparation.prepare(self.directory, run["run_id"], key, target, self.source)
        module, helper_path = preparation.index_helper(bundled=True)
        with (patch.object(preparation, "index_helper", return_value=(module, helper_path)),
              patch.object(module.shutil, "which", side_effect=lambda name: "/fixture/tgrep" if name == "tgrep" else None),
              patch.object(module, "unavailable_indexes", return_value=[])):
            request = preparation.setup_request(self.directory, run["run_id"], key, staged, ["tgrep"])
            self.assertIn(".sembleignore", request["cache_paths"])
            source = Path(staged["source_root"])
            ignore = source / ".sembleignore"
            ignore.write_text(".delta/\n")
            frozen = preparation.finalize(self.directory, run["run_id"], key, staged, request, [], None)
        self.assertEqual(preparation.check(self.directory, run["run_id"], key, frozen, {})["status"], "unchanged")
        copied = preparation.test_copy(self.directory, run["run_id"], key, frozen)
        self.assertFalse((copied / ".sembleignore").exists())
        self.assertEqual((copied / ".delta/source.txt").read_text(), "reviewed delta configuration\n")
        ignore.write_text("changed sealed configuration\n")
        self.assertIn("source", preparation.check(self.directory, run["run_id"], key, frozen, {})["components"])
        ignore.write_text(".delta/\n")
        unrequested = source / "unrequested.txt"
        unrequested.write_text("unexpected source\n")
        with self.assertRaisesRegex(ValueError, "differs from the frozen manifest"):
            preparation.test_copy(self.directory, run["run_id"], key, frozen)
        unrequested.unlink()
        for name in ("source-baseline.json", "index-result.json"):
            artifact = source.parent / name
            original = artifact.read_bytes()
            artifact.write_bytes(original + b" ")
            with self.subTest(artifact=name), self.assertRaisesRegex(ValueError, "evidence changed"):
                preparation.test_copy(self.directory, run["run_id"], key, frozen)
            artifact.write_bytes(original)

    def test_cli_cleanup_holds_before_removing_another_projects_evidence(self):
        other = self.base / "consumer"
        other.mkdir()
        (other / "api.py").write_text("consumer\n")
        run = self.catalogued([str(self.source), str(other)])
        key = next(iter(run["targets"]))
        foreign = next(name for name in run["targets"] if name != key)
        run = workflow.apply_event(run, FIXTURES.event("plan", self.proposed(run, key, True), key))
        initial = {"method": run["method"], "intake": run["intake"], "registry": run["registry"],
                   "orchestrator": FIXTURES.MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"]}
        initial["runtime"] = workflow.runtime.identity()
        (self.directory / "run.json").write_text(json.dumps({"version": 1, "initial": initial, "events": run["events"]}))
        code, result = self.cli(self.directory, "cleanup", "--target", foreign, "--revision", str(run["revision"]))
        self.assertEqual(code, 2, result)
        self.assertEqual(result["code"], "project_resources")
        self.assertTrue(Path(run["targets"][foreign]["frozen"]["source_root"]).is_dir())

    def test_cross_project_interactions_can_bind_an_empty_foreign_project(self):
        empty = self.base / "empty"
        empty.mkdir()
        run = self.catalogued([str(self.source), str(empty)])
        key = next(iter(run["targets"]))
        run = workflow.apply_event(run, FIXTURES.event("plan", self.proposed(run, key, True), key))
        self.assertTrue(run["targets"][key]["plan"]["cross_project"]["required"])


if __name__ == "__main__":
    unittest.main()
