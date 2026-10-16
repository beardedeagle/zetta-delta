#!/usr/bin/env python3
"""Run-record acceptance tests; no Delta dispatch, provider calls or GitHub writes."""

import copy
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/pr-review/scripts"
sys.path.insert(0, str(SCRIPTS))
import workflow

REPO = "fixture/project"
TARGET = REPO + "#12"
REGISTRY = {"version": 1, "thread_limit": 15, "metered_max_spawns": 2,
            "providers": [{"provider_id": "provider-a", "lane": "lane-a", "billing": "flat", "limit": 3},
                          {"provider_id": "provider-b", "lane": "lane-b", "billing": "flat", "limit": 2},
                          {"provider_id": "provider-c", "lane": "lane-c", "billing": "flat", "limit": 2}],
            "models": [{"provider_id": "provider-a", "model_id": "model-a", "family": "family-a"},
                       {"provider_id": "provider-b", "model_id": "model-b", "family": "family-b"},
                       {"provider_id": "provider-c", "model_id": "model-c", "family": "family-c"}]}
MODEL_A = {"provider_id": "provider-a", "model_id": "model-a"}
MODEL_B = {"provider_id": "provider-b", "model_id": "model-b"}
MODEL_C = {"provider_id": "provider-c", "model_id": "model-c"}
PULL = {"number": 12, "url": "https://github.com/fixture/project/pull/12", "title": "Fixture", "author": "author",
        "base_sha": "a" * 40, "head_sha": "b" * 40, "classification": "new", "review_required": True,
        "owner_review": None, "discussion_baseline": None, "discussion_decision_required": False,
        "discussion": workflow.intake.discussion_record({"title": "Fixture", "body": "Intent"}, [], [], []),
        "prior_review": None}
COLLECTION = {"repository": REPO, "owner": "owner", "pull_requests": [PULL]}
FROZEN = {"base_sha": "a" * 40, "head_sha": "b" * 40, "diff_base_sha": "c" * 40,
          "mode": "pr-merge-base", "source_root": "/disposable/source", "source_fingerprint": "d" * 64,
          "inventory": ["src/a.py", "src/b.py"], "parent_root": "/user/checkout", "parent_fingerprint": "e" * 64}

CATALOG: dict = {"roster_path": "/machine/roster.md", "roster_sha256": "f" * 64,
           "profiles": {"fixture-profile": {"identity": workflow.model_record(MODEL_A, REGISTRY), "roles": ["candidate"], "worktree": "isolated"},
                        "vet-profile": {"identity": workflow.model_record(MODEL_B, REGISTRY), "roles": ["candidate"], "worktree": "isolated"},
                        "judge-profile": {"identity": workflow.model_record(MODEL_C, REGISTRY), "roles": ["candidate"], "worktree": "isolated"}},
           "units": {"review-0001-000": {"paths": ["src/a.py"], "artifact": "review.patch", "offset": 0, "bytes": 40, "kind": "metadata", "header": "M"},
                     "review-0002-000": {"paths": ["src/b.py"], "artifact": "review.patch", "offset": 40, "bytes": 40, "kind": "metadata", "header": "M"}}}


def plan(perspectives=None, finder="fixture-profile", vetter="vet-profile", stand_in=None):
    families = perspectives or ["family-a"]
    return {"required_perspectives": families, "boundary_evidence": "two interacting paths", "selection_evidence": "explicit capability assessment",
            "profiles": {name: {"decision": "selected" if name in (finder, vetter, stand_in) else "omitted", "primary": name == finder, "evidence": "fixture selection", "capability": rank}
                         for rank, (name, row) in enumerate(CATALOG["profiles"].items(), 1)},
            "coverage": {key: {"risk": "noncode", "families": families, "shortfall": None} for key in CATALOG["units"]},
            "partitioned": False, "interactions": {}, "context": "fixture intent and contracts",
            "routing": {"finder": finder, "vetter": vetter, "stand_in": stand_in,
                        "stand_in_capability": {"relation": "near", "evidence": "high-end peer capability assessment"} if stand_in else None}}


def profile(model):
    return {"model-a": "fixture-profile", "model-b": "vet-profile", "model-c": "judge-profile"}[model["model_id"]]


def references():
    return [{"path": "src/a.py", "line": 42, "side": "target", "relation": "anchor", "evidence": "actual branch"}]


def event(operation, data, target=TARGET):
    return {"target": target, "operation": operation, "data": data}


def wave_decisions(run, eligible=None, ranks=None, parent_active=0, process_active=0):
    allocated = {key for wave in run["batch_waves"] for key in wave["allocations"]}
    return {"capacity": {"parent_active": parent_active, "process_active": process_active, "evidence": "native observation for this parent and Delta process"},
            "reviewers": {key: {"target": copy.deepcopy(target["frozen"]),
                "profiles": {name: {"eligible": name in eligible[key] if eligible is not None else bool(set(profile["roles"]) & {"reviewer", "candidate"}),
                                    "capability": ranks[name] if ranks else rank, "evidence": "discovery suitability and availability assessment"}
                             for rank, (name, profile) in enumerate(target["catalog"]["profiles"].items(), 1)}}
                          for key, target in run["targets"].items() if target["phase"] == "prepared" and target["catalog"] is not None
                          and target["plan"] is None and not target["resources_removed"] and key not in allocated}}


def assignment(key="review", model=MODEL_A, kind="discovery", required=True, **extra):
    return {"id": key, "kind": kind, "profile": profile(model), "role": "candidate", "roles": ["candidate"],
            "model": model, "scope": FROZEN["inventory"], "required": required, "reason": "coherent complete patch",
            "units": list(CATALOG["units"]), "interactions": [],
            "workload": {"input_budget_bytes": 100000, "capacity_bytes": None, "evidence": "unknown capacity; bounded fixture", "rescope": "split if evidence exceeds budget"},
            "test_plan": {"commands": [], "root": None, "evidence": "static trace sufficient"}, **extra}


def partitioned_run(frozen=FROZEN, catalog=CATALOG):
    run = workflow.new_run("pr-review", COLLECTION, REGISTRY, MODEL_C, "thread-fixture")
    run = workflow.apply_event(run, event("freeze", frozen))
    run = workflow.apply_event(run, event("catalog", catalog))
    proposed = plan()
    proposed["coverage"] = {key: {"risk": "noncode", "families": ["family-a"], "shortfall": None} for key in catalog["units"]}
    proposed.update(partitioned=True, interactions={"caller": {"question": "trace the bounded changes together",
                    "units": list(catalog["units"]), "families": ["family-a"], "shortfall": None}})
    return workflow.apply_event(run, event("plan", proposed))


def start(key="review", model=MODEL_A):
    return {"assignment_id": key, "runtime_handle": key + "-handle", "runtime_identity": model,
            "runtime_profile": profile(model),
            "runtime_evidence": "Delta runtime identity metadata for this handle"}


def report(key="review", findings=None, status="done", verdicts=None):
    return {"assignment_id": key, "status": status, "target": FROZEN, "checked": FROZEN["inventory"],
            "findings": findings or [], "evidence": "recorded source/caller evidence", "source_status": "unchanged",
            "reviewer": workflow.model_record(MODEL_B if key in ("vet", "optional") else MODEL_A, REGISTRY),
            "checked_units": list(CATALOG["units"]), "checked_interactions": [],
            "categories": {key: {"status": "checked", "evidence": "concrete category trace"} for key in workflow.CATEGORIES},
            "verification": {"results": [], "not_run": "static caller trace sufficient"},
            "coverage_gaps": [], "pre_existing": [], "learned": [],
            "verdicts": ([vet(assignment_id=key)] if key == "vet" else []) if verdicts is None else verdicts}


def raw(key="review-01", model=MODEL_A, severity="minor"):
    return {"id": key, "finder": workflow.model_record(model, REGISTRY), "severity": severity, "category": "bugs",
            "path": "src/a.py", "line": 42, "side": "target", "trigger": "an empty input", "impact": "raises instead of returning",
            "evidence": "src/a.py:42 and caller src/b.py:3", "fix": "handle empty input", "root_cause": "empty-input", "references": references()}


def vet(key="review-01", assignment_id="vet", verdict="CONFIRMED", severity="minor"):
    return {"finding_id": key, "assignment_id": assignment_id, "verdict": verdict, "severity": severity,
            "category": "bugs", "evidence": "independent src/a.py:42 trace", "fix_assessment": "guard preserves caller contract", "claim": "empty input raises", "references": references()}


def adjudication(key="review-01", disposition="CONFIRMED", severity="minor"):
    return {"finding_id": key, "disposition": disposition, "severity": severity, "category": "bugs",
            "evidence": "separate orchestrator pass of caller and guard", "reason": "trigger and impact established",
            "root_cause": "empty-input", "claim": "empty input raises", "fix_assessment": "guard preserves contract",
            "argument_assessment": "independent trigger evidence supports both passes", "references": references()}


def discussion_pull() -> dict:
    return dict(PULL, classification="unchanged", review_required=False, discussion_decision_required=True,
                owner_review={"id": 1, "commit_id": PULL["head_sha"], "state": "CHANGES_REQUESTED", "submitted_at": "2026-10-09T10:00:00Z"},
                discussion=workflow.intake.discussion_record({"title": "Fixture", "body": "Intent"}, [],
                    [{"id": 20, "body": "Accepting the risk and declining the fix", "user": {"login": "author"}}], []))


def materiality(pull: dict, changes: list | None = None) -> dict:
    return {"base_sha": pull["base_sha"], "head_sha": pull["head_sha"], "discussion_sha256": pull["discussion"]["sha256"],
            "changes": changes or [], "evidence": "Risk acceptance alone does not change the supported trigger or impact"}


