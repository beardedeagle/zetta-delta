#!/usr/bin/env python3
"""Exercise native checkout cleanup, recovery and retained findings without providers."""

import copy
import importlib.util
import io
import json
import shutil
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "review/scripts"))
import preparation
import workflow


def fixture(name: str) -> ModuleType:
    """Load an existing offline fixture without executing its test runner.

    Args:
        name (str): Repository test filename.

    Returns:
        ModuleType: Loaded fixture definitions sharing this runtime.
    """
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "tests" / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GIT = fixture("pr-review-preparation-test.py")
RECORDS = fixture("pr-review-workflow-test.py")


class NativeLifecycleTests(GIT.GitFixture):
    def state(self) -> tuple[dict, dict]:
        """Read the actual journal and replay its current state.

        Returns:
            tuple[dict, dict]: Persisted envelope and replayed run.
        """
        return workflow.load_run(self.directory / "run.json")

    def append(self, operation: str, data: dict) -> dict:
        """Append an engine-validated event to this target's real journal.

        Args:
            operation (str): Existing workflow transition name.
            data (dict): Transition payload.

        Returns:
            dict: Updated replayed run.
        """
        saved, run = self.state()
        return workflow.append_record(self.directory, saved, run, self.key, operation, data)

    def cli(self, *arguments: str) -> tuple[int, dict]:
        """Invoke the CLI against the physical run with captured JSON output.

        Args:
            *arguments (str): Command and explicit command flags.

        Returns:
            tuple[int, dict]: Exit status and decoded response.
        """
        output = io.StringIO()
        argv = ["workflow.py", "--run", str(self.directory), *arguments]
        with patch.object(sys, "argv", argv), redirect_stdout(output), redirect_stderr(output):
            code = workflow.main()
        return code, json.loads(output.getvalue())

    def check(self, *, stop: bool = True) -> dict:
        """Run the real full source and native-copy checker.

        Args:
            stop (bool): Raise on retained unresolved integrity observations.

        Returns:
            dict: Updated state containing the actual observation.
        """
        saved, run = self.state()
        return workflow.check_sources(self.directory, saved, run, stop=stop)

    def launch(self, assignment_id: str) -> tuple[Path, Path, str]:
        """Issue, physically create and register one isolated native checkout.

        Args:
            assignment_id (str): Queued discovery or recovery assignment.

        Returns:
            tuple[Path, Path, str]: Checkout, clone store and retained parent pin.
        """
        run = self.check()
        saved, _ = self.state()
        capacity = {"parent_active": None, "process_active": None,
                    "runtime_parent_root": str(self.parent), "evidence": "native admission; fixture never contacts a provider"}
        workflow.issue_handoff(self.directory, saved, run, self.key, assignment_id, capacity)
        child, store, pin = self.native_clone(assignment_id)
        saved, run = self.state()
        frozen = run["targets"][self.key]["frozen"]
        proof = preparation.capture_runtime_copy(self.directory, run["run_id"], self.key, frozen, child,
                                                  workflow.runtime_copies(run), workflow.parent_ledgers(self.directory, run),
                                                  str(self.parent))
        workflow.append_record(self.directory, saved, run, self.key, "started", {
            **RECORDS.start(assignment_id), "runtime_root": str(child),
            "runtime_parent_root": str(self.parent), "runtime_copy": proof})
        self.check()
        return child, store, pin

    def inspect(self, assignment_id: str) -> None:
        """Deliver real frozen source bytes for every assigned file.

        Args:
            assignment_id (str): Registered active discovery assignment.
        """
        _, run = self.state()
        target = run["targets"][self.key]
        for name in target["assignments"][assignment_id]["scope"]:
            if target["frozen"]["manifest"]["entries"][name]["kind"] != "file":
                continue
            saved, run = self.state()
            result = workflow.source_read(self.directory, saved, run, self.key, assignment_id, name, 0, 32768, None)
            self.assertIsNone(result["next_offset"])

    def receive(self, assignment_id: str, *, finding: bool = False) -> None:
        """Author a bounded fixture report and receive its immutable artifact.

        Args:
            assignment_id (str): Active assignment whose source was inspected.
            finding (bool): Include one traceable synthetic claim for export checks.
        """
        run = self.check(stop=False)
        saved, _ = self.state()
        row = run["targets"][self.key]["assignments"][assignment_id]
        judgments = RECORDS.report(assignment_id)
        judgments.update(checked=row["scope"], checked_units=row["units"], checked_interactions=row["interactions"])
        for reserved in ("assignment_id", "target", "reviewer", "related_targets"):
            judgments.pop(reserved, None)
        if finding:
            name = next(name for name in row["scope"] if run["targets"][self.key]["frozen"]["manifest"]["entries"][name]["kind"] == "file")
            raw = RECORDS.raw(assignment_id + "-01")
            raw.update(path=name, line=1, evidence="fixture claim anchored to retained frozen bytes",
                       references=[{"path": name, "line": 1, "side": "target", "relation": "anchor",
                                    "evidence": "read receipt binds the exact frozen file"}])
            raw.pop("finder")
            judgments["findings"] = [raw]
        path = self.base / (assignment_id + "-judgments.json")
        path.write_text(json.dumps(judgments))
        receipt = workflow.author_report(self.directory, saved, run, self.key, assignment_id, path)
        self.assertNotIn("errors", receipt, receipt)
        code, response = self.cli("receive-report", "--target", self.key, "--assignment", assignment_id,
                                  "--runtime-handle", row["runtime_handle"], "--evidence", "original native report received",
                                  "--revision", str(receipt["revision"]))
        self.assertEqual(code, 0, response)
        self.assertEqual(self.state()[1]["targets"][self.key]["assignments"][assignment_id]["status"], "done")

    def initialize_run(self, extra_project: Path | None = None) -> dict:
        """Prepare seventeen physical slices and their native assignments.

        Args:
            extra_project (Path | None): Additional target left at intake for CLI
                preparation after the first target's workers have settled.

        Returns:
            dict: Frozen project identity shared by the lifecycle regressions.
        """
        for index in range(17):
            (self.parent / f"slice-{index:02}.py").write_text(f"VALUE = {index}\n")
        self.commit("seventeen source slices")
        self.head = self.git("rev-parse", "HEAD")
        registry = copy.deepcopy(RECORDS.REGISTRY)
        registry["providers"][0]["limit"] = 2
        roots = [str(self.parent)] + ([str(extra_project)] if extra_project is not None else [])
        run = workflow.new_run("pr-review", workflow.project_collection(roots, [[".delta", "native checkout output"]]),
                               registry, RECORDS.MODEL_C, "native-lifecycle", admission_mode="native")
        initial = {"method": run["method"], "intake": run["intake"], "registry": registry,
                   "orchestrator": RECORDS.MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"],
                   "admission_mode": "native", "runtime": workflow.runtime.identity()}
        saved = {"version": 1, "initial": initial, "events": []}
        workflow.write_json(self.directory / "run.json", saved)
        run = workflow.append_record(self.directory, saved, run, None, "native-parent", {
            "root": str(self.parent), "thread_id": run["thread_id"], "source": "terminal-cwd"})
        self.key = next(key for key, target in run["targets"].items() if target["repository"] == str(self.parent))
        staged = preparation.prepare(self.directory, run["run_id"], self.key, run["targets"][self.key], self.parent)
        self.append("prepare", staged)
        frozen = preparation.finalize(self.directory, run["run_id"], self.key, staged, None, [], None)
        run = self.append("freeze", frozen)
        catalog = copy.deepcopy(RECORDS.CATALOG)
        catalog["units"] = workflow.contract.project_units(frozen)
        for profile in catalog["profiles"].values():
            profile["identity"] = workflow.model_record({name: profile["identity"][name]
                                                        for name in ("provider_id", "model_id")}, registry)
        run = self.append("catalog", catalog)
        proposed = RECORDS.plan()
        proposed.update(partitioned=True, cross_project={"required": False, "evidence": "one physical project"},
                        coverage={unit: {"risk": "code", "families": ["family-a"], "shortfall": None} for unit in catalog["units"]},
                        interactions={"lifecycle": {"question": "retain the first source slice through native cleanup",
                                                     "units": [next(iter(catalog["units"]))], "families": ["family-a"], "shortfall": None}})
        self.append("plan", proposed)
        units = list(catalog["units"])
        for index in range(17):
            assigned = units[index::17]
            scope = sorted({path for unit in assigned for path in catalog["units"][unit]["paths"]})
            self.append("assignment", RECORDS.assignment(f"slice-{index:02}", scope=scope, units=assigned,
                                                          interactions=["lifecycle"] if index == 0 else []))
        return frozen

    def test_cli_finalizes_another_target_after_completed_checkout_removal(self):
        other = self.base / "other-project"
        other.mkdir()
        (other / "api.py").write_text("VALUE = 1\n")
        self.initialize_run(other)
        child, store, pin = self.launch("slice-00")
        self.inspect("slice-00")
        self.receive("slice-00", finding=True)
        shutil.rmtree(child)
        shutil.rmtree(store)
        run = self.check()
        self.assertEqual(workflow.active_native_roots(run), ())
        self.assertEqual(len(workflow.runtime_copies(run)), 1)
        self.assertEqual(self.git("rev-parse", pin), self.head)
        key = next(key for key in run["targets"] if key != self.key)
        code, result = self.cli("prepare", "--target", key, "--revision", str(run["revision"]))
        self.assertEqual(code, 0, result)
        run = self.state()[1]
        code, result = self.cli("finalize", "--target", key, "--revision", str(run["revision"]))
        self.assertEqual(code, 0, result)
        run = self.state()[1]
        self.assertEqual(run["targets"][key]["phase"], "prepared")
        self.assertEqual(workflow.integrity_failures(run), ([], []))
        self.assertEqual(self.git("rev-parse", pin), self.head)
        code, exported = self.cli("export")
        self.assertEqual(code, 0, exported)
        self.assertEqual(len(exported["findings"]), 1)

    def test_cli_finalization_reuses_another_targets_adopted_legacy_parent_policy(self):
        other = self.base / "legacy-project"
        other.mkdir()
        (other / "api.py").write_text("VALUE = 1\n")
        for source in (self.parent, other):
            (source / "target").mkdir()
            (source / "target/output").write_text("disposable build output\n")
        collection = workflow.project_collection([str(self.parent), str(other)], [["target", "build output"]])
        run = workflow.new_run("pr-review", collection, RECORDS.REGISTRY, RECORDS.MODEL_C, "legacy-policy")
        initial = {"method": run["method"], "intake": collection, "registry": RECORDS.REGISTRY,
                   "orchestrator": RECORDS.MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"],
                   "runtime": workflow.runtime.identity()}
        workflow.write_json(self.directory / "run.json", {"version": 1, "initial": initial, "events": []})
        keys = {target["repository"]: key for key, target in run["targets"].items()}
        first, second = keys[str(self.parent)], keys[str(other)]
        capture = preparation.project_baseline

        def unversioned(source, exclude=(), **kwargs):
            kwargs.pop("exclude_generated", None)
            kwargs.pop("exclude_indexes", None)
            return capture(source, exclude, **kwargs)

        with patch.object(preparation, "project_baseline", side_effect=unversioned):
            code, result = self.cli("prepare", "--target", first, "--revision", str(run["revision"]))
        self.assertEqual(code, 0, result)
        # Discarding excluded output leaves the first legacy parent unchanged;
        # the second parent's still-present output needs its recorded policy.
        shutil.rmtree(self.parent / "target")
        run = self.state()[1]
        code, result = self.cli("finalize", "--target", first, "--revision", str(run["revision"]))
        self.assertEqual(code, 0, result)
        for command, key, arguments in (("prepare", second, []),
                                         ("baseline", first, ["--id", "other-parent", "--root", str(other)])):
            run = self.state()[1]
            with patch.object(preparation, "project_baseline", side_effect=unversioned):
                code, result = self.cli(command, "--target", key, "--revision", str(run["revision"]), *arguments)
            self.assertEqual(code, 0, result)
        run = self.state()[1]
        staged = run["targets"][second]["setup"]
        artifact = Path(staged["source_root"]).parent / "parent-baseline.json"
        original = artifact.read_bytes()
        self.assertNotIn("exclusion_policy", json.loads(original))
        self.assertEqual(run["targets"][first]["baselines"]["other-parent"]["fingerprint"], staged["parent_fingerprint"])
        code, result = self.cli("adopt-baseline-policy", "--target", first, "--root", str(other),
                                "--exclude", "target", "--exclude-generated", "--revision", str(run["revision"]),
                                "--evidence", "owner adopted the exact original build-output exclusion")
        self.assertEqual(code, 0, result)
        run = self.state()[1]
        self.assertEqual(workflow.integrity_failures(run), ([], []))
        code, result = self.cli("finalize", "--target", second, "--revision", str(run["revision"]))
        self.assertEqual(code, 0, result)
        self.assertEqual(self.state()[1]["targets"][second]["phase"], "prepared")
        self.assertEqual(artifact.read_bytes(), original)
        (other / "api.py").write_text("actual parent source change\n")
        code, result = self.cli("check")
        self.assertEqual(code, 0, result)
        frozen_source = Path(self.state()[1]["targets"][second]["frozen"]["source_root"])
        (frozen_source / "api.py").write_text("actual frozen source change\n")
        code, result = self.cli("check")
        self.assertEqual(code, 2, result)
        self.assertEqual(result["code"], "source_integrity")

    def test_cli_shared_parent_baseline_preserves_reviewed_owners_for_native_start(self):
        cache = self.parent / ".codegraph"
        cache.mkdir()
        (cache / "source.py").write_text("REVIEWED = True\n")
        self.commit("explicit reviewed source inside an index-looking path")
        other = self.base / "other-project"
        other.mkdir()
        (other / "api.py").write_text("VALUE = 1\n")
        inventory, baseline = preparation.project_inventory, preparation.project_baseline

        def legacy_inventory(source, *args, **kwargs):
            kwargs["exclude_indexes"] = False
            return inventory(source, *args, **kwargs)

        def legacy_baseline(source, *args, **kwargs):
            kwargs["exclude_indexes"] = False
            return baseline(source, *args, **kwargs)

        with (patch.object(preparation, "project_inventory", side_effect=legacy_inventory),
              patch.object(preparation, "project_baseline", side_effect=legacy_baseline)):
            frozen = self.initialize_run(other)
        self.assertIn(".codegraph/source.py", frozen["inventory"])
        child, _, _ = self.launch("slice-00")
        run = self.check()
        self.assertEqual(workflow.active_native_roots(run), (str(child),))
        key = next(key for key in run["targets"] if key != self.key)
        for command in ("prepare", "finalize"):
            run = self.state()[1]
            code, result = self.cli(command, "--target", key, "--revision", str(run["revision"]))
            self.assertEqual(code, 0, result)
        self.assertEqual(self.state()[1]["targets"][key]["phase"], "prepared")
        run = self.state()[1]
        code, result = self.cli("baseline", "--target", key, "--id", "shared-parent", "--root", str(self.parent),
                                "--revision", str(run["revision"]))
        self.assertEqual(code, 0, result)
        run = self.check()
        parents = workflow.parent_ledgers(self.directory, run)
        self.assertIn(".codegraph/source.py", parents[str(self.parent)]["files"])
        baseline_record = run["targets"][key]["baselines"]["shared-parent"]
        original_baseline = (self.directory / baseline_record["artifact"]).read_bytes()
        ledger = preparation.read_baseline(self.directory, baseline_record)
        self.assertIn(".codegraph/source.py", ledger["files"])
        second_frozen = run["targets"][key]["frozen"]
        catalog = copy.deepcopy(RECORDS.CATALOG)
        catalog["units"] = workflow.contract.project_units(second_frozen)
        for profile in catalog["profiles"].values():
            profile["identity"] = workflow.model_record({name: profile["identity"][name]
                                                        for name in ("provider_id", "model_id")}, run["registry"])
        proposed = RECORDS.plan()
        proposed.update(cross_project={"required": False, "evidence": "independent second project"},
                        coverage={unit: {"risk": "code", "families": ["family-a"], "shortfall": None} for unit in catalog["units"]})
        for operation, data in (("catalog", catalog), ("plan", proposed),
                                ("assignment", RECORDS.assignment("other-review", scope=second_frozen["inventory"],
                                                                  units=list(catalog["units"])))):
            saved, run = self.state()
            workflow.append_record(self.directory, saved, run, key, operation, data)
        run = self.state()[1]
        code, result = self.cli("task", "--target", key, "--assignment", "other-review", "--revision", str(run["revision"]))
        self.assertEqual(code, 0, result)
        second_child, _, _ = self.native_clone("other-review")
        event = {"target": key, "operation": "started", "data": {
            **RECORDS.start("other-review"), "runtime_root": str(second_child), "runtime_parent_root": str(self.parent)}}
        event_path = self.base / "second-start.json"
        event_path.write_text(json.dumps(event))
        run = self.state()[1]
        code, result = self.cli("record", "--event", str(event_path), "--revision", str(run["revision"]))
        self.assertEqual(code, 0, result)
        self.assertEqual(self.state()[1]["targets"][key]["assignments"]["other-review"]["runtime_copy"]["root"], str(second_child))
        self.inspect("slice-00")
        self.receive("slice-00")
        run = self.state()[1]
        for row in run["targets"][self.key]["assignments"].values():
            if row["status"] == "queued":
                self.append("withdraw", {"assignment_id": row["id"], "target": frozen, "cause": "cancelled",
                                         "evidence": "fixture explicitly cancels remaining unstarted source slices"})
        run = self.state()[1]
        code, result = self.cli("cleanup", "--target", self.key, "--revision", str(run["revision"]))
        self.assertEqual(code, 0, result)
        self.assertFalse(Path(frozen["source_root"]).parent.exists())
        shutil.rmtree(child)
        run = self.check()
        self.assertIn(".codegraph/source.py", workflow.parent_reviewed_paths(run)[str(self.parent)])
        self.assertIn(".codegraph/source.py", workflow.parent_ledgers(self.directory, run)[str(self.parent)]["files"])
        self.assertEqual((self.directory / baseline_record["artifact"]).read_bytes(), original_baseline)
        (cache / "source.py").write_text("actual shared parent source change\n")
        (second_child / "feature.txt").write_text("actual active native source change\n")
        code, result = self.cli("check")
        self.assertEqual(code, 2, result)
        self.assertEqual(result["code"], "source_integrity")
        self.assertIn("native:" + str(second_child), self.state()[1]["targets"][key]["integrity"][-1]["components"])
        self.assertIn("copy:" + key + "/shared-parent", self.state()[1]["targets"][key]["integrity"][-1]["components"])

    def assert_restored_source_rejected(self, kind: str) -> None:
        """Keep real source incidents blocking after their bytes are restored.

        Args:
            kind (str): Parent, artifact-backed copy or completed native source.
        """
        self.initialize_run()
        child, _, _ = self.launch("slice-00")
        self.inspect("slice-00")
        self.receive("slice-00")
        root, component = self.parent, "parent"
        if kind == "copy":
            root = self.base / "extra-copy"
            self.git("clone", "-q", str(self.parent), str(root))
            _, run = self.state()
            record = preparation.capture_baseline(self.directory, "extra", root, workflow.runtime_copies(run))
            self.append("baseline", record)
            component = "copy:" + self.key + "/extra"
        elif kind == "native":
            root, component = child, "native:" + str(child)
        path = root / "feature.txt"
        original = path.read_bytes()
        path.write_text("actual source edit before restoration\n")
        _, run = self.state()
        target = run["targets"][self.key]
        observation = preparation.check(self.directory, run["run_id"], self.key, target["frozen"],
                                        {self.key + "/" + name: row for name, row in target["baselines"].items()},
                                        native_copies=workflow.runtime_copies(run))
        self.assertEqual(observation["status"], "changed", observation)
        self.assertEqual(observation["components"], [component], observation)
        incident = len(target["integrity"])
        self.append("integrity", observation)
        path.write_bytes(original)
        run = self.check(stop=False)
        self.assertEqual(run["targets"][self.key]["integrity"][-1]["status"], "unchanged")
        code, result = self.cli("resolve-integrity", "--target", self.key, "--observation", str(incident),
                                "--cause", "completed-native-copy" if kind == "native" else "registered-native-administration",
                                "--evidence", "source was actually changed and restored; this is not bookkeeping",
                                "--revision", str(run["revision"]))
        self.assertEqual(code, 1, {"response_code": result.get("code"),
                                   "resolutions": self.state()[1]["targets"][self.key]["integrity_resolutions"]})
        self.assertIn("original incident", result["reason"])
        self.assertEqual(workflow.integrity_failures(self.state()[1])[0], [self.key])

    def test_restored_parent_edit_cannot_be_resolved_as_bookkeeping(self):
        self.assert_restored_source_rejected("parent")

    def test_restored_copy_edit_cannot_be_resolved_as_bookkeeping(self):
        self.assert_restored_source_rejected("copy")

    def test_restored_native_edit_cannot_be_resolved_as_completed_cleanup(self):
        self.assert_restored_source_rejected("native")

    def test_original_ref_only_incident_uses_exact_settled_native_registration(self):
        frozen = self.initialize_run()
        self.launch("slice-00")
        self.inspect("slice-00")
        self.receive("slice-00")
        _, run = self.state()
        # Reproduce an observation that lacked exact native normalization while
        # retaining the original ref bytes, as older bookkeeping checks did.
        observation = preparation.check(self.directory, run["run_id"], self.key, frozen, {})
        self.assertEqual(observation["status"], "changed", observation)
        self.assertEqual(observation["components"], ["parent"], observation)
        self.assertEqual(observation["bookkeeping"]["parent"]["native_copies_sha256"], [])
        incident = len(run["targets"][self.key]["integrity"])
        self.append("integrity", observation)
        run = self.check(stop=False)
        self.assertEqual(run["targets"][self.key]["integrity"][-1]["status"], "unchanged")
        code, result = self.cli("resolve-integrity", "--target", self.key, "--observation", str(incident),
                                "--cause", "registered-native-administration",
                                "--evidence", "retained original ref is now bound to the exact settled native copy",
                                "--revision", str(run["revision"]))
        self.assertEqual(code, 0, result)
        self.assertEqual(workflow.integrity_failures(self.state()[1]), ([], []))

    def test_completed_workers_survive_cleanup_retry_resolution_and_held_export(self):
        frozen = self.initialize_run()
        first, first_store, pin = self.launch("slice-00")
        self.launch("slice-01")
        self.inspect("slice-00")
        self.inspect("slice-01")
        _, run = self.state()
        self.assertEqual(sum(row["status"] == "running" for row in run["targets"][self.key]["assignments"].values()), 2)
        with self.subTest("provider admission"), self.assertRaises(preparation.NeedsDecision):
            workflow.contract.dispatch_task(run, self.key, "slice-02", {
                "parent_active": None, "process_active": None, "runtime_parent_root": str(self.parent),
                "evidence": "both provider/family slots are occupied"}, directory=self.directory)
        self.receive("slice-00", finding=True)
        _, before_cleanup = self.cli("export")
        original_findings = [entry["finding"] for entry in before_cleanup["findings"]]
        self.assertEqual(len(original_findings), 1)
        shutil.rmtree(first)
        shutil.rmtree(first_store)
        self.assertEqual(self.git("rev-parse", pin), self.head)
        saved, run = self.state()
        old_observation = preparation.check(self.directory, run["run_id"], self.key, frozen, {},
                                            native_copies=workflow.runtime_copies(run))
        self.assertEqual(old_observation["status"], "unknown")
        self.assertEqual(old_observation["components"], ["native:" + str(first)])
        incident = len(run["targets"][self.key]["integrity"])
        self.append("integrity", old_observation)
        self.receive("slice-01")
        code, held = self.cli("export")
        self.assertEqual(code, 0, held)
        self.assertEqual([entry["finding"] for entry in held["findings"]], original_findings)
        _, run = self.state()
        self.assertEqual(workflow.integrity_failures(run)[0], [self.key])
        code, result = self.cli("resolve-integrity", "--target", self.key, "--observation", str(incident),
                                "--cause", "completed-native-copy", "--evidence", "completed native checkout removed; exact pin retained",
                                "--revision", str(run["revision"]))
        self.assertEqual(code, 0, result)
        self.assertEqual(workflow.integrity_failures(self.state()[1]), ([], []))
        self.launch("slice-02")
        _, run = self.state()
        row = run["targets"][self.key]["assignments"]["slice-02"]
        self.append("runtime-outcome", {"assignment_id": row["id"], "runtime_handle": row["runtime_handle"],
                                        "target": frozen, "status": "failed", "cause": "transient",
                                        "evidence": "native fixture confirms attempt terminated before any report"})
        self.append("recover", {"id": "slice-02-retry", "assignment_id": "slice-02", "kind": "retry",
                                "target": frozen, "reason": "retry the confirmed terminal transient failure",
                                "capacity": {"parent_active": None, "process_active": None,
                                             "evidence": "no active native attempts; waiting slices retain future work"},
                                "route": {"profile": "fixture-profile", "model": RECORDS.MODEL_A,
                                          "evidence": "same selected model and family on the available lane"}})
        self.launch("slice-02-retry")
        self.inspect("slice-02-retry")
        self.receive("slice-02-retry")
        saved, run = self.state()
        self.assertFalse(any(event["operation"] == "withdraw" for event in saved["events"]))
        self.assertEqual(sum(row["status"] == "queued" for row in run["targets"][self.key]["assignments"].values()), 14)
        self.assertEqual([entry["finding"] for entry in workflow.export_findings(self.directory, saved)["findings"]], original_findings)
        source = Path(frozen["source_root"]) / "slice-00.py"
        source.write_text("unexpected changed frozen source\n")
        with self.assertRaises(preparation.NeedsDecision) as blocked:
            self.check()
        self.assertEqual(blocked.exception.code, "source_integrity")
        self.assertIn("source", self.state()[1]["targets"][self.key]["integrity"][-1]["components"])
        code, retained = self.cli("export")
        self.assertEqual(code, 0, retained)
        self.assertEqual([entry["finding"] for entry in retained["findings"]], original_findings)


if __name__ == "__main__":
    unittest.main()
