#!/usr/bin/env python3
"""Recovery, extraction and owner-policy regressions without external services."""

import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "review/scripts"))
import workflow

SPEC = importlib.util.spec_from_file_location("lifecycle_fixtures", ROOT / "tests/pr-review-workflow-test.py")
assert SPEC is not None and SPEC.loader is not None
FIXTURES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURES)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="review-lifecycle-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()

    def cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["workflow.py", "--run", str(self.directory), *arguments]), redirect_stdout(stdout), redirect_stderr(stderr):
            code = workflow.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def discovered(self):
        run = FIXTURES.WorkflowTests().discovered(findings=[FIXTURES.raw()])
        row = run["targets"][FIXTURES.TARGET]["assignments"]["review"]
        row["runtime_copy"] = {"root": "/temporary/native", "git_dir": "/temporary/native/.git",
                               "ref": "refs/heads/native", "head": "a" * 40, "common_dir": "/parent/.git"}
        return run

    def resolution(self, run, index=0, cause="registered-native-administration"):
        target = run["targets"][FIXTURES.TARGET]
        return {"observation": index, "observation_sha256": workflow.preparation.fingerprint(target["integrity"][index]),
                "check": len(target["integrity"]) - 1, "snapshot_fingerprint": target["frozen"]["source_fingerprint"],
                "native_copies_sha256": workflow.preparation.fingerprint({"copies": workflow.runtime_copies(run)}),
                "cause": cause, "evidence": "Complete recheck matched the immutable original ledger with exact registered settled references."}

    def test_bookkeeping_resolution_retains_incident_and_rejects_stale_or_source_bindings(self):
        run = self.discovered()
        frozen = run["targets"][FIXTURES.TARGET]["frozen"]
        refs = b"refs/heads/native\0" + b"a" * 40 + b"\n"
        proof = {"kind": "git-refs", "root": frozen["parent_root"], "common_dir": "/parent/.git",
                 "baseline_fingerprint": frozen["parent_fingerprint"],
                 "baseline_refs_sha256": workflow.hashlib.sha256(b"").hexdigest(),
                 "refs_hex": refs.hex(), "refs_sha256": workflow.hashlib.sha256(refs).hexdigest(),
                 "native_copies_sha256": []}
        run = workflow.apply_event(run, FIXTURES.event("integrity", {"status": "changed", "components": ["parent"],
                                                                   "bookkeeping": {"parent": proof}}))
        run = workflow.apply_event(run, FIXTURES.event("integrity", {"status": "unchanged", "components": []}))
        original = copy.deepcopy(run)
        data = self.resolution(run)
        for field, value in (("observation_sha256", "0" * 64), ("snapshot_fingerprint", "0" * 64),
                             ("native_copies_sha256", "0" * 64), ("check", 0), ("observation", True)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                workflow.apply_event(run, FIXTURES.event("integrity-resolution", dict(data, **{field: value})))
        unrelated_refs = b"refs/heads/native\0" + b"b" * 40 + b"\n"
        for changes in ({"common_dir": "/unrelated/.git"}, {"native_copies_sha256": ["0" * 64]},
                        {"refs_hex": unrelated_refs.hex(), "refs_sha256": workflow.hashlib.sha256(unrelated_refs).hexdigest()}):
            unsafe = copy.deepcopy(run)
            unsafe["targets"][FIXTURES.TARGET]["integrity"][0]["bookkeeping"]["parent"].update(changes)
            with self.subTest(original_proof=changes), self.assertRaises(ValueError):
                workflow.apply_event(unsafe, FIXTURES.event("integrity-resolution", self.resolution(unsafe)))
        resolved = workflow.apply_event(run, FIXTURES.event("integrity-resolution", data))
        self.assertEqual(resolved["targets"][FIXTURES.TARGET]["integrity"], original["targets"][FIXTURES.TARGET]["integrity"])
        self.assertEqual(workflow.integrity_failures(resolved), ([], []))
        with self.assertRaises(ValueError):
            workflow.apply_event(resolved, FIXTURES.event("integrity-resolution", data))
        for component in ("source", "manifest", "artifact:review.patch", "native:/unregistered"):
            with self.subTest(component=component):
                unsafe = self.discovered()
                unsafe = workflow.apply_event(unsafe, FIXTURES.event("integrity", {"status": "changed", "components": [component]}))
                unsafe = workflow.apply_event(unsafe, FIXTURES.event("integrity", {"status": "unchanged", "components": []}))
                with self.assertRaises(ValueError):
                    workflow.apply_event(unsafe, FIXTURES.event("integrity-resolution", self.resolution(unsafe)))

    def test_legacy_incident_needs_original_proof_for_new_resolution_but_accepted_events_replay(self):
        run = self.discovered()
        run = workflow.apply_event(run, FIXTURES.event("integrity", {"status": "changed", "components": ["parent"]}))
        run = workflow.apply_event(run, FIXTURES.event("integrity", {"status": "unchanged", "components": []}))
        event = FIXTURES.event("integrity-resolution", self.resolution(run))
        with self.assertRaisesRegex(ValueError, "original incident"):
            workflow.apply_event(run, event)
        replayed = workflow.apply_event(run, event, replay=True)
        self.assertEqual(workflow.integrity_failures(replayed), ([], []))

    def test_original_bookkeeping_evidence_rejects_malformed_bindings(self):
        for value in (None, [], "relative", 42):
            run = self.discovered()
            proof = {"kind": "missing-native-root", "root": value, "copy_sha256": "a" * 64}
            observation = {"status": "unknown", "components": ["native:/temporary/native"],
                           "bookkeeping": {"native:/temporary/native": proof}}
            with self.subTest(root=value), self.assertRaises(ValueError):
                workflow.apply_event(run, FIXTURES.event("integrity", observation))

    def test_shared_checkout_stays_active_until_every_using_assignment_is_settled(self):
        run = self.discovered()
        target = run["targets"][FIXTURES.TARGET]
        proof = target["assignments"]["review"]["runtime_copy"]
        target["assignments"]["second"] = dict(target["assignments"]["review"], id="second", status="running")
        self.assertEqual(workflow.active_native_roots(run), (proof["root"],))
        self.assertEqual(workflow.runtime_copies(run), (proof,))
        target["assignments"]["second"]["status"] = "failed"
        self.assertEqual(workflow.active_native_roots(run), ())
        self.assertEqual(workflow.runtime_copies(run), (proof,))

    def test_unknown_native_outcome_remains_active_and_cannot_support_cleanup_resolution(self):
        fixtures = FIXTURES.RecoveryTests()
        run = fixtures.running()
        proof = self.discovered()["targets"][FIXTURES.TARGET]["assignments"]["review"]["runtime_copy"]
        run["targets"][FIXTURES.TARGET]["assignments"]["review"]["runtime_copy"] = proof
        run = fixtures.outcome(run, status="unknown")
        self.assertEqual(run["targets"][FIXTURES.TARGET]["assignments"]["review"]["status"], "running")
        self.assertEqual(workflow.active_native_roots(run), (proof["root"],))
        component = "native:" + proof["root"]
        observation = {"status": "unknown", "components": [component], "bookkeeping": {
            component: {"kind": "missing-native-root", "root": proof["root"],
                        "copy_sha256": workflow.preparation.fingerprint(proof)}}}
        run = workflow.apply_event(run, FIXTURES.event("integrity", observation))
        run = workflow.apply_event(run, FIXTURES.event("integrity", {"status": "unchanged", "components": []}))
        with self.assertRaisesRegex(ValueError, "settled native administration"):
            workflow.apply_event(run, FIXTURES.event("integrity-resolution", self.resolution(run, cause="completed-native-copy")))

    def test_export_survives_unreplayable_journal_and_bad_receipt_without_writes(self):
        finding = dict(FIXTURES.raw(), evidence="<script>untrusted</script>")
        saved = {"version": 999, "initial": {}, "events": [
            {"target": "target", "operation": "unknown-future-event", "data": {}},
            {"target": "target", "operation": "report-draft", "data": {"assignment_id": "broken", "report": {"artifact": "../../outside"}}},
            {"target": "target", "operation": "report-input", "data": {"assignment_id": "review", "source_status": "unknown", "report": json.dumps({"findings": [finding]})}},
        ]}
        path = self.directory / "run.json"
        workflow.write_json(path, saved)
        before = path.read_bytes()
        code, output, errors = self.cli("export")
        self.assertEqual((code, errors), (0, ""))
        exported = json.loads(output)
        self.assertEqual(exported["findings"][0]["finding"], finding)
        self.assertEqual(len(exported["errors"]), 1)
        self.assertEqual(len(exported["reports"]), 2)
        code, output, errors = self.cli("export", "--format", "markdown")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("&lt;script&gt;", output)
        self.assertNotIn("<script>", output)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([p.name for p in self.directory.iterdir()], ["run.json"])

    def test_legacy_mutation_requires_explicit_runtime_adoption_but_reads_work(self):
        run = workflow.new_run("pr-review", FIXTURES.COLLECTION, FIXTURES.REGISTRY, FIXTURES.MODEL_C, "legacy-thread", admission_mode="native")
        initial = {"method": "pr-review", "intake": FIXTURES.COLLECTION, "registry": FIXTURES.REGISTRY,
                   "orchestrator": FIXTURES.MODEL_C, "thread_id": "legacy-thread", "run_id": run["run_id"], "admission_mode": "native"}
        path = self.directory / "run.json"
        workflow.write_json(path, {"version": 1, "initial": initial, "events": []})
        original = path.read_bytes()
        self.assertEqual(self.cli("status")[0], 0)
        code, output, _ = self.cli("native-parent", "--revision", "0")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["code"], "runtime_identity")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.cli("runtime-adopt", "--revision", "0", "--evidence", "Owner approved the inspected code/schema migration.")[0], 0)
        self.assertEqual(self.cli("native-parent", "--revision", "1")[0], 0)
        saved, replayed = workflow.load_run(path)
        self.assertNotIn("runtime", saved["initial"])
        self.assertEqual(saved["events"][0]["data"]["before"], None)
        self.assertEqual(replayed["runtime"], workflow.runtime.identity())

    def test_owner_waiver_delivers_local_assessment_without_claiming_vet_or_completion(self):
        run = FIXTURES.WorkflowTests().discovered(findings=[FIXTURES.raw()])
        target = run["targets"][FIXTURES.TARGET]
        run = workflow.apply_event(run, FIXTURES.event("stage-waiver", {
            "stage": "vetting", "snapshot_fingerprint": target["frozen"]["source_fingerprint"],
            "evidence": "Owner explicitly requested orchestrator assessment without separate vetting."}))
        self.assertEqual(workflow.next_decision(run, run["targets"][FIXTURES.TARGET])["code"], "owner_assessment")
        judgment = FIXTURES.adjudication(disposition="UNVETTED")
        judgment.update(reason="Owner waived the independent vet; this is local scrutiny only.", argument_assessment="No independent vet argument was supplied.")
        run = workflow.apply_event(run, FIXTURES.event("adjudication", judgment))
        target = run["targets"][FIXTURES.TARGET]
        self.assertTrue(workflow.local_assessment_complete(target))
        self.assertEqual(target["vets"], {})
        self.assertEqual(workflow.next_decision(run, target)["status"], "assessment-complete")
        self.assertTrue(workflow.completion_gaps(run, target))
        with self.assertRaises(workflow.NeedsDecision):
            workflow.apply_event(run, FIXTURES.event("complete", {"coverage_evidence": "Local assessment was delivered."}))

    def test_explicit_local_waiver_retains_same_family_overlap_and_exports_original_claim(self):
        run = FIXTURES.WorkflowTests().discovered()
        raw = dict(FIXTURES.raw(), id="orchestrator-01", finder=run["orchestrator"])
        source = FIXTURES.event("orchestrator-finding", {"target": FIXTURES.FROZEN, "finding": raw})
        run = workflow.apply_event(run, source)
        judgment = FIXTURES.adjudication(key=raw["id"], disposition="UNVETTED")
        with self.assertRaises(workflow.NeedsDecision):
            workflow.apply_event(run, FIXTURES.event("adjudication", judgment))
        run = workflow.apply_event(run, FIXTURES.event("stage-waiver", {
            "stage": "vetting", "snapshot_fingerprint": FIXTURES.FROZEN["source_fingerprint"],
            "evidence": "Owner requested personal assessment of all available claims, including orchestrator discoveries."}))
        run = workflow.apply_event(run, FIXTURES.event("adjudication", judgment))
        target = run["targets"][FIXTURES.TARGET]
        self.assertEqual(target["adjudications"][raw["id"]]["family_overlap"], ["finder"])
        self.assertEqual(target["adjudications"][raw["id"]]["disposition"], "UNVETTED")
        self.assertTrue(workflow.completion_gaps(run, target))
        exported = workflow.export_findings(self.directory, {"events": [source]})
        self.assertEqual(exported["findings"][0]["finding"], raw)

    def test_pending_launch_blocks_new_mutations_without_invalidating_started(self):
        run = FIXTURES.WorkflowTests().prepared()
        run = workflow.apply_event(run, FIXTURES.event("assignment", FIXTURES.assignment()))
        capacity = {"parent_active": 0, "process_active": 0, "evidence": "Fresh native counts for the issued launch.",
                    "runtime_parent_root": FIXTURES.FROZEN["parent_root"]}
        run = workflow.apply_event(run, FIXTURES.event("task-ready", {"assignment_id": "review", "capacity": capacity}))
        initial = {"method": "pr-review", "intake": FIXTURES.COLLECTION, "registry": FIXTURES.REGISTRY,
                   "orchestrator": FIXTURES.MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"],
                   "runtime": workflow.runtime.identity()}
        path = self.directory / "run.json"
        workflow.write_json(path, {"version": 1, "initial": initial, "events": run["events"]})
        original = path.read_bytes()
        revision = str(run["revision"])
        commands = [
            ["runtime-adopt", "--evidence", "Owner approved adoption."],
            ["waive-stage", "--target", FIXTURES.TARGET, "--stage", "vetting", "--evidence", "Owner waived vetting."],
            ["resolve-integrity", "--target", FIXTURES.TARGET, "--observation", "0", "--cause", "completed-native-copy", "--evidence", "Inspect bookkeeping."],
            ["adopt-baseline-policy", "--target", FIXTURES.TARGET, "--root", FIXTURES.FROZEN["parent_root"], "--evidence", "Inspect original policy."],
        ]
        for operation in ("runtime-adopt", "stage-waiver", "integrity-resolution", "baseline-policy"):
            with self.subTest(operation=operation), self.assertRaises(workflow.NeedsDecision) as caught:
                workflow.apply_event(run, FIXTURES.event(operation, {}))
            self.assertEqual(caught.exception.code, "runtime_registration")
        for command in commands:
            path.write_bytes(original)
            with self.subTest(command=command[0]), patch.object(workflow, "check_sources", side_effect=AssertionError("pending launch must block before source checks")):
                code, output, errors = self.cli(*command, "--revision", revision)
                self.assertEqual((code, errors), (2, ""))
                self.assertEqual(json.loads(output)["code"], "runtime_registration")
                self.assertEqual(path.read_bytes(), original)
        path.write_bytes(original)
        self.assertEqual(self.cli("status")[0], 0)
        self.assertEqual(self.cli("export")[0], 0)
        started = dict(FIXTURES.start(), runtime_root="/native")
        event = self.directory / "started.json"
        workflow.write_json(event, FIXTURES.event("started", started))
        proof = {"root": "/native", "parent_root": FIXTURES.FROZEN["parent_root"], "common_dir": "/native/.git",
                 "git_dir": "/native/.git", "mode": 493, "ref": None, "head": "a" * 40, "created_dirs": {}, "fingerprint": "b" * 64}
        with patch.object(workflow.preparation, "capture_runtime_copy", return_value=proof), patch.object(workflow, "check_sources", side_effect=lambda directory, saved, state, **kwargs: state):
            code, output, errors = self.cli("record", "--event", str(event), "--revision", revision)
        self.assertEqual((code, errors), (0, ""), output)
        self.assertEqual(workflow.load_run(path)[1]["targets"][FIXTURES.TARGET]["assignments"]["review"]["status"], "running")


if __name__ == "__main__":
    unittest.main()