class WorkflowTests(unittest.TestCase):
    def test_identity_helper_requires_a_usable_module_loader(self):
        for spec in (None, SimpleNamespace(loader=None)):
            with (
                self.subTest(spec=spec),
                patch.object(workflow.contract.importlib.util, "spec_from_file_location", return_value=spec),
                self.assertRaisesRegex(ImportError, "identity helper cannot be loaded"),
            ):
                workflow.contract.identity_helper()

    def prepared(self, method="pr-review", perspectives=None, orchestrator=MODEL_C, stand_in=None):
        run = workflow.new_run(method, COLLECTION, REGISTRY, orchestrator, "thread-fixture")
        run = workflow.apply_event(run, event("freeze", FROZEN))
        run = workflow.apply_event(run, event("catalog", CATALOG))
        proposed = plan(perspectives, stand_in=stand_in)
        if method == "pr-review-batch":
            run = workflow.apply_event(run, event("batch-wave", wave_decisions(run, {TARGET: {proposed["routing"]["finder"]}}), None))
        return workflow.apply_event(run, event("plan", proposed))

    def discovered(self, method="pr-review", findings=None):
        run = self.prepared(method)
        for operation, data in (("assignment", assignment()), ("started", start()), ("report", report(findings=findings))):
            run = workflow.apply_event(run, event(operation, data))
        return run

    def vetted(self, findings=None, method="pr-review", verdict="CONFIRMED"):
        findings = [raw()] if findings is None else findings
        run = self.discovered(method, findings)
        for operation, data in (("assignment", assignment("vet", MODEL_B, "vet", finding_ids=[r["id"] for r in findings])),
                                ("started", start("vet", MODEL_B)), ("report", report("vet", verdicts=[vet(r["id"], "vet", verdict, r["severity"]) for r in findings]))):
            run = workflow.apply_event(run, event(operation, data))
        return run

    def complete(self, findings=None, method="pr-review"):
        run = self.vetted(findings, method) if findings else self.discovered(method)
        for item in findings or []:
            run = workflow.apply_event(run, event("adjudication", adjudication(item["id"], severity=item["severity"])))
        return workflow.apply_event(run, event("complete", {"coverage_evidence": "all frozen paths and applicable categories reconciled"}))

    def test_freeze_cannot_move_intake_or_reuse_snapshot(self):
        initial = workflow.new_run("pr-review", COLLECTION, REGISTRY, MODEL_C, "thread")
        with self.assertRaisesRegex(ValueError, "moved"):
            workflow.apply_event(initial, event("freeze", dict(FROZEN, head_sha="f" * 40)))
        self.assertIsNone(initial["targets"][TARGET]["frozen"])
        run = workflow.apply_event(initial, event("freeze", FROZEN))
        with self.assertRaisesRegex(ValueError, "transition"):
            workflow.apply_event(run, event("freeze", FROZEN))

    def test_skipped_intake_is_terminal_and_collection_is_repository_separated(self):
        collection = {"owner": "owner", "organization": "fixture", "repositories": [
            {"repository": REPO, "owner": "owner", "pull_requests": [dict(PULL, author="OwNeR", classification="self-authored", review_required=False, discussion=None)]},
            {"repository": "fixture/other", "owner": "owner", "pull_requests": [dict(PULL, classification="unchanged", review_required=False, discussion_baseline=PULL["discussion"])]},
            {"repository": "fixture/empty", "owner": "owner", "pull_requests": []}]}
        run = workflow.new_run("pr-review-batch", collection, REGISTRY, MODEL_C, "thread")
        self.assertEqual(list(run["targets"]), [TARGET, "fixture/other#12"])
        self.assertEqual(len(run["intake"]["repositories"]), 3)
        self.assertTrue(all(r["next"]["status"] == "skipped" for r in workflow.status(run)["targets"]))
        with self.assertRaisesRegex(ValueError, "terminal"):
            workflow.apply_event(run, event("freeze", FROZEN))
        with self.assertRaisesRegex(ValueError, "one PR"):
            workflow.new_run("pr-review", collection, REGISTRY, MODEL_C, "thread")

    def test_own_pr_review_requires_recorded_override_and_replays_for_both_methods(self):
        pull = dict(PULL, author="OwNeR", classification="self-authored-review")
        collection = dict(COLLECTION, pull_requests=[pull])
        for method in workflow.METHODS:
            with self.subTest(method=method):
                with self.assertRaisesRegex(ValueError, "explicit.*override"):
                    workflow.new_run(method, collection, REGISTRY, MODEL_C, "thread")
                run = workflow.new_run(method, collection, REGISTRY, MODEL_C, "thread", review_own_pr=True)
                self.assertEqual(run["targets"][TARGET]["phase"], "intake")
                initial = {"method": method, "intake": collection, "registry": REGISTRY, "orchestrator": MODEL_C,
                           "thread_id": "thread", "run_id": run["run_id"], "review_own_pr": True}
                with tempfile.TemporaryDirectory() as temp:
                    path = Path(temp) / "run.json"
                    workflow.write_json(path, {"version": 1, "initial": initial, "events": []})
                    self.assertEqual(workflow.load_run(path)[1]["targets"][TARGET]["phase"], "intake")
                    del initial["review_own_pr"]
                    workflow.write_json(path, {"version": 1, "initial": initial, "events": []})
                    with self.assertRaisesRegex(ValueError, "explicit.*override"):
                        workflow.load_run(path)

    def test_own_pr_review_cannot_record_publication_intent_or_request_publication(self):
        for method in workflow.METHODS:
            with self.subTest(method=method):
                run = self.complete([], method)
                run["targets"][TARGET]["intake"].update(author="owner", classification="self-authored-review")
                run = workflow.apply_event(run, event("authorization", {"submission": True, "test_execution": False,
                    "metered_spawns": 0, "evidence": "Submission authorization cannot lift the review-only boundary"}))
                self.assertEqual(workflow.status(run)["targets"][0]["next"]["status"], "complete")
                with self.assertRaises(workflow.NeedsDecision) as held:
                    workflow.apply_event(run, event("publication-intent", {}))
                self.assertEqual(held.exception.code, "review_only")
                for command in ("preflight", "publish", "reconcile"):
                    with patch.object(workflow.publication.intake, "api") as api:
                        with self.assertRaises(workflow.NeedsDecision) as held:
                            workflow.publish_command(Path("/unused"), {}, run, TARGET, command, {})
                        self.assertEqual(held.exception.code, "review_only")
                        api.assert_not_called()

    def test_discussion_requires_judgment_and_nonmaterial_risk_acceptance_stays_skipped(self):
        pull = discussion_pull()
        collection = dict(COLLECTION, pull_requests=[pull])
        run = workflow.new_run("pr-review", collection, REGISTRY, MODEL_C, "thread")
        pending = workflow.status(run)["targets"][0]["next"]
        self.assertEqual((pending["status"], pending["code"]), ("needs_decision", "discussion_materiality"))
        self.assertEqual(pending["discussion"], pull["discussion"])
        with self.assertRaisesRegex(ValueError, "transition"):
            workflow.apply_event(run, event("freeze", FROZEN))
        with self.assertRaises(workflow.NeedsDecision):
            workflow.apply_event(run, event("discussion-decision", {}))
        decided = workflow.apply_event(run, event("discussion-decision", materiality(pull)))
        target = decided["targets"][TARGET]
        self.assertEqual(target["phase"], "skipped")
        self.assertFalse(target["intake"]["review_required"])
        self.assertEqual(target["discussion_decision"]["evaluator"], decided["orchestrator"])
        self.assertEqual(run["targets"][TARGET]["phase"], "discussion", "decision must not mutate its input")
        with self.assertRaisesRegex(ValueError, "terminal"):
            workflow.apply_event(decided, event("publication-intent", {}))

    def test_only_material_finding_effects_reopen_same_head_for_both_methods(self):
        pull = discussion_pull()
        for method in workflow.METHODS:
            for effect, before, after in (("add", None, "major"), ("remove", "minor", None), ("severity", "minor", "major")):
                with self.subTest(method=method, effect=effect):
                    run = workflow.new_run(method, dict(COLLECTION, pull_requests=[pull]), REGISTRY, MODEL_C, "thread")
                    change = {"effect": effect, "before": before, "after": after, "finding": "Empty input violates the caller contract",
                              "sources": ["comments:20"], "evidence": "Concrete new caller constraints change this claim"}
                    updated = workflow.apply_event(run, event("discussion-decision", materiality(pull, [change])))
                    target = updated["targets"][TARGET]
                    self.assertEqual((target["phase"], target["intake"]["classification"], target["intake"]["review_required"]),
                                     ("intake", "discussion-changed", True))
                    self.assertEqual(workflow.discussion_baselines(updated)[TARGET]["discussion"], None,
                                     "a material decision cannot mark an unfinished full review as assessed")
                    prepared = workflow.apply_event(updated, event("freeze", FROZEN))
                    self.assertEqual(prepared["targets"][TARGET]["frozen"]["head_sha"], pull["head_sha"])

    def test_discussion_decision_rejects_stale_bindings_unknown_sources_and_invalid_effects(self):
        pull = discussion_pull()
        run = workflow.new_run("pr-review", dict(COLLECTION, pull_requests=[pull]), REGISTRY, MODEL_C, "thread")
        change = {"effect": "severity", "before": "minor", "after": "major", "finding": "Empty input claim",
                  "sources": ["comments:20"], "evidence": "Changed consequence evidence"}
        bad = [dict(materiality(pull), **{field: "f" * size}) for field, size in
               (("base_sha", 40), ("head_sha", 40), ("discussion_sha256", 64))]
        bad.extend(materiality(pull, [dict(change, **patch)]) for patch in
                   ({"sources": ["comments:99"]}, {"before": "major"}, {"after": None}, {"effect": "risk-accepted"},
                    {"effect": "add"}, {"effect": "remove"}))
        for decision in bad:
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                workflow.apply_event(run, event("discussion-decision", decision))
        with self.assertRaisesRegex(ValueError, "eligibility"):
            workflow.new_run("pr-review", dict(COLLECTION, pull_requests=[dict(pull, discussion_decision_required=False)]), REGISTRY, MODEL_C, "thread")

    def test_replayed_decision_supplies_a_reusable_baseline_and_retains_prior_history(self):
        pull = discussion_pull()
        pull["prior_review"] = {"run_id": "older-run", "findings": {"review-01": raw()}}
        collection = dict(COLLECTION, pull_requests=[pull])
        initial = workflow.new_run("pr-review", collection, REGISTRY, MODEL_C, "thread")
        decision = event("discussion-decision", materiality(pull))
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "run.json"
            workflow.write_json(journal, {"version": 1, "initial": {"method": "pr-review", "intake": collection,
                "registry": REGISTRY, "orchestrator": MODEL_B, "thread_id": "thread", "run_id": initial["run_id"]}, "events": [decision]})
            _, replayed = workflow.load_run(journal)
        baseline = workflow.discussion_baselines(replayed)[TARGET]
        self.assertEqual(baseline["discussion"], pull["discussion"])
        self.assertEqual(baseline["prior_review"]["findings"]["review-01"]["finder"], raw()["finder"])
        repeated = dict(pull, discussion_baseline=baseline["discussion"], discussion_decision_required=False)
        run = workflow.new_run("pr-review", dict(COLLECTION, pull_requests=[repeated]), REGISTRY, MODEL_C, "thread")
        self.assertEqual(run["targets"][TARGET]["phase"], "skipped")

    def test_removed_discussion_can_be_cited_from_the_evaluated_baseline(self):
        pull = dict(discussion_pull(), discussion_baseline=discussion_pull()["discussion"], discussion=PULL["discussion"])
        run = workflow.new_run("pr-review", dict(COLLECTION, pull_requests=[pull]), REGISTRY, MODEL_C, "thread")
        change = {"effect": "add", "before": None, "after": "minor", "finding": "Withdrawn constraint exposes the empty-input bug",
                  "sources": ["baseline:comments:20", "description"], "evidence": "The old input restriction is no longer part of the stated contract"}
        updated = workflow.apply_event(run, event("discussion-decision", materiality(pull, [change])))
        self.assertEqual(updated["targets"][TARGET]["phase"], "intake")
        tampered: dict = copy.deepcopy(pull)
        tampered["discussion"]["snapshot"]["description"]["body"] = "Unhashed modification"
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            workflow.new_run("pr-review", dict(COLLECTION, pull_requests=[tampered]), REGISTRY, MODEL_C, "thread")

    def test_task_includes_discussion_and_prior_findings_without_relabeling_provenance(self):
        run = self.prepared()
        history = {"run_id": "older-run", "head_sha": "c" * 40, "findings": {"review-01": raw()}}
        run["targets"][TARGET]["intake"]["prior_review"] = history
        run = workflow.apply_event(run, event("assignment", assignment()))
        task = workflow.contract.task_block(run, TARGET, "review")["task"]
        payload = json.loads(task.splitlines()[1])
        self.assertEqual(payload["pr_discussion"], PULL["discussion"])
        self.assertEqual(payload["prior_review"], history)
        self.assertIn("accepting risk or declining a fix alone does not resolve", task)

    def test_required_perspectives_are_frozen_and_cannot_be_silently_replaced(self):
        with self.assertRaisesRegex(ValueError, "only the primary"):
            self.prepared(perspectives=["family-a", "family-b"])
        run = self.prepared()
        run = workflow.apply_event(run, event("assignment", assignment()))
        run = workflow.apply_event(run, event("started", start()))
        run = workflow.apply_event(run, event("report", report()))
        with self.assertRaisesRegex(ValueError, "transition"):
            workflow.apply_event(run, event("plan", {"required_perspectives": ["family-a"], "boundary_evidence": "change", "selection_evidence": "change"}))

    def test_batch_requires_one_reviewer_and_completed_cross_family_vet(self):
        run = self.discovered("pr-review-batch", [raw()])
        with self.assertRaisesRegex(ValueError, "exactly one"):
            workflow.apply_event(run, event("assignment", assignment("second")))
        with self.assertRaisesRegex(ValueError, "completed cross-family"):
            workflow.apply_event(run, event("vet", vet(assignment_id=None)))
        run = self.vetted(method="pr-review-batch")
        self.assertEqual(run["targets"][TARGET]["vets"]["review-01"]["vetter"], workflow.model_record(MODEL_B, REGISTRY))
        run = workflow.apply_event(run, event("adjudication", adjudication()))
        self.assertEqual(run["targets"][TARGET]["adjudications"]["review-01"]["adjudicator"], run["orchestrator"])

    def test_identity_role_and_stable_assignment_id_validation(self):
        for bad, message in ((dict(assignment(), id="../review"), "IDs"),
                             (dict(assignment(), role="worker", roles=["worker"]), "review role"),
                             (dict(assignment(), role="reviewer"), "selected profile role")):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(ValueError, message),
            ):
                workflow.apply_event(self.prepared(), event("assignment", bad))
        run = workflow.apply_event(self.prepared(), event("assignment", assignment()))
        with self.assertRaisesRegex(ValueError, "already used"):
            workflow.apply_event(run, event("assignment", assignment()))
        with self.assertRaisesRegex(ValueError, "runtime identity"):
            workflow.apply_event(run, event("started", dict(start(model=MODEL_B), runtime_profile="fixture-profile")))
        self.assertEqual(run["targets"][TARGET]["assignments"]["review"]["status"], "queued")

    def test_report_binds_all_target_fields_and_completed_scope(self):
        run = workflow.apply_event(self.prepared(), event("assignment", assignment()))
        run = workflow.apply_event(run, event("started", start()))
        for bad, message in ((dict(report(), target=dict(FROZEN, diff_base_sha="f" * 40)), "frozen target"),
                             (dict(report(), target=TARGET), "frozen target"),
                             (dict(report(), reviewer="provider-a/model-a"), "runtime identity"),
                             (dict(report(), evidence=["source/caller trace"]), "evidence"),
                             (dict(report(), findings=[dict(raw(), finder="review")]), "finder"),
                             (dict(report(), findings=[raw(severity="optional nit")]), "severity"),
                             (dict(report(), checked=["src/a.py"]), "coverage"),
                             (dict(report(), source_status="changed"), "unchanged"),
                             (dict(report(), findings=[dict(raw(), finder=workflow.model_record(MODEL_B, REGISTRY))]), "finder")):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(ValueError, message),
            ):
                workflow.apply_event(run, event("report", bad))

    def test_task_report_template_binds_discovery_and_vet_without_claiming_completion(self):
        for key, model, kind in (("review", MODEL_A, "discovery"), ("vet", MODEL_B, "vet")):
            with self.subTest(kind=kind):
                run = self.prepared() if kind == "discovery" else self.discovered(findings=[raw()])
                extra = {} if kind == "discovery" else {"finding_ids": ["review-01"]}
                run = workflow.apply_event(run, event("assignment", assignment(key, model, kind, **extra)))
                payload = json.loads(workflow.contract.task_block(run, TARGET, key)["task"].splitlines()[1])
                template = payload["report_template"]
                self.assertEqual({name: template[name] for name in ("assignment_id", "target", "reviewer")},
                                 {"assignment_id": key, "target": FROZEN, "reviewer": workflow.model_record(model, REGISTRY)})
                self.assertIsNone(template["status"])
                self.assertIsNone(template["source_status"])
                self.assertEqual(template["evidence"], "")
                self.assertEqual(payload["severity_values"], list(workflow.contract.SEVERITIES))
                self.assertEqual(payload["finding_template"]["finder"], template["reviewer"])
                self.assertEqual(payload["test_plan"], assignment()["test_plan"])
                reply = copy.deepcopy(template)
                reply.update({name: value for name, value in report(key).items()
                              if name not in ("assignment_id", "target", "reviewer", "findings", "verdicts")})
                if kind == "discovery":
                    finding = dict(payload["finding_template"])
                    finding.update({name: value for name, value in raw().items() if name != "finder"})
                    reply["findings"] = [finding]
                else:
                    self.assertEqual(len(reply["verdicts"]), 1)
                    self.assertEqual(reply["verdicts"][0]["finding_id"], "review-01")
                    self.assertEqual(reply["verdicts"][0]["assignment_id"], key)
                    reply["verdicts"][0].update({name: value for name, value in vet().items()
                                                 if name not in ("finding_id", "assignment_id")})
                run = workflow.apply_event(run, event("started", start(key, model)))
                unfilled = dict(copy.deepcopy(template), status="done", source_status="unchanged")
                with self.assertRaises((ValueError, workflow.NeedsDecision)):
                    workflow.apply_event(run, event("report", unfilled))
                run = workflow.apply_event(run, event("report", reply))
                self.assertEqual(run["targets"][TARGET]["assignments"][key]["status"], "done")

    def test_maximum_length_assignment_can_still_record_a_finding(self):
        key = "r" + "x" * 79
        run = self.prepared()
        for operation, data in (("assignment", assignment(key)), ("started", start(key)),
                                ("report", report(key, [raw(key + "-01")]))):
            run = workflow.apply_event(run, event(operation, data))
        self.assertIn(key + "-01", run["targets"][TARGET]["findings"])

    def test_cross_family_vet_is_per_original_raw_id(self):
        run = self.discovered(findings=[raw(), dict(raw("review-02"), trigger="invalid non-empty input", root_cause="invalid-input")])
        with self.assertRaisesRegex(ValueError, "different families"):
            workflow.apply_event(run, event("assignment", assignment("vet", MODEL_A, "vet", finding_ids=["review-01"])))
        for operation, data in (("assignment", assignment("vet", MODEL_B, "vet", finding_ids=["review-01"])),
                                ("started", start("vet", MODEL_B)), ("report", report("vet")), ("vet", vet())):
            run = workflow.apply_event(run, event(operation, data))
        with self.assertRaisesRegex(ValueError, "missing vet"):
            workflow.apply_event(run, event("adjudication", adjudication("review-02")))
        run = workflow.apply_event(run, event("adjudication", adjudication()))
        with self.assertRaisesRegex(ValueError, "UNVETTED"):
            workflow.apply_event(run, event("complete", {"coverage_evidence": "duplicate trigger"}))

    def test_unvetted_is_provisional_and_does_not_consume_focused_reconsideration(self):
        recovery = RecoveryTests()
        for method in workflow.METHODS:
            with self.subTest(method=method):
                run = self.discovered(method=method, findings=[raw()])
                provisional = adjudication(disposition="UNVETTED")
                run = workflow.apply_event(run, event("adjudication", provisional))
                self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "cross_family_vet")
                run = workflow.apply_event(run, event("assignment", assignment("vet", MODEL_B, "vet", finding_ids=["review-01"])))
                run = workflow.apply_event(run, event("started", start("vet", MODEL_B)))
                run = workflow.apply_event(run, event("report", report("vet")))
                self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "adjudication")
                run = workflow.apply_event(run, event("adjudication", adjudication(disposition="DISPUTED")))
                run = recovery.follow_up(run)
                run = workflow.apply_event(run, event("adjudication", adjudication()))
                target = run["targets"][TARGET]
                self.assertEqual([row["disposition"] for row in target["adjudication_history"]["review-01"]], ["UNVETTED", "DISPUTED"])
                self.assertEqual(workflow.completion_gaps(run, target), [])
                with self.assertRaises(ValueError):
                    workflow.apply_event(run, event("adjudication", adjudication()))

    def test_spillover_keeps_its_finder_and_requires_another_family(self):
        run = self.discovered(findings=[raw()])
        for operation, data in (("assignment", assignment("vet", MODEL_B, "vet", finding_ids=["review-01"])),
                                ("started", start("vet", MODEL_B)), ("report", report("vet", [raw("vet-01", MODEL_B)]))):
            run = workflow.apply_event(run, event(operation, data))
        self.assertEqual(run["targets"][TARGET]["findings"]["vet-01"]["finder"]["family"], "family-b")
        with self.assertRaisesRegex(ValueError, "different families"):
            workflow.apply_event(run, event("assignment", assignment("spillover", MODEL_B, "vet", finding_ids=["vet-01"])))
        run = workflow.apply_event(run, event("assignment", assignment("spillover", MODEL_A, "vet", finding_ids=["vet-01"])))
        self.assertEqual(run["targets"][TARGET]["assignments"]["spillover"]["status"], "queued")

    def test_rejected_vet_cannot_be_promoted_and_still_requires_adjudication(self):
        run = self.vetted(verdict="REJECTED")
        with self.assertRaisesRegex(ValueError, "supporting vet"):
            workflow.apply_event(run, event("adjudication", adjudication()))
        with self.assertRaisesRegex(ValueError, "UNADJUDICATED"):
            workflow.apply_event(run, event("complete", {"coverage_evidence": "vet rejected"}))
        run = workflow.apply_event(run, event("adjudication", adjudication(disposition="REJECTED")))
        completed = workflow.apply_event(run, event("complete", {"coverage_evidence": "all paths reconciled"}))
        self.assertEqual(workflow.review_event(completed["targets"][TARGET]), "APPROVE")

    def test_material_gap_and_unresolved_adjudication_hold_completion(self):
        run = self.discovered()
        run = workflow.apply_event(run, event("gap", {"id": "contract", "reason": "caller contract unavailable", "material": True}))
        with self.assertRaisesRegex(ValueError, "caller contract"):
            workflow.apply_event(run, event("complete", {"coverage_evidence": "none found"}))
        run = workflow.apply_event(run, event("resolve-gap", {"id": "contract", "evidence": "caller contract inspected"}))
        run = workflow.apply_event(run, event("complete", {"coverage_evidence": "all paths reconciled"}))
        self.assertEqual(run["targets"][TARGET]["phase"], "complete")
        unresolved = self.vetted(verdict="UNVERIFIABLE")
        unresolved = workflow.apply_event(unresolved, event("adjudication", adjudication(disposition="UNVERIFIABLE")))
        with self.assertRaisesRegex(ValueError, "UNVERIFIABLE"):
            workflow.apply_event(unresolved, event("complete", {"coverage_evidence": "no confirmed findings"}))

    def test_uncovered_paths_and_failed_required_units_hold_even_without_findings(self):
        initial = workflow.new_run("pr-review", COLLECTION, REGISTRY, MODEL_C, "thread")
        initial = workflow.apply_event(initial, event("freeze", FROZEN))
        initial = workflow.apply_event(initial, event("catalog", CATALOG))
        partition = plan()
        partition["partitioned"] = True
        partition["interactions"] = {"caller": {"question": "trace both paths", "units": list(CATALOG["units"]), "families": ["family-a"], "shortfall": {"reason": "one fixture family", "material": False}}}
        run = workflow.apply_event(initial, event("plan", partition))
        unit = dict(assignment(), scope=["src/a.py"], units=["review-0001-000"])
        for operation, data in (("assignment", unit), ("started", start()), ("report", dict(report(), checked=["src/a.py"], checked_units=["review-0001-000"]))):
            run = workflow.apply_event(run, event(operation, data))
        with self.assertRaisesRegex(ValueError, "uncovered changed paths"):
            workflow.apply_event(run, event("complete", {"coverage_evidence": "no findings"}))
        run = self.prepared()
        for operation, data in (("assignment", assignment()), ("started", start()), ("report", report(status="failed"))):
            run = workflow.apply_event(run, event(operation, data))
        with self.assertRaisesRegex(ValueError, "failed"):
            workflow.apply_event(run, event("complete", {"coverage_evidence": "no findings"}))

    def test_partitioned_discovery_finishes_units_and_interactions_before_vetting(self):
        run = partitioned_run()
        for operation, data in (("assignment", assignment(scope=["src/a.py"], units=["review-0001-000"])),
                                ("started", start()),
                                ("report", dict(report(findings=[raw()]), checked=["src/a.py"], checked_units=["review-0001-000"]))):
            run = workflow.apply_event(run, event(operation, data))
        vet_task = event("assignment", assignment("vet", MODEL_B, "vet", finding_ids=["review-01"]))
        self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "discovery_coverage")
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, vet_task)
        self.assertEqual(caught.exception.code, "discovery_coverage")
        remaining = assignment("remainder", scope=["src/b.py"], units=["review-0002-000"], interactions=["caller"])
        run = workflow.apply_event(run, event("assignment", remaining))
        run = workflow.apply_event(run, event("started", start("remainder")))
        run = workflow.apply_event(run, event("report", dict(report("remainder"), checked=["src/b.py"],
                                   checked_units=remaining["units"], checked_interactions=["caller"])))
        self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "cross_family_vet")
        run = workflow.apply_event(run, vet_task)
        self.assertEqual(run["targets"][TARGET]["phase"], "vetting")

    def test_all_publication_events_are_derived_after_completed_adjudication(self):
        for method in workflow.METHODS:
            for severity, expected in ((None, "APPROVE"), ("nit", "APPROVE"), ("minor", "REQUEST_CHANGES"), ("major", "REQUEST_CHANGES"), ("blocker", "REQUEST_CHANGES")):
                with self.subTest(method=method, severity=severity):
                    run = self.complete([raw(severity=severity)] if severity else [], method)
                    self.assertEqual(workflow.status(run)["targets"][0]["derived_event"], expected)

    def test_publication_scope_movement_uncertainty_and_receipt_matching(self):
        run = self.complete()
        intent = {"base_sha": FROZEN["base_sha"], "head_sha": FROZEN["head_sha"], "state": "open", "body": None,
                  "authorization_evidence": "user authorized this PR"}
        with self.assertRaisesRegex(ValueError, "authorization"):
            workflow.apply_event(run, event("publication-intent", intent))
        run = workflow.apply_event(run, event("authorization", {"submission": True, "test_execution": False, "metered_spawns": 0,
                                                              "evidence": intent["authorization_evidence"]}))
        for bad in (dict(intent, base_sha="f" * 40), dict(intent, head_sha="f" * 40), dict(intent, state="closed")):
            with self.assertRaisesRegex(ValueError, "publication held"):
                workflow.apply_event(run, event("publication-intent", bad))
        run = workflow.apply_event(run, event("publication-intent", intent))
        self.assertEqual(workflow.status(run)["targets"][0]["next"]["code"], "reconcile_publication")
        with self.assertRaisesRegex(ValueError, "transition"):
            workflow.apply_event(run, event("publication-intent", intent))
        receipt = {"id": 42, "html_url": "https://github.com/fixture/project/pull/12#pullrequestreview-42", "commit_id": FROZEN["head_sha"],
                   "state": "APPROVED", "body": "", "author": "OwNeR", "evidence": "GitHub receipt metadata"}
        for field, value in (("author", "other"), ("commit_id", "f" * 40), ("state", "COMMENTED"), ("body", "different")):
            with self.assertRaisesRegex(ValueError, "receipt does not match"):
                workflow.apply_event(run, event("publication-receipt", dict(receipt, **{field: value})))
        run = workflow.apply_event(run, event("publication-receipt", receipt))
        self.assertEqual(run["targets"][TARGET]["phase"], "published")
        with self.assertRaisesRegex(ValueError, "terminal"):
            workflow.apply_event(run, event("publication-receipt", receipt))

    def test_nits_require_explicit_nonblocking_body(self):
        run = self.complete([raw(severity="nit")])
        run = workflow.apply_event(run, event("authorization", {"submission": True, "test_execution": False, "metered_spawns": 0, "evidence": "user requested posting"}))
        intent = {"base_sha": FROZEN["base_sha"], "head_sha": FROZEN["head_sha"], "state": "open", "body": "Approved.", "authorization_evidence": "user requested posting"}
        with self.assertRaisesRegex(ValueError, "do not block merging"):
            workflow.apply_event(run, event("publication-intent", intent))
        run = workflow.apply_event(run, event("publication-intent", dict(intent, body="Approved. These nits do not block merging.\n- Nit: clarify the local name.")))
        self.assertEqual(run["targets"][TARGET]["publication"]["event"], "APPROVE")

    def test_original_evidence_is_immutable_and_events_replay(self):
        run = self.vetted()
        original = copy.deepcopy(run)
        changed = workflow.apply_event(run, event("adjudication", adjudication()))
        self.assertEqual(run, original)
        self.assertEqual(changed["targets"][TARGET]["findings"], original["targets"][TARGET]["findings"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.json"
            initial = {"method": run["method"], "intake": COLLECTION, "registry": REGISTRY, "orchestrator": MODEL_C,
                       "thread_id": run["thread_id"], "run_id": run["run_id"]}
            workflow.write_json(path, {"version": 1, "initial": initial, "events": changed["events"]})
            self.assertEqual(workflow.load_run(path)[1], changed)
            saved = workflow.read_json(path)
            saved["events"].append(event("freeze", FROZEN))
            workflow.write_json(path, saved)
            with self.assertRaisesRegex(ValueError, "transition"):
                workflow.load_run(path)

    def test_status_distinguishes_publication_from_native_dispatch_and_isolation(self):
        run = self.discovered()
        with patch.object(workflow.subprocess, "run", side_effect=AssertionError("recording must not dispatch or post")):
            state = workflow.status(run)
        self.assertEqual(state["limits"], {"per_thread": 15, "overall": 30})
        self.assertEqual(state["enforcement"], {"preparation": True, "write_detection": True,
                                               "dispatch_gate": True, "dispatch": False, "write_prevention": False, "publication": True})
        self.assertEqual(state["targets"][0]["next"]["status"], "needs_decision")

    def test_precise_next_decisions_distinguish_method_judgments(self):
        for method, code in (("pr-review", "cross_family_vet"), ("pr-review-batch", "cross_family_vet")):
            run = self.discovered(method, [raw()])
            decision = workflow.status(run)["targets"][0]["next"]
            self.assertEqual(decision["code"], code)
            self.assertEqual(decision["finding_ids"], ["review-01"])
            run = self.vetted(method=method)
            self.assertEqual(workflow.status(run)["targets"][0]["next"]["code"], "adjudication")
        run = workflow.apply_event(self.prepared(), event("assignment", assignment()))
        self.assertEqual(workflow.status(run)["targets"][0]["next"]["code"], "task_preflight")
        run = workflow.apply_event(run, event("started", start()))
        self.assertEqual(workflow.status(run)["targets"][0]["next"]["code"], "runtime_completion")

    def test_source_changes_hold_other_targets_and_optional_work_cannot_hide_them(self):
        for origin in ("observation", "report", "rejected-report"):
            for source_status in ("changed", "unknown"):
                with self.subTest(origin=origin, source_status=source_status):
                    run = self.discovered(findings=[raw()])
                    run = workflow.apply_event(run, event("assignment", assignment("optional", MODEL_B, "vet", required=False, finding_ids=["review-01"])))
                    run = workflow.apply_event(run, event("started", start("optional", MODEL_B)))
                    if origin == "observation":
                        failure = event("integrity", {"status": source_status, "components": ["source"]})
                    elif origin == "report":
                        failure = event("report", dict(report("optional", status="partial"), source_status=source_status))
                    else:
                        failure = event("report-input", {"assignment_id": "optional", "runtime_handle": start("optional", MODEL_B)["runtime_handle"],
                            "target": FROZEN, "report": {}, "evidence": "incomplete native reply", "source_status": source_status})
                    run = workflow.apply_event(run, failure)
                    with self.assertRaisesRegex(ValueError, "source integrity"):
                        workflow.apply_event(run, event("complete", {"coverage_evidence": "required coverage completed"}))
                    with self.assertRaisesRegex(ValueError, "hold further dispatch"):
                        workflow.apply_event(run, event("assignment", assignment("another", required=False)))
                    with self.assertRaises(workflow.NeedsDecision) as caught:
                        workflow.apply_event(run, event("batch-wave", {}, None))
                    self.assertEqual(caught.exception.code, "source_integrity")
                    decision = workflow.status(run)["targets"][0]["next"]
                    self.assertEqual(decision["code"], "source_integrity")
                    field = "target_ids" if origin == "observation" else "assignment_ids"
                    self.assertEqual(decision[field], [TARGET if origin == "observation" else "optional"])

    def test_completed_vet_reports_register_judgments_and_replay_legacy_records(self):
        findings = [dict(raw(f"review-{number:02}"), trigger=f"input case {number}", root_cause=f"case-{number}") for number in range(1, 4)]
        for method in workflow.METHODS:
            with self.subTest(method=method):
                run = self.vetted(findings, method)
                target = run["targets"][TARGET]
                expected = {item["id"]: dict(vet(item["id"]), vetter=workflow.model_record(MODEL_B, REGISTRY)) for item in findings}
                self.assertEqual(target["vets"], expected)
                self.assertEqual(target["adjudications"], {})
                self.assertEqual(workflow.next_decision(run, target)["code"], "adjudication")
                self.assertEqual(run["events"][-1]["operation"], "report")
                legacy = run
                for item in findings:
                    legacy = workflow.apply_event(legacy, event("vet", vet(item["id"])))
                self.assertEqual(legacy["targets"][TARGET]["vets"], expected)
                self.assertEqual(legacy["targets"][TARGET]["vet_history"], {})
                with self.assertRaisesRegex(ValueError, "already recorded"):
                    workflow.apply_event(legacy, event("vet", vet()))
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "run.json"
                    initial = {"method": method, "intake": COLLECTION, "registry": REGISTRY, "orchestrator": MODEL_C,
                               "thread_id": run["thread_id"], "run_id": run["run_id"]}
                    workflow.write_json(path, {"version": 1, "initial": initial, "events": legacy["events"]})
                    self.assertEqual(workflow.load_run(path)[1], legacy)
                for item in findings:
                    run = workflow.apply_event(run, event("adjudication", adjudication(item["id"])))
                run = workflow.apply_event(run, event("complete", {"coverage_evidence": "all claims adjudicated after automatic vet registration"}))
                self.assertEqual(run["targets"][TARGET]["phase"], "complete")


class DispatchGateTests(unittest.TestCase):
    prepared = WorkflowTests.prepared

    def queued(self):
        return workflow.apply_event(self.prepared(), event("assignment", assignment()))

    def capacity(self, parent=0, process=0):
        return {"parent_active": parent, "process_active": process, "evidence": "fresh native active counts",
                "runtime_parent_root": FROZEN["parent_root"]}

    def test_pending_launch_requires_its_exact_registration_or_withdrawal(self):
        run = workflow.apply_event(self.queued(), event("task-ready", {"assignment_id": "review", "capacity": self.capacity()}))
        for settling in (None, (TARGET, "other"), ("other/repo#1", "review")):
            with self.subTest(settling=settling), self.assertRaises(workflow.NeedsDecision) as caught:
                workflow.require_registered_launch(run, settling)
            self.assertEqual(caught.exception.code, "runtime_registration")
        workflow.require_registered_launch(run, (TARGET, "review"))
        historical = copy.deepcopy(run)
        historical["targets"][TARGET]["assignments"]["other"] = dict(historical["targets"][TARGET]["assignments"]["review"], id="other")
        workflow.require_registered_launch(historical, (TARGET, "other"))
        started = workflow.apply_event(run, event("started", start()))
        workflow.require_registered_launch(started)
        failed = workflow.apply_event(run, event("integrity", {"status": "changed", "components": ["parent"]}))
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.require_registered_launch(failed)
        self.assertEqual(caught.exception.code, "source_integrity")

    def test_unknown_counts_require_explicit_native_admission_and_keep_local_limits(self):
        run = self.queued()
        capacity = self.capacity(None, None)
        with self.assertRaises(ValueError):
            workflow.contract.native_capacity(run, capacity, "review")
        run["admission_mode"] = "native"
        self.assertEqual(workflow.contract.native_capacity(run, capacity, "review"), (15, 0))
        for counts in ((0, 0), (None, 0), (0, None)):
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                workflow.contract.native_capacity(run, self.capacity(*counts), "review")
        rows = run["targets"][TARGET]["assignments"]
        rows.update({f"active-{i}": {"status": "running"} for i in range(15)})
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.contract.native_capacity(run, capacity, "review")
        self.assertEqual(caught.exception.code, "native_capacity")
        rows.pop("active-14")
        self.assertEqual(workflow.contract.native_capacity(run, capacity, "review"), (1, 0))
        with self.assertRaises(workflow.NeedsDecision):
            workflow.contract.native_capacity(run, capacity)

    def test_captured_parent_rejects_another_registered_root(self):
        run = self.queued()
        run.update(admission_mode="observed", native_parent=None)
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.contract.dispatch_task(run, TARGET, "review", self.capacity())
        self.assertEqual(caught.exception.code, "runtime_parent")
        observation = {"root": "/native/actual-parent", "thread_id": run["thread_id"], "source": "terminal-cwd"}
        run = workflow.apply_event(run, event("native-parent", observation, None))
        run = workflow.apply_event(run, event("baseline", {
            "id": "actual-parent", "root": observation["root"], "fingerprint": "f" * 64,
            "artifact": "baseline-" + "a" * 32 + ".json", "artifact_sha256": "b" * 64}))
        with self.assertRaisesRegex(ValueError, "captured native parent"):
            workflow.contract.dispatch_task(run, TARGET, "review", self.capacity())
        capacity = dict(self.capacity(), runtime_parent_root=observation["root"])
        task = workflow.contract.dispatch_task(run, TARGET, "review", capacity)
        self.assertEqual(task["dispatch"]["runtime_parent_root"], observation["root"])
        with self.assertRaisesRegex(ValueError, "already captured"):
            workflow.apply_event(run, event("native-parent", observation, None))

    def test_dispatch_requires_registered_native_parent_and_binds_start_to_it(self):
        run = self.queued()
        for root in (None, "/unregistered/project"):
            capacity = self.capacity()
            if root is None:
                del capacity["runtime_parent_root"]
            else:
                capacity["runtime_parent_root"] = root
            with self.subTest(root=root):
                with self.assertRaises(workflow.NeedsDecision) as caught:
                    workflow.contract.dispatch_task(run, TARGET, "review", capacity)
                self.assertEqual(caught.exception.code, "runtime_parent")
        run = workflow.apply_event(run, event("baseline", {
            "id": "native-project", "root": "/native/project", "fingerprint": "f" * 64,
            "artifact": "baseline-" + "a" * 32 + ".json", "artifact_sha256": "b" * 64}))
        capacity = dict(self.capacity(), runtime_parent_root="/native/project")
        run = workflow.apply_event(run, event("task-ready", {"assignment_id": "review", "capacity": capacity}))
        self.assertEqual(workflow.contract.dispatch_task(run, TARGET, "review", capacity)["dispatch"]["runtime_parent_root"], "/native/project")
        proof = {"root": "/runtime/copy", "parent_root": FROZEN["parent_root"],
                 "common_dir": "/runtime/copy/.git", "git_dir": "/runtime/copy/.git", "mode": 0o755,
                 "ref": None, "head": FROZEN["head_sha"], "created_dirs": {}, "fingerprint": "f" * 64}
        with self.assertRaisesRegex(ValueError, "approved native parent"):
            workflow.apply_event(run, event("started", dict(start(), runtime_root=proof["root"], runtime_copy=proof)))
        proof["parent_root"] = "/native/project"
        started = workflow.apply_event(run, event("started", dict(start(), runtime_root=proof["root"], runtime_copy=proof)))
        self.assertEqual(started["targets"][TARGET]["assignments"]["review"]["status"], "running")

    def test_legacy_task_approval_replays_without_certifying_new_dispatch(self):
        run = self.queued()
        capacity = self.capacity()
        del capacity["runtime_parent_root"]
        old = event("task-ready", {"assignment_id": "review", "capacity": capacity})
        replayed = workflow.apply_event(run, old, replay=True)
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.contract.dispatch_task(replayed, TARGET, "review", capacity)
        self.assertEqual(caught.exception.code, "runtime_parent")

    def test_native_project_ownership_requires_registration_and_retains_dispatch_parent(self):
        run = self.queued()
        project = "/native/project"
        parent = project + "/.delta/worktrees/parent/repo"
        for index, (name, root) in enumerate((("project", project), ("parent", parent))):
            run = workflow.apply_event(run, event("baseline", {
                "id": name, "root": root, "fingerprint": "f" * 64,
                "artifact": f"baseline-{index:032x}.json", "artifact_sha256": "b" * 64}))
        capacity = dict(self.capacity(), runtime_parent_root=parent)
        run = workflow.apply_event(run, event("task-ready", {"assignment_id": "review", "capacity": capacity}))
        root = project + "/.delta/worktrees/child/repo"
        proof = {"root": root, "parent_root": parent, "project_root": project,
                 "common_dir": project + "/.delta/clones/child/repo.git",
                 "git_dir": project + "/.delta/clones/child/repo.git", "mode": 0o755,
                 "ref": None, "head": FROZEN["head_sha"], "created_dirs": {}, "fingerprint": "f" * 64,
                 "clone_store": {"ref": f"refs/delta/child/repo/{FROZEN['head_sha']}",
                                 "ref_store": project + "/.git", "fingerprint": "e" * 64}}
        started = workflow.apply_event(run, event("started", dict(start(), runtime_root=root, runtime_copy=proof)))
        self.assertEqual(started["targets"][TARGET]["assignments"]["review"]["runtime_copy"]["parent_root"], parent)
        for change in ({"project_root": "/unregistered"}, {"project_root": FROZEN["parent_root"]},
                       {"parent_root": project}, {"created_dirs": {".delta/user-content": ["directory", 0o755]}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                workflow.apply_event(run, event("started", dict(start(), runtime_root=root,
                                                               runtime_copy=dict(proof, **change))))
        without_store = dict(proof)
        del without_store["clone_store"]
        with self.assertRaisesRegex(ValueError, "native project ownership"):
            workflow.apply_event(run, event("started", dict(start(), runtime_root=root, runtime_copy=without_store)))

    def test_emitted_source_commands_preserve_index_and_skip_cache_creation(self):
        run = self.queued()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve() / "source's with spaces"
            source.mkdir()
            workflow.preparation.git(source, "init", "-q")
            (source / "feature.txt").write_text("feature\n")
            workflow.preparation.git(source, "add", "feature.txt")
            workflow.preparation.git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "fixture")
            run["targets"][TARGET]["frozen"]["source_root"] = str(source)
            task = workflow.contract.task_block(run, TARGET, "review")["task"]
            payload = json.loads(task.splitlines()[1])
            index = (source / ".git/index").read_bytes()
            os.utime(source / "feature.txt", (1, 1))
            for command in (payload["source_git_prefix"] + " status --porcelain",
                            payload["source_search_prefix"] + " -F -- feature " + shlex.quote(str(source))):
                result = subprocess.run(command, shell=True, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((source / ".git/index").read_bytes(), index)
            self.assertEqual(sorted(path.name for path in source.iterdir()), [".git", "feature.txt"])
            self.assertIn("Do not run SessionStart maintenance", task)

    def test_launch_uses_its_reserved_slot_and_preserves_both_native_limits(self):
        run = self.queued()
        task = workflow.contract.dispatch_task(run, TARGET, "review", self.capacity(14, 29))
        self.assertEqual(task["dispatch"]["action"], "spawn")
        self.assertEqual(task["dispatch"]["identity"], workflow.model_record(MODEL_A, REGISTRY))
        for capacity in (self.capacity(15, 29), self.capacity(14, 30), self.capacity(0, 31), self.capacity(2, 1)):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                workflow.contract.dispatch_task(run, TARGET, "review", capacity)
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.contract.dispatch_task(run, TARGET, "review", {})
        self.assertEqual(caught.exception.code, "native_capacity")

    def test_queued_task_rechecks_lane_availability(self):
        run = self.queued()
        run["unavailable_lanes"]["lane-a"] = {"cause": "auth", "evidence": "native failure on another assignment"}
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.contract.dispatch_task(run, TARGET, "review", self.capacity())
        self.assertEqual(caught.exception.code, "unavailable_lane")

    def test_task_emission_and_fresh_approval_honor_revoked_test_authorization(self):
        for recovery_kind in (None, "retry", "correction"):
            with self.subTest(recovery=recovery_kind):
                run = self.prepared()
                auth = {"submission": False, "test_execution": True, "metered_spawns": 0, "evidence": "user permits tests"}
                run = workflow.apply_event(run, event("authorization", auth))
                root = "/disposable/test-" + "a" * 32
                run = workflow.apply_event(run, event("test-copy", {"root": root}))
                run = workflow.apply_event(run, event("assignment", assignment(test_plan={"commands": ["rtk proxy python3 -m unittest"],
                                               "root": root, "evidence": "focused execution"})))
                key = "review"
                if recovery_kind:
                    run = workflow.apply_event(run, event("started", start()))
                    recovery = RecoveryTests()
                    run = recovery.outcome(run) if recovery_kind == "retry" else recovery.receive(run, {"status": "done"})
                    key = "recovered"
                    run = workflow.apply_event(run, event("recover", recovery.request(run, recovery_kind, key=key)))
                run = workflow.apply_event(run, event("authorization", dict(auth, test_execution=False, evidence="user revokes tests")))
                before = copy.deepcopy(run)
                for action in (lambda run=run, key=key: workflow.contract.task_block(run, TARGET, key),
                               lambda run=run, key=key: workflow.contract.dispatch_task(run, TARGET, key, self.capacity()),
                               lambda run=run, key=key: workflow.apply_event(run, event("task-ready", {"assignment_id": key, "capacity": self.capacity()}))):
                    with self.assertRaises(workflow.NeedsDecision) as caught:
                        action()
                    self.assertEqual(caught.exception.code, "test_authorization")
                self.assertEqual(run, before)
                run = workflow.apply_event(run, event("authorization", auth))
                self.assertIn("rtk proxy python3 -m unittest", workflow.contract.dispatch_task(run, TARGET, key, self.capacity())["task"])

    def test_more_waiting_units_than_native_slots_can_drain_without_withdrawal(self):
        frozen = dict(FROZEN, inventory=[f"src/f{index}.py" for index in range(17)])
        catalog = copy.deepcopy(CATALOG)
        catalog["units"] = {f"review-{index:04d}-000": dict(next(iter(CATALOG["units"].values())), paths=[path], offset=index * 40)
                            for index, path in enumerate(frozen["inventory"])}
        run = partitioned_run(frozen, catalog)
        for index, (key, unit) in enumerate(catalog["units"].items()):
            run = workflow.apply_event(run, event("assignment", assignment(f"unit{index}", scope=unit["paths"], units=[key])))
        for index in range(15):
            key = f"unit{index}"
            run = workflow.apply_event(run, event("task-ready", {"assignment_id": key, "capacity": self.capacity(index, index)}))
            run = workflow.apply_event(run, event("started", start(key)))
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.contract.dispatch_task(run, TARGET, "unit15", self.capacity(15, 15))
        self.assertEqual(caught.exception.code, "native_capacity")
        first = dict(report("unit0"), target=frozen, checked=[frozen["inventory"][0]], checked_units=["review-0000-000"])
        run = workflow.apply_event(run, event("report", first))
        run = workflow.apply_event(run, event("task-ready", {"assignment_id": "unit15", "capacity": self.capacity(14, 29)}))
        run = workflow.apply_event(run, event("started", start("unit15")))
        self.assertEqual(sum(row["status"] == "running" for row in run["targets"][TARGET]["assignments"].values()), 15)

    def test_follow_up_reuses_the_exact_idle_handle_and_requires_new_approval(self):
        recovery = RecoveryTests()
        run = recovery.running()
        run = recovery.receive(run, {"status": "done"})
        run["targets"][TARGET]["assignments"]["review"]["task_ready"] = {"revision": run["revision"], "capacity": self.capacity()}
        run = workflow.apply_event(run, event("recover", recovery.request(run, "correction", key="correct")))
        self.assertNotIn("task_ready", run["targets"][TARGET]["assignments"]["correct"])
        task = workflow.contract.dispatch_task(run, TARGET, "correct", self.capacity())
        self.assertEqual(task["dispatch"]["action"], "follow-up")
        self.assertEqual(task["runtime_handle"], "review-handle")
        run["targets"][TARGET]["assignments"]["review"]["status"] = "running"
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.contract.dispatch_task(run, TARGET, "correct", self.capacity(1, 1))
        self.assertEqual(caught.exception.code, "runtime_reconciliation")

    def test_cli_requires_generated_current_approval_and_records_no_native_launch(self):
        run = self.queued()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            initial = {"method": run["method"], "intake": run["intake"], "registry": run["registry"],
                       "orchestrator": MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"]}
            workflow.write_json(root / "run.json", {"version": 1, "initial": initial, "events": run["events"]})
            workflow.write_json(root / "capacity.json", self.capacity())

            def call(command, *arguments):
                current = workflow.load_run(root / "run.json")[1]
                argv = ["workflow.py", "--run", str(root), command, "--revision", str(current["revision"]), *arguments]
                output = io.StringIO()
                with (patch.object(sys, "argv", argv), patch.object(workflow, "check_sources", return_value=current),
                      patch.object(workflow, "parent_ledgers", return_value={}),
                      patch.object(workflow.preparation, "capture_runtime_copy", return_value={
                          "root": "/runtime/copy", "parent_root": FROZEN["parent_root"], "common_dir": "/runtime/copy/.git",
                          "git_dir": "/runtime/copy/.git", "mode": 0o755,
                          "ref": None, "head": FROZEN["head_sha"], "created_dirs": {}, "fingerprint": "f" * 64}),
                      patch.object(workflow.subprocess, "run", side_effect=AssertionError("gate must not launch")),
                      patch.object(sys, "stdout", output), patch.object(sys, "stderr", io.StringIO())):
                    code = workflow.main()
                return code, json.loads(output.getvalue()) if output.getvalue() else None

            workflow.write_json(root / "event.json", event("started", start()))
            code, output = call("record", "--event", str(root / "event.json"))
            self.assertEqual(code, 2)
            self.assertEqual(output["code"], "task_preflight")
            workflow.write_json(root / "event.json", event("task-ready", {"assignment_id": "review", "capacity": self.capacity()}))
            self.assertEqual(call("record", "--event", str(root / "event.json"))[0], 1)

            def task():
                return call("task", "--target", TARGET, "--assignment", "review", "--capacity", str(root / "capacity.json"))

            original = (root / "run.json").read_bytes()
            with patch.object(workflow, "write_json", side_effect=OSError("approval persistence failed")):
                self.assertEqual(task(), (1, None))
            self.assertEqual((root / "run.json").read_bytes(), original)
            self.assertEqual(task()[0], 0)
            saved, approved = workflow.load_run(root / "run.json")
            self.assertEqual(saved["events"][-1]["operation"], "task-ready")
            self.assertEqual(workflow.status(approved)["targets"][0]["next"]["code"], "native_dispatch")
            current_approval = (root / "run.json").read_bytes()
            workflow.append_record(root, saved, approved, TARGET, "integrity", {"status": "unchanged", "components": []})
            workflow.write_json(root / "event.json", event("started", start()))
            self.assertEqual(call("record", "--event", str(root / "event.json"))[0], 2)
            code, generated = task()
            self.assertEqual((code, generated["code"]), (2, "runtime_registration"))
            # Fork the original fixture approval to exercise valid registration
            # separately from the deliberately stale historical branch.
            (root / "run.json").write_bytes(current_approval)
            code, output = call("record", "--event", str(root / "event.json"))
            self.assertEqual((code, output["code"]), (2, "runtime_copy"))
            original = (root / "run.json").read_bytes()
            workflow.write_json(root / "event.json", event("started", dict(start(), runtime_root="/runtime/copy", runtime_copy={})))
            self.assertEqual(call("record", "--event", str(root / "event.json"))[0], 1)
            self.assertEqual((root / "run.json").read_bytes(), original)
            workflow.write_json(root / "event.json", event("started", dict(start(), runtime_root="/runtime/copy")))
            self.assertEqual(call("record", "--event", str(root / "event.json"))[0], 0)
            self.assertEqual(workflow.load_run(root / "run.json")[1]["targets"][TARGET]["assignments"]["review"]["status"], "running")


class FileHandoffTests(unittest.TestCase):
    queued = DispatchGateTests.queued
    prepared = WorkflowTests.prepared
    discovered = WorkflowTests.discovered
    capacity = DispatchGateTests.capacity

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        run = self.queued()
        run = workflow.apply_event(run, event("integrity", {"status": "unchanged", "components": []}))
        initial = {"method": run["method"], "intake": run["intake"], "registry": run["registry"],
                   "orchestrator": MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"]}
        self.saved = {"version": 1, "initial": initial, "events": run["events"]}
        workflow.write_json(self.root / "run.json", self.saved)
        self.dispatch = workflow.issue_handoff(self.root, self.saved, run, TARGET, "review", self.capacity())

    def load(self):
        return workflow.load_run(self.root / "run.json")

    def begin(self):
        saved, run = self.load()
        return workflow.append_record(self.root, saved, run, TARGET, "started", start())

    def judgments(self):
        result = report(findings=[raw()])
        for key in ("assignment_id", "target", "reviewer"):
            result.pop(key)
        result["findings"][0].pop("finder")
        result["evidence"] = "Exact observations: café, λ and punctuation."
        return result

    def author(self, raw_input, assignment_id="review"):
        path = self.root / "judgments.json"
        path.write_bytes(raw_input)
        saved, run = self.load()
        return workflow.author_report(self.root, saved, run, TARGET, assignment_id, path)

    def receive(self, handle="review-handle", assignment_id="review"):
        _, run = self.load()
        output = io.StringIO()
        argv = ["workflow.py", "--run", str(self.root), "receive-report", "--target", TARGET,
                "--assignment", assignment_id, "--runtime-handle", handle, "--evidence", "native completion receipt",
                "--revision", str(run["revision"])]
        with (patch.object(sys, "argv", argv), patch.object(workflow, "check_sources", return_value=run),
              patch.object(sys, "stdout", output), patch.object(sys, "stderr", io.StringIO())):
            code = workflow.main()
        return code

    def test_compact_bootstrap_reads_exact_task_and_stale_approval_cannot_be_used(self):
        saved, run = self.load()
        task = workflow.handoff_task(self.root, run, TARGET, "review")
        self.assertEqual(task["report_template"]["target"], FROZEN)
        self.assertEqual(task["report_template"]["reviewer"], workflow.model_record(MODEL_A, REGISTRY))
        self.assertIn("task-read", self.dispatch["task"])
        self.assertNotIn(FROZEN["parent_fingerprint"], self.dispatch["task"])
        self.assertIn("author-report", task["task"])
        view = workflow.handoff.task_view(task, "task-read")
        self.assertIn("FILE REPORT HANDOFF:", view[:2048])
        self.assertIn("author-report", view[:2048])
        self.assertNotIn(FROZEN["parent_fingerprint"], view)
        run = workflow.append_record(self.root, saved, run, TARGET, "integrity", {"status": "unchanged", "components": []})
        with self.assertRaisesRegex(ValueError, "current queued approval"):
            workflow.handoff_task(self.root, run, TARGET, "review")

    def test_pending_native_clone_is_not_checked_before_started_registration(self):
        before = (self.root / "run.json").read_bytes()
        for command in ("task-read", "author-report"):
            argv = ["workflow.py", "--run", str(self.root), command, "--target", TARGET, "--assignment", "review"]
            if command == "author-report":
                argv += ["--input", str(self.root / "not-authored-yet.json")]
            output = io.StringIO()
            with (patch.object(sys, "argv", argv), patch.object(sys, "stdout", output),
                  patch.object(workflow, "check_sources", side_effect=AssertionError("pending clone must not be fingerprinted"))):
                self.assertEqual(workflow.main(), 2)
            self.assertEqual(json.loads(output.getvalue())["code"], "runtime_registration")
            self.assertEqual((self.root / "run.json").read_bytes(), before)
        self.begin()
        _, run = self.load()
        output = io.StringIO()
        argv = ["workflow.py", "--run", str(self.root), "task-read", "--target", TARGET, "--assignment", "review"]
        with (patch.object(sys, "argv", argv), patch.object(sys, "stdout", output),
              patch.object(workflow, "check_sources", return_value=run) as check):
            self.assertEqual(workflow.main(), 0)
        check.assert_called_once()
        self.assertIn("FILE REPORT HANDOFF", output.getvalue())

    def test_task_corruption_and_redirected_artifacts_are_refused(self):
        _, run = self.load()
        path = self.root / self.dispatch["handoff"]["artifact"]
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            workflow.handoff_task(self.root, run, TARGET, "review")
        path.write_bytes(original)
        external = self.root / "replacement.json"
        external.write_bytes(original)
        path.unlink()
        path.symlink_to(external)
        with self.assertRaises(OSError):
            workflow.handoff_task(self.root, run, TARGET, "review")
        path.unlink()
        path.write_bytes(original)
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "permissions changed"):
            workflow.handoff_task(self.root, run, TARGET, "review")
        path.chmod(0o600)
        parent = path.parent
        renamed = parent.with_name("redirected")
        parent.rename(renamed)
        parent.symlink_to(renamed, target_is_directory=True)
        with self.assertRaises(OSError):
            workflow.handoff_task(self.root, run, TARGET, "review")

    def test_reviewer_judgments_and_received_report_bytes_are_preserved(self):
        self.begin()
        judgments = self.judgments()
        original = (json.dumps(judgments, ensure_ascii=False, indent=3) + "\n\n").encode()
        proof = self.author(original)
        self.assertEqual(workflow.handoff.read(self.root, proof["input"]), original)
        sealed = workflow.handoff.read(self.root, proof["report"])
        full = json.loads(sealed)
        self.assertEqual(full["target"], FROZEN)
        self.assertEqual(full["findings"][0]["finder"], full["reviewer"])
        self.assertEqual(full["evidence"], judgments["evidence"])
        self.assertEqual(self.receive("another-handle"), 1)
        self.assertEqual(self.receive(), 0)
        saved, run = self.load()
        self.assertEqual(run["targets"][TARGET]["assignments"]["review"]["report"], full)
        self.assertEqual(saved["events"][-1]["data"]["report"].encode(), sealed)
        self.assertEqual(self.receive(), 1)

    def test_parent_cannot_rewrite_an_authored_report_or_receive_changed_bytes(self):
        self.begin()
        proof = self.author(json.dumps(self.judgments()).encode())
        _, run = self.load()
        data = {"assignment_id": "review", "runtime_handle": "review-handle", "target": FROZEN,
                "report": "{}", "evidence": "native receipt", "source_status": "unchanged"}
        with self.assertRaisesRegex(ValueError, "sealed bytes"):
            workflow.apply_event(run, event("report-input", data))
        with self.assertRaisesRegex(ValueError, "original report-input"):
            workflow.apply_event(run, event("report", report()))
        (self.root / proof["report"]["artifact"]).write_bytes(b"{}")
        before = (self.root / "run.json").read_bytes()
        self.assertEqual(self.receive(), 1)
        self.assertEqual((self.root / "run.json").read_bytes(), before)

    def test_invalid_author_inputs_remain_drafts_without_consuming_a_correction(self):
        for original in (b"{broken JSON\n", b"\xff", json.dumps(report()).encode()):
            with self.subTest(original=original):
                self.setUp()
                self.begin()
                proof = self.author(original)
                self.assertIn("assembly_error", proof)
                self.assertEqual(workflow.handoff.read(self.root, proof["input"]), original)
                self.assertEqual(workflow.handoff.read(self.root, proof["report"]), original)
                self.assertEqual(proof["code"], "report_draft")
                self.assertEqual(self.receive(), 1)
                _, run = self.load()
                row = run["targets"][TARGET]["assignments"]["review"]
                self.assertNotIn("rejected_report", row)
                self.assertNotIn("authored_report", row)
                self.assertEqual(row["drafts"][0]["input"], proof["input"])
                self.assertEqual(row["status"], "running")

    def test_nested_bindings_are_generated_without_changing_vet_judgments(self):
        template = {"assignment_id": "vet", "target": FROZEN, "reviewer": workflow.model_record(MODEL_B, REGISTRY)}
        judgments = {"verdicts": [{key: value for key, value in vet().items() if key != "assignment_id"}]}
        full = workflow.handoff.assemble(template, judgments)
        self.assertEqual(full["verdicts"], [vet()])
        self.assertNotIn("assignment_id", judgments["verdicts"][0])
        with self.assertRaisesRegex(ValueError, "omitting assignment_id"):
            workflow.handoff.assemble(template, {"verdicts": [vet()]})

    def test_file_retry_preserves_retained_finder_and_binds_only_new_claims(self):
        registry: dict = copy.deepcopy(REGISTRY)
        alternate = {"provider_id": "provider-b", "model_id": "model-a"}
        registry["models"].append(dict(alternate, family="family-a"))
        catalog = copy.deepcopy(CATALOG)
        catalog["profiles"]["alternate"] = dict(catalog["profiles"]["fixture-profile"], identity=workflow.model_record(alternate, registry))
        run = workflow.new_run("pr-review", COLLECTION, registry, MODEL_C, "thread")
        run = workflow.apply_event(run, event("freeze", FROZEN))
        run = workflow.apply_event(run, event("catalog", catalog))
        proposed = plan()
        proposed["profiles"]["alternate"] = {"decision": "omitted", "primary": False, "evidence": "verified available same-model route", "capability": 1}
        run = workflow.apply_event(run, event("plan", proposed))
        run = workflow.apply_event(run, event("assignment", assignment()))
        run = workflow.apply_event(run, event("started", start()))
        original = raw()
        run = workflow.apply_event(run, event("report", report(findings=[original], status="partial")))
        run = workflow.apply_event(run, event("runtime-outcome", {"assignment_id": "review", "runtime_handle": "review-handle",
            "target": FROZEN, "status": "partial", "cause": "auth", "evidence": "verified route failure after partial report"}))
        run = workflow.apply_event(run, event("integrity", {"status": "unchanged", "components": []}))
        run = workflow.apply_event(run, event("recover", {"id": "retry", "assignment_id": "review", "kind": "retry", "target": FROZEN,
            "reason": "same model through the verified alternate provider", "capacity": self.capacity(),
            "route": {"profile": "alternate", "model": alternate, "evidence": "verified available route"}}))
        initial = {"method": run["method"], "intake": run["intake"], "registry": run["registry"],
                   "orchestrator": MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"]}
        saved = {"version": 1, "initial": initial, "events": run["events"]}
        workflow.write_json(self.root / "run.json", saved)
        workflow.issue_handoff(self.root, saved, run, TARGET, "retry", self.capacity())
        saved, run = self.load()
        workflow.append_record(self.root, saved, run, TARGET, "started", dict(start("retry"), runtime_profile="alternate", runtime_identity=alternate))
        judgments = self.judgments()
        new = dict(judgments["findings"][0], id="review-02", trigger="a separate trigger", root_cause="separate-root")
        judgments["findings"].append(new)
        corrupted = copy.deepcopy(judgments)
        corrupted["findings"][0]["impact"] = "changed original judgment"
        rejected = self.author(json.dumps(corrupted).encode(), "retry")
        self.assertEqual(rejected["code"], "report_draft")
        self.assertTrue(any("immutable" in error["reason"] for error in rejected["errors"]))
        final = self.author(json.dumps(judgments).encode(), "retry")
        self.assertNotIn("errors", final)
        full = json.loads(workflow.handoff.read(self.root, final["report"]))
        self.assertEqual(full["findings"][0], original)
        self.assertEqual(full["findings"][1]["finder"], workflow.model_record(alternate, registry))
        self.assertEqual(self.receive("retry-handle", "retry"), 0)
        _, run = self.load()
        self.assertEqual(run["targets"][TARGET]["findings"]["review-01"], original)
        self.assertEqual(run["targets"][TARGET]["assignments"]["review"]["report"]["findings"], [original])

    def test_default_controls_distinguish_all_coverage_ids_from_findings(self):
        run = self.discovered(findings=[raw()])
        target = run["targets"][TARGET]
        target["catalog"]["units"] = {f"review-{index:04}-000": copy.deepcopy(CATALOG["units"]["review-0001-000"])
                                      for index in range(1, 10)}
        target["plan"]["interactions"]["caller"] = {"question": "check the shared caller", "units": list(target["catalog"]["units"])}
        row = dict(assignment("vet", MODEL_B, "vet", finding_ids=["review-01"]),
                   units=list(target["catalog"]["units"]), scope=["src/a.py"], interactions=["caller"],
                   identity=workflow.model_record(MODEL_B, REGISTRY), status="queued")
        target["assignments"]["vet"] = row
        task = workflow.contract.task_block(run, TARGET, "vet")
        view = workflow.handoff.task_view(task, "task-read")
        for label, expected in (("EXPECTED COVERAGE PATHS", row["scope"]), ("EXPECTED COVERAGE UNITS", row["units"]),
                                ("EXPECTED COVERAGE INTERACTIONS", row["interactions"])):
            self.assertIn(label + ": " + json.dumps(expected), view)
        self.assertIn('ASSIGNED RAW IDS: ["review-01"]', view)
        for name in ("checked", "checked_units", "checked_interactions"):
            self.assertEqual(task["report_template"][name], [])

    def test_draft_reports_all_independent_errors_before_sealing_and_preserves_bytes(self):
        self.begin()
        judgments = self.judgments()
        judgments["checked_units"] = ["review-01"]
        judgments["coverage_gaps"] = [{"id": "missing-test", "reason": "material contract unverified", "material": True}]
        judgments["categories"]["security"]["evidence"] = ""
        original = (json.dumps(judgments, ensure_ascii=False, indent=3) + "\n").encode()
        proof = self.author(original)
        self.assertEqual(proof["code"], "report_draft")
        self.assertTrue({"checked_units", "coverage_gaps/0", "categories/security"} <= {row["field"] for row in proof["errors"]})
        self.assertEqual(workflow.handoff.read(self.root, proof["input"]), original)
        _, run = self.load()
        row = run["targets"][TARGET]["assignments"]["review"]
        self.assertNotIn("authored_report", row)
        self.assertNotIn("rejected_report", row)
        self.assertEqual(run["targets"][TARGET]["findings"], {})
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["drafts"][0]["input"], proof["input"])
        self.assertEqual(self.receive(), 1)
        with self.assertRaises(workflow.NeedsDecision):
            workflow.apply_event(run, event("recover", {"id": "unneeded", "assignment_id": "review", "kind": "correction",
                "target": FROZEN, "reason": "a draft is not a submitted report", "capacity": self.capacity()}))
        target = run["targets"][TARGET]
        before = copy.deepcopy(target)
        errors = []
        workflow.accept_report(target, row, report(findings=[raw()]), errors=errors)
        self.assertEqual(errors, [])
        self.assertEqual(target, before)
        final = self.author(json.dumps(self.judgments()).encode())
        self.assertNotIn("errors", final)
        with self.assertRaisesRegex(ValueError, "one original input"):
            self.author(json.dumps(self.judgments()).encode())
        self.assertEqual(self.receive(), 0)
        self.assertEqual(workflow.handoff.read(self.root, proof["input"]), original)

    def test_author_file_diagnostics_do_not_misidentify_the_run_or_echo_input(self):
        self.begin()
        _, run = self.load()
        before = (self.root / "run.json").read_bytes()
        for argument in ('{"private": "sensitive-judgment"}', str(self.root / "missing.json")):
            with self.subTest(argument=argument):
                output = io.StringIO()
                argv = ["workflow.py", "--run", str(self.root), "author-report", "--target", TARGET,
                        "--assignment", "review", "--input", argument]
                with (patch.object(sys, "argv", argv), patch.object(sys, "stderr", output),
                      patch.object(workflow, "check_sources", return_value=run)):
                    self.assertEqual(workflow.main(), 1)
                diagnostic = json.loads(output.getvalue())["reason"]
                self.assertIn("judgment JSON file", diagnostic)
                self.assertNotIn("run input", diagnostic)
                self.assertNotIn("sensitive-judgment", diagnostic)
                self.assertEqual((self.root / "run.json").read_bytes(), before)

    def test_draft_collects_nested_evidence_faults_without_rewriting_them(self):
        self.begin()
        judgments = self.judgments()
        judgments["findings"][0].update(severity="MAJOR", line=0, references=[{
            "path": "../escape", "line": False, "side": "both", "relation": "guess", "evidence": ""}])
        repeated = self.judgments()["findings"][0]
        judgments["findings"].append(repeated)
        judgments["verification"] = {"results": [{"command": "unauthorized-test", "root": "/user/checkout",
                                                    "exit_code": "0", "evidence": ""}], "not_run": None}
        judgments["coverage_gaps"] = [{"id": "missing", "reason": "", "material": "false"}]
        original = json.dumps(judgments).encode()
        proof = self.author(original)
        fields = {row["field"] for row in proof["errors"]}
        self.assertTrue({"findings/0/severity", "findings/0/line", "findings/0/references/0/path",
                         "findings/0/references/0/line", "findings/0/references/0/side",
                         "findings/0/references/0/relation", "findings/0/references/0/evidence",
                         "verification/results/0", "verification/not_run", "coverage_gaps/0"} <= fields)
        self.assertTrue(any(row["reason"] == "shortfall materiality must be boolean" for row in proof["errors"]))
        self.assertTrue(any(row["reason"] == "duplicate finding ID in report" for row in proof["errors"]))
        self.assertEqual(workflow.handoff.read(self.root, proof["input"]), original)
        _, run = self.load()
        self.assertEqual(run["targets"][TARGET]["findings"], {})
        self.assertNotIn("authored_report", run["targets"][TARGET]["assignments"]["review"])

    def test_material_gap_requires_reviewers_partial_decision_and_remains_material(self):
        self.begin()
        judgments = self.judgments()
        judgments.update(status="partial", coverage_gaps=[{"id": "missing", "reason": "unverified contract", "material": True}])
        proof = self.author(json.dumps(judgments).encode())
        self.assertNotIn("errors", proof)
        self.assertEqual(self.receive(), 0)
        _, run = self.load()
        row = run["targets"][TARGET]["assignments"]["review"]
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["report"]["coverage_gaps"], judgments["coverage_gaps"])
        self.assertTrue(run["targets"][TARGET]["coverage_gaps"]["review/missing"]["material"])

    def test_vet_draft_returns_both_native_faults_without_registering_verdicts(self):
        run = self.discovered(findings=[raw()])
        run = workflow.apply_event(run, event("integrity", {"status": "unchanged", "components": []}))
        run = workflow.apply_event(run, event("assignment", assignment("vet", MODEL_B, "vet", finding_ids=["review-01"])))
        initial = {"method": run["method"], "intake": run["intake"], "registry": run["registry"],
                   "orchestrator": MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"]}
        saved = {"version": 1, "initial": initial, "events": run["events"]}
        workflow.write_json(self.root / "run.json", saved)
        workflow.issue_handoff(self.root, saved, run, TARGET, "vet", self.capacity())
        saved, run = self.load()
        workflow.append_record(self.root, saved, run, TARGET, "started", start("vet", MODEL_B))
        judgments = report("vet", verdicts=[vet()])
        for key in ("assignment_id", "target", "reviewer"):
            judgments.pop(key)
        judgments["verdicts"][0].pop("assignment_id")
        judgments.update(checked_units=["review-01"], coverage_gaps=[{"id": "missing", "reason": "unverified contract", "material": True}])
        proof = self.author(json.dumps(judgments).encode(), "vet")
        self.assertTrue({"checked_units", "coverage_gaps/0"} <= {row["field"] for row in proof["errors"]})
        self.assertEqual(proof["expected_coverage"]["checked_units"], list(CATALOG["units"]))
        _, run = self.load()
        self.assertEqual(run["targets"][TARGET]["vets"], {})
        judgments.update(checked_units=list(CATALOG["units"]), status="partial")
        final = self.author(json.dumps(judgments).encode(), "vet")
        self.assertNotIn("errors", final)
        self.assertEqual(self.receive("vet-handle", "vet"), 0)
        _, run = self.load()
        self.assertEqual(run["targets"][TARGET]["vets"], {})
        self.assertEqual(run["targets"][TARGET]["assignments"]["vet"]["report"]["coverage_gaps"], judgments["coverage_gaps"])

    def test_adjudication_draft_collects_errors_and_leaves_decisions_unaccepted(self):
        run = WorkflowTests().vetted([raw()])
        target = copy.deepcopy(run["targets"][TARGET])
        row = {"id": "stand-in", "kind": "adjudication", "actual_identity": workflow.model_record(MODEL_C, REGISTRY),
               "finding_ids": ["review-01"], "vet_bindings": {"review-01": "vet"}, "status": "running"}
        judgment: dict = dict(adjudication(), assignment_id="stand-in", severity="MAJOR", evidence="")
        judgment["references"][0]["line"] = 0
        reply = {"assignment_id": "stand-in", "status": "done", "target": FROZEN, "reviewer": row["actual_identity"],
                 "adjudications": [judgment], "evidence": "independent final assessment", "source_status": "unchanged"}
        before = copy.deepcopy(target)
        errors = []
        workflow.accept_report(target, row, reply, errors=errors)
        self.assertTrue({"adjudications/0/severity", "adjudications/0/evidence", "adjudications/0/references/0/line"} <= {issue["field"] for issue in errors})
        self.assertEqual(target, before)
        self.assertEqual(row["status"], "running")
        with self.assertRaises((ValueError, workflow.NeedsDecision)):
            workflow.accept_report(copy.deepcopy(target), copy.deepcopy(row), reply)

    def test_complete_handoff_budget_is_checked_before_assignment_reservation(self):
        base = self.prepared()
        unit = assignment()
        run = workflow.apply_event(base, event("assignment", unit))
        task = workflow.contract.task_block(run, TARGET, "review")
        partial = len(task["task"].partition("\nFILE REPORT HANDOFF:")[0].encode()) + sum(unit["bytes"] for unit in CATALOG["units"].values())
        unit["workload"]["input_budget_bytes"] = partial + 50
        before = copy.deepcopy(base)
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(base, event("assignment", unit))
        self.assertEqual(caught.exception.code, "assignment_capacity")
        self.assertEqual(base, before)

    def test_same_handle_correction_gets_fresh_artifacts_and_retains_original_input(self):
        self.begin()
        saved, run = self.load()
        # An already submitted legacy report still uses the one correction cap.
        original: dict = {"assignment_id": "review", "task_sha256": self.dispatch["handoff"]["sha256"],
                    "assembly_error": "malformed authored JSON"}
        original.update({name.removesuffix(".json"): proof for name, proof in workflow.handoff.store(
            self.root, {"input.json": b"{broken JSON}", "report.json": b"{broken JSON}"}).items()})
        workflow.append_record(self.root, saved, run, TARGET, "report-authored", original)
        self.assertEqual(self.receive(), 0)
        saved, run = self.load()
        run = workflow.append_record(self.root, saved, run, TARGET, "recover", {
            "id": "corrected", "assignment_id": "review", "kind": "correction", "target": FROZEN,
            "reason": "correct the malformed authored JSON", "capacity": self.capacity()})
        workflow.issue_handoff(self.root, saved, run, TARGET, "corrected", self.capacity())
        saved, run = self.load()
        runtime = dict(start("corrected"), runtime_handle="review-handle")
        workflow.append_record(self.root, saved, run, TARGET, "started", runtime)
        corrected = self.author(json.dumps(self.judgments()).encode(), "corrected")
        self.assertNotEqual(original["report"], corrected["report"])
        self.assertEqual(self.receive(assignment_id="corrected"), 0)
        _, run = self.load()
        self.assertEqual(run["targets"][TARGET]["assignments"]["corrected"]["status"], "done")
        self.assertEqual(workflow.handoff.read(self.root, original["input"]), b"{broken JSON}")
        self.assertIn("rejected_report", run["targets"][TARGET]["assignments"]["review"])

    def test_file_transport_overhead_cannot_bypass_the_assignment_budget(self):
        saved, run = self.load()
        inline = workflow.contract.task_block(run, TARGET, "review")
        run["targets"][TARGET]["assignments"]["review"]["workload"]["input_budget_bytes"] = inline["input_floor_bytes"] + 50
        before = (self.root / "run.json").read_bytes()
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.issue_handoff(self.root, saved, run, TARGET, "review", self.capacity())
        self.assertEqual(caught.exception.code, "assignment_capacity")
        self.assertEqual((self.root / "run.json").read_bytes(), before)

    def test_fitting_complete_budget_is_identical_at_reservation_and_dispatch(self):
        base = self.prepared()
        unit = assignment()
        directory = self.root / "private ü {run}"
        directory.mkdir()
        run = workflow.apply_event(base, event("assignment", unit), directory=directory)
        floor = workflow.contract.task_block(run, TARGET, "review", directory=directory)["input_floor_bytes"]
        for _ in range(3):
            unit["workload"]["input_budget_bytes"] = floor
            run = workflow.apply_event(base, event("assignment", unit), directory=directory)
            measured = workflow.contract.task_block(run, TARGET, "review", directory=directory)["input_floor_bytes"]
            if measured == floor:
                break
            floor = measured
        self.assertEqual(measured, floor)
        initial = {"method": run["method"], "intake": run["intake"], "registry": run["registry"],
                   "orchestrator": MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"]}
        saved = {"version": 1, "initial": initial, "events": run["events"]}
        workflow.write_json(directory / "run.json", saved)
        dispatched = workflow.issue_handoff(directory, saved, run, TARGET, "review", self.capacity())
        self.assertEqual(dispatched["input_floor_bytes"], unit["workload"]["input_budget_bytes"])
        self.assertIn(shlex.quote(str(directory)), dispatched["task"])
        self.assertEqual(workflow.load_run(directory / "run.json")[1]["targets"][TARGET]["assignments"]["review"]["status"], "queued")

    def test_large_discussion_cannot_bury_vet_controls_or_replace_assigned_ids(self):
        run = self.discovered(findings=[raw()])
        run = workflow.apply_event(run, event("assignment", assignment("vet", MODEL_B, "vet", finding_ids=["review-01"])))
        target = run["targets"][TARGET]
        target["intake"]["discussion"] = {"snapshot": {"review_comments": [{"body": "historical comment " * 40000}]}}
        target["assignments"]["vet"]["workload"]["input_budget_bytes"] = 2000000
        task = workflow.contract.task_block(run, TARGET, "vet")
        command = "rtk proxy python3 workflow.py --run /private/run task-read --target fixture/project#12 --assignment vet"
        view = workflow.handoff.task_view(task, command)
        self.assertLess(len(view.encode()), 16384)
        self.assertIn('ASSIGNED RAW IDS: ["review-01"]', view[:1024])
        self.assertIn('"commands": [], "root": null', view[:2048])
        self.assertIn("No test commands are authorized", view[:2048])
        self.assertIn("Historical PR comments are context", view[:1024])
        self.assertIn("exactly one CONFIRMED/ADJUSTED/REJECTED/UNVERIFIABLE", view)
        self.assertIn("--section findings_to_vet", view)
        self.assertNotIn("historical comment", view)
        # A nested read retrieves the same preserved evidence, including Unicode.
        _, raw_task = task["task"].split("\n", 1)
        payload, _ = json.JSONDecoder().raw_decode(raw_task)
        payload["pr_discussion"]["snapshot"]["review_comments"][0]["body"] += " café λ"
        expected = json.dumps(payload["pr_discussion"], ensure_ascii=True, separators=(",", ":"))
        task["task"] = "VET vet\n" + json.dumps(payload) + "\nRULES: preserved"
        parts = []
        for offset in range(0, len(expected), 32768):
            page = workflow.handoff.task_view(task, command, section="pr_discussion", offset=offset, length=32768)
            heading, body = page.split("\n", 1)
            self.assertIn(f"offset={offset}", heading)
            self.assertLessEqual(len(body), 32768)
            parts.append(body)
        self.assertEqual("".join(parts), expected)
        nested = workflow.handoff.task_view(task, command, section="pr_discussion/snapshot/review_comments/0/body", length=100)
        self.assertEqual(nested.split("\n", 1)[1], json.dumps(payload["pr_discussion"]["snapshot"]["review_comments"][0]["body"], ensure_ascii=True)[:100])

    def test_evidence_pages_use_registered_verified_task_and_cannot_bypass_tampering(self):
        self.begin()
        _, run = self.load()
        argv = ["workflow.py", "--run", str(self.root), "task-read", "--target", TARGET,
                "--assignment", "review", "--section", "scope", "--offset", "0", "--length", "128"]
        output = io.StringIO()
        with (patch.object(sys, "argv", argv), patch.object(sys, "stdout", output),
              patch.object(workflow, "check_sources", return_value=run) as check):
            self.assertEqual(workflow.main(), 0)
        check.assert_called_once()
        self.assertIn("SECTION scope", output.getvalue())
        path = self.root / self.dispatch["handoff"]["artifact"]
        path.write_bytes(path.read_bytes() + b" ")
        with (patch.object(sys, "argv", argv), patch.object(sys, "stderr", io.StringIO()),
              patch.object(workflow, "check_sources", side_effect=AssertionError("changed task must be rejected first"))):
            self.assertEqual(workflow.main(), 1)

    def test_bounded_evidence_rejects_invalid_sections_and_ranges(self):
        _, run = self.load()
        task = workflow.handoff_task(self.root, run, TARGET, "review")
        cases: tuple[dict, ...] = ({"section": "missing"}, {"section": "scope/missing"}, {"section": "scope", "offset": -1},
                        {"section": "scope", "length": 0}, {"section": "scope", "length": 32769},
                        {"section": "scope", "offset": 1000000}, {"offset": 1})
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                workflow.handoff.task_view(task, "task-read", **options)


class AssignmentContractTests(unittest.TestCase):
    prepared = WorkflowTests.prepared
    discovered = WorkflowTests.discovered
    vetted = WorkflowTests.vetted

    def unplanned(self):
        run = workflow.new_run("pr-review", COLLECTION, REGISTRY, MODEL_C, "thread")
        run = workflow.apply_event(run, event("freeze", FROZEN))
        return workflow.apply_event(run, event("catalog", CATALOG))

    def test_missing_decisions_hold_without_mutating_input(self):
        run = self.unplanned()
        original = copy.deepcopy(run)
        for missing, code in (("coverage", "review_plan"), ("profiles", "review_plan")):
            data = plan()
            del data[missing]
            with self.assertRaises(workflow.NeedsDecision) as caught:
                workflow.apply_event(run, event("plan", data))
            self.assertEqual(caught.exception.code, code)
            self.assertIn(missing, str(caught.exception))
        self.assertEqual(run, original)

        for missing in (None, " "):
            with self.assertRaises(workflow.NeedsDecision) as caught:
                workflow.apply_event(run, event("plan", dict(plan(), selection_evidence=missing)))
            self.assertEqual(caught.exception.code, "review_plan")
            self.assertIn("selection_evidence", str(caught.exception))

    def test_roster_roles_identity_and_selection_cannot_be_forged_by_assignment(self):
        run = self.prepared()
        for data in (dict(assignment(), roles=["reviewer"], role="reviewer"),
                     dict(assignment(), profile="invented"), dict(assignment(), model=MODEL_B)):
            with self.assertRaises(ValueError):
                workflow.apply_event(run, event("assignment", data))
        missing = plan()
        del missing["profiles"]["vet-profile"]
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(self.unplanned(), event("plan", missing))
        self.assertEqual(caught.exception.code, "profile_selection")

    def test_batch_strongest_selection_requires_all_available_capability_judgments(self):
        run = self.unplanned()
        run["method"] = "pr-review-batch"
        proposed = plan()
        proposed["profiles"]["fixture-profile"]["capability"] = 2
        proposed["profiles"]["vet-profile"]["capability"] = 1
        run = workflow.apply_event(run, event("batch-wave", wave_decisions(run, {TARGET: {"fixture-profile"}},
                                   {name: row["capability"] for name, row in proposed["profiles"].items()}), None))
        workflow.apply_event(run, event("plan", proposed))
        run["targets"][TARGET]["catalog"]["profiles"]["alternative"] = copy.deepcopy(CATALOG["profiles"]["fixture-profile"])
        proposed["profiles"]["alternative"] = {"decision": "omitted", "primary": False, "evidence": "same-family alternative", "capability": 1}
        with self.assertRaisesRegex(ValueError, "weaker"):
            workflow.apply_event(run, event("plan", proposed))
        proposed["profiles"]["alternative"]["capability"] = None
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("plan", proposed))
        self.assertEqual(caught.exception.code, "reviewer_capability")
        proposed["profiles"]["alternative"]["decision"] = "unavailable"
        accepted = workflow.apply_event(run, event("plan", proposed))
        self.assertEqual(accepted["targets"][TARGET]["plan"], proposed)

    def test_single_finder_covers_high_risk_and_material_shortfalls_hold(self):
        proposed = plan()
        for row in proposed["coverage"].values():
            row["risk"] = "high-risk"
        workflow.apply_event(self.unplanned(), event("plan", proposed))
        for row in proposed["coverage"].values():
            row["shortfall"] = {"reason": "other required evidence is unavailable", "material": True}
        run = workflow.apply_event(self.unplanned(), event("plan", proposed))
        for operation, data in (("assignment", assignment()), ("started", start()), ("report", report())):
            run = workflow.apply_event(run, event(operation, data))
        with self.assertRaisesRegex(workflow.NeedsDecision, "unavailable"):
            workflow.apply_event(run, event("complete", {"coverage_evidence": "no findings"}))
        for key in run["targets"][TARGET]["coverage_gaps"]:
            run = workflow.apply_event(run, event("resolve-gap", {"id": key, "evidence": "explicit evidence resolves the missing material contract"}))
        run = workflow.apply_event(run, event("complete", {"coverage_evidence": "all material evidence reconciled"}))
        self.assertEqual(run["targets"][TARGET]["phase"], "complete")

    def test_replay_ignores_install_path_but_live_dispatch_keeps_actual_budget_guard(self):
        base = self.prepared()
        unit = assignment()
        run = workflow.apply_event(base, event("assignment", unit))
        floor = workflow.contract.task_block(run, TARGET, "review")["input_floor_bytes"]
        while True:
            unit["workload"]["input_budget_bytes"] = floor
            run = workflow.apply_event(base, event("assignment", unit))
            measured = workflow.contract.task_block(run, TARGET, "review")["input_floor_bytes"]
            if measured == floor:
                break
            floor = measured
        run = workflow.apply_event(run, event("task-ready", {"assignment_id": "review",
            "capacity": {"parent_active": 0, "process_active": 0, "evidence": "native counts",
                         "runtime_parent_root": FROZEN["parent_root"]}}))
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "run.json"
            initial = {"method": run["method"], "intake": run["intake"], "registry": run["registry"],
                       "orchestrator": MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"]}
            saved = {"version": 1, "initial": initial, "events": run["events"]}
            workflow.write_json(journal, saved)
            with patch.object(workflow.contract, "__file__", "/long-installed-path/" + "x" * 120 + "/review_contract.py"):
                replayed = workflow.load_run(journal)[1]
                self.assertEqual(replayed, run)
                with self.assertRaises(workflow.NeedsDecision) as caught:
                    workflow.contract.dispatch_task(replayed, TARGET, "review", {"parent_active": 0,
                        "process_active": 0, "evidence": "fresh counts after relocation"})
                self.assertEqual(caught.exception.code, "assignment_capacity")
                saved["events"][-2]["data"]["workload"]["input_budget_bytes"] = 1
                workflow.write_json(journal, saved)
                with self.assertRaises(workflow.NeedsDecision):
                    workflow.load_run(journal)

    def test_workload_checks_measured_task_and_patch_without_guessing_capacity(self):
        run = self.prepared()
        for changes in ({"input_budget_bytes": 100}, {"capacity_bytes": 100}):
            data = assignment()
            data["workload"].update(changes)
            with self.assertRaises(workflow.NeedsDecision) as caught:
                workflow.apply_event(run, event("assignment", data))
            self.assertEqual(caught.exception.code, "assignment_capacity")
        data = assignment()
        del data["workload"]["rescope"]
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("assignment", data))
        self.assertEqual(caught.exception.code, "assignment_workload")

    def test_reports_require_all_categories_and_actual_identity_and_units(self):
        run = workflow.apply_event(self.prepared(), event("assignment", assignment()))
        run = workflow.apply_event(run, event("started", start()))
        missing = report()
        del missing["categories"]["privacy"]
        gap = report()
        gap["categories"]["security"]["status"] = "gap"
        for data in (missing, gap):
            with self.assertRaises(workflow.NeedsDecision) as caught:
                workflow.apply_event(run, event("report", data))
            self.assertEqual(caught.exception.code, "category_evidence")
        for data in (dict(report(), checked_units=[]), dict(report(), reviewer=workflow.model_record(MODEL_B, REGISTRY))):
            with self.assertRaises(ValueError):
                workflow.apply_event(run, event("report", data))
        gap["status"] = "partial"
        accepted = workflow.apply_event(run, event("report", gap))
        self.assertEqual(accepted["targets"][TARGET]["assignments"]["review"]["status"], "partial")

    def test_vet_reports_require_exact_original_ids_and_preserve_judgments(self):
        run = self.discovered(findings=[raw(), dict(raw("review-02"), trigger="invalid non-empty input", root_cause="invalid-input")])
        run = workflow.apply_event(run, event("assignment", assignment("vet", MODEL_B, "vet", finding_ids=["review-01", "review-02"])))
        run = workflow.apply_event(run, event("started", start("vet", MODEL_B)))
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("report", report("vet")))
        self.assertEqual(caught.exception.code, "vet_verdicts")
        for verdicts in ([vet(), vet()], [vet(), vet("invented-01")]):
            with self.assertRaises(ValueError):
                workflow.apply_event(run, event("report", report("vet", verdicts=verdicts)))
        with self.assertRaisesRegex(ValueError, "use ADJUSTED"):
            workflow.apply_event(run, event("report", report("vet", verdicts=[dict(vet(), severity="major"), vet("review-02")])))
        for status in ("partial", "blocked", "failed", "stopped"):
            with self.subTest(status=status):
                reply = report("vet", status=status, verdicts=[vet()])
                held = workflow.apply_event(run, event("report", reply))
                self.assertEqual(held["targets"][TARGET]["vets"], {})
                self.assertEqual(held["targets"][TARGET]["assignments"]["vet"]["report"], reply)
        run = workflow.apply_event(run, event("report", report("vet", verdicts=[vet(), vet("review-02")])))
        with self.assertRaisesRegex(ValueError, "retained original"):
            workflow.apply_event(run, event("vet", dict(vet(), verdict="REJECTED")))

    def test_interaction_assignment_and_report_coverage_are_bound_to_plan(self):
        proposed = plan()
        proposed["partitioned"] = True
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(self.unplanned(), event("plan", proposed))
        self.assertEqual(caught.exception.code, "interaction_plan")
        proposed["interactions"] = {"caller": {"question": "trace API callers across both units", "units": list(CATALOG["units"]),
                                               "families": ["family-a"], "shortfall": None}}
        run = workflow.apply_event(self.unplanned(), event("plan", proposed))
        unit = assignment(interactions=["caller"])
        run = workflow.apply_event(run, event("assignment", unit))
        run = workflow.apply_event(run, event("started", start()))
        with self.assertRaisesRegex(ValueError, "interactions"):
            workflow.apply_event(run, event("report", report()))
        run = workflow.apply_event(run, event("report", dict(report(), checked_interactions=["caller"])))
        self.assertEqual(workflow.completion_gaps(run, run["targets"][TARGET]), [])

    def test_report_material_gaps_are_retained_and_cannot_hide_in_supplemental_work(self):
        run = workflow.apply_event(self.prepared(), event("assignment", assignment()))
        run = workflow.apply_event(run, event("started", start()))
        data = dict(report(status="partial"), coverage_gaps=[{"id": "contract", "reason": "missing caller contract", "material": True}])
        run = workflow.apply_event(run, event("report", data))
        self.assertIn("review/contract", run["targets"][TARGET]["coverage_gaps"])
        self.assertTrue(any("missing caller" in gap for gap in workflow.completion_gaps(run, run["targets"][TARGET])))

    def test_orchestrator_discoveries_keep_provenance_and_hold_for_independent_routing(self):
        run = self.discovered()
        original = raw("orchestrator-01", MODEL_C)
        run = workflow.apply_event(run, event("orchestrator-finding", {"target": FROZEN, "finding": original}))
        self.assertIn("orchestrator-01", run["targets"][TARGET]["findings"])
        self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "finding_route")
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("assignment", assignment("vet", MODEL_B, "vet", finding_ids=["orchestrator-01"])))
        self.assertEqual(caught.exception.code, "finding_route")
        with self.assertRaises(workflow.NeedsDecision):
            workflow.apply_event(run, event("adjudication", adjudication("orchestrator-01")))
        self.assertEqual(run["targets"][TARGET]["findings"]["orchestrator-01"], original)

    def test_test_commands_require_authorization_and_an_owned_copy(self):
        run = self.prepared()
        data = assignment()
        data["test_plan"] = {"commands": ["rtk proxy python3 -m unittest"], "root": "/arbitrary/copy", "evidence": "focused execution"}
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("assignment", data))
        self.assertEqual(caught.exception.code, "test_authorization")
        run = workflow.apply_event(run, event("authorization", {"submission": False, "test_execution": True, "metered_spawns": 0, "evidence": "user permits tests"}))
        with self.assertRaisesRegex(ValueError, "owned disposable"):
            workflow.apply_event(run, event("assignment", data))
        root = "/disposable/test-" + "a" * 32
        run = workflow.apply_event(run, event("test-copy", {"root": root}))
        data["test_plan"]["root"] = root
        run = workflow.apply_event(run, event("assignment", data))
        run = workflow.apply_event(run, event("started", start()))
        bad = report()
        bad["verification"] = {"results": [{"command": data["test_plan"]["commands"][0], "root": "/user/checkout", "exit_code": 0, "evidence": "pass"}], "not_run": None}
        with self.assertRaisesRegex(ValueError, "disposable root"):
            workflow.apply_event(run, event("report", bad))

    def test_tasks_withhold_discovery_findings_and_include_only_assigned_vet_claims(self):
        run = workflow.apply_event(self.prepared(), event("assignment", assignment()))
        task = workflow.contract.task_block(run, TARGET, "review")
        self.assertNotIn("findings_to_vet", task["task"])
        self.assertNotIn("model_override", task)
        self.assertGreater(task["input_floor_bytes"], 80)
        run = self.discovered(findings=[raw(), dict(raw("review-02"), trigger="unrelated-secret-trigger")])
        run = workflow.apply_event(run, event("assignment", assignment("vet", MODEL_B, "vet", finding_ids=["review-01"])))
        task = workflow.contract.task_block(run, TARGET, "vet")["task"]
        self.assertIn('"findings_to_vet"', task)
        self.assertIn('"id":"review-01"', task)
        self.assertNotIn("unrelated-secret-trigger", task)

    def test_ledger_and_runtime_profile_must_match_recorded_locations_and_route(self):
        run = workflow.apply_event(self.prepared(), event("assignment", assignment()))
        with self.assertRaisesRegex(ValueError, "runtime profile"):
            workflow.apply_event(run, event("started", dict(start(), runtime_profile="other")))
        run = workflow.apply_event(run, event("started", start()))
        item = raw()
        item["references"][0]["line"] = 41
        with self.assertRaisesRegex(ValueError, "anchored location"):
            workflow.apply_event(run, event("report", report(findings=[item])))

    def test_scout_cannot_replace_a_review_participant(self):
        run = self.unplanned()
        saved = run["targets"][TARGET]["catalog"]
        saved["profiles"]["fixture-profile"]["roles"] = ["scout"]
        with self.assertRaisesRegex(ValueError, "primary perspectives"):
            workflow.apply_event(run, event("plan", plan()))
        run = self.unplanned()
        run["targets"][TARGET]["catalog"]["profiles"]["vet-profile"]["roles"] = ["scout"]
        with self.assertRaisesRegex(ValueError, "eligibility"):
            workflow.apply_event(run, event("plan", plan()))

    def test_explicit_unprofiled_override_keeps_role_identity_and_request_evidence(self):
        proposed = plan(["family-b"])
        proposed["profiles"]["fixture-profile"].update(decision="selected", primary=True, model=MODEL_B, override_evidence="user explicitly requested model-b")
        proposed["routing"].update(finder="fixture-profile", vetter="vet-profile")
        with self.assertRaisesRegex(ValueError, "pinned profiles"):
            workflow.apply_event(self.unplanned(), event("plan", proposed))
        initial = self.unplanned()
        profiles = initial["targets"][TARGET]["catalog"]["profiles"]
        profiles["Worker (built-in)"] = profiles.pop("fixture-profile")
        proposed["profiles"]["Worker (built-in)"] = proposed["profiles"].pop("fixture-profile")
        proposed["routing"].update(finder="Worker (built-in)", vetter="vet-profile")
        profiles["vet-profile"]["identity"] = workflow.model_record(MODEL_A, REGISTRY)
        run = workflow.apply_event(initial, event("plan", proposed))
        unit = assignment(model=MODEL_B, profile="Worker (built-in)", override_evidence="user explicitly requested model-b")
        run = workflow.apply_event(run, event("assignment", unit))
        task = workflow.contract.task_block(run, TARGET, "review")
        self.assertEqual(task["model_override"], "provider-b/model-b")
        self.assertEqual(run["targets"][TARGET]["assignments"]["review"]["role"], "candidate")


class FamilyRoutingTests(unittest.TestCase):
    prepared = WorkflowTests.prepared
    discovered = WorkflowTests.discovered
    vetted = WorkflowTests.vetted
    unplanned = AssignmentContractTests.unplanned

    def stand_in_ready(self, findings=None):
        run = self.prepared("pr-review-batch", orchestrator=MODEL_A, stand_in="judge-profile")
        for operation, data in (("assignment", assignment()), ("started", start()),
                                ("report", report(findings=[raw()] if findings is None else findings)),
                                ("assignment", assignment("vet", MODEL_B, "vet", finding_ids=["review-01"])),
                                ("started", start("vet", MODEL_B)), ("report", report("vet")), ("vet", vet())):
            run = workflow.apply_event(run, event(operation, data))
        return run

    def judge_report(self, judgments=None, status="done"):
        return {"assignment_id": "judge", "status": status, "target": FROZEN,
                "reviewer": workflow.model_record(MODEL_C, REGISTRY),
                "adjudications": [dict(adjudication(), assignment_id="judge")] if judgments is None else judgments,
                "evidence": "independent source and competing-argument assessment", "source_status": "unchanged"}

    def judge_running(self):
        run = self.stand_in_ready()
        run = workflow.apply_event(run, event("assignment", assignment("judge", MODEL_C, "adjudication", finding_ids=["review-01"])))
        return workflow.apply_event(run, event("started", start("judge", MODEL_C)))

    def test_three_actual_families_are_required_despite_different_profile_names(self):
        for role in ("vet-profile", "judge-profile"):
            run = self.unplanned()
            run["targets"][TARGET]["catalog"]["profiles"][role]["identity"] = workflow.model_record(MODEL_A, REGISTRY)
            if role == "judge-profile":
                run["orchestrator"] = workflow.model_record(MODEL_A, REGISTRY)
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "different.*families"):
                workflow.apply_event(run, event("plan", plan()))
        run = self.unplanned()
        legacy = plan()
        del legacy["routing"]
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("plan", legacy))
        self.assertEqual(caught.exception.code, "review_plan")
        self.assertIn("routing", str(caught.exception))

    def test_different_models_and_provider_routes_do_not_create_another_family(self):
        for provider in ("provider-a", "provider-b"):
            run = self.unplanned()
            alias = {"provider_id": provider, "model_id": "another-model"}
            run["registry"]["models"].append(dict(alias, family="family-a"))
            run["targets"][TARGET]["catalog"]["profiles"]["vet-profile"]["identity"] = workflow.model_record(alias, run["registry"])
            with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "different model families"):
                workflow.apply_event(run, event("plan", plan()))

    def test_reciprocal_spillover_completes_with_three_families_per_finding(self):
        run = self.discovered(findings=[raw()])
        for operation, data in (("assignment", assignment("vet", MODEL_B, "vet", finding_ids=["review-01"])),
                                ("started", start("vet", MODEL_B)), ("report", report("vet", [raw("vet-01", MODEL_B)])),
                                ("vet", vet())):
            run = workflow.apply_event(run, event(operation, data))
        self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["profiles"], {"fixture-profile": ["vet-01"]})
        with self.assertRaisesRegex(ValueError, "approved finding route"):
            workflow.apply_event(run, event("assignment", assignment("wrong", MODEL_C, "vet", finding_ids=["vet-01"])))
        spill_verdict = vet("vet-01", "spillover")
        for operation, data in (("assignment", assignment("spillover", MODEL_A, "vet", finding_ids=["vet-01"])),
                                ("started", start("spillover")),
                                ("report", report("spillover", verdicts=[spill_verdict])), ("vet", spill_verdict),
                                ("adjudication", adjudication()), ("adjudication", adjudication("vet-01")),
                                ("complete", {"coverage_evidence": "original and reciprocal spillover reconciled"})):
            run = workflow.apply_event(run, event(operation, data))
        target = run["targets"][TARGET]
        for key in target["findings"]:
            self.assertEqual({target["findings"][key]["finder"]["family"], target["vets"][key]["vetter"]["family"],
                              target["adjudications"][key]["adjudicator"]["family"]}, {"family-a", "family-b", "family-c"})
        self.assertEqual(target["findings"]["vet-01"], raw("vet-01", MODEL_B))
        self.assertEqual(target["phase"], "complete")

    def test_batch_spillover_and_orchestrator_origin_hold_without_guessing_routes(self):
        run = self.discovered("pr-review-batch", [raw()])
        for operation, data in (("assignment", assignment("vet", MODEL_B, "vet", finding_ids=["review-01"])),
                                ("started", start("vet", MODEL_B)), ("report", report("vet", [raw("vet-01", MODEL_B)])), ("vet", vet())):
            run = workflow.apply_event(run, event(operation, data))
        self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "batch_spillover_route")
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("assignment", assignment("spillover", MODEL_A, "vet", finding_ids=["vet-01"])))
        self.assertEqual(caught.exception.code, "batch_spillover_route")
        run = self.stand_in_ready()
        run = workflow.apply_event(run, event("orchestrator-finding", {"target": FROZEN, "finding": raw("orchestrator-01")}))
        self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "finding_route")

    def test_batch_stand_in_requires_independence_and_capability_judgment(self):
        run = self.unplanned()
        run["method"] = "pr-review-batch"
        run["orchestrator"] = workflow.model_record(MODEL_A, REGISTRY)
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("plan", plan()))
        self.assertEqual(caught.exception.code, "stand_in_adjudicator")
        for assessment in (None, {"relation": "unknown", "evidence": "no reliable comparison"},
                           {"relation": "lower", "evidence": "less capable"}):
            proposed = plan(stand_in="judge-profile")
            proposed["routing"]["stand_in_capability"] = assessment
            with self.subTest(assessment=assessment), self.assertRaises(workflow.NeedsDecision) as caught:
                workflow.apply_event(run, event("plan", proposed))
            self.assertEqual(caught.exception.code, "stand_in_capability")
        proposed = plan(stand_in="judge-profile")
        run["targets"][TARGET]["catalog"]["profiles"]["judge-profile"]["identity"] = workflow.model_record(MODEL_B, REGISTRY)
        with self.assertRaisesRegex(ValueError, "already participated"):
            workflow.apply_event(run, event("plan", proposed))
        with self.assertRaisesRegex(ValueError, "unnecessary"):
            workflow.apply_event(self.unplanned() | {"method": "pr-review-batch"}, event("plan", plan(stand_in="judge-profile")))

    def test_stand_in_task_and_receipt_preserve_the_actual_judgment(self):
        run = self.stand_in_ready()
        unit = assignment("judge", MODEL_C, "adjudication", finding_ids=["review-01"])
        run = workflow.apply_event(run, event("assignment", unit))
        task = workflow.contract.task_block(run, TARGET, "judge")["task"]
        self.assertTrue(task.startswith("ADJUDICATE judge\n"))
        self.assertIn('"findings_to_adjudicate"', task)
        self.assertIn('"vet_verdicts"', task)
        self.assertNotIn('"findings_to_vet"', task)
        with self.assertRaisesRegex(ValueError, "already has a stand-in"):
            workflow.apply_event(run, event("assignment", dict(unit, id="another")))
        with self.assertRaisesRegex(ValueError, "runtime identity"):
            workflow.apply_event(run, event("started", dict(start("judge", MODEL_B), runtime_profile="judge-profile")))
        run = workflow.apply_event(run, event("started", start("judge", MODEL_C)))
        with self.assertRaisesRegex(ValueError, "active assignments"):
            workflow.apply_event(run, event("adjudication", dict(adjudication(), assignment_id="judge")))
        original = copy.deepcopy(run)
        payload = json.loads(task.splitlines()[1])
        delegated = copy.deepcopy(payload["report_template"])
        self.assertIsNone(delegated["status"])
        self.assertEqual(delegated["adjudications"][0]["finding_id"], "review-01")
        self.assertEqual(delegated["adjudications"][0]["assignment_id"], "judge")
        delegated.update({name: value for name, value in self.judge_report().items()
                          if name not in ("assignment_id", "target", "reviewer", "adjudications")})
        delegated["adjudications"][0].update({name: value for name, value in self.judge_report()["adjudications"][0].items()
                                             if name not in ("finding_id", "assignment_id")})
        run = workflow.apply_event(run, event("adjudication-report", delegated))
        self.assertEqual(original["targets"][TARGET]["assignments"]["judge"]["status"], "running")
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("adjudication", adjudication()))
        self.assertEqual(caught.exception.code, "stand_in_adjudicator")
        judgment = delegated["adjudications"][0]
        with self.assertRaisesRegex(ValueError, "original stand-in judgment"):
            workflow.apply_event(run, event("adjudication", dict(judgment, reason="orchestrator replacement judgment")))
        run = workflow.apply_event(run, event("adjudication", judgment))
        stored = run["targets"][TARGET]["adjudications"]["review-01"]
        self.assertEqual(stored, dict(judgment, adjudicator=workflow.model_record(MODEL_C, REGISTRY), family_overlap=[], vet_assignment_id="vet"))
        self.assertEqual(run["targets"][TARGET]["assignments"]["judge"]["report"], delegated)
        with self.assertRaisesRegex(ValueError, "unadjudicated"):
            workflow.apply_event(run, event("adjudication", judgment))
        run = workflow.apply_event(run, event("complete", {"coverage_evidence": "complete independent finding/vet/adjudication"}))
        self.assertEqual(workflow.review_event(run["targets"][TARGET]), "REQUEST_CHANGES")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.json"
            initial = {"method": run["method"], "intake": COLLECTION, "registry": REGISTRY, "orchestrator": MODEL_A,
                       "thread_id": run["thread_id"], "run_id": run["run_id"]}
            workflow.write_json(path, {"version": 1, "initial": initial, "events": run["events"]})
            self.assertEqual(workflow.load_run(path)[1], run)

    def test_stand_in_reports_reject_wrong_bindings_ids_and_missing_evidence(self):
        run = self.judge_running()
        judgment = self.judge_report()["adjudications"][0]
        malformed = [dict(self.judge_report(), **changes) for changes in
                     ({"target": dict(FROZEN, head_sha="f" * 40)}, {"reviewer": workflow.model_record(MODEL_A, REGISTRY)},
                      {"adjudications": [judgment, judgment]}, {"adjudications": [dict(judgment, assignment_id="other")]},
                      {"adjudications": [dict(judgment, finding_id="unknown-01")]}, {"source_status": "unknown"})]
        for bad in malformed:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                workflow.apply_event(run, event("adjudication-report", bad))
        for judgments, code in (([], "adjudication_report"), ([dict(judgment, argument_assessment="")], "adjudication_evidence")):
            with self.subTest(judgments=judgments), self.assertRaises(workflow.NeedsDecision) as caught:
                workflow.apply_event(run, event("adjudication-report", self.judge_report(judgments)))
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(ValueError):
            workflow.apply_event(run, event("report", dict(report("judge"), reviewer=workflow.model_record(MODEL_C, REGISTRY))))
        partial = workflow.apply_event(run, event("adjudication-report", self.judge_report([], status="partial")))
        with self.assertRaises(workflow.NeedsDecision):
            workflow.apply_event(partial, event("adjudication", judgment))
        with self.assertRaisesRegex(workflow.NeedsDecision, "partial"):
            workflow.apply_event(partial, event("complete", {"coverage_evidence": "cannot omit partial stand-in"}))

    def test_empty_batch_review_does_not_create_vet_or_stand_in_work(self):
        run = self.prepared("pr-review-batch", orchestrator=MODEL_A, stand_in="judge-profile")
        for operation, data in (("assignment", assignment()), ("started", start()), ("report", report()),
                                ("complete", {"coverage_evidence": "complete PR has no findings"})):
            run = workflow.apply_event(run, event(operation, data))
        self.assertEqual(list(run["targets"][TARGET]["assignments"]), ["review"])
        self.assertEqual(workflow.review_event(run["targets"][TARGET]), "APPROVE")

    def test_stand_in_family_can_work_on_another_pr_while_orchestrator_adjudicates_it(self):
        other = REPO + "#13"
        other_frozen = dict(FROZEN, source_root="/disposable/other/source")
        collection = dict(COLLECTION, pull_requests=[PULL, dict(PULL, number=13, url="https://github.com/fixture/project/pull/13")])
        run = workflow.new_run("pr-review-batch", collection, REGISTRY, MODEL_A, "thread")
        for key, frozen, proposed in ((TARGET, FROZEN, plan(stand_in="judge-profile")),
                                     (other, other_frozen, plan(["family-b"], finder="vet-profile", vetter="judge-profile"))):
            for operation, data in (("freeze", frozen), ("catalog", CATALOG)):
                run = workflow.apply_event(run, event(operation, data, key))
        run = workflow.apply_event(run, event("batch-wave", wave_decisions(run, {TARGET: {"fixture-profile"}, other: {"vet-profile"}}), None))
        for key, proposed in ((TARGET, plan(stand_in="judge-profile")),
                              (other, plan(["family-b"], finder="vet-profile", vetter="judge-profile"))):
            run = workflow.apply_event(run, event("plan", proposed, key))
        for key, frozen, finder, vetter, prefix in ((TARGET, FROZEN, MODEL_A, MODEL_B, "first"),
                                                  (other, other_frozen, MODEL_B, MODEL_C, "second")):
            finding_id, vet_id = prefix + "-01", prefix + "-vet"
            discovery = dict(report(prefix, [raw(finding_id, finder)]), target=frozen, reviewer=workflow.model_record(finder, REGISTRY))
            verdict = vet(finding_id, vet_id)
            vet_report = dict(report(vet_id, verdicts=[verdict]), target=frozen, reviewer=workflow.model_record(vetter, REGISTRY))
            for operation, data in (("assignment", assignment(prefix, finder)), ("started", start(prefix, finder)),
                                    ("report", discovery), ("assignment", assignment(vet_id, vetter, "vet", finding_ids=[finding_id])),
                                    ("started", start(vet_id, vetter)), ("report", vet_report), ("vet", verdict)):
                run = workflow.apply_event(run, event(operation, data, key))
        run = workflow.apply_event(run, event("assignment", assignment("judge", MODEL_C, "adjudication", finding_ids=["first-01"])))
        run = workflow.apply_event(run, event("started", start("judge", MODEL_C)))
        run = workflow.apply_event(run, event("adjudication", adjudication("second-01"), other))
        run = workflow.apply_event(run, event("complete", {"coverage_evidence": "other PR independently adjudicated"}, other))
        self.assertEqual(run["targets"][TARGET]["assignments"]["judge"]["status"], "running")
        self.assertEqual(run["targets"][other]["adjudications"]["second-01"]["adjudicator"], run["orchestrator"])
        delegated = self.judge_report([dict(adjudication("first-01"), assignment_id="judge")])
        run = workflow.apply_event(run, event("adjudication-report", delegated))
        run = workflow.apply_event(run, event("adjudication", delegated["adjudications"][0]))
        self.assertEqual(run["targets"][TARGET]["adjudications"]["first-01"]["adjudicator"]["family"], "family-c")


