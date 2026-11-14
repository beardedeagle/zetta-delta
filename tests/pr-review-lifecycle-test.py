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
                               "ref": "refs/heads/native", "head": "a" * 40}
        return run

    def resolution(self, run, index=0, cause="registered-native-administration"):
        target = run["targets"][FIXTURES.TARGET]
        return {"observation": index, "observation_sha256": workflow.preparation.fingerprint(target["integrity"][index]),
                "check": len(target["integrity"]) - 1, "snapshot_fingerprint": target["frozen"]["source_fingerprint"],
                "native_copies_sha256": workflow.preparation.fingerprint({"copies": workflow.runtime_copies(run)}),
                "cause": cause, "evidence": "Complete recheck matched the immutable original ledger with exact registered settled references."}

    def test_bookkeeping_resolution_retains_incident_and_rejects_stale_or_source_bindings(self):
        run = self.discovered()
        run = workflow.apply_event(run, FIXTURES.event("integrity", {"status": "changed", "components": ["parent"]}))
        run = workflow.apply_event(run, FIXTURES.event("integrity", {"status": "unchanged", "components": []}))
        original = copy.deepcopy(run)
        data = self.resolution(run)
        for field, value in (("observation_sha256", "0" * 64), ("snapshot_fingerprint", "0" * 64),
                             ("native_copies_sha256", "0" * 64), ("check", 0), ("observation", True)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                workflow.apply_event(run, FIXTURES.event("integrity-resolution", dict(data, **{field: value})))
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


if __name__ == "__main__":
    unittest.main()
