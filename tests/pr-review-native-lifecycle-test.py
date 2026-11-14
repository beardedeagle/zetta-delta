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

    def test_completed_workers_survive_cleanup_retry_resolution_and_held_export(self):
        for index in range(17):
            (self.parent / f"slice-{index:02}.py").write_text(f"VALUE = {index}\n")
        self.commit("seventeen source slices")
        self.head = self.git("rev-parse", "HEAD")
        registry = copy.deepcopy(RECORDS.REGISTRY)
        registry["providers"][0]["limit"] = 2
        run = workflow.new_run("pr-review", workflow.project_collection([str(self.parent)], [[".delta", "native checkout output"]]),
                               registry, RECORDS.MODEL_C, "native-lifecycle", admission_mode="native")
        initial = {"method": run["method"], "intake": run["intake"], "registry": registry,
                   "orchestrator": RECORDS.MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"],
                   "admission_mode": "native", "runtime": workflow.runtime.identity()}
        saved = {"version": 1, "initial": initial, "events": []}
        workflow.write_json(self.directory / "run.json", saved)
        run = workflow.append_record(self.directory, saved, run, None, "native-parent", {
            "root": str(self.parent), "thread_id": run["thread_id"], "source": "terminal-cwd"})
        self.key = next(iter(run["targets"]))
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