class RecoveryTests(unittest.TestCase):
    prepared = WorkflowTests.prepared
    discovered = WorkflowTests.discovered
    vetted = WorkflowTests.vetted
    stand_in_ready = FamilyRoutingTests.stand_in_ready
    judge_report = FamilyRoutingTests.judge_report
    judge_running = FamilyRoutingTests.judge_running

    def test_unlaunched_vet_work_can_settle_after_a_lane_failure_without_claiming_coverage(self):
        capacity = {"parent_active": 0, "process_active": 0, "evidence": "native observation: no active tasks"}
        claims = [dict(raw(f"review-{number:02}"), root_cause=f"cause-{number}", trigger=f"input {number}")
                  for number in range(1, 12)]
        for method in workflow.METHODS:
            with self.subTest(method=method):
                run = self.discovered(findings=claims, method=method)
                for key, findings in (("vet1", claims[:10]), ("vet2", claims[10:])):
                    run = workflow.apply_event(run, event("assignment", assignment(key, MODEL_B, "vet",
                        finding_ids=[finding["id"] for finding in findings])))
                run = workflow.apply_event(run, event("started", start("vet1", MODEL_B)))
                run = self.outcome(run, "vet1", cause="auth")
                self.assertEqual(workflow.contract.native_capacity(run, capacity)[1], 1)
                run = workflow.apply_event(run, event("withdraw", {"assignment_id": "vet2", "target": FROZEN,
                    "cause": "auth", "evidence": "No approval was issued; the selected lane is now unavailable"}))
                target = run["targets"][TARGET]
                self.assertEqual(target["assignments"]["vet2"]["status"], "stopped")
                self.assertEqual(workflow.contract.native_capacity(run, capacity)[1], 0)
                self.assertIn("finding review-11: UNVETTED", workflow.completion_gaps(run, target))
                run = workflow.apply_event(run, event("cleanup", {"resources": []}))
                self.assertTrue(run["targets"][TARGET]["resources_removed"])

    def test_issued_approval_requires_exact_native_nonlaunch_reconciliation_before_withdrawal(self):
        run = workflow.apply_event(self.prepared(), event("assignment", assignment()))
        run = workflow.apply_event(run, event("task-ready", {"assignment_id": "review",
            "capacity": {"parent_active": 0, "process_active": 0, "evidence": "fresh native counts",
                         "runtime_parent_root": FROZEN["parent_root"]}}))
        data = {"assignment_id": "review", "target": FROZEN, "cause": "transient", "evidence": "Native launch failed"}
        with self.assertRaises(workflow.NeedsDecision):
            workflow.apply_event(run, event("withdraw", data))
        approval = run["targets"][TARGET]["assignments"]["review"]["task_ready"]["revision"]
        for revision, status in ((approval - 1, "not-started"), (approval, "unknown")):
            with self.subTest(revision=revision, status=status), self.assertRaises(ValueError):
                workflow.apply_event(run, event("withdraw", dict(data, native_reconciliation={
                    "approval_revision": revision, "status": status, "evidence": "Native receipt for this approval"})))
        run = workflow.apply_event(run, event("withdraw", dict(data, native_reconciliation={
            "approval_revision": approval, "status": "not-started", "evidence": "Native launch returned before creating a task"})))
        self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "retry_route")
        run = workflow.apply_event(run, event("recover", self.request(run)))
        self.assertEqual(run["targets"][TARGET]["assignments"]["retry"]["status"], "queued")
        self.assertNotIn("withdrawal", run["targets"][TARGET]["assignments"]["retry"])
        self.assertEqual(run["targets"][TARGET]["assignments"]["review"]["withdrawal"]["cause"], "transient")

    def running(self, method="pr-review"):
        run = workflow.apply_event(self.prepared(method), event("assignment", assignment()))
        return workflow.apply_event(run, event("started", start()))

    def outcome(self, run, key="review", status="failed", cause="transient"):
        row = run["targets"][TARGET]["assignments"][key]
        return workflow.apply_event(run, event("runtime-outcome", {
            "assignment_id": key, "runtime_handle": row["runtime_handle"], "target": FROZEN,
            "status": status, "cause": cause, "evidence": "terminal native task receipt for this handle"}))

    def request(self, run, kind="retry", prior="review", key="retry", **extra):
        row = run["targets"][TARGET]["assignments"][prior]
        data = {"id": key, "assignment_id": prior, "kind": kind, "target": FROZEN,
                "reason": "address the recorded failure or missing evidence",
                "capacity": {"parent_active": 0, "process_active": 0, "evidence": "fresh native active counts"}}
        if kind == "retry":
            data["route"] = {"profile": row["profile"], "model": row["model"], "evidence": "verified same-model runtime route"}
        if kind == "follow-up":
            data.update(finding_id="review-01", question="Does the caller permit an empty input?",
                        arguments={"finder": copy.deepcopy(run["targets"][TARGET]["findings"]["review-01"]),
                                   "vetter": copy.deepcopy(run["targets"][TARGET]["vets"]["review-01"])},
                        new_evidence=[{"path": "src/b.py", "line": 3, "side": "target", "relation": "caller", "evidence": "new caller contract to investigate"}],
                        units=["review-0001-000"])
        return dict(data, **extra)

    def receive(self, run, reply, key="review", source_status="unchanged"):
        row = run["targets"][TARGET]["assignments"][key]
        return workflow.apply_event(run, event("report-input", {"assignment_id": key,
            "runtime_handle": row["runtime_handle"], "target": FROZEN, "report": reply,
            "source_status": source_status, "evidence": "native task returned this original reply"}))

    def follow_up(self, run, verdict="CONFIRMED"):
        run = workflow.apply_event(run, event("recover", self.request(run, "follow-up", "vet", "focused")))
        row = run["targets"][TARGET]["assignments"]["focused"]
        run = workflow.apply_event(run, event("started", dict(start("focused", MODEL_B), runtime_handle="vet-handle")))
        judgment = vet(assignment_id="focused", verdict=verdict)
        reply = dict(report("focused", verdicts=[judgment]), reviewer=workflow.model_record(MODEL_B, REGISTRY),
                     checked=row["scope"], checked_units=row["units"])
        run = self.receive(run, reply, "focused")
        self.assertEqual(run["targets"][TARGET]["vets"]["review-01"]["assignment_id"], "focused")
        return run

    def test_vet_correction_registers_the_bundle_without_losing_the_rejected_reply(self):
        findings = [raw(), dict(raw("review-02"), trigger="another input", root_cause="another-input")]
        run = self.discovered(findings=findings)
        run = workflow.apply_event(run, event("assignment", assignment("vet", MODEL_B, "vet", finding_ids=[item["id"] for item in findings])))
        run = workflow.apply_event(run, event("started", start("vet", MODEL_B)))
        rejected = report("vet", verdicts=[vet(), dict(vet("review-02"), severity="major")])
        run = self.receive(run, rejected, "vet")
        self.assertEqual(run["targets"][TARGET]["vets"], {})
        run = workflow.apply_event(run, event("recover", self.request(run, "correction", "vet", "correct")))
        run = workflow.apply_event(run, event("started", dict(start("correct", MODEL_B), runtime_handle="vet-handle")))
        verdicts = [vet(item["id"], "correct") for item in findings]
        corrected = dict(report("correct", verdicts=verdicts), reviewer=workflow.model_record(MODEL_B, REGISTRY))
        run = self.receive(run, corrected, "correct")
        target = run["targets"][TARGET]
        self.assertEqual(target["assignments"]["vet"]["rejected_report"]["report"], rejected)
        self.assertEqual(target["assignments"]["correct"]["report"], corrected)
        self.assertEqual(target["vets"], {item["finding_id"]: dict(item, vetter=workflow.model_record(MODEL_B, REGISTRY)) for item in verdicts})
        self.assertEqual(target["vet_history"], {})
        self.assertEqual(workflow.next_decision(run, target)["code"], "adjudication")

    def test_uncertain_outcome_holds_retry_and_report_until_reconciled(self):
        run = self.outcome(self.running(), status="unknown")
        for action in (event("recover", self.request(run)), event("report", report())):
            with self.assertRaises(workflow.NeedsDecision):
                workflow.apply_event(run, action)
        with self.assertRaises(workflow.NeedsDecision):
            self.receive(run, report())
        self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "runtime_reconciliation")
        run = self.outcome(run)
        self.assertEqual(run["targets"][TARGET]["assignments"]["review"]["status"], "failed")
        with self.assertRaises(ValueError):
            self.outcome(run)

    def test_one_retry_preserves_original_outcome_and_completes_both_methods(self):
        for method in workflow.METHODS:
            with self.subTest(method=method):
                run = self.outcome(self.running(method))
                original = copy.deepcopy(run["targets"][TARGET]["assignments"]["review"])
                run = workflow.apply_event(run, event("recover", self.request(run)))
                with self.assertRaisesRegex(ValueError, "fresh runtime"):
                    workflow.apply_event(run, event("started", dict(start("retry"), runtime_handle="review-handle")))
                run = workflow.apply_event(run, event("started", start("retry")))
                run = self.receive(run, report("retry"), "retry")
                self.assertEqual(workflow.completion_gaps(run, run["targets"][TARGET]), [])
                self.assertEqual(run["targets"][TARGET]["assignments"]["review"], dict(original, superseded_by="retry"))
                run = workflow.apply_event(run, event("complete", {"coverage_evidence": "retry checked all frozen units and categories"}))
                self.assertEqual(run["targets"][TARGET]["phase"], "complete")
        run = self.outcome(self.running())
        run = workflow.apply_event(run, event("recover", self.request(run)))
        run = workflow.apply_event(run, event("started", start("retry")))
        run = self.outcome(run, "retry")
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("recover", self.request(run, prior="retry", key="again")))
        self.assertEqual(caught.exception.code, "recovery_exhausted")
        self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "recovery_exhausted")

    def test_unavailable_lanes_are_run_wide_and_only_verified_same_model_routes_retry(self):
        for cause in ("auth", "quota", "rate"):
            run = self.outcome(self.running(), cause=cause)
            with self.subTest(cause=cause), self.assertRaises(workflow.NeedsDecision) as caught:
                workflow.apply_event(run, event("recover", self.request(run)))
            self.assertEqual(caught.exception.code, "unavailable_lane")
            self.assertEqual(set(run["unavailable_lanes"]), {"lane-a"})
        registry: dict = copy.deepcopy(REGISTRY)
        alternate = {"provider_id": "provider-b", "model_id": "model-a"}
        registry["models"].append(dict(alternate, family="family-a"))
        catalog = copy.deepcopy(CATALOG)
        catalog["profiles"]["alternate"] = dict(catalog["profiles"]["fixture-profile"], identity=workflow.model_record(alternate, registry))
        run = workflow.new_run("pr-review", COLLECTION, registry, MODEL_C, "thread")
        run = workflow.apply_event(run, event("freeze", FROZEN))
        run = workflow.apply_event(run, event("catalog", catalog))
        proposed = plan()
        proposed["profiles"]["alternate"] = {"decision": "omitted", "primary": False, "evidence": "verified alternate route for the selected model", "capability": 1}
        run = workflow.apply_event(run, event("plan", proposed))
        run = workflow.apply_event(run, event("assignment", assignment()))
        run = workflow.apply_event(run, event("started", start()))
        run = self.outcome(run, cause="auth")
        for model, name in ((MODEL_B, "vet-profile"), (MODEL_C, "judge-profile")):
            with self.assertRaisesRegex(ValueError, "exact selected effective model"):
                workflow.apply_event(run, event("recover", self.request(run, route={"profile": name, "model": model, "evidence": "different model"})))
        run = workflow.apply_event(run, event("recover", self.request(run, route={"profile": "alternate", "model": alternate, "evidence": "verified available route"})))
        run = workflow.apply_event(run, event("started", dict(start("retry"), runtime_profile="alternate", runtime_identity=alternate)))
        run = self.receive(run, dict(report("retry"), reviewer=workflow.model_record(alternate, registry)), "retry")
        self.assertEqual(workflow.completion_gaps(run, run["targets"][TARGET]), [])
        self.assertEqual(run["targets"][TARGET]["plan"]["routing"]["finder"], "fixture-profile")

    def test_deterministic_rejections_and_cancelled_jobs_do_not_retry(self):
        for cause in ("identity", "role", "source", "cancelled"):
            run = self.outcome(self.running(), cause=cause)
            with self.subTest(cause=cause), self.assertRaises(workflow.NeedsDecision) as caught:
                workflow.apply_event(run, event("recover", self.request(run)))
            self.assertEqual(caught.exception.code, "retry_cause")
            self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "runtime_rejection")

    def test_reply_after_native_failure_preserves_failure_and_unavailable_lane(self):
        for cause in ("auth", "transient", "role"):
            failed = self.outcome(self.running(), cause=cause)
            outcome = copy.deepcopy(failed["targets"][TARGET]["assignments"]["review"]["runtime_outcome"])
            for reply in ("bad JSON", report(), report(status="partial")):
                with self.subTest(cause=cause, reply=reply):
                    run = self.receive(failed, reply)
                    row = run["targets"][TARGET]["assignments"]["review"]
                    self.assertEqual(row["status"], "failed")
                    self.assertEqual(row["runtime_outcome"], outcome)
                    if cause == "auth":
                        self.assertIn("lane-a", run["unavailable_lanes"])
                    self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "runtime_rejection" if cause == "role" else "retry_route")
                    with self.assertRaises(workflow.NeedsDecision):
                        workflow.apply_event(run, event("recover", self.request(run, "correction", key="correct")))

    def test_correction_retains_rejected_input_and_draft_ids_on_the_same_handle(self):
        reply = report(findings=[raw()])
        del reply["categories"]
        run = self.receive(self.running(), reply)
        rejected = copy.deepcopy(run["targets"][TARGET]["assignments"]["review"]["rejected_report"])
        self.assertEqual(rejected["report"], reply)
        self.assertIn("categories", rejected["reason"])
        self.assertEqual(run["targets"][TARGET]["findings"], {})
        run = workflow.apply_event(run, event("recover", self.request(run, "correction", key="correct")))
        task = workflow.contract.task_block(run, TARGET, "correct")
        self.assertEqual(task["runtime_handle"], "review-handle")
        payload = json.loads(task["task"].splitlines()[1])
        self.assertIn(rejected["reason"], payload["correction"]["reason"])
        self.assertEqual(payload["report_template"]["assignment_id"], "correct")
        self.assertEqual(payload["report_template"]["target"], FROZEN)
        self.assertEqual(payload["report_template"]["reviewer"], workflow.model_record(MODEL_A, REGISTRY))
        self.assertEqual(payload["finding_template"]["id"], "review-NN")
        with self.assertRaisesRegex(ValueError, "reuse"):
            workflow.apply_event(run, event("started", start("correct")))
        run = workflow.apply_event(run, event("started", dict(start("correct"), runtime_handle="review-handle")))
        corrected = copy.deepcopy(payload["report_template"])
        corrected.update({name: value for name, value in report("correct", findings=[raw()]).items()
                          if name not in ("assignment_id", "target", "reviewer")})
        run = self.receive(run, corrected, "correct")
        self.assertEqual(run["targets"][TARGET]["findings"], {"review-01": raw()})
        self.assertEqual(run["targets"][TARGET]["assignments"]["review"]["rejected_report"], rejected)
        self.assertNotIn("assignment review: blocked", workflow.completion_gaps(run, run["targets"][TARGET]))

    def test_persistent_malformed_reports_exhaust_correction_and_hold_completion(self):
        run = self.receive(self.running(), "not JSON")
        run = workflow.apply_event(run, event("recover", self.request(run, "correction", key="correct")))
        run = workflow.apply_event(run, event("started", dict(start("correct"), runtime_handle="review-handle")))
        run = self.receive(run, None, "correct")
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("recover", self.request(run, "correction", "correct", "again")))
        self.assertEqual(caught.exception.code, "recovery_exhausted")
        with self.assertRaises(workflow.NeedsDecision):
            workflow.apply_event(run, event("complete", {"coverage_evidence": "no findings returned"}))
        self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "recovery_exhausted")
        with self.assertRaises(ValueError):
            self.receive(run, report("correct"), "correct")

    def test_malformed_nested_finding_ids_become_precise_report_errors(self):
        run = self.discovered(findings=[raw()])
        run = workflow.apply_event(run, event("assignment", assignment("vet", MODEL_B, "vet", finding_ids=["review-01"])))
        run = workflow.apply_event(run, event("started", start("vet", MODEL_B)))
        bad = report("vet", verdicts=[dict(vet(), finding_id=[])])
        rejected = self.receive(run, bad, "vet")
        self.assertEqual(rejected["targets"][TARGET]["assignments"]["vet"]["rejected_report"]["code"], "invalid_report")
        stand_in = self.judge_running()
        rejected = self.receive(stand_in, self.judge_report([dict(adjudication(), assignment_id="judge", finding_id=[])]), "judge")
        self.assertEqual(rejected["targets"][TARGET]["assignments"]["judge"]["status"], "blocked")

    def test_partial_correction_does_not_erase_original_material_gap(self):
        original = dict(report(status="partial"), coverage_gaps=[{"id": "caller", "reason": "missing caller contract", "material": True}])
        run = self.receive(self.running(), original)
        run = workflow.apply_event(run, event("recover", self.request(run, "correction", key="correct")))
        run = workflow.apply_event(run, event("started", dict(start("correct"), runtime_handle="review-handle")))
        run = self.receive(run, report("correct"), "correct")
        self.assertEqual(run["targets"][TARGET]["assignments"]["review"]["report"], original)
        with self.assertRaisesRegex(workflow.NeedsDecision, "missing caller contract"):
            workflow.apply_event(run, event("complete", {"coverage_evidence": "corrected report"}))
        run = workflow.apply_event(run, event("resolve-gap", {"id": "review/caller", "evidence": "caller contract traced in src/b.py:3"}))
        self.assertEqual(workflow.completion_gaps(run, run["targets"][TARGET]), [])

    def test_runtime_retry_keeps_accepted_findings_and_report_provenance(self):
        run = self.receive(self.running(), report(findings=[raw()], status="failed"))
        run = self.outcome(run)
        run = workflow.apply_event(run, event("recover", self.request(run)))
        payload = json.loads(workflow.contract.task_block(run, TARGET, "retry")["task"].splitlines()[1])
        self.assertEqual(payload["retained_findings"], [raw()])
        run = workflow.apply_event(run, event("started", start("retry")))
        run = self.receive(run, report("retry", findings=[raw()]), "retry")
        target = run["targets"][TARGET]
        self.assertEqual(target["findings"], {"review-01": raw()})
        self.assertEqual(target["assignments"]["review"]["report"]["status"], "failed")
        self.assertEqual(target["assignments"]["retry"]["report"]["findings"], [raw()])
        self.assertIn("finding review-01: UNVETTED", workflow.completion_gaps(run, target))

    def test_recovery_observations_include_other_running_work_and_queued_reservations(self):
        run = self.outcome(self.running())
        other = "fixture/project#13"
        run["targets"][other] = copy.deepcopy(run["targets"][TARGET])
        run["targets"][other]["assignments"] = {"other": dict(assignment("other"), status="running", actual_identity=workflow.model_record(MODEL_A, REGISTRY), report=None)}
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("recover", self.request(run)))
        self.assertEqual(caught.exception.code, "native_capacity")
        run["targets"][other]["assignments"]["other"]["status"] = "queued"
        capacity = {"parent_active": 14, "process_active": 29, "evidence": "one slot already reserved locally"}
        with self.assertRaises(workflow.NeedsDecision):
            workflow.apply_event(run, event("recover", self.request(run, capacity=capacity)))

    def test_recovery_capacity_source_and_target_bindings_preserve_state(self):
        run = self.outcome(self.running())
        original = copy.deepcopy(run)
        for change in ({"target": dict(FROZEN, head_sha="f" * 40)},
                       {"capacity": {"parent_active": 15, "process_active": 30, "evidence": "full"}},
                       {"capacity": {"parent_active": True, "process_active": 0, "evidence": "invalid"}},
                       {"units": []}, {"route": None}):
            with self.subTest(change=change), self.assertRaises((ValueError, workflow.NeedsDecision)):
                workflow.apply_event(run, event("recover", self.request(run, **change)))
            self.assertEqual(run, original)
        changed = workflow.apply_event(run, event("integrity", {"status": "unknown", "components": ["parent"]}))
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(changed, event("recover", self.request(changed)))
        self.assertEqual(caught.exception.code, "source_integrity")
        queued = workflow.apply_event(run, event("recover", self.request(run)))
        with self.assertRaises(ValueError):
            workflow.apply_event(queued, event("recover", self.request(queued, key="bypass")))
        with self.assertRaisesRegex(ValueError, "already assigned"):
            workflow.apply_event(run, event("assignment", assignment("bypass")))

    def test_one_focused_vet_and_designated_adjudication_retain_both_histories(self):
        for method in workflow.METHODS:
            with self.subTest(method=method):
                run = self.vetted(method=method, verdict="UNVERIFIABLE")
                run = workflow.apply_event(run, event("adjudication", adjudication(disposition="DISPUTED")))
                original = copy.deepcopy(run["targets"][TARGET])
                run = self.follow_up(run)
                target = run["targets"][TARGET]
                self.assertEqual(target["vet_history"]["review-01"], [original["vets"]["review-01"]])
                self.assertEqual(target["findings"], original["findings"])
                self.assertTrue(workflow.readjudication_allowed(target, "review-01"))
                run = workflow.apply_event(run, event("adjudication", adjudication()))
                target = run["targets"][TARGET]
                self.assertEqual(target["adjudication_history"]["review-01"], [original["adjudications"]["review-01"]])
                self.assertEqual(target["adjudications"]["review-01"]["adjudicator"], run["orchestrator"])
                self.assertEqual(workflow.completion_gaps(run, target), [])
                with self.assertRaises(ValueError):
                    workflow.apply_event(run, event("adjudication", adjudication()))
                with self.assertRaises((ValueError, workflow.NeedsDecision)):
                    workflow.apply_event(run, event("recover", self.request(run, "follow-up", "focused", "again")))

    def test_dispute_without_new_evidence_or_original_arguments_does_not_launch(self):
        run = self.vetted(verdict="UNVERIFIABLE")
        request = self.request(run, "follow-up", "vet", "focused")
        for change in ({"new_evidence": references()}, {"arguments": {"finder": raw(), "vetter": dict(vet(), claim="different argument")}},
                       {"assignment_id": "review"}, {"route": {"profile": "judge-profile"}}):
            with self.subTest(change=change), self.assertRaises((ValueError, workflow.NeedsDecision)):
                workflow.apply_event(run, event("recover", dict(request, **change)))
        run = workflow.apply_event(run, event("adjudication", adjudication(disposition="DISPUTED")))
        run = self.follow_up(run, "UNVERIFIABLE")
        run = workflow.apply_event(run, event("adjudication", adjudication(disposition="DISPUTED")))
        with self.assertRaises(workflow.NeedsDecision):
            workflow.apply_event(run, event("complete", {"coverage_evidence": "still disputed"}))
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("recover", self.request(run, "follow-up", "focused", "again")))
        self.assertEqual(caught.exception.code, "dispute_exhausted")

    def test_stand_in_reconsideration_uses_new_evidence_and_preserves_its_exact_judgment(self):
        run = self.judge_running()
        with self.assertRaises(workflow.NeedsDecision):
            workflow.apply_event(run, event("recover", self.request(run, "follow-up", "vet", "focused")))
        first = dict(adjudication(disposition="DISPUTED"), assignment_id="judge")
        run = self.receive(run, self.judge_report([first]), "judge")
        run = workflow.apply_event(run, event("adjudication", first))
        run = self.follow_up(run)
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("adjudication", first))
        self.assertEqual(caught.exception.code, "stale_adjudication")
        run = workflow.apply_event(run, event("assignment", assignment("rejudge", MODEL_C, "adjudication", finding_ids=["review-01"])))
        run = workflow.apply_event(run, event("started", start("rejudge", MODEL_C)))
        final = dict(adjudication(), assignment_id="rejudge")
        reply = dict(self.judge_report([final]), assignment_id="rejudge")
        run = self.receive(run, reply, "rejudge")
        with self.assertRaisesRegex(ValueError, "one stand-in reconsideration"):
            workflow.apply_event(run, event("assignment", assignment("extra", MODEL_C, "adjudication", finding_ids=["review-01"])))
        run = workflow.apply_event(run, event("adjudication", final))
        self.assertEqual(run["targets"][TARGET]["assignments"]["rejudge"]["report"], reply)
        self.assertEqual(run["targets"][TARGET]["adjudications"]["review-01"]["adjudicator"], workflow.model_record(MODEL_C, REGISTRY))
        self.assertEqual(workflow.completion_gaps(run, run["targets"][TARGET]), [])

    def test_focused_vet_does_not_stale_an_unincorporated_neighbor_in_stand_in_bundle(self):
        run = self.prepared("pr-review-batch", orchestrator=MODEL_A, stand_in="judge-profile")
        findings = [raw(), dict(raw("review-02"), root_cause="other-cause", trigger="other input")]
        for operation, data in (("assignment", assignment()), ("started", start()), ("report", report(findings=findings)),
                                ("assignment", assignment("vet", MODEL_B, "vet", finding_ids=[item["id"] for item in findings])),
                                ("started", start("vet", MODEL_B)), ("report", report("vet", verdicts=[vet(item["id"]) for item in findings])),
                                ("assignment", assignment("judge", MODEL_C, "adjudication", finding_ids=[item["id"] for item in findings])),
                                ("started", start("judge", MODEL_C))):
            run = workflow.apply_event(run, event(operation, data))
        first = dict(adjudication(disposition="DISPUTED"), assignment_id="judge")
        neighbor = dict(adjudication("review-02"), assignment_id="judge")
        run = self.receive(run, self.judge_report([first, neighbor]), "judge")
        run = workflow.apply_event(run, event("adjudication", first))
        run = self.follow_up(run)
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("adjudication", first))
        self.assertEqual(caught.exception.code, "stale_adjudication")
        run = workflow.apply_event(run, event("adjudication", neighbor))
        self.assertEqual(run["targets"][TARGET]["adjudications"]["review-02"]["vet_assignment_id"], "vet")
        run = workflow.apply_event(run, event("assignment", assignment("rejudge", MODEL_C, "adjudication", finding_ids=["review-01"])))
        run = workflow.apply_event(run, event("started", start("rejudge", MODEL_C)))
        final = dict(adjudication(), assignment_id="rejudge")
        run = self.receive(run, dict(self.judge_report([final]), assignment_id="rejudge"), "rejudge")
        run = workflow.apply_event(run, event("adjudication", final))
        self.assertEqual(workflow.completion_gaps(run, run["targets"][TARGET]), [])
        with self.assertRaisesRegex(ValueError, "settle|unadjudicated|already"):
            workflow.apply_event(run, event("assignment", assignment("extra", MODEL_C, "adjudication", finding_ids=["review-02"])))

    def test_same_model_restatements_and_repeated_vet_assignments_do_not_reset_cycles(self):
        run = self.running()
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("report", report(findings=[raw(), raw("review-02")])))
        self.assertEqual(caught.exception.code, "finding_restatement")
        self.assertEqual(run["targets"][TARGET]["findings"], {})
        run = self.vetted()
        with self.assertRaisesRegex(ValueError, "already has a vet assignment"):
            workflow.apply_event(run, event("assignment", assignment("extra", MODEL_B, "vet", finding_ids=["review-01"])))
        run = self.follow_up(run)
        run = workflow.apply_event(run, event("vet", vet(assignment_id="focused")))
        with self.assertRaisesRegex(ValueError, "already recorded"):
            workflow.apply_event(run, event("vet", vet(assignment_id="focused")))

    def test_reciprocal_vetter_can_corroborate_its_own_original_without_a_new_vet_wave(self):
        run = self.discovered(findings=[raw()])
        run = workflow.apply_event(run, event("assignment", assignment("vet", MODEL_B, "vet", finding_ids=["review-01"])))
        run = workflow.apply_event(run, event("started", start("vet", MODEL_B)))
        run = self.receive(run, report("vet", findings=[raw("vet-01", MODEL_B)]), "vet")
        run = workflow.apply_event(run, event("assignment", assignment("spill", MODEL_A, "vet", finding_ids=["vet-01"])))
        run = workflow.apply_event(run, event("started", start("spill")))
        spill_verdict = vet("vet-01", "spill")
        run = self.receive(run, report("spill", findings=[raw()], verdicts=[spill_verdict]), "spill")
        self.assertEqual(set(run["targets"][TARGET]["findings"]), {"review-01", "vet-01"})
        self.assertEqual(set(run["targets"][TARGET]["vets"]), {"review-01", "vet-01"})

    def test_recovery_and_rejected_reports_replay_without_losing_original_events(self):
        run = self.receive(self.running(), "bad JSON")
        run = workflow.apply_event(run, event("recover", self.request(run, "correction", key="correct")))
        run = workflow.apply_event(run, event("started", dict(start("correct"), runtime_handle="review-handle")))
        run = self.receive(run, report("correct"), "correct")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            initial = {"method": run["method"], "intake": run["intake"], "registry": run["registry"], "orchestrator": MODEL_C,
                       "thread_id": run["thread_id"], "run_id": run["run_id"]}
            workflow.write_json(path, {"version": 1, "initial": initial, "events": run["events"]})
            self.assertEqual(workflow.load_run(path)[1], run)
        self.assertEqual(run["revision"], len(run["events"]))
        self.assertEqual(run["events"][-1]["operation"], "report-input")

    def test_cli_retains_source_observation_and_rejected_report_before_returning_status(self):
        run = self.running()
        reply = event("report-input", {"assignment_id": "review", "runtime_handle": "review-handle", "target": FROZEN,
            "report": report(), "source_status": "unknown", "evidence": "native completed reply with unreadable parent"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            initial = {"method": run["method"], "intake": run["intake"], "registry": run["registry"], "orchestrator": MODEL_C,
                       "thread_id": run["thread_id"], "run_id": run["run_id"]}
            workflow.write_json(root / "run.json", {"version": 1, "initial": initial, "events": run["events"]})
            workflow.write_json(root / "event.json", reply)
            argv = ["workflow.py", "--run", str(root), "record", "--event", str(root / "event.json"), "--revision", str(run["revision"])]
            def observe(directory, saved, current, stop):
                self.assertFalse(stop)
                return workflow.append_record(directory, saved, current, TARGET, "integrity", {"status": "unknown", "components": ["parent"]})
            with patch.object(sys, "argv", argv), patch.object(workflow, "check_sources", side_effect=observe) as check, patch.object(sys, "stdout", io.StringIO()):
                self.assertEqual(workflow.main(), 0)
            check.assert_called_once()
            accepted = workflow.load_run(root / "run.json")[1]
            self.assertEqual(accepted["events"][-2]["operation"], "integrity")
            self.assertEqual(accepted["events"][-1], reply)
            self.assertEqual(workflow.next_decision(accepted, accepted["targets"][TARGET])["code"], "source_integrity")
            with self.assertRaises(workflow.NeedsDecision):
                workflow.apply_event(accepted, event("recover", self.request(accepted, "correction", key="correct")))


class BatchWaveTests(unittest.TestCase):
    def pending(self, count=1, catalog=None, registry=None):
        catalog, registry = catalog or CATALOG, registry or REGISTRY
        pulls = [dict(PULL, number=12 + index, url=f"https://github.com/{REPO}/pull/{12 + index}") for index in range(count)]
        run = workflow.new_run("pr-review-batch", dict(COLLECTION, pull_requests=pulls), registry, MODEL_C, "thread")
        for index, key in enumerate(run["targets"]):
            frozen = FROZEN if index == 0 else dict(FROZEN, source_root=f"/disposable/{index}/source")
            run = workflow.apply_event(run, event("freeze", frozen, key))
            run = workflow.apply_event(run, event("catalog", catalog, key))
        return run

    def allocate(self, run, **kwargs):
        return workflow.apply_event(run, event("batch-wave", wave_decisions(run, **kwargs), None))

    def seven_families(self):
        catalog = copy.deepcopy(CATALOG)
        registry: dict = copy.deepcopy(REGISTRY)
        for letter in "defg":
            model = {"provider_id": "provider-a", "model_id": "model-" + letter}
            registry["models"].append(dict(model, family="family-" + letter))
            catalog["profiles"]["profile-" + letter] = {"identity": workflow.model_record(model, registry), "roles": ["reviewer"], "worktree": "isolated"}
        return catalog, registry

    def test_wave_size_diversity_and_repeated_instances_follow_pr_count(self):
        catalog, registry = self.seven_families()
        for count in (1, 2, 6, 7, 15, 22):
            with self.subTest(count=count):
                run = self.allocate(self.pending(count, catalog, registry))
                wave = run["batch_waves"][0]
                allocations = list(wave["allocations"].values())
                families = [row["identity"]["family"] for row in allocations]
                self.assertEqual(len(allocations), min(count, 15))
                self.assertEqual(len(wave["deferred"]), max(0, count - 15))
                self.assertEqual(len(set(families)), min(count, 7))
                self.assertEqual("family-c" in families, count >= 7)
                if count >= 7:
                    loads = [families.count(family) for family in set(families)]
                    self.assertLessEqual(max(loads) - min(loads), 1)
                if count >= 15:
                    self.assertEqual(max(families.count(family) for family in set(families)), 3)
                self.assertFalse(workflow.status(run)["enforcement"]["dispatch"])

    def test_extra_slot_rotates_after_owned_reservations_are_removed(self):
        catalog, registry = self.seven_families()
        run = self.allocate(self.pending(30, catalog, registry))
        first = list(run["batch_waves"][0]["allocations"].values())
        for key in run["batch_waves"][0]["allocations"]:
            run = workflow.apply_event(run, event("cleanup", {"resources": []}, key))
        run = self.allocate(run)
        second = list(run["batch_waves"][1]["allocations"].values())
        def extra(rows):
            return next(family for family in {row["identity"]["family"] for row in rows}
                        if sum(row["identity"]["family"] == family for row in rows) == 3)
        self.assertEqual(extra(first), "family-a")
        self.assertEqual(extra(second), "family-b")
        self.assertFalse(set(run["batch_waves"][0]["allocations"]) & set(run["batch_waves"][1]["allocations"]))

    def test_matching_preserves_restricted_pr_and_avoids_unnecessary_stand_in(self):
        run = self.pending(2)
        other = next(key for key in run["targets"] if key != TARGET)
        run = self.allocate(run, eligible={TARGET: {"fixture-profile", "vet-profile"}, other: {"fixture-profile"}})
        allocations = run["batch_waves"][0]["allocations"]
        self.assertEqual(allocations[TARGET]["profile"], "vet-profile")
        self.assertEqual(allocations[other]["profile"], "fixture-profile")

    def test_strongest_identity_is_selected_within_family_despite_provider_alias(self):
        catalog = copy.deepcopy(CATALOG)
        registry: dict = copy.deepcopy(REGISTRY)
        model = {"provider_id": "provider-b", "model_id": "strong-a"}
        registry["models"].append(dict(model, family="family-a"))
        catalog["profiles"]["strong-profile"] = dict(catalog["profiles"]["fixture-profile"], identity=workflow.model_record(model, registry))
        run = self.pending(1, catalog, registry)
        ranks = {name: 2 for name in catalog["profiles"]} | {"strong-profile": 1}
        run = self.allocate(run, ranks=ranks)
        chosen = run["batch_waves"][0]["allocations"][TARGET]
        self.assertEqual(chosen["profile"], "strong-profile")
        self.assertEqual(chosen["identity"]["family"], "family-a")
        self.assertEqual(chosen["identity"]["provider_id"], "provider-b")

    def test_native_capacity_and_local_reservations_bound_successive_waves(self):
        run = self.allocate(self.pending(20), parent_active=13, process_active=29)
        self.assertEqual(len(run["batch_waves"][0]["allocations"]), 1)
        with self.assertRaises(workflow.NeedsDecision) as caught:
            self.allocate(run, parent_active=13, process_active=29)
        self.assertEqual(caught.exception.code, "native_capacity")
        run = self.allocate(run, parent_active=13, process_active=28)
        self.assertEqual(run["batch_waves"][1]["reserved"], 1)
        self.assertEqual(len(run["batch_waves"][1]["allocations"]), 1)

    def test_queued_and_running_jobs_are_counted_and_stale_observation_holds(self):
        run = self.allocate(self.pending(3), parent_active=14, process_active=14)
        chosen = run["batch_waves"][0]["allocations"][TARGET]
        self.assertEqual(chosen["profile"], "fixture-profile")
        run = workflow.apply_event(run, event("plan", plan()))
        run = workflow.apply_event(run, event("assignment", assignment()))
        with self.assertRaises(workflow.NeedsDecision):
            self.allocate(run, parent_active=14, process_active=14)
        run = workflow.apply_event(run, event("started", start()))
        with self.assertRaises(workflow.NeedsDecision) as caught:
            self.allocate(run)
        self.assertEqual(caught.exception.code, "native_capacity")
        observed = self.allocate(run, parent_active=1, process_active=1)
        self.assertEqual(len(observed["batch_waves"][1]["allocations"]), 2)

    def test_no_reviewer_holds_only_affected_target_and_can_be_reassessed(self):
        run = self.pending(2)
        other = next(key for key in run["targets"] if key != TARGET)
        run = self.allocate(run, eligible={TARGET: set(), other: {"vet-profile"}})
        wave = run["batch_waves"][0]
        self.assertEqual(wave["held"][TARGET]["code"], "batch_reviewer")
        self.assertEqual(set(wave["allocations"]), {other})
        self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "batch_reviewer")
        run = self.allocate(run)
        self.assertEqual(set(run["batch_waves"][1]["allocations"]), {TARGET})

    def test_deferred_judgments_are_reused_without_another_model_decision(self):
        run = self.allocate(self.pending(3), parent_active=14, process_active=14)
        data = {"capacity": {"parent_active": 12, "process_active": 12, "evidence": "fresh native observation"}, "reviewers": {}}
        run = workflow.apply_event(run, event("batch-wave", data, None))
        self.assertEqual(len(run["batch_waves"][1]["allocations"]), 2)
        self.assertEqual(run["events"][-1]["data"]["reviewers"], {})
        self.assertEqual(set(run["batch_waves"][1]["decisions"]["reviewers"]), set(run["batch_waves"][1]["allocations"]))

    def test_scout_and_absent_reviewer_do_not_supply_discovery_capacity(self):
        catalog = copy.deepcopy(CATALOG)
        catalog["profiles"]["scout"] = dict(catalog["profiles"]["fixture-profile"], roles=["scout"])
        run = self.pending(1, catalog)
        data = wave_decisions(run)
        data["reviewers"][TARGET]["profiles"]["scout"]["eligible"] = True
        with self.assertRaisesRegex(ValueError, "reviewer/candidate"):
            workflow.apply_event(run, event("batch-wave", data, None))
        with self.assertRaises(workflow.NeedsDecision) as caught:
            self.allocate(run, eligible={TARGET: set()})
        self.assertEqual(caught.exception.code, "batch_reviewer")

    def test_missing_and_malformed_judgments_preserve_state(self):
        run = self.pending()
        baseline = copy.deepcopy(run)
        data = wave_decisions(run)
        cases = []
        for name, value in (("parent_active", True), ("parent_active", -1), ("parent_active", 16), ("process_active", 31)):
            bad = copy.deepcopy(data)
            bad["capacity"][name] = value
            cases.append((bad, ValueError))
        bad = copy.deepcopy(data)
        bad["capacity"].update(parent_active=2, process_active=1)
        cases.append((bad, ValueError))
        bad = copy.deepcopy(data)
        bad["reviewers"][TARGET]["target"]["head_sha"] = "f" * 40
        cases.append((bad, ValueError))
        for field in ("eligible", "capability", "evidence"):
            bad = copy.deepcopy(data)
            del bad["reviewers"][TARGET]["profiles"]["fixture-profile"][field]
            cases.append((bad, workflow.NeedsDecision))
        for bad, exception in cases:
            original = copy.deepcopy(bad)
            with self.subTest(data=bad), self.assertRaises(exception):
                workflow.apply_event(run, event("batch-wave", bad, None))
            self.assertEqual(bad, original)
            self.assertEqual(run, baseline)
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("batch-wave", dict(data, reviewers={}), None))
        self.assertEqual(caught.exception.code, "batch_reviewers")

    def test_allocation_is_mandatory_and_finder_judgments_cannot_be_rewritten(self):
        run = self.pending()
        self.assertEqual(workflow.next_decision(run, run["targets"][TARGET])["code"], "batch_wave")
        with self.assertRaises(workflow.NeedsDecision) as caught:
            workflow.apply_event(run, event("plan", plan()))
        self.assertEqual(caught.exception.code, "batch_wave")
        run = self.allocate(run)
        for proposed in (plan(["family-b"], finder="vet-profile", vetter="fixture-profile"), plan()):
            if proposed["routing"]["finder"] == "fixture-profile":
                proposed["profiles"]["fixture-profile"]["capability"] = 9
            with self.assertRaisesRegex(ValueError, "wave"):
                workflow.apply_event(run, event("plan", proposed))
        run = workflow.apply_event(run, event("plan", plan()))
        self.assertEqual(run["targets"][TARGET]["plan"]["routing"]["finder"], "fixture-profile")

    def test_wave_is_run_scoped_replayable_and_rejects_source_failure(self):
        run = self.pending(2)
        data = wave_decisions(run)
        with self.assertRaisesRegex(ValueError, "target must be null"):
            workflow.apply_event(run, event("batch-wave", data))
        original = copy.deepcopy(data)
        run = workflow.apply_event(run, event("batch-wave", data, None))
        self.assertEqual(data, original)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            initial = {"method": "pr-review-batch", "intake": run["intake"], "registry": run["registry"],
                       "orchestrator": MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"]}
            workflow.write_json(path, {"version": 1, "initial": initial, "events": run["events"]})
            self.assertEqual(workflow.load_run(path)[1], run)
        run = self.pending()
        run = workflow.apply_event(run, event("integrity", {"status": "unknown", "components": ["source"]}))
        with self.assertRaises(workflow.NeedsDecision) as caught:
            self.allocate(run)
        self.assertEqual(caught.exception.code, "source_integrity")
        with self.assertRaisesRegex(ValueError, "requires pr-review-batch"):
            self.allocate(self.pending() | {"method": "pr-review"})

    def test_cli_checks_sources_and_persists_run_event_under_revision_lock(self):
        run = self.pending()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            initial = {"method": "pr-review-batch", "intake": run["intake"], "registry": run["registry"],
                       "orchestrator": MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"]}
            workflow.write_json(root / "run.json", {"version": 1, "initial": initial, "events": run["events"]})
            workflow.write_json(root / "event.json", event("batch-wave", wave_decisions(run), None))
            argv = ["workflow.py", "--run", str(root), "record", "--event", str(root / "event.json"), "--revision", str(run["revision"])]
            with patch.object(sys, "argv", argv), patch.object(workflow, "check_sources", return_value=run) as check, patch.object(sys, "stdout", io.StringIO()):
                self.assertEqual(workflow.main(), 0)
            check.assert_called_once()
            accepted = workflow.load_run(root / "run.json")[1]
            self.assertEqual(len(accepted["batch_waves"]), 1)
            original = (root / "run.json").read_bytes()
            with patch.object(sys, "argv", argv), patch.object(workflow, "check_sources") as check, patch.object(sys, "stderr", io.StringIO()):
                self.assertEqual(workflow.main(), 1)
            check.assert_not_called()
            self.assertEqual((root / "run.json").read_bytes(), original)


class CliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="review-workflow-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.directory = self.base / "run"
        for name, data in (("registry", REGISTRY), ("orchestrator", MODEL_B), ("event", event("freeze", FROZEN))):
            (self.base / (name + ".json")).write_text(json.dumps(data))
        (self.base / "rtk").write_text('#!/bin/sh\n[ "$1" = proxy ] || exit 2\nshift\nexec "$@"\n')
        (self.base / "gh").write_text("#!" + sys.executable + "\n" + '''
import json, os, sys
args = sys.argv[1:]
assert args[:3] == ["api", "--method", "GET"], args
endpoint = args[3]
if endpoint == "user":
    result = {"login": "owner"}
elif "reviews" in endpoint:
    result = json.loads(os.environ.get("WORKFLOW_REVIEWS", "[[]]"))
elif "comments" in endpoint:
    result = [[]]
else:
    result = {"number": 12, "html_url": "https://github.com/fixture/project/pull/12", "title": "Fixture", "body": os.environ.get("WORKFLOW_BODY", "Intent"),
              "user": {"login": os.environ.get("WORKFLOW_AUTHOR", "author")}, "base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}}
print(json.dumps(result))
''')
        for name in ("gh", "rtk"):
            (self.base / name).chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.base) + ":" + os.environ["PATH"])

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "workflow.py"), "--run", str(self.directory), *args],
                              text=True, capture_output=True, env=self.env, check=False)

    def create(self, *extra):
        return self.cli("create", "--method", "pr-review", "--thread-id", "thread", "--repo", REPO, "--pr", "12",
                        "--registry", str(self.base / "registry.json"), "--orchestrator", str(self.base / "orchestrator.json"), *extra)

    def test_admission_mode_and_cwd_capture_are_generated_persisted_and_immutable(self):
        for mode in ("observed", "native"):
            with self.subTest(mode=mode):
                self.directory = self.base / mode
                created = self.create(*([] if mode == "observed" else ["--admission-mode", mode]))
                self.assertEqual(created.returncode, 0, created.stderr)
                saved = json.loads((self.directory / "run.json").read_text())
                self.assertEqual(saved["initial"]["admission_mode"], mode)
                (self.base / "parent.json").write_text(json.dumps(event("native-parent", {
                    "root": "/invented/root", "thread_id": "thread", "source": "terminal-cwd"}, None)))
                manual = self.cli("record", "--event", str(self.base / "parent.json"), "--revision", "0")
                self.assertNotEqual(manual.returncode, 0)
                self.assertIn("dedicated command", manual.stderr)
                command = [sys.executable, str(SCRIPTS / "workflow.py"), "--run", str(self.directory),
                           "native-parent", "--revision", "0"]
                captured = subprocess.run(command, cwd=self.base, env=self.env, text=True, capture_output=True, check=False)
                self.assertEqual(captured.returncode, 0, captured.stderr)
                replay = json.loads(self.cli("status").stdout)
                self.assertEqual(replay["admission_mode"], mode)
                self.assertEqual(replay["native_parent"], {"root": str(self.base), "thread_id": "thread", "source": "terminal-cwd"})
                original = (self.directory / "run.json").read_bytes()
                repeated = self.cli("native-parent", "--revision", "1")
                self.assertNotEqual(repeated.returncode, 0)
                self.assertEqual((self.directory / "run.json").read_bytes(), original)

    def test_cli_own_pr_override_is_explicit_persisted_and_survives_status_replay(self):
        self.env["WORKFLOW_AUTHOR"] = "owner"
        self.assertEqual(self.create().returncode, 0)
        self.assertEqual(json.loads(self.cli("status").stdout)["targets"][0]["phase"], "skipped")
        for method in workflow.METHODS:
            with self.subTest(method=method):
                self.directory = self.base / method
                created = self.cli("create", "--method", method, "--thread-id", "thread", "--repo", REPO,
                                   "--pr", "12", "--review-own-pr", "--registry", str(self.base / "registry.json"),
                                   "--orchestrator", str(self.base / "orchestrator.json"))
                self.assertEqual(created.returncode, 0, created.stderr)
                saved = json.loads((self.directory / "run.json").read_text())
                self.assertIs(saved["initial"]["review_own_pr"], True)
                self.assertEqual(saved["initial"]["intake"]["pull_requests"][0]["classification"], "self-authored-review")
                status = self.cli("status")
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertEqual(json.loads(status.stdout)["targets"][0]["phase"], "intake")

    def test_previous_run_skips_only_after_a_recorded_nonmaterial_discussion_decision(self):
        self.env["WORKFLOW_REVIEWS"] = json.dumps([[{"id": 1, "commit_id": PULL["head_sha"], "state": "APPROVED",
            "user": {"login": "owner"}, "submitted_at": "2026-10-09T10:00:00Z", "body": ""}]])
        created = self.create()
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(json.loads(created.stdout)["targets"][0]["next"]["code"], "discussion_materiality")
        _, run = workflow.load_run(self.directory / "run.json")
        (self.base / "event.json").write_text(json.dumps(event("discussion-decision", materiality(run["targets"][TARGET]["intake"]))))
        recorded = self.cli("record", "--event", str(self.base / "event.json"), "--revision", "0")
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        previous = self.directory
        self.directory = self.base / "next-run"
        repeated = self.create("--previous-run", str(previous))
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout)["targets"][0]["phase"], "skipped")
        self.env["WORKFLOW_BODY"] = "Changed explanation"
        self.directory = self.base / "edited-run"
        edited = self.create("--previous-run", str(previous))
        self.assertEqual(edited.returncode, 0, edited.stderr)
        self.assertEqual(json.loads(edited.stdout)["targets"][0]["phase"], "discussion")

    def test_cli_intake_private_storage_revision_and_failed_write_preserve_record(self):
        created = self.create()
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(json.loads(created.stdout)["revision"], 0)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        record = self.directory / "run.json"
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(self.create().returncode, 0)
        recorded = self.cli("record", "--event", str(self.base / "event.json"), "--revision", "0")
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        before = record.read_bytes()
        stale = self.cli("record", "--event", str(self.base / "event.json"), "--revision", "0")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("stale revision", stale.stderr)
        self.assertEqual(record.read_bytes(), before)
        invalid = self.cli("record", "--event", str(self.base / "event.json"), "--revision", "1")
        self.assertNotEqual(invalid.returncode, 0)
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(json.loads(self.cli("status").stdout)["revision"], 1)

    def test_corrupt_or_symlink_record_is_refused(self):
        self.assertEqual(self.create().returncode, 0)
        record = self.directory / "run.json"
        original = record.read_bytes()
        record.write_text('{"version": 999}')
        self.assertNotEqual(self.cli("status").returncode, 0)
        record.unlink()
        outside = self.base / "outside.json"
        outside.write_bytes(original)
        record.symlink_to(outside)
        result = self.cli("status")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outside.read_bytes(), original)

    def test_competing_processes_cannot_both_mutate_the_same_revision(self):
        self.assertEqual(self.create().returncode, 0)
        command = [sys.executable, str(SCRIPTS / "workflow.py"), "--run", str(self.directory), "record",
                   "--event", str(self.base / "event.json"), "--revision", "0"]
        processes = [subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env) for _ in range(2)]
        outputs = [process.communicate(timeout=10) for process in processes]
        self.assertEqual(sorted(process.returncode for process in processes), [0, 1], outputs)
        self.assertEqual(json.loads(self.cli("status").stdout)["revision"], 1)
        self.assertTrue(any("stale revision" in stderr for _, stderr in outputs))

    def test_failed_atomic_replace_preserves_original_bytes_and_removes_own_temporary(self):
        path = self.base / "record.json"
        workflow.write_json(path, {"original": True})
        before = path.read_bytes()
        with (
            patch.object(workflow.os, "replace", side_effect=OSError("fixture failure")),
            self.assertRaises(OSError),
        ):
            workflow.write_json(path, {"replacement": True})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.base.glob(".review-record-*")), [])

    def test_missing_identity_judgment_returns_needs_decision_without_creating_a_run(self):
        registry = copy.deepcopy(REGISTRY)
        registry["models"] = []
        (self.base / "registry.json").write_text(json.dumps(registry))
        result = self.create()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["code"], "identity_resolution")
        self.assertFalse(self.directory.exists())


