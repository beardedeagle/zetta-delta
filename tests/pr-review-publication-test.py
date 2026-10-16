#!/usr/bin/env python3
"""Offline publication acceptance: no live models, credentials or GitHub writes."""

import copy
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_fixtures", ROOT / "tests/pr-review-workflow-test.py")
if spec is None or spec.loader is None:
    raise ImportError("review fixtures cannot be loaded")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
workflow = fixture.workflow
publication = workflow.publication
TARGET = fixture.TARGET


def draft(severity="minor", ids=None) -> dict:
    text = "`src/a.py:42`: Empty input raises instead of returning. Handle empty input before the lookup."
    if severity == "nit":
        text = "Nit: " + text
    return {"groups": [{"finding_ids": ids or ["review-01"], "text": text, "trigger": "Empty input",
                        "impact": "raises instead of returning", "action": "Handle empty input before the lookup"}],
            "limitations": "Tests were not run; the caller and guard were traced."}


class GitHub:
    """In-memory REST inventory and a POST recorder inspecting durable intent."""

    def __init__(self, directory, *, response="success"):
        self.directory = directory
        self.response = response
        self.login = "owner"
        self.reviews = []
        self.comments = []
        self.inline = []
        self.reads = []
        self.posts = []
        self.pull: dict = {"number": 12, "html_url": fixture.PULL["url"], "title": "Fixture", "body": "Intent",
                     "state": "open", "base": {"sha": fixture.FROZEN["base_sha"]},
                     "head": {"sha": fixture.FROZEN["head_sha"]}, "user": {"login": "author"}}
        self.advance_after_post = False

    def api(self, endpoint, paginate=False, *, hostname=None):
        self.reads.append((endpoint, paginate, hostname))
        if endpoint == "user":
            return {"login": self.login}
        if "/reviews/" in endpoint:
            key = int(endpoint.rsplit("/", 1)[1])
            return copy.deepcopy(next(row for row in self.reviews if row["id"] == key))
        if endpoint.endswith("/reviews?per_page=100"):
            self.assert_paginated(paginate)
            return copy.deepcopy(self.reviews)
        if "/issues/" in endpoint:
            self.assert_paginated(paginate)
            return copy.deepcopy(self.comments)
        if endpoint.endswith("/comments?per_page=100"):
            self.assert_paginated(paginate)
            return copy.deepcopy(self.inline)
        return copy.deepcopy(self.pull)

    @staticmethod
    def assert_paginated(value):
        if not value:
            raise AssertionError("discussion and reconciliation must read every page")

    def post(self, command, **kwargs):
        saved, run = workflow.load_run(self.directory / "run.json")
        intent = run["targets"][TARGET]["publication"]
        if run["targets"][TARGET]["phase"] != "publication-pending" or saved["events"][-1]["operation"] != "publication-intent":
            raise AssertionError("POST preceded durable intent")
        if command[:4] != ["rtk", "proxy", "gh", "api"] or command[command.index("--method") + 1] != "POST":
            raise AssertionError("unexpected write command")
        filename = Path(command[command.index("--input") + 1])
        if filename.stat().st_mode & 0o777 != 0o600 or filename.parent != self.directory:
            raise AssertionError("payload must stay private and run-owned")
        payload = json.loads(filename.read_text())
        if payload != {"commit_id": intent["commit_id"], "event": intent["event"], **({"body": intent["body"]} if intent["body"] is not None else {})}:
            raise AssertionError("payload differs from the journal")
        self.posts.append((command, payload, kwargs))
        if self.response not in ("absent", "http-error"):
            self.reviews.append({"id": 42, "html_url": self.pull["html_url"] + "#pullrequestreview-42",
                                 "commit_id": payload["commit_id"], "state": "APPROVED" if payload["event"] == "APPROVE" else "CHANGES_REQUESTED",
                                 "body": payload.get("body", ""), "user": {"login": "OwNeR"}, "submitted_at": "2026-10-11T12:00:00Z"})
        if self.advance_after_post:
            self.pull["head"]["sha"] = "f" * 40
        if self.response == "timeout":
            raise subprocess.TimeoutExpired(command, 60)
        if self.response == "lost":
            return SimpleNamespace(returncode=1, stdout="", stderr="response lost")
        if self.response == "malformed":
            return SimpleNamespace(returncode=0, stdout="not JSON", stderr="")
        if self.response == "http-error":
            return SimpleNamespace(returncode=1, stdout='{"secret":"fixture-secret"}',
                                   stderr="gh: Validation failed (HTTP 422)\nAuthorization: Bearer fixture-secret\nhttps://user:password@example.test/?token=private")
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.reviews[-1]) if self.reviews else "{}", stderr="")


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.github = GitHub(self.directory)

    def save(self, findings=None, method="pr-review", authorized=True):
        run = fixture.WorkflowTests().complete(findings or [], method)
        if authorized:
            run = workflow.apply_event(run, fixture.event("authorization", {"submission": True, "test_execution": False,
                "metered_spawns": 0, "evidence": "User explicitly authorized submission for fixture/project#12"}))
        initial = {"method": method, "intake": fixture.COLLECTION, "registry": fixture.REGISTRY,
                   "orchestrator": fixture.MODEL_C, "thread_id": run["thread_id"], "run_id": run["run_id"]}
        saved = {"version": 1, "initial": initial, "events": run["events"]}
        workflow.write_json(self.directory / "run.json", saved)
        return saved, run

    def execute(self, command="publish", body=None):
        saved, run = workflow.load_run(self.directory / "run.json")
        with (patch.object(workflow, "check_sources", side_effect=lambda directory, saved, run, stop=True: run),
              patch.object(publication.intake, "api", side_effect=self.github.api),
              patch.object(publication.subprocess, "run", side_effect=self.github.post)):
            return workflow.publish_command(self.directory, saved, run, TARGET, command,
                                            (body or {"groups": [], "limitations": None}) if command == "publish" else body)

    def test_three_outcomes_submit_only_one_frozen_head_review_and_read_back(self):
        for method in workflow.METHODS:
            for severity, expected in ((None, "APPROVE"), ("nit", "APPROVE"), ("minor", "REQUEST_CHANGES"), ("major", "REQUEST_CHANGES"), ("blocker", "REQUEST_CHANGES")):
                with self.subTest(method=method, severity=severity):
                    self.github = GitHub(self.directory)
                    self.save([fixture.raw(severity=severity)] if severity else [], method)
                    run = self.execute(body=draft(severity) if severity else None)
                    payload = self.github.posts[0][1]
                    self.assertEqual(len(self.github.posts), 1)
                    self.assertEqual(payload["event"], expected)
                    self.assertEqual(payload["commit_id"], fixture.FROZEN["head_sha"])
                    self.assertNotIn("comments", payload)
                    if severity is None:
                        self.assertNotIn("body", payload)
                    elif severity == "nit":
                        self.assertIn("do not block merging", payload["body"])
                    self.assertEqual(workflow.status(run)["targets"][0]["next"]["status"], "receipt_verified")
                    self.assertEqual(workflow.load_run(self.directory / "run.json")[1], run)
                    self.assertFalse(list(self.directory.glob(".review-payload-*")))
                    self.assertTrue(any(path.endswith("/reviews/42") for path, _, _ in self.github.reads))
                    with self.assertRaisesRegex(ValueError, "transition"):
                        self.execute()
                    self.assertEqual(len(self.github.posts), 1)

    def test_lost_malformed_and_timed_out_responses_reconcile_without_second_post(self):
        for response in ("lost", "malformed", "timeout"):
            with self.subTest(response=response):
                self.github = GitHub(self.directory, response=response)
                self.save()
                run = self.execute()
                self.assertEqual(run["targets"][TARGET]["phase"], "published")
                self.assertEqual(len(self.github.posts), 1)
                self.assertFalse(list(self.directory.glob(".review-payload-*")))

    def test_failed_post_keeps_safe_diagnostic_but_never_authorizes_another_post(self):
        self.github.response = "http-error"
        self.save()
        with self.assertRaises(workflow.NeedsDecision):
            self.execute()
        run = workflow.load_run(self.directory / "run.json")[1]
        outcome = run["targets"][TARGET]["publication"]["outcome"]
        self.assertEqual(outcome["diagnostic"], {"http_status": 422, "reason": "validation"})
        self.assertIn(workflow.report_text("http_status"), workflow.report(run))
        self.assertIn("validation", workflow.report(run))
        serialized = (self.directory / "run.json").read_text()
        for secret in ("fixture-secret", "password", "token=private", "Authorization:"):
            self.assertNotIn(secret, serialized)
        with self.assertRaises(ValueError):
            self.execute()
        with self.assertRaises(workflow.NeedsDecision):
            self.execute("reconcile")
        self.assertEqual(len(self.github.posts), 1)
        self.assertFalse(list(self.directory.glob(".review-payload-*")))

    def test_null_live_pr_authors_have_precise_rejection_before_publication(self):
        for author in (None, [], "author"):
            with self.subTest(author=author):
                self.save()
                self.github.pull["user"] = author
                with self.assertRaisesRegex(ValueError, "live PR lacks author metadata"):
                    self.execute()
                self.assertEqual(self.github.posts, [])
                self.assertEqual(workflow.load_run(self.directory / "run.json")[1]["targets"][TARGET]["phase"], "complete")

    def test_caller_contract_prose_is_allowed_while_attribution_is_still_rejected(self):
        _, run = self.save([fixture.raw()])
        target = run["targets"][TARGET]
        for text in ("The value is reviewed by the caller before use.", "The result is generated by the caller."):
            publication.check_wording(text, target)
        for text in ("Reviewed by the caller", "**Generated by Claude**", "The review was reviewed by Alice.",
                     "A result generated by model-a", "Signed-off-by: Human"):
            with self.subTest(text=text), self.assertRaises(workflow.NeedsDecision):
                publication.check_wording(text, target)

    def test_absent_receipt_persists_uncertainty_and_never_reposts(self):
        self.github.response = "absent"
        self.save()
        with self.assertRaisesRegex(workflow.NeedsDecision, "hold without another POST"):
            self.execute()
        run = workflow.load_run(self.directory / "run.json")[1]
        self.assertEqual(run["targets"][TARGET]["phase"], "publication-pending")
        self.assertEqual(len(run["targets"][TARGET]["publication"]["reconciliations"]), 1)
        with self.assertRaisesRegex(ValueError, "transition"):
            self.execute()
        with self.assertRaises(workflow.NeedsDecision):
            self.execute("reconcile")
        self.assertEqual(len(self.github.posts), 1)
        payload = self.github.posts[0][1]
        self.github.reviews = [{"id": 99, "html_url": fixture.PULL["url"] + "#pullrequestreview-99",
                               "commit_id": payload["commit_id"], "state": "APPROVED", "body": "",
                               "user": {"login": "owner"}, "submitted_at": "2026-10-11T12:00:00Z"}]
        run = self.execute("reconcile")
        self.assertEqual(run["targets"][TARGET]["publication"]["receipt"]["id"], 99)
        self.assertEqual(len(self.github.posts), 1)

    def test_post_intent_survives_process_interruption(self):
        self.save()
        with patch.object(publication, "submit", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.execute()
        run = workflow.load_run(self.directory / "run.json")[1]
        self.assertEqual(run["targets"][TARGET]["phase"], "publication-pending")
        self.assertIsNone(run["targets"][TARGET]["publication"]["outcome"])
        with self.assertRaises(workflow.NeedsDecision):
            self.execute("reconcile")
        self.assertFalse(self.github.posts)

    def test_unpersisted_intent_cannot_send_a_review(self):
        self.save()
        write = workflow.write_json

        def refuse_intent(path, data):
            if data["events"][-1]["operation"] == "publication-intent":
                raise OSError("intent persistence failed")
            write(path, data)

        with patch.object(workflow, "write_json", side_effect=refuse_intent), self.assertRaises(OSError):
            self.execute()
        self.assertFalse(self.github.posts)
        self.assertEqual(workflow.load_run(self.directory / "run.json")[1]["targets"][TARGET]["phase"], "complete")

    def test_missing_publication_judgments_return_precise_needs_decision(self):
        _, run = self.save([fixture.raw()])
        for data, code in (({}, "publication_draft"), ({"groups": [{}], "limitations": None}, "publication_evidence")):
            with self.subTest(code=code), self.assertRaises(workflow.NeedsDecision) as raised:
                publication.render_body(run["targets"][TARGET], data)
            self.assertEqual(raised.exception.code, code)

    def test_authorization_account_author_and_comparison_hold_before_post(self):
        self.save(authorized=False)
        with self.assertRaisesRegex(workflow.NeedsDecision, "not authorized"):
            self.execute()
        self.assertFalse(self.github.reads)
        for field, value in (("login", "different"), ("state", "closed"), ("base", {"sha": "f" * 40}),
                             ("head", {"sha": "f" * 40}), ("user", {"login": "owner"})):
            with self.subTest(field=field):
                self.save()
                self.github = GitHub(self.directory)
                if field == "login":
                    assert isinstance(value, str)
                    self.github.login = value
                else:
                    self.github.pull[field] = value
                with self.assertRaises(workflow.NeedsDecision):
                    self.execute()
                self.assertFalse(self.github.posts)

    def test_changes_during_preflight_or_receipt_readback_hold(self):
        self.save()

        def move_during_discussion(endpoint, *args, **kwargs):
            response = self.github.api(endpoint, *args, **kwargs)
            if "/issues/" in endpoint:
                self.github.pull["head"]["sha"] = "f" * 40
            return response

        with patch.object(publication.intake, "api", side_effect=move_during_discussion), self.assertRaises(workflow.NeedsDecision):
            publication.live_context(workflow.load_run(self.directory / "run.json")[1],
                                     workflow.load_run(self.directory / "run.json")[1]["targets"][TARGET])
        self.assertFalse(self.github.posts)
        self.github = GitHub(self.directory)

        def changed_receipt(endpoint, *args, **kwargs):
            response = self.github.api(endpoint, *args, **kwargs)
            return dict(response, body="Changed during readback") if "/reviews/" in endpoint else response

        with (patch.object(workflow, "check_sources", side_effect=lambda directory, saved, run, stop=True: run),
              patch.object(publication.intake, "api", side_effect=changed_receipt),
              patch.object(publication.subprocess, "run", side_effect=self.github.post),
              self.assertRaises(workflow.NeedsDecision)):
            saved, run = workflow.load_run(self.directory / "run.json")
            workflow.publish_command(self.directory, saved, run, TARGET, "publish", {"groups": [], "limitations": None})
        self.assertEqual(workflow.load_run(self.directory / "run.json")[1]["targets"][TARGET]["phase"], "publication-pending")
        run = self.execute("reconcile")
        self.assertEqual(run["targets"][TARGET]["phase"], "published")
        self.assertEqual(len(self.github.posts), 1)

    def test_changed_discussion_requires_precise_materiality_and_reuses_nonmaterial_judgment(self):
        self.save([fixture.raw()])
        self.github.comments = [{"id": 20, "body": "Risk accepted; we decline this fix", "user": {"login": "author"}}]
        with self.assertRaisesRegex(workflow.NeedsDecision, "changed live discussion"):
            self.execute(body=draft())
        saved, run = workflow.load_run(self.directory / "run.json")
        context = run["targets"][TARGET]["publication_context"]
        decision = {"base_sha": context["base_sha"], "head_sha": context["head_sha"],
                    "discussion_sha256": context["discussion"]["sha256"], "changes": [],
                    "evidence": "Risk acceptance and rejecting the fix do not refute the demonstrated bug"}
        with self.assertRaisesRegex(ValueError, "bind the captured"):
            workflow.apply_event(run, fixture.event("publication-discussion", dict(decision, discussion_sha256="f" * 64)))
        workflow.append_record(self.directory, saved, run, TARGET, "publication-discussion", decision)
        run = self.execute(body=draft())
        self.assertEqual(run["targets"][TARGET]["publication"]["event"], "REQUEST_CHANGES")
        self.assertEqual(len(self.github.posts), 1)

    def test_material_discussion_holds_and_new_snapshot_invalidates_old_decision(self):
        self.save([fixture.raw()])
        self.github.pull["body"] = "New contract explains why empty input is impossible"
        self.execute("preflight")
        saved, run = workflow.load_run(self.directory / "run.json")
        context = run["targets"][TARGET]["publication_context"]
        decision = {"base_sha": context["base_sha"], "head_sha": context["head_sha"], "discussion_sha256": context["discussion"]["sha256"],
                    "changes": [{"effect": "remove", "finding": "Empty input cannot reach this caller", "before": "minor", "after": None,
                                 "sources": ["description"], "evidence": "The documented caller contract changes the evaluated trigger"}],
                    "evidence": "Concrete trigger is refuted"}
        workflow.append_record(self.directory, saved, run, TARGET, "publication-discussion", decision)
        with self.assertRaisesRegex(workflow.NeedsDecision, "materially changes"):
            self.execute(body=draft())
        self.github.pull["body"] = "Another contract update"
        self.execute("preflight")
        run = workflow.load_run(self.directory / "run.json")[1]
        self.assertIsNone(run["targets"][TARGET]["publication_discussion"])
        self.assertFalse(self.github.posts)

    def test_concurrent_owner_review_and_pending_review_hold_without_overwriting(self):
        for state in ("APPROVED", "PENDING"):
            with self.subTest(state=state):
                self.save()
                self.github = GitHub(self.directory)
                self.github.reviews = [{"id": 30, "body": "", "user": {"login": "owner"}, "state": state,
                                       "commit_id": fixture.FROZEN["head_sha"], "submitted_at": "2026-10-11T11:00:00Z" if state != "PENDING" else None}]
                with self.assertRaises(workflow.NeedsDecision):
                    self.execute()
                self.assertFalse(self.github.posts)

    def test_push_after_post_reports_superseded_frozen_head(self):
        self.save()
        self.github.advance_after_post = True
        run = self.execute()
        status = workflow.status(run)["targets"][0]["next"]
        self.assertTrue(status["superseded"])
        self.assertEqual(status["receipt"]["commit_id"], fixture.FROZEN["head_sha"])
        self.assertEqual(len(self.github.posts), 1)

    def test_verified_review_becomes_discussion_baseline_but_later_comments_do_not(self):
        self.save()
        run = self.execute()
        baseline = workflow.discussion_baselines(run)[TARGET]
        actual = workflow.intake.discussion_record(self.github.pull, self.github.reviews, [], [])
        self.assertEqual(baseline["discussion"], actual)
        self.assertEqual(baseline["owner_review_id"], 42)
        later = workflow.intake.discussion_record(self.github.pull, self.github.reviews,
            [{"id": 21, "body": "New explanation after submission", "user": {"login": "author"}}], [])
        self.assertNotEqual(baseline["discussion"], later)

    def test_body_covers_exactly_retained_roots_and_excludes_unpublished_claims(self):
        _, run = self.save([fixture.raw(), dict(fixture.raw("review-02"), trigger="a distinct empty input caller")])
        target = run["targets"][TARGET]
        body = publication.render_body(target, draft(ids=["review-01", "review-02"]))
        self.assertEqual(body.count("\n- "), 1)
        for bad in (draft(), draft(ids=["review-01", "unknown"]),
                    {"groups": [draft()["groups"][0], draft(ids=["review-02"])["groups"][0]], "limitations": None}):
            with self.subTest(bad=bad), self.assertRaises((ValueError, workflow.NeedsDecision)):
                publication.render_body(target, bad)
        for field, value in (("text", "No evidence"), ("trigger", "not in the prose"), ("text", draft()["groups"][0]["text"] + "\n- extra claim")):
            bad = draft(ids=["review-01", "review-02"])
            bad["groups"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                publication.render_body(target, bad)
        rejected = copy.deepcopy(target)
        rejected["adjudications"]["review-02"]["disposition"] = "REJECTED"
        with self.assertRaises(ValueError):
            publication.render_body(rejected, draft(ids=["review-01", "review-02"]))

    def test_wording_checks_preserve_clean_user_body_without_claiming_tone(self):
        _, run = self.save()
        target = run["targets"][TARGET]
        self.assertIsNone(publication.render_body(target, {"groups": [], "limitations": None}))
        clean = {"groups": [], "limitations": None, "clean_body": "Approved, as requested.", "user_body_evidence": "User supplied this exact wording"}
        self.assertEqual(publication.render_body(target, clean), "Approved, as requested.")
        publication.check_wording("`f(k)` reaches `orchestrator` only when the index is zero.", target)
        for text in ("fuck", "f**k", "sh*t", "Generated by an agent", "Reviewed by GPT"):
            with self.subTest(text=text), self.assertRaises(workflow.NeedsDecision):
                publication.render_body(target, dict(clean, clean_body=text))
        with self.assertRaises(ValueError):
            publication.render_body(target, dict(clean, user_body_evidence=None))

    def test_wording_allows_recorded_citations_and_generic_identity_words_but_rejects_metadata(self):
        _, run = self.save([fixture.raw()])
        target = run["targets"][TARGET]
        claim = target["findings"].pop("review-01")
        final = target["adjudications"].pop("review-01")
        claim["finder"]["model_id"] = "default"
        claim.update(id="a-01", path="src/a-01.py")
        reference = dict(claim["references"][0], path="src/a-01.py")
        claim["references"] = final["references"] = [reference]
        target["findings"]["a-01"] = claim
        target["adjudications"]["a-01"] = final
        body = "`src/a-01.py:42`: The default value raises for empty input. Handle empty input."
        draft_body = {"groups": [{"finding_ids": ["a-01"], "text": body, "trigger": "empty input",
                                 "impact": "raises", "action": "Handle empty input"}], "limitations": None}
        self.assertIn(body, publication.render_body(target, draft_body))
        for metadata in ("Finding ID: a-01", "`a-01`", "Model: default", "provider is provider-a", "Reviewed by model-a"):
            with self.subTest(metadata=metadata), self.assertRaises(workflow.NeedsDecision):
                publication.check_wording(body + " " + metadata, target)

    def test_incomplete_or_stale_adjudication_and_source_failures_hold(self):
        saved, run = self.save([fixture.raw()])
        run["targets"][TARGET]["adjudications"]["review-01"]["vet_assignment_id"] = "stale"
        with self.assertRaisesRegex(workflow.NeedsDecision, "stale adjudication"):
            workflow.publish_command(self.directory, saved, run, TARGET, "publish", draft())
        saved, run = self.save()
        with (patch.object(workflow, "check_sources", side_effect=workflow.NeedsDecision("source_integrity", "changed source")),
              self.assertRaises(workflow.NeedsDecision)):
            workflow.publish_command(self.directory, saved, run, TARGET, "publish", {"groups": [], "limitations": None})
        self.assertFalse(self.github.posts)

    def test_reconciliation_rejects_ambiguous_mismatched_and_old_receipts(self):
        self.github.response = "absent"
        self.save()
        with self.assertRaises(workflow.NeedsDecision):
            self.execute()
        good = {"id": 42, "html_url": fixture.PULL["url"] + "#pullrequestreview-42", "commit_id": fixture.FROZEN["head_sha"],
                "state": "APPROVED", "body": "", "user": {"login": "owner"}, "submitted_at": "2026-10-11T12:00:00Z"}
        for field, value in (("body", "different"), ("state", "COMMENTED"), ("commit_id", "f" * 40), ("user", {"login": "other"})):
            self.github.reviews = [dict(good, **{field: value})]
            with self.subTest(field=field), self.assertRaises(workflow.NeedsDecision):
                self.execute("reconcile")
        self.github.reviews = [good, dict(good, id=43, html_url=fixture.PULL["url"] + "#pullrequestreview-43")]
        with self.assertRaises(workflow.NeedsDecision) as raised:
            self.execute("reconcile")
        self.assertEqual(raised.exception.code, "publication_ambiguous")
        self.assertEqual(len(self.github.posts), 1)
        _, run = workflow.load_run(self.directory / "run.json")
        target = copy.deepcopy(run["targets"][TARGET])
        target["publication"]["prior_review_ids"] = [42, 43]
        with patch.object(publication.intake, "api", side_effect=self.github.api):
            self.assertIsNone(publication.reconcile(run, target)["receipt"])

    def test_cli_rejects_forged_receipts_and_stale_publish_writers(self):
        _, run = self.save()
        forged = self.directory / "forged.json"
        forged.write_text(json.dumps(fixture.event("publication-receipt", {})))
        executable = ["workflow.py", "--run", str(self.directory)]
        for args in (["record", "--event", str(forged), "--revision", str(run["revision"])],
                     ["publish", "--target", TARGET, "--draft", str(forged), "--revision", str(run["revision"] - 1)]):
            with (self.subTest(args=args), patch.object(sys, "argv", executable + args),
                  patch.object(sys, "stderr", io.StringIO()) as stderr):
                self.assertEqual(workflow.main(), 1)
                self.assertTrue("dedicated command" in stderr.getvalue() or "stale revision" in stderr.getvalue())
        self.assertFalse(self.github.posts)

    def test_exact_review_approval_cannot_authorize_changed_wording(self):
        saved, run = self.save()
        authorization = dict(run["targets"][TARGET]["authorization"], approved_review={"event": "APPROVE", "body": "Exact approved text."})
        workflow.append_record(self.directory, saved, run, TARGET, "authorization", authorization)
        with self.assertRaisesRegex(workflow.NeedsDecision, "exact user-approved"):
            self.execute()
        self.assertFalse(self.github.posts)
        run = self.execute(body={"groups": [], "limitations": None, "clean_body": "Exact approved text.",
                                 "user_body_evidence": "User approved this exact body"})
        self.assertEqual(run["targets"][TARGET]["publication"]["body"], "Exact approved text.")

    def test_cli_preflight_previews_exact_review_before_post_authorization(self):
        _, run = self.save([fixture.raw()], authorized=False)
        prose = draft()
        path = self.directory / "draft.json"
        path.write_text(json.dumps(prose))
        args = ["workflow.py", "--run", str(self.directory), "preflight", "--target", TARGET,
                "--draft", str(path), "--revision", str(run["revision"])]
        with (patch.object(sys, "argv", args), patch.object(sys, "stdout", io.StringIO()) as output,
              patch.object(workflow, "check_sources", side_effect=lambda directory, saved, run, stop=True: run),
              patch.object(publication.intake, "api", side_effect=self.github.api),
              patch.object(publication.subprocess, "run", side_effect=AssertionError("preview cannot POST"))):
            self.assertEqual(workflow.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["review"], {"event": "REQUEST_CHANGES",
                         "body": publication.render_body(run["targets"][TARGET], prose)})
        self.assertFalse(self.github.posts)

    def test_host_binding_and_paginated_helper_do_not_rely_on_ambient_gh_host(self):
        _, run = self.save()
        target = copy.deepcopy(run["targets"][TARGET])
        target["intake"]["url"] = "https://github.example.test/fixture/project/pull/12"
        self.github.pull["html_url"] = target["intake"]["url"]
        with patch.object(publication.intake, "api", side_effect=self.github.api):
            self.assertEqual(publication.live_context(run, target)["hostname"], "github.example.test")
        self.assertTrue(all(host == "github.example.test" for _, _, host in self.github.reads))
        with patch.object(workflow.intake.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="[[{}], [{}]]")) as command:
            self.assertEqual(workflow.intake.api("repos/a/b/pulls/1/reviews", True, hostname="github.example.test"), [{}, {}])
        self.assertIn("--slurp", command.call_args.args[0])
        self.assertIn("github.example.test", command.call_args.args[0])
        target["repository"] = "Fixture/Project"
        self.assertEqual(publication.endpoint(target)[1], "repos/Fixture/Project/pulls/12")


if __name__ == "__main__":
    unittest.main()