class ReportingTests(unittest.TestCase):
    def test_recommendations_require_completed_review_and_use_final_severity(self):
        fixture = WorkflowTests()
        for method in workflow.METHODS:
            for severity, recommendation, derived in ((None, "merge-ready", "APPROVE"),
                    ("nit", "merge-ready (optional nits)", "APPROVE"),
                    ("minor", "merge after fixes", "REQUEST_CHANGES"), ("blocker", "do not merge", "REQUEST_CHANGES")):
                with self.subTest(method=method, severity=severity):
                    run = fixture.complete([raw(severity=severity)] if severity else None, method)
                    before = copy.deepcopy(run)
                    text = workflow.report(run)
                    self.assertIn(f"| complete | {recommendation} | {workflow.report_text(derived)} | not posted |", text)
                    self.assertIn(workflow.report_text("publication_authorization"), text)
                    self.assertIn("Tests not run: static caller trace sufficient", text)
                    self.assertEqual(run, before, "reporting cannot add state or authorize publication")
        incomplete = fixture.discovered()
        self.assertIn("| discovery | review incomplete | not derived | not posted |", workflow.report(incomplete))
        self.assertNotIn("merge-ready", workflow.report(incomplete))
        unsafe = workflow.apply_event(fixture.complete(), event("integrity", {"status": "changed", "components": ["parent"]}))
        self.assertIn("| complete | review held |", workflow.report(unsafe))
        self.assertNotIn("merge-ready", workflow.report(unsafe))

    def test_local_completion_reports_actual_verification_without_github_publication(self):
        local = {"local": {"source_root": "/user/checkout", "comparison": {
            "mode": "range-two-dot", "left": "before", "right": "after", "untracked": []}}}
        run = workflow.new_run("pr-review", local, REGISTRY, MODEL_C, "thread")
        frozen = dict(FROZEN, mode="range-two-dot", diff_base_sha=FROZEN["base_sha"])
        test_root = "/disposable/test-" + "a" * 32
        test_plan = {"commands": ["rtk proxy pytest tests/empty.py"], "root": test_root, "evidence": "authorized focused regression"}
        result = report()
        result["target"] = frozen
        result["verification"] = {"results": [{"command": test_plan["commands"][0], "root": test_root,
            "exit_code": 1, "evidence": "fixture assertion failed before guard"}], "not_run": None}
        for operation, data in (("freeze", frozen), ("catalog", CATALOG), ("plan", plan()),
                                ("authorization", {"submission": False, "test_execution": True, "metered_spawns": 0, "evidence": "explicit fixture test authorization"}),
                                ("test-copy", {"root": test_root}), ("assignment", assignment(test_plan=test_plan)), ("started", start()), ("report", result),
                                ("complete", {"coverage_evidence": "all assigned paths reconciled"})):
            run = workflow.apply_event(run, event(operation, data, "local"))
        text = workflow.report(run)
        self.assertIn("none (local comparison)", text)
        self.assertIn("local comparison has no GitHub PR publication target", text)
        self.assertIn(f"| rtk proxy pytest tests/empty.py | {test_root} | 1 | fixture assertion failed before guard |", text)
        self.assertNotIn("Tests not run:", text)
        self.assertIn("| local | local | complete | merge-ready | APPROVE | not posted |", text)

    def test_finding_ledger_preserves_rejections_gaps_and_shared_root_causes(self):
        fixture = WorkflowTests()
        originals = [dict(raw(f"review-0{number}"), trigger=f"distinct input {number}") for number in range(1, 4)]
        run = fixture.vetted(originals)
        for item in originals:
            run = workflow.apply_event(run, event("adjudication", adjudication(item["id"])))
        self.assertIn("| provisional | empty-input | minor |", workflow.report(run))
        run = workflow.apply_event(run, event("complete", {"coverage_evidence": "shared cause adjudicated; all paths reconciled"}))
        text = workflow.report(run)
        self.assertIn("3 raw IDs; 1 retained adjudicated root causes", text)
        self.assertIn("| F-01 | empty-input | minor |", text)
        for item in originals:
            self.assertIn(item["id"], text)
        rejected = fixture.vetted(verdict="REJECTED")
        rejected = workflow.apply_event(rejected, event("adjudication", dict(adjudication(disposition="REJECTED"),
            reason="caller precondition prevents the trigger", evidence="src/b.py:3 validates before this call")))
        rejected = workflow.apply_event(rejected, event("complete", {"coverage_evidence": "refuted trigger; all paths reconciled"}))
        text = workflow.report(rejected)
        self.assertIn("1 raw IDs; 0 retained adjudicated root causes", text)
        self.assertIn("Adjudication: REJECTED", text)
        self.assertIn("caller precondition prevents the trigger", text)
        pending = fixture.discovered(findings=[raw()])
        pending = workflow.apply_event(pending, event("gap", {"id": "caller-gap", "reason": "caller cannot be inspected", "material": True}))
        text = workflow.report(pending)
        self.assertIn("Vet: UNVETTED", text)
        self.assertIn("Adjudication: UNADJUDICATED", text)
        self.assertIn("caller cannot be inspected", text)
        self.assertNotIn("merge-ready", text)

    def test_batch_stand_in_keeps_actual_adjudicator_and_exact_decision(self):
        fixture = FamilyRoutingTests()
        run = fixture.judge_running()
        reply = fixture.judge_report()
        run = workflow.apply_event(run, event("adjudication-report", reply))
        run = workflow.apply_event(run, event("adjudication", reply["adjudications"][0]))
        run = workflow.apply_event(run, event("complete", {"coverage_evidence": "stand-in and coverage reconciled"}))
        text = workflow.report(run)
        self.assertIn("Orchestrator (recorded): model-a; family-a", text)
        self.assertIn("adjudicator: model-c; family-c; provider-c; lane-c; flat", text)
        self.assertIn("independent source and competing-argument assessment", text)
        self.assertIn("trigger and impact established", text)

    def test_skipped_and_empty_collections_do_not_claim_review_or_publication(self):
        collection = {"owner": "owner", "organization": "fixture", "repositories": [
            {"repository": REPO, "owner": "owner", "pull_requests": [dict(PULL, author="owner", classification="self-authored", review_required=False, discussion=None)]},
            {"repository": "fixture/empty", "owner": "owner", "pull_requests": []}]}
        run = workflow.new_run("pr-review-batch", collection, REGISTRY, MODEL_C, "thread")
        text = workflow.report(run)
        self.assertIn("authenticated owner: owner", text)
        self.assertIn("fixture/empty", text)
        self.assertIn("| self-authored | skipped | not reviewed | not derived | not posted |", text)
        collection["repositories"][0]["pull_requests"] = []
        text = workflow.report(workflow.new_run("pr-review-batch", collection, REGISTRY, MODEL_C, "thread"))
        self.assertIn("No PRs were collected", text)
        self.assertNotIn("merge-ready", text)

    def test_pending_recorded_verified_and_superseded_receipts_remain_distinct(self):
        run = WorkflowTests().complete()
        run = workflow.apply_event(run, event("authorization", {"submission": True, "test_execution": False,
                                  "metered_spawns": 0, "evidence": "user authorized this PR"}))
        context = {"base_sha": FROZEN["base_sha"], "head_sha": FROZEN["head_sha"], "state": "open",
                   "discussion": PULL["discussion"], "review_ids": [], "owner": "owner", "hostname": "github.com", "observed_at": "2026-10-11T00:00:00Z"}
        run = workflow.apply_event(run, event("publication-context", context))
        run = workflow.apply_event(run, event("publication-intent", {"base_sha": FROZEN["base_sha"], "head_sha": FROZEN["head_sha"],
            "state": "open", "body": None, "authorization_evidence": "user authorized this PR", "draft": {"groups": [], "limitations": None}}))
        text = workflow.report(run)
        self.assertIn("pending reconciliation", text)
        self.assertIn("reconcile without another POST", text)
        receipt = {"id": 42, "html_url": "https://github.com/fixture/project/pull/12#pullrequestreview-42",
                   "commit_id": FROZEN["head_sha"], "state": "APPROVED", "body": "", "author": "owner", "evidence": "GitHub readback"}
        recorded = workflow.apply_event(run, event("publication-receipt", receipt))
        self.assertIn(workflow.report_text("receipt_recorded"), workflow.report(recorded))
        self.assertNotIn(workflow.report_text("receipt_verified"), workflow.report(recorded))
        receipt.update(verification="github-rest-readback", submitted_at=context["observed_at"],
                       live={"base_sha": FROZEN["base_sha"], "head_sha": "f" * 40})
        run = workflow.apply_event(run, event("publication-reconciliation", {"review_ids": [42], "matching_ids": [42],
                                   "receipt": receipt, "observed_at": context["observed_at"]}))
        verified = workflow.apply_event(run, event("publication-receipt", receipt))
        text = workflow.report(verified)
        self.assertIn(workflow.report_text("receipt_verified") + " (superseded comparison)", text)
        self.assertIn("does not review that newer comparison", text)
        self.assertIn(f"frozen head: {FROZEN['head_sha']}", text)
        self.assertIn("Body: bodyless", text)
        self.assertIn("pullrequestreview-42", text)

    def test_cli_replays_without_live_checks_or_journal_changes_and_rejects_invalid_events(self):
        run = WorkflowTests().complete()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            initial = {"method": run["method"], "intake": run["intake"], "registry": run["registry"], "orchestrator": MODEL_C,
                       "thread_id": run["thread_id"], "run_id": run["run_id"]}
            path = directory / "run.json"
            saved = {"version": 1, "initial": initial, "events": run["events"]}
            workflow.write_json(path, saved)
            before = path.read_bytes()
            argv = ["workflow.py", "--run", str(directory), "report"]
            with (patch.object(sys, "argv", argv), patch.object(sys, "stdout", io.StringIO()) as output,
                  patch.object(workflow, "check_sources", side_effect=AssertionError("report must not check live sources")),
                  patch.object(workflow.publication, "live_context", side_effect=AssertionError("report must not read GitHub")),
                  patch.object(workflow.publication, "submit", side_effect=AssertionError("report must not publish"))):
                self.assertEqual(workflow.main(), 0)
            self.assertEqual(output.getvalue(), workflow.report(run))
            self.assertEqual(path.read_bytes(), before)
            saved["events"].append(event("unknown-operation", {}))
            workflow.write_json(path, saved)
            with patch.object(sys, "argv", argv), patch.object(sys, "stdout", io.StringIO()) as output, patch.object(sys, "stderr", io.StringIO()) as error:
                self.assertEqual(workflow.main(), 1)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(json.loads(error.getvalue())["status"], "invalid")

    def test_untrusted_prose_cannot_inject_report_tables_html_or_links(self):
        prose = '| APPROVE |\n<script>alert(1)</script> [approve](javascript:alert(1)) `code` *bold*'
        run = WorkflowTests().discovered(findings=[dict(raw(), evidence=prose)])
        text = workflow.report(run)
        self.assertNotIn(prose, text)
        self.assertIn('&#124; APPROVE &#124;<br>&lt;script&gt;', text)
        self.assertIn(r'\[approve\](javascript:alert(1))', text)
        self.assertNotIn('<script>', text)
        self.assertNotIn('[approve](javascript:', text)
        self.assertIn('| discovery | review incomplete |', text)


if __name__ == "__main__":
    unittest.main()
