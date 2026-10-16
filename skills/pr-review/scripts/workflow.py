#!/usr/bin/env python3
"""Shared review journal, source preparation and authorized GitHub publication."""

import argparse
import copy
import fcntl
import hashlib
import html
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import handoff
import intake
import preparation
import publication
import review_contract as contract
from review_contract import (
    SEVERITIES,
    NeedsDecision,
    choice,
    evidence,
    fields,
    finding,
    identity,
    model_record,
    paths,
    stable_id,
    strings,
)

METHODS = ("pr-review", "pr-review-batch")
ADMISSION_MODES = ("observed", "native")
CATEGORIES = contract.CATEGORIES


def target_record(repository: str, metadata: dict) -> dict:
    """Initialize one independent target's record.

    Args:
        repository (str): Verified slug or explicit local checkout root.
        metadata (dict): Original intake/local metadata.

    Returns:
        dict: Empty review state preserving eligibility and original metadata.
    """
    return {"intake": copy.deepcopy(metadata), "repository": repository,
            "phase": "discussion" if metadata.get("discussion_decision_required") else
                     "intake" if metadata.get("review_required", True) else "skipped",
            "frozen": None, "plan": None, "assignments": {}, "findings": {}, "vets": {},
            "adjudications": {}, "coverage_gaps": {}, "authorization": None, "publication": None,
            "vet_history": {}, "adjudication_history": {},
            "baselines": {}, "integrity": [], "test_copies": [], "resources_removed": False,
            "setup": None, "index_setup": None, "catalog": None, "discussion_decision": None,
            "publication_context": None, "publication_discussion": None}


def run_record(method: str, collection: dict, registry: dict, orchestrator: dict,
               thread_id: str, owner: str | None, targets: dict, admission_mode: str | None = None) -> dict:
    """Create the common run envelope after method-specific intake validation.

    Args:
        method (str): Review method.
        collection (dict): Original comparison/intake evidence.
        registry (dict): Sanitized identity registry.
        orchestrator (dict): Effective identity request.
        thread_id (str): Parent thread ID.
        owner (str | None): Invoking GitHub account, absent for a local comparison.
        targets (dict): Validated target records.
        admission_mode (str | None): Explicit count preflight or native admission;
            absent only for legacy journals.

    Returns:
        dict: Versioned initial run state.

    Raises:
        ValueError: Identity or thread metadata is invalid.
        NeedsDecision: Model identity needs a decision.
    """
    result = {"version": 1, "run_id": "run-" + uuid.uuid4().hex, "method": method,
            "thread_id": identity.text(thread_id, "thread ID"), "owner": owner,
            "registry": copy.deepcopy(registry), "orchestrator": model_record(orchestrator, registry),
            "intake": copy.deepcopy(collection), "targets": targets, "batch_waves": [], "unavailable_lanes": {}, "revision": 0, "events": []}
    if admission_mode is not None:
        result.update(admission_mode=admission_mode, native_parent=None)
    return result


def new_run(method: str, collection: dict, registry: dict, orchestrator: dict, thread_id: str,
            *, review_own_pr: bool = False, admission_mode: str | None = None) -> dict:
    """Validate intake or an explicit local comparison and initialize its journal.

    Args:
        method (str): Single or batch review method.
        collection (dict): Shared intake output or explicit local comparison.
        registry (dict): Sanitized effective identity registry.
        orchestrator (dict): Exact model/provider identity request.
        thread_id (str): Actual parent thread ID.
        review_own_pr (bool): Explicit self-authored, review-only intake override.
        admission_mode (str | None): Immutable admission choice; None replays
            journals created before native parent capture was introduced.

    Returns:
        dict: New run with separate state for every target.

    Raises:
        ValueError: Intake, comparison, eligibility or method is invalid.
        NeedsDecision: Effective identity cannot be resolved.
    """
    choice(method, METHODS, "review method")
    if admission_mode is not None:
        choice(admission_mode, ADMISSION_MODES, "admission mode")
    if type(review_own_pr) is not bool:
        raise ValueError("review-own-pr override must be boolean")
    if "local" in collection:
        if review_own_pr:
            raise ValueError("review-own-pr requires explicit GitHub PR intake")
        fields(collection, ("local",))
        if method != "pr-review":
            raise ValueError("local comparisons use pr-review")
        local = fields(collection["local"], ("source_root", "comparison"))
        root = Path(local["source_root"])
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("local source must be an absolute canonical root")
        request = fields(local["comparison"], ("mode", "left", "right", "untracked"))
        choice(request["mode"], ("range-two-dot", "range-three-dot", "branch-merge-base", "working"), "comparison mode")
        identity.text(request["right"], "comparison target")
        if request["mode"] in ("range-two-dot", "range-three-dot") or request["left"] is not None:
            identity.text(request["left"], "comparison base")
        paths(request["untracked"], empty=True)
        if request["mode"] != "working" and request["untracked"]:
            raise ValueError("untracked scope requires a working comparison")
        if request["mode"] == "working" and (request["left"] is not None or request["right"] != "HEAD"):
            raise ValueError("working changes compare against baseline HEAD")
        target = target_record(str(root), {"classification": "local"})
        target["comparison"] = copy.deepcopy(request)
        return run_record(method, collection, registry, orchestrator, thread_id, None, {"local": target}, admission_mode)
    owner = intake.github_login(collection.get("owner"), "authenticated owner")
    groups = collection.get("repositories", [collection])
    if not isinstance(groups, list):
        raise ValueError("intake repository groups must be a list")  # noqa: TRY004 - validation errors retain the ValueError contract.
    targets = {}
    repositories = []
    for group in groups:
        fields(group, ("repository", "owner", "pull_requests"))
        repo = identity.text(group["repository"], "repository")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo) or repo.split("/")[1] in (".", ".."):
            raise ValueError("invalid intake repository")
        if repo.casefold() in repositories or group["owner"].casefold() != owner.casefold():
            raise ValueError("duplicate repository or mismatched authenticated owner")
        repositories.append(repo.casefold())
        if not isinstance(group["pull_requests"], list):
            raise ValueError("intake pull_requests must be a list")  # noqa: TRY004 - validation errors retain the ValueError contract.
        for pull in group["pull_requests"]:
            number = intake.positive(pull.get("number"), "PR number")
            key = f"{repo}#{number}"
            if key in targets:
                raise ValueError("duplicate intake target")
            author = intake.github_login(pull.get("author"), "PR author")
            classification = choice(pull.get("classification"), ("new", "changed", "unchanged", "self-authored", "self-authored-review"), "classification")
            if classification == "self-authored-review" and not review_own_pr:
                raise ValueError("self-authored review requires an explicit recorded override")
            if ((author.casefold() == owner.casefold()) != (classification in ("self-authored", "self-authored-review"))
                    or type(pull.get("review_required")) is not bool
                    or pull["review_required"] != (classification in ("new", "changed", "self-authored-review"))):
                raise ValueError("inconsistent intake eligibility")
            validate_intake_discussion(pull, classification)
            targets[key] = target_record(repo, pull)
            for field in ("base_sha", "head_sha"):
                targets[key]["intake"][field] = intake.sha(pull.get(field), field)
    if method == "pr-review" and len(targets) != 1:
        raise ValueError("pr-review requires one PR; use pr-review-batch for collections")
    return run_record(method, collection, registry, orchestrator, thread_id, owner, targets, admission_mode)


def validate_intake_discussion(pull: dict, classification: str) -> None:
    """Prevent stale or missing discussion evidence from bypassing the intake gate.

    Args:
        pull (dict): Shared intake entry.
        classification (str): Validated head-based eligibility.

    Raises:
        ValueError: Discussion evidence or its required decision is inconsistent.
    """
    if classification == "self-authored":
        if pull.get("discussion") is not None or pull.get("discussion_decision_required", False):
            raise ValueError("self-authored PRs cannot enter discussion evaluation")
        return
    intake.validate_discussion(pull.get("discussion"))
    baseline = pull.get("discussion_baseline")
    if baseline is not None:
        intake.validate_discussion(baseline)
    expected = classification == "unchanged" and (baseline is None or baseline != pull["discussion"])
    if type(pull.get("discussion_decision_required")) is not bool or pull["discussion_decision_required"] != expected:
        raise ValueError("inconsistent discussion eligibility")


def discussion_baselines(run: dict) -> dict:
    """Reuse evaluated discussion and preserve prior finding provenance privately.

    Args:
        run (dict): Replayed previous run, never an unvalidated JSON envelope.

    Returns:
        dict: Per-target snapshots bound to the invoking owner and prior review.
            Incomplete evaluations cannot become skip baselines.
    """
    baselines = {}
    for key, target in run["targets"].items():
        metadata = target["intake"]
        if not metadata.get("discussion"):
            continue
        completed = target["phase"] in ("complete", "publication-pending", "published")
        assessed = target["phase"] == "published" or (target["phase"] == "skipped" and not metadata["discussion_decision_required"])
        receipt = target["publication"]["receipt"] if target["publication"] else None
        discussion = metadata["discussion"] if assessed else metadata.get("discussion_baseline")
        if receipt and receipt.get("verification") == "github-rest-readback":
            snapshot = target["publication"]["context"]["discussion"]["snapshot"]
            review = {field: receipt[field] for field in ("id", "html_url", "commit_id", "state", "body", "submitted_at")}
            review["user"] = {"login": receipt["author"]}
            discussion = intake.discussion_record(snapshot["description"], [*snapshot["reviews"], review],
                                                  snapshot["comments"], snapshot["review_comments"])
        prior = metadata["owner_review"]
        history = metadata.get("prior_review")
        if completed:
            history = {"run_id": run["run_id"], "head_sha": metadata["head_sha"],
                       "findings": copy.deepcopy(target["findings"]), "vets": copy.deepcopy(target["vets"]),
                       "adjudications": copy.deepcopy(target["adjudications"]), "previous": history}
        baselines[key] = {"owner": run["owner"], "base_sha": metadata["base_sha"], "head_sha": metadata["head_sha"],
                          "owner_review_id": receipt["id"] if receipt else prior["id"] if prior else None,
                          "discussion": copy.deepcopy(discussion),
                          "prior_review": history}
    return baselines


def discussion_decision(data: dict, target: dict) -> None:
    """Validate a model's materiality judgment without inferring it from PR text.

    Args:
        data (dict): Snapshot-bound judgment and concrete finding effects.
        target (dict): PR waiting for a discussion decision.

    Raises:
        NeedsDecision: Judgment or effect evidence is missing.
        ValueError: Binding, source IDs or severity transitions are invalid.
    """
    contract.decisions(data, ("base_sha", "head_sha", "discussion_sha256", "changes", "evidence"), "discussion_materiality")
    metadata = target["intake"]
    if (any(data[field] != metadata[field] for field in ("base_sha", "head_sha"))
            or data["discussion_sha256"] != metadata["discussion"]["sha256"]):
        raise ValueError("discussion decision must bind the captured comparison and snapshot")
    evidence(data["evidence"])
    if not isinstance(data["changes"], list):
        raise ValueError("discussion changes must be a list")  # noqa: TRY004 - validation errors retain the ValueError contract.
    snapshot = metadata["discussion"]["snapshot"]
    sources = {"description"} | {f"{kind}:{row['id']}" for kind in ("reviews", "comments", "review_comments") for row in snapshot[kind]}
    baseline = metadata.get("discussion_baseline")
    if baseline is not None:
        sources |= {"baseline:description"} | {f"baseline:{kind}:{row['id']}" for kind in
                    ("reviews", "comments", "review_comments") for row in baseline["snapshot"][kind]}
    for change in data["changes"]:
        contract.decisions(change, ("effect", "finding", "before", "after", "sources", "evidence"),
                           "discussion_materiality", nullable=("before", "after"))
        effect = choice(change["effect"], ("add", "remove", "severity"), "discussion effect")
        evidence(change["finding"])
        evidence(change["evidence"])
        if not set(strings(change["sources"], "discussion source")) <= sources:
            raise ValueError("discussion effect references an uncaptured source")
        before, after = change["before"], change["after"]
        for severity in (before, after):
            if severity is not None:
                choice(severity, SEVERITIES, "discussion severity")
        if ((effect == "add" and (before is not None or after is None))
                or (effect == "remove" and (before is None or after is not None))
                or (effect == "severity" and (before is None or after is None or before == after))):
            raise ValueError("discussion effect must add, remove or change the severity of a finding")


def require_phase(target, *phases):
    if target["phase"] not in phases:
        raise ValueError(f"invalid transition from {target['phase']}; expected {' or '.join(phases)}")


def integrity_failures(run: dict) -> tuple[list[str], list[str]]:
    """Collect recorded source failures for transition guards and status reporting.

    Args:
        run (dict): Validated run state with retained integrity observations/reports.

    Returns:
        tuple[list[str], list[str]]: Target IDs with unsafe observations and
            assignment IDs with unsafe accepted or rejected reports, in record order.
    """
    targets = [key for key, target in run["targets"].items()
               if any(check["status"] != "unchanged" for check in target["integrity"])]
    assignments = [key for target in run["targets"].values() for key, row in target["assignments"].items()
                   if (row["report"] and row["report"]["source_status"] != "unchanged")
                   or row.get("rejected_report", {}).get("source_status", "unchanged") != "unchanged"]
    return targets, assignments


def pending_launches(run: dict) -> list[tuple[str, str]]:
    """Find issued launches that have not completed native registration.

    Args:
        run (dict): Validated replayed state.

    Returns:
        list[tuple[str, str]]: Target and assignment IDs awaiting settlement.
    """
    return [(key, row["id"]) for key, target in run["targets"].items()
            for row in target["assignments"].values() if row["status"] == "queued" and row.get("task_ready")]


def require_registered_launch(run: dict, settling: tuple[str, str] | None = None) -> None:
    """Hold further commands until an issued native launch is settled.

    Args:
        run (dict): Current replayed state, including retained integrity failures.
        settling (tuple[str, str] | None): Exact started or verified withdrawal
            request; its ordinary validators and source checks still apply.

    Raises:
        NeedsDecision: A retained integrity failure or pending registration holds
            unrelated commands before they can scan sources or change the journal.
    """
    pending = pending_launches(run)
    if not pending or settling in pending:
        return
    if any(integrity_failures(run)):
        raise NeedsDecision("source_integrity", "preserve the recorded integrity failure; pending registration cannot clear it")
    raise NeedsDecision("runtime_registration", "settle the issued native launch with started or verified withdrawal before another task or fresh scan: "
                        + ", ".join(f"{key}/{assignment}" for key, assignment in pending))


def discovery_gaps(target: dict) -> list[str]:
    """Reconcile planned discovery independently of finding-processing stages.

    Args:
        target (dict): Frozen comparison, review plan and recorded assignments.

    Returns:
        list[str]: Missing discovery units, paths, families and interactions.
    """
    gaps = contract.coverage_gaps(target)
    discoveries = [row for row in target["assignments"].values() if row["kind"] == "discovery"]
    if not discoveries:
        gaps.append("missing discovery assignments")
    completed = [row for row in discoveries if row["status"] == "done"]
    if target["frozen"] and not set(target["frozen"]["inventory"]) <= {path for row in completed for path in row["scope"]}:
        gaps.append("uncovered changed paths")
    if target["plan"]:
        for family in target["plan"]["required_perspectives"]:
            if family not in {row["actual_identity"]["family"] for row in completed if row["required"] and row["role"] != "scout"}:
                gaps.append(f"missing required perspective: {family}")
    else:
        gaps.append("missing review plan")
    return gaps


def completion_gaps(run, target):
    gaps = discovery_gaps(target)
    for key, row in target["assignments"].items():
        latest = row
        while latest.get("superseded_by"):
            latest = target["assignments"][latest["superseded_by"]]
        if row["status"] in ("queued", "running") or ((row["required"] or row.get("rejected_report")) and latest["status"] != "done"):
            gaps.append(f"assignment {key}: {row['status']}")
        if row["report"] and row["report"]["source_status"] != "unchanged":
            gaps.append(f"assignment {key}: source integrity {row['report']['source_status']}")
        if row.get("rejected_report", {}).get("source_status", "unchanged") != "unchanged":
            gaps.append(f"assignment {key}: rejected report source integrity")
    for key, row in target["findings"].items():
        if not target["vets"].get(key):
            gaps.append(f"finding {key}: UNVETTED")
        adjudication = target["adjudications"].get(key)
        if adjudication is None:
            gaps.append(f"finding {key}: UNADJUDICATED")
        elif adjudication["disposition"] in ("UNVERIFIABLE", "UNVETTED", "DISPUTED"):
            gaps.append(f"finding {key}: {adjudication['disposition']}")
        elif target["vets"].get(key) and adjudication.get("vet_assignment_id") != target["vets"][key]["assignment_id"]:
            gaps.append(f"finding {key}: stale adjudication")
    gaps.extend(f"coverage {key}: {row['reason']}" for key, row in target["coverage_gaps"].items()
                if row["material"] and row.get("resolution") is None)
    return gaps


def review_event(target):
    retained = [row for row in target["adjudications"].values()
                if row["disposition"] in ("CONFIRMED", "ADJUSTED")]
    return "REQUEST_CHANGES" if any(row["severity"] != "nit" for row in retained) else "APPROVE"


def retain_findings(target: dict, row: dict, originals: object, *, errors: list[dict] | None = None) -> None:
    """Retain raw IDs and flag same-model restatements without merging judgments.

    Args:
        target (dict): Target whose findings are updated after full validation.
        row (dict): Reporting assignment and actual participant identity.
        originals (object): Untrusted originals or unchanged recovery
            corroborations; must be a list of finding objects.
        errors (list[dict] | None): Draft diagnostics, or None for strict failure.

    Raises:
        NeedsDecision: A restatement needs its existing ID or focused evidence path.
        ValueError: IDs, provenance or immutable originals are changed.
    """
    if not isinstance(originals, list):
        raise ValueError("report findings must be a list")  # noqa: TRY004 - retain the validation error contract.
    seen = set()
    prefixes = [row["id"]]
    ancestor = row
    while ancestor.get("recovery"):
        ancestor = target["assignments"][ancestor["recovery"]["assignment_id"]]
        prefixes.append(ancestor["id"])
    for index, raw in enumerate(originals):
        before = len(errors) if errors is not None else 0
        finding(raw, errors=errors, field=f"findings/{index}")
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        with contract.report_check(errors, f"findings/{index}"):
            if raw["id"] in seen:
                raise ValueError("duplicate finding ID in report")
        seen.add(raw["id"])
        existing = target["findings"].get(raw["id"])
        if existing is not None:
            with contract.report_check(errors, f"findings/{index}"):
                if raw != existing or any(raw["finder"][field] != row["actual_identity"][field] for field in ("model_id", "family")):
                    raise ValueError("finding IDs are immutable and unique; retain the original and record new evidence in the focused dispute")
            continue
        with contract.report_check(errors, f"findings/{index}/id"):
            if not any(re.fullmatch(re.escape(prefix) + r"-[0-9]{2,}", raw["id"]) for prefix in prefixes):
                raise ValueError("finding ID must be <assignment-id>-NN")
        with contract.report_check(errors, f"findings/{index}/finder"):
            if raw.get("finder") != row["actual_identity"]:
                raise ValueError("finding finder must match recorded runtime identity")
        if raw.get("finder") == row["actual_identity"]:
            repeated = [key for key, prior in target["findings"].items()
                        if all(prior[field] == raw.get(field) for field in ("root_cause", "trigger"))
                        and all(prior["finder"][field] == raw["finder"][field] for field in ("model_id", "family"))]
            with contract.report_check(errors, f"findings/{index}"):
                if repeated:
                    raise NeedsDecision("finding_restatement", "corroborate existing raw IDs " + ", ".join(repeated) + "; new evidence uses the bounded focused dispute")
        if errors is None or len(errors) == before:
            target["findings"][raw["id"]] = copy.deepcopy(raw)
    with contract.report_check(errors, "findings"):
        if sum(isinstance(raw, dict) and raw.get("severity") == "nit" for raw in originals) > 5:
            raise ValueError("reports contain at most five optional nits")


def readjudication_allowed(target: dict, key: str) -> bool:
    """Allow first vetted judgment or one focused reconsideration, retaining history.

    Args:
        target (dict): Recorded assignments, vet verdicts and decision history.
        key (str): Original finding ID.

    Returns:
        bool: Whether the designated adjudicator may assess the new evidence once.
    """
    prior = target["adjudications"].get(key)
    vet = target["vets"].get(key)
    if not prior or not vet:
        return False
    if prior["disposition"] == "UNVETTED":
        return prior.get("vet_assignment_id") is None
    if prior["disposition"] not in ("DISPUTED", "UNVERIFIABLE") or any(
            row["disposition"] != "UNVETTED" for row in target["adjudication_history"].get(key, [])):
        return False
    row = target["assignments"][vet["assignment_id"]]
    root = contract.unit_root(target, row)
    return root.get("recovery", {}).get("kind") == "follow-up" and vet["assignment_id"] != prior.get("vet_assignment_id")


def accept_report(target: dict, row: dict, data: dict, *, errors: list[dict] | None = None) -> None:
    """Retain a validated terminal report and register its completed vet judgments.

    Args:
        target (dict): Private candidate state; discard it on validation failure.
        row (dict): Assignment bound to the exact runtime handle and identity.
        data (dict): Original or corrected discovery/vet/stand-in report.
        errors (list[dict] | None): Collect draft diagnostics without accepting;
            None preserves strict receipt validation and state registration.

    Raises:
        NeedsDecision: Coverage, identity or analytical evidence is missing.
        ValueError: Binding, provenance, scope or report fields are invalid.
    """
    if errors is not None:
        target = copy.deepcopy(target)
    with contract.report_check(errors, "report"):
        if row["kind"] == "adjudication":
            contract.decisions(data, ("assignment_id", "status", "target", "reviewer", "adjudications", "evidence", "source_status"), "adjudication_report")
        else:
            fields(data, ("assignment_id", "status", "target", "checked", "findings", "evidence", "source_status"), ("reviewer", "checked_units", "checked_interactions", "categories", "verification", "coverage_gaps", "pre_existing", "learned", "verdicts"))
    if not isinstance(data, dict):
        return
    if row["kind"] == "adjudication":
        with contract.report_check(errors, "reviewer"):
            if data.get("reviewer") != row["actual_identity"]:
                raise ValueError("adjudication report target/identity differs from its assignment")
        with contract.report_check(errors, "adjudications"):
            if row["vet_bindings"] != {raw_id: target["vets"][raw_id]["assignment_id"] for raw_id in row["finding_ids"]}:
                raise NeedsDecision("stale_adjudication", "stand-in evidence changed since assignment; preserve the original report and request the designated reconsideration")
    with contract.report_check(errors, "target"):
        if data.get("assignment_id") != row["id"] or data.get("target") != target["frozen"]:
            raise ValueError("report does not bind its assignment and the complete frozen target")
    with contract.report_check(errors, "status"):
        choice(data.get("status"), ("done", "partial", "blocked", "failed", "stopped"), "report status")
    with contract.report_check(errors, "source_status"):
        choice(data.get("source_status"), ("unchanged", "changed", "unknown"), "source status")
        if data.get("source_status") != "unchanged" and data.get("status") == "done":
            raise ValueError("done requires an unchanged source record")
    with contract.report_check(errors, "evidence"):
        evidence(data.get("evidence"))
    if row["kind"] == "adjudication":
        judgments = data.get("adjudications")
        with contract.report_check(errors, "adjudications"):
            if not isinstance(judgments, list):
                raise ValueError("adjudications must be a list")  # noqa: TRY004 - retain the validation error contract.
        ids = []
        for index, judgment in enumerate(judgments if isinstance(judgments, list) else ()):
            label = f"adjudications/{index}"
            contract.adjudication(judgment, target, errors=errors, field=label)
            with contract.report_check(errors, label):
                if not isinstance(judgment, dict) or judgment.get("assignment_id") != row["id"]:
                    raise ValueError("delegated judgment must bind its stand-in assignment")
            if isinstance(judgment, dict) and isinstance(judgment.get("finding_id"), str):
                ids.append(judgment["finding_id"])
        with contract.report_check(errors, "adjudications"):
            if len(ids) != len(set(ids)) or not set(ids) <= set(row["finding_ids"]):
                raise ValueError("delegated judgment IDs must be unique assigned findings")
        with contract.report_check(errors, "adjudications"):
            if data.get("status") == "done" and set(ids) != set(row["finding_ids"]):
                raise NeedsDecision("adjudication_report", "done requires one final decision per assigned raw ID")
    else:
        if "checked" in data:
            with contract.report_check(errors, "checked"):
                checked = paths(data["checked"], empty=True)
                if not set(checked) <= set(row["scope"]) or (data.get("status") == "done" and set(checked) != set(row["scope"])):
                    raise ValueError("declared checked paths do not match assigned coverage")
        contract.report(data, row, target["findings"], errors=errors)
        with contract.report_check(errors, "findings"):
            retain_findings(target, row, data.get("findings"), errors=errors)
    if row["kind"] == "vet" and data.get("status") == "done" and isinstance(data.get("verdicts"), list):
        for index, judgment in enumerate(data["verdicts"]):
            if not isinstance(judgment, dict) or not isinstance(judgment.get("finding_id"), str):
                continue
            prior = target["vets"].get(judgment["finding_id"])
            with contract.report_check(errors, f"verdicts/{index}"):
                if prior:
                    root = contract.unit_root(target, row)
                    if root.get("recovery", {}).get("kind") != "follow-up" or root["recovery"]["finding_id"] != judgment["finding_id"] or target["vet_history"].get(judgment["finding_id"]):
                        raise ValueError("vet verdict already recorded; only its one focused follow-up may revise it")
    if errors is not None:
        return
    if row["kind"] != "adjudication":
        for gap in data["coverage_gaps"]:
            gap_id = row["id"] + "/" + gap["id"]
            target["coverage_gaps"][gap_id] = dict(copy.deepcopy(gap), assignment_id=row["id"], resolution=None)
    row.update(status=data["status"], report=copy.deepcopy(data))
    if row["kind"] == "vet" and row["status"] == "done":
        for judgment in data["verdicts"]:
            key = judgment["finding_id"]
            prior = target["vets"].get(key)
            if prior:
                target["vet_history"].setdefault(key, []).append(copy.deepcopy(prior))
            target["vets"][key] = dict(copy.deepcopy(judgment), vetter=copy.deepcopy(row["actual_identity"]))
        target["phase"] = "vetting"


def apply_event(run: dict, event: dict, *, replay: bool = False, directory: Path | None = None) -> dict:
    """Validate a transition, retaining original reports, baselines and judgments.

    Args:
        run (dict): Replayed run state.
        event (dict): One typed target operation.
        replay (bool): Size historical tasks with a logical script filename;
            live operations always validate the actual installed path.
        directory (Path | None): Actual journal directory for complete live sizing;
            in-memory callers default to the prepared source's run root.

    Returns:
        dict: Updated copy; the input remains unchanged.

    Raises:
        ValueError: The record or transition is invalid.
        NeedsDecision: Required evidence, authorization or integrity is missing.
    """
    fields(event, ("target", "operation", "data"))
    result = copy.deepcopy(run)
    if event["operation"] == "native-parent":
        if event["target"] is not None or "admission_mode" not in result:
            raise ValueError("native-parent requires a new run operation with null target")
        if result["native_parent"] is not None:
            raise ValueError("native parent is already captured; create a new run to change it")
        data = fields(event["data"], ("root", "thread_id", "source"))
        if (not isinstance(data["root"], str) or not Path(data["root"]).is_absolute()
                or ".." in Path(data["root"]).parts or data["thread_id"] != result["thread_id"]
                or data["source"] != "terminal-cwd"):
            raise ValueError("invalid captured native parent")
        result["native_parent"] = copy.deepcopy(data)
        result["revision"] += 1
        result["events"].append(copy.deepcopy(event))
        return result
    if event["operation"] == "batch-wave":
        if event["target"] is not None:
            raise ValueError("batch-wave is a run operation; target must be null")
        if any(integrity_failures(result)):
            raise NeedsDecision("source_integrity", "preserve integrity failures before allocating another wave")
        result["batch_waves"].append(contract.batch_wave(result, event["data"]))
        result["revision"] += 1
        result["events"].append(copy.deepcopy(event))
        return result
    if event["target"] not in result["targets"]:
        raise ValueError("unknown target")
    target = result["targets"][event["target"]]
    if target["phase"] in ("skipped", "published") and event["operation"] not in ("integrity", "cleanup"):
        raise ValueError("skipped and published targets are terminal")
    operation, data = event["operation"], event["data"]
    if operation in ("assignment", "recover", "task-ready", "started", "complete", "publication-intent", "test-copy") and any(integrity_failures(result)):
        raise NeedsDecision("source_integrity", "source integrity changed or unknown; hold further dispatch records and publication")
    if operation in ("prepare", "freeze", "index-setup", "catalog", "plan", "assignment", "started", "complete", "publication-intent", "test-copy") and target["resources_removed"]:
        raise NeedsDecision("resources_removed", "review resources were cleaned; create a new run")
    if operation == "discussion-decision":
        require_phase(target, "discussion")
        discussion_decision(data, target)
        target["discussion_decision"] = dict(copy.deepcopy(data), evaluator=copy.deepcopy(result["orchestrator"]))
        required = bool(data["changes"])
        target["intake"].update(classification="discussion-changed" if required else "unchanged",
                                review_required=required, discussion_decision_required=False)
        target["phase"] = "intake" if required else "skipped"
    elif operation in ("prepare", "freeze"):
        if operation == "prepare":
            require_phase(target, "intake")
        else:
            require_phase(target, "intake", "setup")
        fields(data, ("base_sha", "head_sha", "diff_base_sha", "mode", "source_root", "source_fingerprint", "inventory", "parent_root", "parent_fingerprint"), ("preparation",))
        expected_mode = target.get("comparison", {}).get("mode", "pr-merge-base")
        choice(data["mode"], (expected_mode,), "comparison mode")
        for field in ("base_sha", "head_sha", "diff_base_sha"):
            preparation.oid(data[field])
        if "comparison" not in target and any(data[field] != target["intake"][field] for field in ("base_sha", "head_sha")):
            raise ValueError("comparison moved since intake; create a new run")
        for field in ("source_root", "parent_root"):
            if not Path(data[field]).is_absolute():
                raise ValueError(f"{field} must be absolute")
        for field in ("source_fingerprint", "parent_fingerprint"):
            if not isinstance(data[field], str) or not re.fullmatch(r"[0-9a-f]{64}", data[field]):
                raise ValueError(f"{field} must be SHA-256")
        paths(data["inventory"], empty=True)
        if "preparation" in data:
            proof = fields(data["preparation"], ("resource", "artifacts", "baseline_head_sha", "staged_tree_sha"))
            if not re.fullmatch(r"snapshot-[0-9a-f]{32}", proof["resource"]):
                raise ValueError("invalid preparation resource")
            preparation.oid(proof["baseline_head_sha"])
            if proof["staged_tree_sha"] is not None:
                preparation.oid(proof["staged_tree_sha"])
            if ((data["mode"] == "working") != (proof["staged_tree_sha"] is not None)
                    or proof["baseline_head_sha"] != data["base_sha" if data["mode"] == "working" else "head_sha"]
                    or (data["mode"] in ("range-two-dot", "working") and data["diff_base_sha"] != data["base_sha"])):
                raise ValueError("inconsistent preparation comparison")
            if not isinstance(proof["artifacts"], dict) or not {"owner.json", "source-baseline.json", "parent-baseline.json", "review.patch", "inventory.json", "name-status.z", "numstat.z", "stat.txt"} <= proof["artifacts"].keys():
                raise ValueError("missing preparation artifacts")
            for name, checksum in proof["artifacts"].items():
                if Path(name).name != name or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                    raise ValueError("invalid preparation artifact")
        if operation == "freeze" and target["setup"] is not None:
            if "preparation" not in data:
                raise ValueError("finalization requires owned preparation evidence; use finalize")
            original = target["setup"]
            if (any(data[field] != value for field, value in original.items() if field not in ("source_fingerprint", "preparation"))
                    or any(data["preparation"][field] != original["preparation"][field]
                           for field in ("resource", "baseline_head_sha", "staged_tree_sha"))
                    or any(data["preparation"]["artifacts"].get(name) != checksum
                           for name, checksum in original["preparation"]["artifacts"].items())
                    or not {"final-source-baseline.json", "index-result.json"} <= data["preparation"]["artifacts"].keys()):
                raise ValueError("finalization must retain the original comparison and artifacts")
        target["setup" if operation == "prepare" else "frozen"] = copy.deepcopy(data)
        target["phase"] = "setup" if operation == "prepare" else "prepared"
    elif operation == "index-setup":
        require_phase(target, "setup")
        fields(data, ("tools", "available", "helper", "helper_fingerprint", "cache_paths"))
        tools = strings(data["tools"], "index tool")
        available = strings(data["available"], "available index tool", empty=True)
        if (target["index_setup"] is not None or target["resources_removed"]
                or not set(tools) <= set(preparation.INDEX_TOOLS) or not set(available) <= set(tools)
                or not Path(data["helper"]).is_absolute()
                or not re.fullmatch(r"[0-9a-f]{64}", data["helper_fingerprint"])):
            raise ValueError("invalid or duplicate index setup intent")
        paths(data["cache_paths"])
        target["index_setup"] = copy.deepcopy(data)
    elif operation == "baseline":
        fields(data, ("id", "root", "fingerprint"), ("artifact", "artifact_sha256"))
        if ("artifact" in data) != ("artifact_sha256" in data):
            raise ValueError("baseline artifact requires its checksum")
        if "artifact" in data and (not isinstance(data["artifact"], str) or not re.fullmatch(r"baseline-[0-9a-f]{32}\.json", data["artifact"])
                                    or not isinstance(data["artifact_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["artifact_sha256"])):
            raise ValueError("invalid generated baseline artifact")
        key = stable_id(data["id"])
        if target["frozen"] is None or key in target["baselines"] or target["resources_removed"]:
            raise ValueError("baseline requires prepared resources and a new copy ID")
        if not Path(data["root"]).is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", data["fingerprint"]):
            raise ValueError("invalid copy baseline")
        target["baselines"][key] = copy.deepcopy(data)
    elif operation == "integrity":
        fields(data, ("status", "components"))
        choice(data["status"], ("unchanged", "changed", "unknown"), "integrity status")
        strings(data["components"], "integrity component", empty=True)
        if (target["frozen"] is None and target["setup"] is None) or (data["status"] == "unchanged" and data["components"]):
            raise ValueError("invalid integrity observation")
        target["integrity"].append(copy.deepcopy(data))
    elif operation == "test-copy":
        fields(data, ("root",))
        if not target["authorization"] or not target["authorization"]["test_execution"]:
            raise NeedsDecision("test_authorization", "test execution requires explicit recorded authorization")
        root = Path(data["root"])
        if (target["frozen"] is None or not root.is_absolute()
                or root.parent != Path(target["frozen"]["source_root"]).parent
                or not re.fullmatch(r"test-[0-9a-f]{32}", root.name)
                or data["root"] in target["test_copies"]):
            raise ValueError("invalid owned test-copy path")
        target["test_copies"].append(data["root"])
    elif operation == "cleanup":
        fields(data, ("resources",))
        strings(data["resources"], "removed resource", empty=True)
        if any(row["status"] in ("queued", "running") for row in target["assignments"].values()):
            raise NeedsDecision("active_assignments", "settle active assignments before cleaning review resources")
        target["resources_removed"] = target["frozen"] is not None or target["setup"] is not None
    elif operation == "catalog":
        require_phase(target, "prepared")
        if target["catalog"] is not None or target["plan"] is not None:
            raise ValueError("review catalog is frozen before planning")
        contract.validate_catalog(data, target["frozen"], result["registry"])
        target["catalog"] = copy.deepcopy(data)
    elif operation == "plan":
        require_phase(target, "prepared")
        contract.plan(data, target, result["method"], result["registry"], result["orchestrator"])
        perspectives = strings(data["required_perspectives"], "required family")
        if run["method"] == "pr-review-batch" and len(perspectives) != 1:
            raise ValueError("batch plan requires one reviewer's family")
        if run["method"] == "pr-review-batch":
            contract.batch_selection(result, event["target"], data)
        if target["plan"] is not None:
            raise ValueError("plan is frozen before dispatch; do not relabel failed perspectives")
        evidence(data["boundary_evidence"])
        evidence(data["selection_evidence"])
        target["plan"] = copy.deepcopy(data)
        for group in ("coverage", "interactions"):
            for key, decision in data[group].items():
                if decision["shortfall"]:
                    target["coverage_gaps"][f"plan/{group}/{key}"] = dict(copy.deepcopy(decision["shortfall"]), resolution=None)
    elif operation == "assignment":
        require_phase(target, "prepared", "discovery", "vetting", "adjudication")
        fields(data, ("id", "kind", "profile", "role", "roles", "model", "scope", "required", "reason"), ("finding_ids", "units", "interactions", "workload", "test_plan", "override_evidence"))
        key = stable_id(data["id"])
        if key == "orchestrator":
            raise ValueError("orchestrator is reserved for top-level discoveries")
        if any(key in row["assignments"] for row in result["targets"].values()):
            raise ValueError("assignment ID already used in this run")
        kind = choice(data["kind"], ("discovery", "vet", "adjudication"), "assignment kind")
        strings(data["roles"], "profile role")
        choice(data["role"], data["roles"], "selected profile role")
        choice(data["role"], ("reviewer", "candidate"), "review role")
        if type(data["required"]) is not bool:
            raise ValueError("required perspective must be boolean")
        identity.text(data["profile"], "profile")
        evidence(data["reason"])
        effective = model_record(data["model"], result["registry"])
        if effective["lane"] in result["unavailable_lanes"]:
            raise NeedsDecision("unavailable_lane", "this effective lane is unavailable for the run")
        scope = paths(data["scope"], empty=True)
        if not set(scope) <= set(target["frozen"]["inventory"]):
            raise ValueError("assignment contains paths outside frozen inventory")
        if target["plan"] is None:
            raise NeedsDecision("review_plan", "record the review plan before assignments")
        contract.assignment(data, target, effective, result["method"], result["registry"])
        if kind == "discovery":
            require_phase(target, "prepared", "discovery")
            if data["required"] and effective["family"] not in target["plan"]["required_perspectives"]:
                raise ValueError("required assignment must serve a planned required perspective")
            if run["method"] == "pr-review-batch" and (not data["required"] or effective["family"] not in target["plan"]["required_perspectives"]):
                raise ValueError("batch reviewer must serve its required perspective")
            if data.get("finding_ids"):
                raise ValueError("independent discovery cannot receive findings")
            if run["method"] == "pr-review-batch" and target["assignments"]:
                raise ValueError("batch review has exactly one discovery reviewer per PR")
            if any(set(data["units"]) & set(row["units"]) for row in target["assignments"].values() if row["kind"] == "discovery"):
                raise ValueError("discovery units are already assigned; use bounded recovery for existing work")
            target["phase"] = "discovery"
        elif kind == "vet":
            ids = strings(data.get("finding_ids"), "finding to vet")
            if len(ids) > 10:
                raise ValueError("vet batches contain at most 10 findings")
            for raw_id in ids:
                if raw_id not in target["findings"] or target["findings"][raw_id]["finder"]["family"] == effective["family"]:
                    raise ValueError("vet requires existing findings from different families")
                if data["profile"] != contract.vet_profile(target, target["findings"][raw_id], result["method"], result["registry"]):
                    raise ValueError("vet profile contradicts the approved finding route")
                if any(raw_id in row.get("finding_ids", []) for row in target["assignments"].values() if row["kind"] == "vet"):
                    raise ValueError("finding already has a vet assignment; use bounded recovery")
            if any(row["kind"] == "discovery" and row["status"] in ("queued", "running") for row in target["assignments"].values()):
                raise NeedsDecision("discovery_completion", "settle independent discovery before cross-family vetting")
            gaps = discovery_gaps(target)
            if gaps:
                raise NeedsDecision("discovery_coverage", "; ".join(gaps))
            target["phase"] = "vetting"
        else:
            ids = strings(data.get("finding_ids"), "finding to adjudicate")
            if any(key not in target["vets"] or (key in target["adjudications"] and not readjudication_allowed(target, key)) for key in ids):
                raise ValueError("stand-in assignment requires vetted, unadjudicated findings")
            if any(row["status"] in ("queued", "running") and row["kind"] != "adjudication" for row in target["assignments"].values()):
                raise NeedsDecision("review_completion", "settle discovery and vetting before delegated adjudication")
            if any(effective["family"] == row["actual_identity"]["family"] for row in target["assignments"].values()
                   if row["kind"] != "adjudication" and row["actual_identity"]):
                raise ValueError("stand-in family already participated on this PR")
            if any(set(ids) & set(row.get("finding_ids", [])) and
                   (row["status"] in ("queued", "running") or any(not readjudication_allowed(target, key) for key in set(ids) & set(row["finding_ids"])))
                   for row in target["assignments"].values() if row["kind"] == "adjudication"):
                raise ValueError("finding already has a stand-in assignment")
            if any(set(ids) & set(row.get("readjudication_ids", [])) for row in target["assignments"].values()):
                raise ValueError("finding already has its one stand-in reconsideration; use bounded recovery")
            target["phase"] = "adjudication"
        target["assignments"][key] = dict(copy.deepcopy(data), identity=effective, status="queued", actual_identity=None, report=None)
        if kind == "adjudication":
            target["assignments"][key]["vet_bindings"] = {raw_id: target["vets"][raw_id]["assignment_id"] for raw_id in ids}
            target["assignments"][key]["readjudication_ids"] = [raw_id for raw_id in ids if raw_id in target["adjudications"]]
        contract.task_block(result, event["target"], key, replay=replay, directory=directory)
    elif operation == "recover":
        require_phase(target, "discovery", "vetting", "adjudication")
        row = contract.recovery(result, target, data)
        target["assignments"][row["id"]] = row
        if data["kind"] != "follow-up":
            target["assignments"][data["assignment_id"]]["superseded_by"] = row["id"]
        target["phase"] = {"discovery": "discovery", "vet": "vetting", "adjudication": "adjudication"}[row["kind"]]
        contract.task_block(result, event["target"], row["id"], replay=replay, directory=directory)
    elif operation == "task-ready":
        require_phase(target, "discovery", "vetting", "adjudication")
        contract.decisions(data, ("assignment_id", "capacity"), "task_preflight", ("handoff",))
        if "handoff" in data:
            handoff.validate(data["handoff"])
            if not data["handoff"]["artifact"].endswith("/task.json"):
                raise ValueError("task approval requires its task artifact")
        contract.dispatch_task(result, event["target"], stable_id(data["assignment_id"]), data["capacity"], replay=replay, directory=directory)
        target["assignments"][data["assignment_id"]]["task_ready"] = {
            "revision": result["revision"] + 1, "capacity": copy.deepcopy(data["capacity"])}
        if "handoff" in data:
            target["assignments"][data["assignment_id"]]["task_ready"]["handoff"] = copy.deepcopy(data["handoff"])
    elif operation == "withdraw":
        require_phase(target, "discovery", "vetting", "adjudication")
        fields(data, ("assignment_id", "target", "cause", "evidence"), ("native_reconciliation",))
        row = target["assignments"].get(stable_id(data["assignment_id"]))
        if (row is None or row["status"] != "queued" or row.get("runtime_handle")
                or row["actual_identity"] is not None or data["target"] != target["frozen"]):
            raise ValueError("withdrawal requires an unstarted queued assignment and its frozen target")
        choice(data["cause"], ("transient", "auth", "quota", "rate", "cancelled"), "withdrawal cause")
        evidence(data["evidence"])
        approval = row.get("task_ready")
        if approval:
            reconciliation = data.get("native_reconciliation")
            if reconciliation is None:
                raise NeedsDecision("runtime_reconciliation", "an issued task approval needs native evidence that launch/follow-up did not start")
            fields(reconciliation, ("approval_revision", "status", "evidence"))
            if (type(reconciliation["approval_revision"]) is not int
                    or reconciliation["approval_revision"] != approval["revision"]
                    or reconciliation["status"] != "not-started"):
                raise ValueError("withdrawal reconciliation must bind the exact issued approval and unstarted native work")
            evidence(reconciliation["evidence"])
        elif "native_reconciliation" in data:
            raise ValueError("no task approval exists to reconcile")
        row.update(status="stopped", withdrawal=copy.deepcopy(data))
    elif operation == "started":
        require_phase(target, "discovery", "vetting", "adjudication")
        contract.decisions(data, ("assignment_id", "runtime_handle", "runtime_identity", "runtime_profile", "runtime_evidence"),
                           "runtime_identity", ("runtime_root", "runtime_parent_root", "runtime_copy"))
        row = target["assignments"].get(data["assignment_id"])
        if row is None or row["status"] != "queued":
            raise ValueError("only a queued assignment can start")
        if data["runtime_profile"] != row["profile"]:
            raise ValueError("runtime profile does not match assignment")
        contract.task_block(result, event["target"], row["id"], replay=replay)
        actual = model_record(data["runtime_identity"], result["registry"])
        if any(actual[field] != row["identity"][field] for field in ("provider_id", "model_id", "family", "lane", "billing")):
            raise ValueError("runtime identity does not match assignment")
        if actual["lane"] in result["unavailable_lanes"]:
            raise NeedsDecision("unavailable_lane", "this effective lane is unavailable for the run")
        handle = identity.text(data["runtime_handle"], "runtime handle")
        reuse = row.get("recovery", {}).get("runtime_handle")
        if reuse is not None and handle != reuse:
            raise ValueError("correction/follow-up must reuse its selected runtime handle")
        if reuse is None and any(handle == assignment.get("runtime_handle") for unit in result["targets"].values() for assignment in unit["assignments"].values()):
            raise ValueError("each discovery/vet assignment needs a fresh runtime conversation")
        if any(handle == assignment.get("runtime_handle") and assignment["status"] == "running" for unit in result["targets"].values() for assignment in unit["assignments"].values()):
            raise NeedsDecision("runtime_reconciliation", "this runtime handle still has active recorded work")
        evidence(data["runtime_evidence"])
        if ("runtime_root" in data) != ("runtime_copy" in data):
            raise ValueError("native root requires its generated copy evidence")
        if "runtime_parent_root" in data and "runtime_root" not in data:
            raise ValueError("native parent declaration requires an actual runtime root")
        if "runtime_copy" in data:
            proof = fields(data["runtime_copy"], ("root", "parent_root", "common_dir", "git_dir", "mode", "ref", "head", "created_dirs", "fingerprint"), ("clone_store", "project_root"))
            parents = {unit["parent_root"] for item in result["targets"].values()
                       for unit in [item["frozen"] or item["setup"]] if unit is not None}
            parents.update(record["root"] for item in result["targets"].values() for record in item["baselines"].values())
            if proof["root"] != data["runtime_root"] or proof["parent_root"] not in parents:
                raise ValueError("native copy must bind its actual root and a recorded protected parent")
            if "runtime_parent_root" in data and data["runtime_parent_root"] != proof["parent_root"]:
                raise ValueError("native parent declaration must match generated evidence")
            approved_parent = row.get("task_ready", {}).get("capacity", {}).get("runtime_parent_root")
            if approved_parent is not None and proof["parent_root"] != approved_parent:
                raise ValueError("native copy must match the approved native parent")
            project = proof.get("project_root", proof["parent_root"])
            for path in (proof["root"], proof["parent_root"], proof["common_dir"], proof["git_dir"], project):
                if not isinstance(path, str) or not Path(path).is_absolute() or ".." in Path(path).parts:
                    raise ValueError("native copy paths must be absolute and canonical")
            if "project_root" in proof and (project not in parents or "clone_store" not in proof
                                           or not Path(proof["parent_root"]).is_relative_to(project)):
                raise ValueError("native project ownership requires a registered project containing the actual parent")
            if type(proof["mode"]) is not int or not 0 <= proof["mode"] <= 0o7777:
                raise ValueError("native copy mode must be a valid permission mask")
            preparation.oid(proof["head"])
            if not isinstance(proof["fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", proof["fingerprint"]):
                raise ValueError("native copy fingerprint must be SHA-256")
            if proof["ref"] is not None and (not isinstance(proof["ref"], str) or not proof["ref"].startswith("refs/heads/")
                                             or any(char.isspace() for char in proof["ref"])):
                raise ValueError("native copy ref must identify its local branch")
            if "clone_store" in proof:
                store = fields(proof["clone_store"], ("ref", "ref_store", "fingerprint"))
                pin = preparation.clone_reference(Path(proof["root"]), Path(project),
                                                  Path(proof["git_dir"]), proof["head"])
                if (pin is None or store["ref"] != pin or proof["common_dir"] != proof["git_dir"]
                        or not isinstance(store["ref_store"], str) or not Path(store["ref_store"]).is_absolute()
                        or ".." in Path(store["ref_store"]).parts or not isinstance(store["fingerprint"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", store["fingerprint"])):
                    raise ValueError("native clone evidence must bind its exact store and pin")
            if not isinstance(proof["created_dirs"], dict):
                raise ValueError("native copy container evidence must be an object")
            for name, mode in proof["created_dirs"].items():
                relative = preparation.relative_path(name)
                path = Path(project) / relative
                ancestors = set(Path(proof["root"]).parents)
                if "clone_store" in proof:
                    ancestors.update(Path(proof["git_dir"]).parents)
                if (relative.split("/")[0] != ".delta" or path not in ancestors
                        or not isinstance(mode, list) or len(mode) != 2 or mode[0] != "directory"
                        or type(mode[1]) is not int or not 0 <= mode[1] <= 0o7777):
                    raise ValueError("native copy container must be an exact ancestor directory")
            prior_copy = next((unit["runtime_copy"] for unit in target["assignments"].values()
                               if unit.get("runtime_handle") == handle and "runtime_copy" in unit), None)
            if prior_copy is not None and proof != prior_copy:
                raise ValueError("native copy baselines are immutable for a runtime handle")
            if reuse is None and any(proof["root"] == copy["root"] for copy in runtime_copies(result)):
                raise ValueError("new runtime handles require separate native copies")
            row["runtime_copy"] = copy.deepcopy(proof)
        row.update(status="running", actual_identity=actual, runtime_handle=handle, runtime_evidence=data["runtime_evidence"])
    elif operation == "runtime-outcome":
        require_phase(target, "discovery", "vetting", "adjudication")
        contract.decisions(data, ("assignment_id", "runtime_handle", "target", "status", "cause", "evidence"), "runtime_outcome")
        row = target["assignments"].get(stable_id(data["assignment_id"]))
        if (row is None or row.get("superseded_by") or row.get("runtime_outcome", {}).get("status") not in (None, "unknown")
                or (row["status"] != "running" and not (row["report"] and row["status"] != "done" and row["status"] == data["status"]))
                or data["runtime_handle"] != row["runtime_handle"] or data["target"] != target["frozen"]):
            raise ValueError("runtime outcome requires the exact running handle and frozen target")
        choice(data["status"], ("done", "failed", "stopped", "blocked", "partial", "unknown"), "native outcome")
        choice(data["cause"], ("transient", "auth", "quota", "rate", "identity", "role", "source", "incomplete-report", "evidence-gap", "cancelled"), "runtime cause")
        evidence(data["evidence"])
        if data["status"] == "done" and data["cause"] != "incomplete-report":
            raise ValueError("done without a report requires incomplete-report; record the report separately")
        row["runtime_outcome"] = copy.deepcopy(data)
        if data["status"] != "unknown":
            row["status"] = "blocked" if data["status"] == "done" else data["status"]
        if data["cause"] in ("auth", "quota", "rate"):
            result["unavailable_lanes"][row["actual_identity"]["lane"]] = copy.deepcopy(data)
    elif operation in ("report-draft", "report-authored"):
        require_phase(target, "discovery", "vetting", "adjudication")
        fields(data, ("assignment_id", "task_sha256", "input", "report"), ("assembly_error", "errors") if operation == "report-draft" else ("assembly_error",))
        row = target["assignments"].get(stable_id(data["assignment_id"]))
        if (row is None or row["status"] != "running" or row.get("authored_report")
                or row.get("report") or row.get("rejected_report") or row.get("superseded_by")
                or data["task_sha256"] != row.get("task_ready", {}).get("handoff", {}).get("sha256")):
            raise ValueError("report authoring requires the exact running file-backed assignment")
        for name in ("input", "report"):
            handoff.validate(data[name])
            if not data[name]["artifact"].endswith(f"/{name}.json"):
                raise ValueError("report authoring artifact kind changed")
        if "assembly_error" in data:
            evidence(data["assembly_error"])
        if operation == "report-draft":
            if not isinstance(data.get("errors"), list) or not data["errors"]:
                raise ValueError("an invalid draft requires its validation errors")
            for error in data["errors"]:
                fields(error, ("field", "code", "reason"))
                for value in error.values():
                    evidence(value)
            row.setdefault("drafts", []).append(copy.deepcopy(data))
        else:
            row["authored_report"] = copy.deepcopy(data)
    elif operation == "report-input":
        require_phase(target, "discovery", "vetting", "adjudication")
        contract.decisions(data, ("assignment_id", "runtime_handle", "target", "report", "evidence", "source_status"), "report_input", nullable=("report",))
        row = target["assignments"].get(stable_id(data["assignment_id"]))
        if (row is None or row["report"] is not None or row.get("rejected_report") or row.get("superseded_by")
                or row["status"] == "queued" or data["runtime_handle"] != row["runtime_handle"] or data["target"] != target["frozen"]):
            raise ValueError("report input requires its exact unreplaced runtime handle and frozen target")
        if row.get("runtime_outcome", {}).get("status") == "unknown":
            raise NeedsDecision("runtime_reconciliation", "reconcile the native outcome before accepting a report")
        evidence(data["evidence"])
        choice(data["source_status"], ("unchanged", "changed", "unknown"), "source status")
        authored = row.get("authored_report")
        invalid_encoding = authored and "assembly_error" in authored and data["report"] is None
        if authored and not invalid_encoding and (not isinstance(data["report"], str)
                         or hashlib.sha256(data["report"].encode("utf-8")).hexdigest() != authored["report"]["sha256"]):
            raise ValueError("authored report input must preserve its sealed bytes")
        try:
            if authored and "assembly_error" in authored:
                raise ValueError(authored["assembly_error"])
            reply = json.loads(data["report"]) if isinstance(data["report"], str) else data["report"]
            if not isinstance(reply, dict) or reply.get("assignment_id") != row["id"] or reply.get("source_status") != data["source_status"]:
                raise ValueError("report must be a JSON object matching its assignment and observed source status")
            outcome = row.get("runtime_outcome", {})
            if outcome.get("cause", "incomplete-report") != "incomplete-report" and reply.get("status") == "done":
                raise ValueError("a done report cannot erase the recorded native failure")
            candidate = copy.deepcopy(target)
            accept_report(candidate, candidate["assignments"][row["id"]], reply)
            if outcome.get("cause", "incomplete-report") not in ("incomplete-report", "evidence-gap"):
                candidate["assignments"][row["id"]]["status"] = row["status"]
            result["targets"][event["target"]] = candidate
        except (ValueError, NeedsDecision) as error:
            row["rejected_report"] = dict(copy.deepcopy(data), code=getattr(error, "code", "invalid_report"), reason=str(error))
            if "runtime_outcome" not in row:
                row.update(status="blocked", runtime_outcome={"status": "done", "cause": "incomplete-report", "evidence": data["evidence"]})
    elif operation in ("report", "adjudication-report"):
        phases = ("adjudication",) if operation == "adjudication-report" else ("discovery", "vetting")
        require_phase(target, *phases)
        contract.decisions(data, ("assignment_id",), "report_evidence", tuple(data.keys() - {"assignment_id"}) if isinstance(data, dict) else ())
        row = target["assignments"].get(stable_id(data["assignment_id"]))
        if row and row.get("authored_report"):
            raise ValueError("file-authored reports require original report-input receipt")
        if row is None or row["status"] != "running" or (row["kind"] == "adjudication") != (operation == "adjudication-report"):
            raise ValueError("report requires its running discovery/vet or stand-in assignment")
        if row.get("runtime_outcome", {}).get("status") == "unknown":
            raise NeedsDecision("runtime_reconciliation", "reconcile the exact native task before accepting its report")
        accept_report(target, row, data)
    elif operation == "orchestrator-finding":
        require_phase(target, "discovery", "vetting", "adjudication")
        fields(data, ("target", "finding"))
        if data["target"] != target["frozen"]:
            raise ValueError("orchestrator finding must bind the complete frozen target")
        raw = data["finding"]
        finding(raw)
        if not re.fullmatch(r"orchestrator-[0-9]{2,}", raw["id"]) or raw["finder"] != result["orchestrator"]:
            raise ValueError("orchestrator discovery requires its own ID and actual orchestrator identity")
        if raw["id"] in target["findings"]:
            raise ValueError("finding IDs are immutable and unique")
        target["findings"][raw["id"]] = copy.deepcopy(raw)
    elif operation == "vet":
        require_phase(target, "discovery", "vetting", "adjudication")
        contract.decisions(data, ("finding_id", "assignment_id", "verdict", "severity", "category", "evidence", "fix_assessment", "claim", "references"), "vet_evidence", nullable=("assignment_id",))
        raw = target["findings"].get(data["finding_id"])
        if raw is None:
            raise ValueError("unknown finding")
        contract.verdict(data, raw)
        row = target["assignments"].get(data["assignment_id"])
        if row is None or row["kind"] != "vet" or row["status"] != "done" or data["finding_id"] not in row["finding_ids"]:
            raise ValueError("verdict requires its completed cross-family vet assignment")
        if data not in row["report"]["verdicts"]:
            raise ValueError("vet judgment must match the retained original vet report")
        # Existing journals may acknowledge a report-derived verdict once.
        if target["vets"].get(data["finding_id"], {}).get("assignment_id") != row["id"] or any(
                prior["target"] == event["target"] and prior["operation"] == "vet"
                and prior["data"]["finding_id"] == data["finding_id"] and prior["data"]["assignment_id"] == row["id"]
                for prior in result["events"]):
            raise ValueError("vet verdict already recorded or superseded")
    elif operation == "adjudication":
        require_phase(target, "discovery", "vetting", "adjudication")
        if any(row["status"] in ("queued", "running") for row in target["assignments"].values()):
            raise ValueError("settle all active assignments before the separate adjudication pass")
        contract.adjudication(data, target)
        key = data["finding_id"]
        if key not in target["findings"] or (key in target["adjudications"] and not readjudication_allowed(target, key)):
            raise ValueError("adjudication requires an existing unadjudicated finding")
        vet = target["vets"].get(key)
        delegated = target["plan"]["routing"]["stand_in"]
        if delegated is None:
            if data.get("assignment_id") is not None:
                raise ValueError("this finding requires personal orchestrator adjudication")
            adjudicator = result["orchestrator"]
        else:
            row = target["assignments"].get(data.get("assignment_id"))
            if row is None or row["kind"] != "adjudication" or row["status"] != "done" or row.get("recovery", {}).get("participant", row["profile"]) != delegated:
                raise NeedsDecision("stand_in_adjudicator", "incorporate this PR's completed stand-in judgment; do not adjudicate it again")
            if data not in row["report"]["adjudications"]:
                raise ValueError("adjudication must preserve the original stand-in judgment")
            if row["vet_bindings"].get(key) != target["vets"][key]["assignment_id"]:
                raise NeedsDecision("stale_adjudication", "incorporate a judgment based on the latest focused vet evidence")
            adjudicator = row["actual_identity"]
        overlap = [stage for stage, person in (("finder", target["findings"][key]["finder"]), ("vetter", vet["vetter"] if vet else None))
                   if person and person["family"] == adjudicator["family"]]
        if overlap:
            raise NeedsDecision("independent_adjudication", "adjudication must use a different family from this finding's actual finder and vetter")
        if key in target["adjudications"]:
            target["adjudication_history"].setdefault(key, []).append(copy.deepcopy(target["adjudications"][key]))
        target["adjudications"][key] = dict(copy.deepcopy(data), adjudicator=copy.deepcopy(adjudicator), family_overlap=[], vet_assignment_id=vet["assignment_id"] if vet else None)
        target["phase"] = "adjudication"
    elif operation == "gap":
        require_phase(target, "intake", "setup", "prepared", "discovery", "vetting", "adjudication")
        fields(data, ("id", "reason", "material"))
        key = stable_id(data["id"])
        if key in target["coverage_gaps"] or type(data["material"]) is not bool:
            raise ValueError("gap ID must be unique and material must be boolean")
        evidence(data["reason"])
        target["coverage_gaps"][key] = dict(copy.deepcopy(data), resolution=None)
    elif operation == "resolve-gap":
        fields(data, ("id", "evidence"))
        gap = target["coverage_gaps"].get(data["id"])
        if gap is None or gap["resolution"] is not None:
            raise ValueError("resolution requires an open gap")
        gap["resolution"] = evidence(data["evidence"])
    elif operation == "authorization":
        fields(data, ("submission", "test_execution", "metered_spawns", "evidence"), ("approved_review",))
        if any(type(data[field]) is not bool for field in ("submission", "test_execution")):
            raise ValueError("authorization flags must be boolean")
        if type(data["metered_spawns"]) is not int or data["metered_spawns"] < 0:
            raise ValueError("metered authorization must be a nonnegative spawn count")
        evidence(data["evidence"])
        if "approved_review" in data:
            approved = fields(data["approved_review"], ("event", "body"))
            choice(approved["event"], ("APPROVE", "REQUEST_CHANGES"), "approved review event")
            if approved["body"] is not None:
                evidence(approved["body"])
        if target["phase"] == "publication-pending":
            raise ValueError("reconcile publication before changing authorization")
        target["authorization"] = copy.deepcopy(data)
    elif operation == "complete":
        require_phase(target, "discovery", "vetting", "adjudication")
        fields(data, ("coverage_evidence",))
        evidence(data["coverage_evidence"])
        gaps = completion_gaps(result, target)
        if gaps:
            raise NeedsDecision("incomplete_review", "incomplete review: " + "; ".join(gaps))
        target["coverage_evidence"] = data["coverage_evidence"]
        target["phase"] = "complete"
    elif operation == "publication-context":
        require_phase(target, "complete")
        fields(data, ("base_sha", "head_sha", "state", "discussion", "review_ids", "owner", "hostname", "observed_at"))
        intake.validate_discussion(data["discussion"])
        if data["state"] != "open" or any(data[field] != target["frozen"][field] for field in ("base_sha", "head_sha")):
            raise NeedsDecision("comparison_changed", "live PR closed or comparison moved; publication held")
        if data["owner"].casefold() != result["owner"].casefold() or data["hostname"] != publication.endpoint(target)[0]:
            raise ValueError("publication context account or host changed")
        if not isinstance(data["review_ids"], list) or len(set(data["review_ids"])) != len(data["review_ids"]):
            raise ValueError("publication context requires unique live review IDs")
        for key in data["review_ids"]:
            intake.positive(key, "review ID")
        evidence(data["observed_at"])
        previous = target["publication_context"]
        if previous is None or previous["discussion"] != data["discussion"]:
            target["publication_discussion"] = None
        target["publication_context"] = copy.deepcopy(data)
    elif operation == "publication-discussion":
        require_phase(target, "complete")
        context = target["publication_context"]
        if context is None or target["publication_discussion"] is not None:
            raise ValueError("publication discussion requires an unevaluated live snapshot")
        metadata = dict(target["intake"], discussion=context["discussion"], discussion_baseline=target["intake"]["discussion"])
        discussion_decision(data, {"intake": metadata})
        target["publication_discussion"] = dict(copy.deepcopy(data), evaluator=copy.deepcopy(result["orchestrator"]))
    elif operation == "publication-intent":
        if target["intake"]["classification"] == "self-authored-review":
            raise NeedsDecision("review_only", "self-authored reviews remain local; GitHub publication is disabled")
        if "comparison" in target:
            raise NeedsDecision("local_publication", "local comparison has no GitHub PR; no review submission applies")
        require_phase(target, "complete")
        fields(data, ("base_sha", "head_sha", "state", "body", "authorization_evidence"), ("draft",))
        authorization = target["authorization"]
        if authorization is None or not authorization["submission"]:
            raise NeedsDecision("publication_authorization", "publication lacks authorization for this PR")
        if data["state"] != "open" or any(data[field] != target["frozen"][field] for field in ("base_sha", "head_sha")):
            raise NeedsDecision("comparison_changed", "live PR closed or comparison moved; publication held")
        if data["authorization_evidence"] != authorization["evidence"]:
            raise ValueError("publication authorization does not match recorded scope")
        if authorization.get("approved_review") is not None and authorization["approved_review"] != {"event": review_event(target), "body": data["body"]}:
            raise NeedsDecision("publication_authorization", "event or body differs from the exact user-approved review")
        gaps = completion_gaps(result, target)
        if gaps:
            raise NeedsDecision("incomplete_review", "; ".join(gaps))
        if "draft" in data:
            hold_publication_discussion(target)
            if data["body"] != publication.render_body(target, data["draft"]):
                raise ValueError("publication body must match the evidence-bound draft")
        retained = [row for row in target["adjudications"].values() if row["disposition"] in ("CONFIRMED", "ADJUSTED")]
        body = data["body"]
        if retained:
            evidence(body)
            if all(row["severity"] == "nit" for row in retained) and "do not block merging" not in body:
                raise ValueError("nits-only approval must explicitly state nits do not block merging")
        elif body is not None:
            evidence(body)
        target["publication"] = {"event": review_event(target), "body": body,
                                 "commit_id": target["frozen"]["head_sha"], "receipt": None}
        if "draft" in data:
            target["publication"].update(draft=copy.deepcopy(data["draft"]),
                                         prior_review_ids=copy.deepcopy(target["publication_context"]["review_ids"]),
                                         context=copy.deepcopy(target["publication_context"]), outcome=None, reconciliations=[])
        target["phase"] = "publication-pending"
    elif operation == "publication-outcome":
        require_phase(target, "publication-pending")
        fields(data, ("status", "returncode", "response_sha256", "evidence"), ("diagnostic",))
        if "diagnostic" in data:
            diagnostic = fields(data["diagnostic"], ("http_status", "reason"))
            code = diagnostic["http_status"]
            if code is not None and (type(code) is not int or not 400 <= code <= 599):
                raise ValueError("invalid publication diagnostic HTTP status")
            choice(diagnostic["reason"], ("authentication", "authorization", "not_found", "conflict",
                                         "validation", "rate_limit", "server", "request_failed"), "publication diagnostic")
        choice(data["status"], ("unknown", "responded"), "publication response")
        evidence(data["evidence"])
        if (data["status"] == "unknown" and (data["returncode"] is not None or data["response_sha256"] is not None)) or (
                data["status"] == "responded" and (type(data["returncode"]) is not int or not isinstance(data["response_sha256"], str)
                                                  or not re.fullmatch(r"[0-9a-f]{64}", data["response_sha256"]))):
            raise ValueError("publication outcome must retain a valid response observation")
        if "outcome" not in target["publication"] or target["publication"]["outcome"] is not None:
            raise ValueError("publication has no new pending POST outcome")
        target["publication"]["outcome"] = copy.deepcopy(data)
    elif operation == "publication-reconciliation":
        require_phase(target, "publication-pending")
        fields(data, ("review_ids", "matching_ids", "receipt", "observed_at"))
        if "reconciliations" not in target["publication"]:
            raise NeedsDecision("publication_provenance", "legacy attempt lacks a pre-submit inventory; hold without retry")
        for field in ("review_ids", "matching_ids"):
            if not isinstance(data[field], list) or len(set(data[field])) != len(data[field]):
                raise ValueError("reconciliation requires unique review ID lists")
            for key in data[field]:
                intake.positive(key, "review ID")
        evidence(data["observed_at"])
        if not set(data["matching_ids"]) <= set(data["review_ids"]):
            raise ValueError("reconciliation matches must belong to the live inventory")
        target["publication"]["reconciliations"].append(copy.deepcopy(data))
    elif operation == "publication-receipt":
        require_phase(target, "publication-pending")
        fields(data, ("id", "html_url", "commit_id", "state", "body", "author", "evidence"), ("verification", "submitted_at", "live"))
        expected = target["publication"]
        intake.positive(data["id"], "review ID")
        identity.text(data["html_url"], "review URL")
        evidence(data["evidence"])
        state = "APPROVED" if expected["event"] == "APPROVE" else "CHANGES_REQUESTED"
        if (data["commit_id"] != expected["commit_id"] or data["state"] != state
                or data["body"] != (expected["body"] or "") or data["author"].casefold() != result["owner"].casefold()):
            raise ValueError("publication receipt does not match author, frozen head, event and body")
        if "verification" in data:
            choice(data["verification"], ("github-rest-readback",), "receipt verification")
            observations = expected.get("reconciliations", [])
            if not observations or observations[-1]["matching_ids"] != [data["id"]] or observations[-1]["receipt"] != data:
                raise ValueError("verified receipt must match the latest unique independent readback")
        expected["receipt"] = copy.deepcopy(data)
        target["phase"] = "published"
    else:
        raise ValueError("unknown operation")
    result["revision"] += 1
    result["events"].append(copy.deepcopy(event))
    return result


def hold_publication_discussion(target: dict) -> None:
    """Hold publication until changed discussion has a bound materiality decision.

    Args:
        target (dict): Completed PR target and latest live snapshot.

    Raises:
        NeedsDecision: Live preflight or a nonmaterial discussion judgment is missing.
    """
    context = target["publication_context"]
    if context is None:
        raise NeedsDecision("publication_preflight", "read live PR comparison and complete discussion with preflight or publish")
    if context["discussion"] == target["intake"]["discussion"]:
        return
    decision = target["publication_discussion"]
    if decision is None or decision["discussion_sha256"] != context["discussion"]["sha256"]:
        raise NeedsDecision("discussion_materiality", "assess the changed live discussion for finding additions, removals or severity changes")
    if decision["changes"]:
        raise NeedsDecision("material_discussion", "discussion materially changes findings; create a new review run with this evidence before publication")


def status(run: dict) -> dict:
    """Describe recorded state and the next missing decision.

    Args:
        run (dict): Validated journal state.

    Returns:
        dict: Factual status with explicit enforcement limits.
    """
    rows = []
    for key, target in run["targets"].items():
        phase = target["phase"]
        if phase == "skipped":
            decision = {"status": "skipped", "reason": target["intake"]["classification"]}
        elif phase == "published":
            receipt = target["publication"]["receipt"]
            decision = {"status": "receipt_verified" if receipt.get("verification") == "github-rest-readback" else "receipt_recorded", "receipt": receipt,
                        "superseded": any(receipt.get("live", {}).get(field, target["frozen"][field]) != target["frozen"][field] for field in ("base_sha", "head_sha"))}
        elif phase == "publication-pending":
            decision = {"status": "needs_decision", "code": "reconcile_publication", "reason": "read all live review pages and match the attempted review before any retry"}
        else:
            decision = next_decision(run, target)
        rows.append({"target": key, "phase": phase, "next": decision, "frozen": target["frozen"],
                     "classification": target["intake"]["classification"], "discussion_decision": target["discussion_decision"],
                     "setup": target["setup"] if phase == "setup" else None,
                     "test_copies": target["test_copies"], "resources_removed": target["resources_removed"],
                     "catalog": target["catalog"], "plan": target["plan"],
                     "recovery": [{"assignment_id": row["id"], "status": row["status"],
                                   "request": {name: row["recovery"][name] for name in ("kind", "unit_id", "assignment_id", "participant", "runtime_handle", "finding_id") if name in row["recovery"]} if row.get("recovery") else None,
                                   "runtime_outcome": row.get("runtime_outcome"),
                                   "report_error": {name: row["rejected_report"][name] for name in ("code", "reason")} if row.get("rejected_report") else None}
                                  for row in target["assignments"].values() if row.get("recovery") or row.get("runtime_outcome") or row.get("rejected_report")],
                     "completion_gaps": completion_gaps(run, target) if target["frozen"] else [],
                     "publication_context": target["publication_context"], "publication_discussion": target["publication_discussion"],
                     "publication": target["publication"],
                     "derived_event": review_event(target) if phase in ("complete", "publication-pending", "published") else None})
    return {"run_id": run["run_id"], "revision": run["revision"], "method": run["method"],
            "owner": run["owner"], "limits": {"per_thread": 15, "overall": 30},
            "admission_mode": run.get("admission_mode", "observed"), "native_parent": run.get("native_parent"),
            "unavailable_lanes": run["unavailable_lanes"],
            "enforcement": {"preparation": True, "write_detection": True, "dispatch_gate": True, "dispatch": False,
                            "write_prevention": False, "publication": True},
            "batch_waves": [{name: wave[name] for name in ("id", "slots", "reserved", "allocations", "held", "deferred")}
                            for wave in run["batch_waves"]], "targets": rows}


def report_text(value: object) -> str:
    """Escape recorded data for Markdown prose and table cells.

    Args:
        value (object): Journal data, including untrusted paths and model prose.

    Returns:
        str: Literal text with safe line breaks and escaped HTML/link syntax.
    """
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    text = html.escape("".join(char for char in text if char.isprintable() or char == "\n"), quote=False)
    text = re.sub(r"([\\`*_\[\]!])", r"\\\1", text)
    return text.replace("|", "&#124;").replace("\n", "<br>")


def report_table(headers: tuple[str, ...], rows: list[tuple]) -> list[str]:
    """Render literal recorded cells as a Markdown table.

    Args:
        headers (tuple[str, ...]): Column labels.
        rows (list[tuple]): Recorded values in column order.

    Returns:
        list[str]: Table lines, with surrounding blank lines.
    """
    if not rows:
        return []
    return ["", "| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(report_text(cell) for cell in row) + " |" for row in rows), ""]


def report_identity(record: dict | None) -> str:
    """Describe a recorded identity without treating self-report as verification.

    Args:
        record (dict | None): Resolved identity, absent before a recorded start.

    Returns:
        str: Model, family, provider, lane and billing, or an explicit absence.
    """
    return "; ".join(str(record[field]) for field in ("model_id", "family", "provider_id", "lane", "billing")) if record else "not recorded"


def report(run: dict) -> str:
    """Render status and completion evidence from one replayed journal snapshot.

    Args:
        run (dict): Validated run state; this function performs no live checks.

    Returns:
        str: Markdown status table and per-target evidence, holds and receipts.
            Recommendations apply only to the completed frozen comparison.
    """
    snapshot = status(run)
    lines = ["# PR review report", "", f"Run: {report_text(run['run_id'])}; revision: {run['revision']}; method: {run['method']}.",
             f"Parent: {report_text(run['thread_id'])}; authenticated owner: {report_text(run['owner'] or 'none (local comparison)')}.",
             f"Orchestrator (recorded): {report_text(report_identity(run['orchestrator']))}."]
    lines.append(f"Admission: {report_text(snapshot['admission_mode'])}; captured parent: {report_text(snapshot['native_parent'])}.")
    if snapshot["admission_mode"] == "native":
        lines.append("Live parent/process counts are unavailable; Delta owns admission under unchanged 15/30 limits.")
    collection = run["intake"]
    groups = collection.get("repositories", [collection])
    repositories = [group["repository"] for group in groups if "repository" in group]
    lines.append(f"Requested scope: {report_text(collection.get('organization') or collection.get('local') or repositories)}.")
    if repositories:
        lines.append(f"Collected visible repositories (including empty groups): {report_text(repositories)}.")
    summary = []
    for row in snapshot["targets"]:
        target = run["targets"][row["target"]]
        completed = row["derived_event"] is not None
        retained = [item for item in target["adjudications"].values() if item["disposition"] in ("CONFIRMED", "ADJUSTED")]
        recommendation = "review incomplete"
        if row["phase"] == "skipped":
            recommendation = "not reviewed"
        elif completed:
            recommendation = "do not merge" if any(item["severity"] == "blocker" for item in retained) else (
                "merge after fixes" if row["derived_event"] == "REQUEST_CHANGES" else "merge-ready (optional nits)" if retained else "merge-ready")
            if row["next"].get("code") in ("source_integrity", "discussion_materiality", "material_discussion"):
                recommendation = "review held"
        publication_state = row["next"]["status"] if row["phase"] == "published" else (
            "pending reconciliation" if row["phase"] == "publication-pending" else "not posted")
        if row["next"].get("superseded"):
            publication_state += " (superseded comparison)"
        summary.append((row["target"], row["classification"], row["phase"], recommendation, row["derived_event"] or "not derived",
                        publication_state, row["next"].get("code") or row["next"].get("reason", "none")))
    lines += report_table(("Target", "Intake", "Phase", "Frozen-comparison recommendation", "Derived event", "Publication", "Next / hold"), summary)
    if not summary:
        lines.append("No PRs were collected; no reviews were performed or published.")
    if snapshot["batch_waves"]:
        lines += ["## Batch waves", *report_table(("Wave", "Slots", "Reserved", "Allocations", "Held", "Deferred"),
                    [tuple(wave[field] for field in ("id", "slots", "reserved", "allocations", "held", "deferred")) for wave in snapshot["batch_waves"]])]
    if run["unavailable_lanes"]:
        lines.append(f"Unavailable lanes: {report_text(run['unavailable_lanes'])}.")
    for row in snapshot["targets"]:
        target = run["targets"][row["target"]]
        lines += ["", f"## {report_text(row['target'])}", ""]
        metadata = target["intake"]
        for field in ("url", "title", "author"):
            if field in metadata:
                lines.append(f"- {field}: {report_text(metadata[field])}")
        comparison = target["frozen"] or target["setup"]
        if comparison:
            for field in ("mode", "base_sha", "head_sha", "diff_base_sha", "source_root", "source_fingerprint", "inventory"):
                lines.append(f"- {field}: {report_text(comparison[field])}")
        else:
            lines.append("- Frozen comparison: not prepared.")
        lines.append(f"- Next / hold: {report_text(row['next'].get('code', row['next']['status']))}: {report_text(row['next'].get('reason', 'none'))}.")
        for gap in row["completion_gaps"]:
            lines.append(f"- Completion gap: {report_text(gap)}")
        for field in ("discussion_decision", "publication_discussion", "authorization", "coverage_evidence"):
            lines.append(f"- {field}: {report_text(target.get(field) or 'not recorded')}")
        lines.append(f"- Source observations: {report_text(target['integrity'] or 'not observed')}; resources removed: {target['resources_removed']}.")
        if target["plan"]:
            plan = target["plan"]
            lines += [f"- Boundaries: {report_text(plan['boundary_evidence'])}", f"- Selection: {report_text(plan['selection_evidence'])}",
                      f"- Coverage plan: {report_text(plan['coverage'])}", f"- Interaction plan: {report_text(plan['interactions'])}",
                      *report_table(("Profile", "Captured identity", "Decision / capability", "Evidence"),
                          [(name, report_identity(target["catalog"]["profiles"][name]["identity"]),
                            f"{decision['decision']} / {decision['capability']}", decision["evidence"]) for name, decision in plan["profiles"].items()])]
        if target["catalog"]:
            units = target["catalog"]["units"]
            lines.append(f"Measured patch: {len(units)} metadata/hunk units, {sum(unit['bytes'] for unit in units.values())} bytes across recorded patch artifacts.")
        assignments = target["assignments"]
        lines += report_table(("Assignment", "Kind / role / status", "Profile", "Requested identity", "Recorded actual identity", "Runtime handle", "Recovery"),
            [(key, f"{unit['kind']} / {unit['role']} / {unit['status']}", unit["profile"], report_identity(unit["identity"]),
              report_identity(unit["actual_identity"]), unit.get("runtime_handle") or "not recorded", unit.get("recovery") or "none") for key, unit in assignments.items()])
        for key, unit in assignments.items():
            lines += ["", f"### Assignment {report_text(key)}", "", f"- Scope: {report_text(unit['scope'])}; units: {report_text(unit['units'])}; interactions: {report_text(unit['interactions'])}.",
                      f"- Workload: {report_text(unit['workload'])}; tests planned: {report_text(unit['test_plan'])}."]
            for field in ("runtime_evidence", "runtime_copy", "runtime_outcome", "withdrawal", "superseded_by"):
                if unit.get(field):
                    lines.append(f"- {field}: {report_text(unit[field])}")
            if unit.get("rejected_report"):
                error = unit["rejected_report"]
                lines.append(f"- Rejected report: {report_text(error['code'])}: {report_text(error['reason'])}.")
            accepted = unit["report"]
            if not accepted:
                lines.append("- Accepted report: none; no checked coverage inferred.")
                continue
            for field in ("checked", "checked_units", "checked_interactions", "evidence", "source_status", "coverage_gaps", "pre_existing", "learned"):
                if field in accepted:
                    lines.append(f"- {field}: {report_text(accepted[field])}")
            lines += report_table(("Category", "Reported status", "Evidence"),
                [(name, result["status"], result["evidence"]) for name, result in accepted.get("categories", {}).items()])
            verification = accepted.get("verification")
            if verification:
                lines += report_table(("Command", "Test root", "Exit code", "Evidence"),
                    [tuple(result[field] for field in ("command", "root", "exit_code", "evidence")) for result in verification["results"]])
                if verification["not_run"]:
                    lines.append(f"Tests not run: {report_text(verification['not_run'])}.")
        retained = {key: item for key, item in target["adjudications"].items() if item["disposition"] in ("CONFIRMED", "ADJUSTED")}
        roots: dict[str, list[str]] = {}
        for key, item in retained.items():
            roots.setdefault(item["root_cause"], []).append(key)
        lines += ["", f"### Finding ledger ({len(target['findings'])} raw IDs; {len(roots)} retained adjudicated root causes)", "",
                  "Root-cause grouping uses recorded adjudications; raw claims and disagreements remain separate."]
        groups = [(root, min((retained[key]["severity"] for key in ids), key=SEVERITIES.index), sorted(ids)) for root, ids in roots.items()]
        groups.sort(key=lambda group: (SEVERITIES.index(group[1]), group[0]))
        lines += report_table(("Group", "Adjudicated root cause", "Highest retained severity", "Raw IDs"),
            [(f"F-{number:02d}" if row["derived_event"] is not None else "provisional", *group) for number, group in enumerate(groups, 1)])
        for key, raw in sorted(target["findings"].items(), key=lambda pair: (SEVERITIES.index(target["adjudications"].get(pair[0], pair[1])["severity"]), pair[0])):
            vet = target["vets"].get(key)
            final = target["adjudications"].get(key)
            vetter = assignments[vet["assignment_id"]]["actual_identity"] if vet else None
            lines += ["", f"#### {report_text(key)}", "",
                      f"- Original: {raw['severity']} / {raw['category']}; {report_text(raw['path'])}:{raw['line']} ({raw['side']}).",
                      f"- Finder: {report_text(report_identity(raw['finder']))}.",
                      f"- Trigger: {report_text(raw['trigger'])}; impact: {report_text(raw['impact'])}; suggested fix: {report_text(raw['fix'])}.",
                      f"- Original evidence: {report_text(raw['evidence'])}; references: {report_text(raw['references'])}.",
                      f"- Vet: {report_text(vet['verdict'] if vet else 'UNVETTED')}; vetter: {report_text(report_identity(vetter))}.",
                      f"- Adjudication: {report_text(final['disposition'] if final else 'UNADJUDICATED')}; adjudicator: {report_text(report_identity(final['adjudicator'] if final else None))}."]
            for label, decision in (("Vet", vet), ("Final", final)):
                if decision:
                    for field in ("severity", "category", "claim", "root_cause", "evidence", "references", "reason", "fix_assessment", "argument_assessment", "vet_assignment_id"):
                        if field in decision:
                            lines.append(f"- {label} {field}: {report_text(decision[field])}")
            for field in ("vet_history", "adjudication_history"):
                if target[field].get(key):
                    lines.append(f"- {field}: {report_text(target[field][key])}")
        for key, gap in target["coverage_gaps"].items():
            lines += ["", f"- Coverage gap {report_text(key)}: {report_text(gap)}"]
        posted = target["publication"]
        if posted:
            lines += ["", "### Publication", "", f"- Event: {posted['event']}; frozen head: {posted['commit_id']}.",
                      f"- Body: {report_text(posted['body']) if posted['body'] is not None else 'bodyless'}.",
                      f"- Receipt: {report_text(posted['receipt'] or 'not verified; reconcile without another POST')}."]
            if posted.get("outcome"):
                lines.append(f"- Response observation (not a verified receipt): {report_text(posted['outcome'])}.")
            if row["next"].get("superseded"):
                lines.append("The last recorded live comparison moved; this receipt does not review that newer comparison.")
    lines += ["", "Python gates the required tool path; Delta owns native 15/30 admission. Bypassed launches remain outside Python control.",
              "Fingerprints detect persistent observed writes; they do not prevent writes or detect restored changes.",
              "Recorded fields do not prove reasoning, served identity or natural tone. This report performs no fresh source, runtime or GitHub checks."]
    return "\n".join(lines) + "\n"


def next_decision(run: dict, target: dict) -> dict:
    """Identify required evidence or judgment without choosing it for the model.

    Args:
        run (dict): Shared review state.
        target (dict): Target being reported.

    Returns:
        dict: Precise next decision or completed local-comparison status.
    """
    def needs(code, reason, **details):
        return {"status": "needs_decision", "code": code, "reason": reason, **details}
    observations, integrity = integrity_failures(run)
    if observations:
        return needs("source_integrity", "preserve unexpected changes and account for the recorded integrity failure; create a new run", target_ids=observations)
    if target["resources_removed"]:
        return needs("resources_removed", "review resources were cleaned; create a new run for further work")
    if integrity:
        return needs("source_integrity", "account for changed/unknown source and preserve user work before further dispatch or publication", assignment_ids=integrity)
    pending = pending_launches(run)
    if pending:
        own = [assignment for key, assignment in pending if run["targets"][key] is target]
        return needs("native_dispatch" if own else "runtime_registration",
                     "dispatch only the issued task, obtain its native root and record started before another task or fresh scan; reconcile an unstarted launch with verified withdrawal",
                     assignment_ids=own, pending_launches=pending)
    if target["phase"] == "discussion":
        metadata = target["intake"]
        return needs("discussion_materiality", "evaluate changed or previously uncaptured discussion; reopen only for a supported finding addition, removal or severity change",
                     base_sha=metadata["base_sha"], head_sha=metadata["head_sha"],
                     discussion=metadata["discussion"], baseline=metadata.get("discussion_baseline"),
                     prior_review=metadata.get("prior_review"))
    if target["phase"] == "intake":
        return needs("prepare_source", "run prepare with the verified source checkout to pin comparison, patches and baselines")
    if target["phase"] == "setup":
        return needs("finalize_source", "finish selected index setup, then finalize the unchanged comparison; no assignments until finalization",
                     index_tools=target["index_setup"]["tools"] if target["index_setup"] else [])
    if target["catalog"] is None:
        return needs("review_catalog", "run catalog with the configured roster to inventory frozen metadata and hunks")
    if target["plan"] is None:
        if run["method"] == "pr-review-batch":
            key = f"{target['repository']}#{target['intake']['number']}"
            if not any(key in wave["allocations"] for wave in run["batch_waves"]):
                held = next((wave["held"][key] for wave in reversed(run["batch_waves"]) if key in wave["held"]), None)
                if held:
                    return needs(held["code"], held["reason"])
                return needs("batch_wave", "record discovery reviewer judgments and native capacity to allocate a batch wave")
        return needs("plan_assignments", "choose coherent boundaries, required family perspectives and model-selection reasons")
    if target["phase"] == "complete":
        if target["intake"]["classification"] == "self-authored-review":
            return {"status": "complete", "reason": "self-authored review is local; GitHub publication is disabled"}
        if "comparison" in target:
            return {"status": "complete", "reason": "local comparison has no GitHub PR publication target"}
        if not target["authorization"] or not target["authorization"]["submission"]:
            return needs("publication_authorization", "submission is not authorized for this PR")
        try:
            hold_publication_discussion(target)
        except NeedsDecision as error:
            return needs(error.code, str(error), discussion=target["publication_context"], baseline=target["intake"]["discussion"])
        return needs("publication_draft", "supply the final evidence-bound wording to publish; the command rechecks live GitHub state")
    queued = [key for key, row in target["assignments"].items() if row["status"] == "queued"]
    running = [key for key, row in target["assignments"].items() if row["status"] == "running"]
    if not target["assignments"]:
        return needs("assignment", "record workload-sized assignments for the selected finder")
    if queued:
        ready = [key for key in queued if target["assignments"][key].get("task_ready", {}).get("revision") == run["revision"]]
        if ready:
            return {"status": "ready", "code": "native_dispatch", "reason": "use the returned task for one native launch/follow-up, then immediately record started", "assignment_ids": ready}
        return needs("task_preflight", "run task with the current revision and fresh native capacity before each launch/follow-up", assignment_ids=queued)
    if running:
        uncertain = [key for key in running if target["assignments"][key].get("runtime_outcome", {}).get("status") == "unknown"]
        if uncertain:
            return needs("runtime_reconciliation", "resolve the exact native handles before report acceptance or retry; elapsed time is not terminal evidence", assignment_ids=uncertain)
        return needs("runtime_completion", "record terminal native runtime outcomes and original reports", assignment_ids=running)
    for key, row in target["assignments"].items():
        if row["status"] == "done" or row.get("superseded_by"):
            continue
        root = contract.unit_root(target, row)
        used = {item["recovery"]["kind"] for item in target["assignments"].values() if item.get("recovery", {}).get("unit_id") == root["id"]}
        outcome = row.get("runtime_outcome") or row.get("withdrawal")
        if (outcome or {}).get("cause", "incomplete-report") in ("incomplete-report", "evidence-gap") and (row.get("rejected_report") or row["status"] in ("partial", "blocked")):
            if "correction" in used:
                return needs("recovery_exhausted", "report correction was already requested once; retain missing fields/verdicts as incomplete coverage", assignment_ids=[key])
            return needs("report_correction", "request the one report correction through this selected runtime handle", assignment_ids=[key], runtime_handle=row["runtime_handle"],
                         error={name: row["rejected_report"][name] for name in ("code", "reason")} if row.get("rejected_report") else None)
        if not outcome:
            return needs("runtime_outcome", "record the terminal native cause; do not wait for a report from a failed/stopped task", assignment_ids=[key])
        if outcome["cause"] in ("transient", "auth", "quota", "rate"):
            if "retry" in used:
                return needs("recovery_exhausted", "this logical unit already used its one runtime retry; retain incomplete coverage", assignment_ids=[key])
            return needs("retry_route", "choose a verified route for the exact selected model/family outside unavailable lanes; no silent substitution", assignment_ids=[key], identity=root["identity"])
        return needs("runtime_rejection", "correct the recorded identity/role/source failure or retain incomplete coverage; identical retries are prohibited", assignment_ids=[key], cause=outcome["cause"])
    gaps = discovery_gaps(target)
    if gaps:
        return needs("discovery_coverage", "complete planned discovery before vetting or adjudication", gaps=gaps)
    unvetted = [key for key in target["findings"] if key not in target["vets"]]
    if unvetted:
        routes = {}
        for key in unvetted:
            try:
                profile = contract.vet_profile(target, target["findings"][key], run["method"], run["registry"])
            except NeedsDecision as error:
                return needs(error.code, str(error), finding_ids=[key])
            routes.setdefault(profile, []).append(key)
        return needs("cross_family_vet", "record each original/spillover finding's approved independent vet pass", finding_ids=unvetted, profiles=routes)
    unadjudicated = [key for key in target["findings"] if key not in target["adjudications"] or readjudication_allowed(target, key)]
    if unadjudicated:
        profile = target["plan"]["routing"]["stand_in"]
        if profile is not None:
            return needs("stand_in_adjudication", "incorporate completed stand-in judgments with actual provenance; do not adjudicate them again", finding_ids=unadjudicated, profile=profile)
        if any(target["findings"][key]["finder"]["family"] == run["orchestrator"]["family"] for key in unadjudicated):
            return needs("independent_adjudication", "orchestrator-originated findings need independent adjudication routing", finding_ids=unadjudicated)
        return needs("adjudication", "the third-family orchestrator must separately adjudicate every raw ID", finding_ids=unadjudicated)
    disputed = [key for key, row in target["adjudications"].items() if row["disposition"] in ("DISPUTED", "UNVERIFIABLE")
                and not any(unit.get("recovery", {}).get("kind") == "follow-up" and unit["recovery"]["finding_id"] == key for unit in target["assignments"].values())]
    if disputed:
        return needs("dispute_evidence", "identify concrete new evidence and both arguments for the one focused follow-up by the selected vetter; otherwise preserve the unresolved decision", finding_ids=disputed)
    gaps = completion_gaps(run, target)
    if gaps:
        return needs("incomplete_evidence", "record the missing coverage or resolve material evidence gaps; do not guess", gaps=gaps)
    return needs("completion_evidence", "reconcile recorded units, categories, interactions and every finding before completion")


def handoff_task(directory: Path, run: dict, key: str, assignment_id: str) -> dict:
    """Read the currently issued task without trusting a caller-supplied file.

    Args:
        directory (Path): Locked private run directory.
        run (dict): Replayed journal.
        key (str): Target key.
        assignment_id (str): Exact queued or running attempt.

    Returns:
        dict: Original verified task envelope and report template.

    Raises:
        ValueError: The binding, approval, artifact or assignment is stale.
        OSError: Sealed task bytes cannot be read.
    """
    target = run["targets"][key]
    row = target["assignments"][assignment_id]
    ready = row.get("task_ready", {})
    if (row["status"] not in ("queued", "running") or row.get("superseded_by")
            or target["resources_removed"] or "handoff" not in ready
            or (row["status"] == "queued" and ready["revision"] != run["revision"])):
        raise ValueError("file handoff requires the current queued approval or its running attempt")
    task = json.loads(handoff.read(directory, ready["handoff"]))
    template = task["report_template"]
    if (task["run_id"] != run["run_id"] or task["target_id"] != key or task["assignment_id"] != row["id"]
            or task["revision"] != ready["revision"] or template["assignment_id"] != row["id"]
            or template["target"] != target["frozen"] or template["reviewer"] != row["identity"]):
        raise ValueError("file handoff binding changed")
    return task


def issue_handoff(directory: Path, saved: dict, run: dict, key: str, assignment_id: str, capacity: dict) -> dict:
    """Persist the exact approved task and return a small native bootstrap.

    Args:
        directory (Path): Locked private run directory.
        saved (dict): Journal envelope.
        run (dict): Integrity-checked run.
        key (str): Target key.
        assignment_id (str): Queued assignment.
        capacity (dict): Fresh native capacity and registered parent evidence.

    Returns:
        dict: Native dispatch metadata and file-reading task text.

    Raises:
        NeedsDecision: Launch or byte-budget invariants are not satisfied.
        ValueError: Task metadata is invalid.
        OSError: Artifacts or journal cannot be durably persisted.
    """
    data = {"assignment_id": assignment_id, "capacity": capacity}
    approved = apply_event(run, {"target": key, "operation": "task-ready", "data": data}, directory=directory)
    task = contract.dispatch_task(approved, key, assignment_id, capacity, directory=directory)
    task["target_id"] = key
    data["handoff"] = handoff.store(directory, {"task.json": (json.dumps(task, ensure_ascii=True) + "\n").encode()})["task.json"]
    append_record(directory, saved, run, key, "task-ready", data)
    result = {name: value for name, value in task.items() if name not in ("task", "bootstrap", "report_template", "target_id")}
    result["handoff"] = data["handoff"]
    result["task"] = task["bootstrap"]
    return result


def author_report(directory: Path, saved: dict, run: dict, key: str, assignment_id: str, input_path: Path) -> dict:
    """Validate reviewer drafts before preserving one final report for receipt.

    Args:
        directory (Path): Locked private run directory.
        saved (dict): Journal envelope.
        run (dict): Replayed state with independently recorded runtime identity.
        key (str): Target key.
        assignment_id (str): Running attempt, authored once.
        input_path (Path): Path to the reviewer's original UTF-8 judgment document.

    Returns:
        dict: Sealed final receipt, or preserved draft diagnostics and exact
            expected coverage with no final submission or correction consumed.

    Raises:
        ValueError: The attempt is stale, already authored, has wrong identity,
            or does not name a readable judgment JSON file.
        OSError: Private persistence is unavailable.
    """
    task = handoff_task(directory, run, key, assignment_id)
    row = run["targets"][key]["assignments"][assignment_id]
    if row["status"] != "running" or row.get("authored_report") or row["actual_identity"] != task["report_template"]["reviewer"]:
        raise ValueError("author-report requires its running native identity and one original input")
    try:
        original = input_path.read_bytes()
    except OSError as error:
        raise ValueError("author-report --input requires a readable judgment JSON file path; inline JSON is not supported") from error
    data = {"assignment_id": assignment_id, "task_sha256": row["task_ready"]["handoff"]["sha256"]}
    errors = []
    try:
        payload, _ = json.JSONDecoder().raw_decode(task["task"].split("\n", 1)[1])
        reply = handoff.assemble(task["report_template"], json.loads(original), originals=payload.get("retained_findings"))
        report_bytes = (json.dumps(reply, ensure_ascii=True) + "\n").encode()
    except (ValueError, UnicodeError) as error:
        report_bytes = original
        data["assembly_error"] = str(error)
        errors.append({"field": "input", "code": "invalid_report", "reason": str(error)})
    else:
        accept_report(run["targets"][key], row, reply, errors=errors)
        with contract.report_check(errors, "status"):
            outcome = row.get("runtime_outcome", {})
            if outcome.get("status") == "unknown":
                raise NeedsDecision("runtime_reconciliation", "reconcile the native outcome before sealing a report")
            if outcome.get("cause", "incomplete-report") != "incomplete-report" and reply.get("status") == "done":
                raise ValueError("a done report cannot erase the recorded native failure")
        with contract.report_check(errors, "source_status"):
            observations = run["targets"][key]["integrity"]
            if observations and reply.get("source_status") != observations[-1]["status"]:
                raise ValueError("report source status must match the actual baseline check")
    proofs = handoff.store(directory, {"input.json": original, "report.json": report_bytes})
    data.update(input=proofs["input.json"], report=proofs["report.json"])
    if errors:
        data["errors"] = errors
        append_record(directory, saved, run, key, "report-draft", data)
        return dict(data, status="needs_decision", code="report_draft", expected_coverage={
            "checked": row["scope"], "checked_units": row["units"], "checked_interactions": row["interactions"],
            "finding_ids": row.get("finding_ids", [])})
    append_record(directory, saved, run, key, "report-authored", data)
    return data


def read_json(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def write_json(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=".review-record-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(data, output, indent=2, ensure_ascii=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_run(path):
    saved = read_json(path)
    if not isinstance(saved, dict) or saved.get("version") != 1:
        raise ValueError("unsupported run record")
    fields(saved, ("initial", "events", "version"))
    initial = saved["initial"]
    fields(initial, ("method", "intake", "registry", "orchestrator", "thread_id", "run_id"), ("review_own_pr", "admission_mode"))
    run = new_run(initial["method"], initial["intake"], initial["registry"], initial["orchestrator"], initial["thread_id"],
                  review_own_pr=initial.get("review_own_pr", False), admission_mode=initial.get("admission_mode"))
    run["run_id"] = stable_id(initial["run_id"])
    if not isinstance(saved["events"], list):
        raise ValueError("events must be a list")  # noqa: TRY004 - validation errors retain the ValueError contract.
    for event in saved["events"]:
        run = apply_event(run, event, replay=True)
    return saved, run


def append_record(directory: Path, saved: dict, run: dict, key: str | None, operation: str, data: dict) -> dict:
    """Validate and atomically persist one generated event under the caller's lock.

    Args:
        directory (Path): Locked private run directory.
        saved (dict): Persisted journal envelope, updated after successful validation.
        run (dict): Current replayed state.
        key (str | None): Target key, or null for a batch wave.
        operation (str): Typed transition.
        data (dict): Generated evidence.

    Returns:
        dict: Updated run state.

    Raises:
        ValueError: The transition is invalid.
        NeedsDecision: A required invariant is missing.
        OSError: Atomic persistence failed.
    """
    event = {"target": key, "operation": operation, "data": data}
    updated = apply_event(run, event, directory=directory)
    saved["events"].append(event)
    write_json(directory / "run.json", saved)
    return updated


def runtime_copies(run: dict, extra: dict | None = None) -> tuple[dict, ...]:
    """Collect immutable native-copy evidence across participating targets.

    Args:
        run (dict): Replayed run with actual assignment provenance.
        extra (dict | None): Generated copy being checked before its start is saved.

    Returns:
        tuple[dict, ...]: Unique copy ledgers, including completed participants.

    Raises:
        ValueError: Two records give conflicting baselines for one root.
    """
    copies = {}
    records = [row["runtime_copy"] for target in run["targets"].values()
               for row in target["assignments"].values() if "runtime_copy" in row]
    for proof in records + ([extra] if extra is not None else []):
        if proof["root"] in copies and copies[proof["root"]] != proof:
            raise ValueError("native copy baselines are immutable")
        copies[proof["root"]] = proof
    return tuple(copies.values())


def parent_ledgers(directory: Path, run: dict) -> dict[str, dict]:
    """Collect recorded parent evidence without assuming the native project's repo.

    Args:
        directory (Path): Private run directory.
        run (dict): Replayed source and participating-copy records.

    Returns:
        dict[str, dict]: Canonical roots mapped to original validated ledgers.

    Raises:
        ValueError: Parent evidence changed or two records disagree.
        OSError: A private ledger cannot be read.
    """
    parents = {}
    for key, target in run["targets"].items():
        frozen = target["frozen"] or target["setup"]
        if frozen and not target["resources_removed"] and "preparation" in frozen:
            root = frozen["parent_root"]
            if root in parents:
                if preparation.fingerprint(parents[root]) != frozen["parent_fingerprint"]:
                    raise ValueError("protected parent records disagree")
            else:
                resource = preparation.owned(directory, frozen["preparation"]["resource"], run["run_id"], key)
                path = resource / "parent-baseline.json"
                if path.is_symlink() or preparation.digest(path) != frozen["preparation"]["artifacts"][path.name]:
                    raise ValueError("parent baseline evidence changed")
                ledger = json.loads(path.read_text())
                if preparation.fingerprint(ledger) != frozen["parent_fingerprint"]:
                    raise ValueError("parent ledger disagrees with frozen fingerprint")
                parents[frozen["parent_root"]] = ledger
        for record in target["baselines"].values():
            if "artifact" in record:
                ledger = preparation.read_baseline(directory, record)
                if record["root"] in parents and parents[record["root"]] != ledger:
                    raise ValueError("protected parent records disagree")
                parents[record["root"]] = ledger
    return parents


def check_sources(directory: Path, saved: dict, run: dict, stop: bool = True,
                  record_unchanged: bool = True, runtime_copy: dict | None = None) -> dict:
    """Persist actual baseline observations before accepting dependent records.

    Args:
        directory (Path): Locked private run directory.
        saved (dict): Journal envelope.
        run (dict): Current replayed state.
        stop (bool): Hold immediately on any changed or unreadable source.
        record_unchanged (bool): Journal healthy observations; read-only diff access
            skips these so it preserves the issued launch approval.
        runtime_copy (dict | None): Validated native-start copy awaiting persistence.

    Returns:
        dict: State with original integrity observations retained.

    Raises:
        NeedsDecision: Preparation is missing or observed integrity is unsafe.
        ValueError: An observation is invalid.
        OSError: Journal persistence failed.
    """
    observations: dict = {}
    baselines = {key + "/" + name: record for key, target in run["targets"].items()
                 for name, record in target["baselines"].items()}
    for key, target in list(run["targets"].items()):
        comparison = target["frozen"] or target["setup"]
        if comparison is None or target["resources_removed"]:
            continue
        if "preparation" not in comparison:
            raise NeedsDecision("prepare_source", "recorded freeze evidence lacks owned preparation resources; create and prepare a new run")
        observation = preparation.check(directory, run["run_id"], key, comparison, baselines,
                                        setup=target["index_setup"] if target["frozen"] is None else None,
                                        observations=observations, native_copies=runtime_copies(run, runtime_copy))
        if record_unchanged or observation["status"] != "unchanged":
            run = append_record(directory, saved, run, key, "integrity", observation)
    failures, _ = integrity_failures(run)
    if stop and failures:
        raise NeedsDecision("source_integrity", "changed/unknown source evidence recorded; preserve user work and create a new run")
    return run


def local_collection(args: argparse.Namespace) -> dict:
    """Translate explicit CLI comparison semantics into a replayable local request.

    Args:
        args (argparse.Namespace): Parsed creation arguments.

    Returns:
        dict: Validated local comparison request.

    Raises:
        ValueError: Flags conflict or the range lacks explicit endpoints.
    """
    if args.method != "pr-review" or args.pr:
        raise ValueError("local comparisons require pr-review without --pr")
    if args.range:
        match = re.fullmatch(r"(.+?)(\.\.\.?)(.+)", args.range)
        if not match or args.base or args.untracked:
            raise ValueError("range requires two explicit endpoints and no --base/--untracked")
        left, separator, right = match.groups()
        mode = "range-three-dot" if separator == "..." else "range-two-dot"
    elif args.branch:
        if args.untracked:
            raise ValueError("--untracked requires --working")
        mode, left, right = "branch-merge-base", args.base, args.branch
    elif args.working:
        if args.base:
            raise ValueError("working changes compare against HEAD")
        mode, left, right = "working", None, "HEAD"
    else:
        raise ValueError("local source requires --range, --branch or --working")
    return {"local": {"source_root": str(Path(args.source).resolve()),
                      "comparison": {"mode": mode, "left": left, "right": right, "untracked": args.untracked or []}}}


def publish_command(directory: Path, saved: dict, run: dict, key: str, command: str,
                    draft: dict | None) -> dict:
    """Execute publication under the journal lock, persisting before every POST.

    Args:
        directory (Path): Private run directory held under an exclusive lock.
        saved (dict): Persisted journal envelope.
        run (dict): Replayed state.
        key (str): Exact PR target key.
        command (str): Preflight, publish or reconcile.
        draft (dict | None): Final prose for a first publish, absent otherwise.

    Returns:
        dict: State with live observations or a verified receipt.

    Raises:
        NeedsDecision: Authorization, discussion, coverage, source or receipt is unresolved.
        ValueError: Target state or draft is invalid.
        OSError: Persistence or GitHub access failed.
    """
    choice(command, ("preflight", "publish", "reconcile"), "publication command")
    target = run["targets"][key]
    if target["intake"]["classification"] == "self-authored-review":
        raise NeedsDecision("review_only", "self-authored reviews remain local; GitHub publication is disabled")
    publication.endpoint(target)
    if command == "reconcile":
        require_phase(target, "publication-pending")
        run = check_sources(directory, saved, run, stop=False)
    else:
        require_phase(target, "complete")
        gaps = completion_gaps(run, target)
        if gaps:
            raise NeedsDecision("incomplete_review", "; ".join(gaps))
        if command == "publish" and (not target["authorization"] or not target["authorization"]["submission"]):
            raise NeedsDecision("publication_authorization", "submission is not authorized for this PR")
        body = None
        if command == "publish" or draft is not None:
            if not isinstance(draft, dict):
                raise NeedsDecision("publication_draft", "supply evidence-bound review prose before publishing")
            body = publication.render_body(target, draft)
        run = check_sources(directory, saved, run)
        context = publication.live_context(run, run["targets"][key])
        run = append_record(directory, saved, run, key, "publication-context", context)
        if command == "preflight":
            return run
        hold_publication_discussion(run["targets"][key])
        run = check_sources(directory, saved, run)
        publication.authenticated_owner(run, context["hostname"])
        data = {**{name: context[name] for name in ("base_sha", "head_sha", "state")}, "body": body,
                "draft": draft, "authorization_evidence": run["targets"][key]["authorization"]["evidence"]}
        run = append_record(directory, saved, run, key, "publication-intent", data)
        outcome = publication.submit(directory, run["targets"][key])
        run = append_record(directory, saved, run, key, "publication-outcome", outcome)
    observation = publication.reconcile(run, run["targets"][key])
    run = append_record(directory, saved, run, key, "publication-reconciliation", observation)
    if observation["receipt"] is None:
        code = "publication_ambiguous" if len(observation["matching_ids"]) > 1 else "publication_uncertain"
        raise NeedsDecision(code, "all review pages read without one unique verified receipt; hold without another POST")
    return append_record(directory, saved, run, key, "publication-receipt", observation["receipt"])


def main() -> int:
    """Execute locked workflow commands; only publish can send a GitHub review.

    Returns:
        int: Zero for success, two for a precise hold, one for invalid input.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="new private run directory outside every checkout")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--method", required=True, choices=METHODS)
    create.add_argument("--thread-id", required=True)
    create.add_argument("--registry", required=True)
    create.add_argument("--orchestrator", required=True, help="sanitized provider/model identity JSON file")
    scope = create.add_mutually_exclusive_group(required=True)
    scope.add_argument("--repo")
    scope.add_argument("--org")
    scope.add_argument("--source", help="local comparison checkout; PR preparation uses prepare --source")
    create.add_argument("--pr", type=int, nargs="+")
    create.add_argument("--review-own-pr", action="store_true", help="review explicitly named self-authored PRs locally; no publication")
    create.add_argument("--admission-mode", choices=ADMISSION_MODES, default="observed",
                        help="observed requires live counts; native records unavailable counts and uses Delta admission")
    create.add_argument("--previous-run", help="previous private run directory; reuse evaluated discussion and finding history")
    comparison = create.add_mutually_exclusive_group()
    comparison.add_argument("--range", help="explicit LEFT..RIGHT or LEFT...RIGHT")
    comparison.add_argument("--branch")
    comparison.add_argument("--working", action="store_true")
    create.add_argument("--base", help="explicit branch base; otherwise require origin/HEAD")
    create.add_argument("--untracked", nargs="*", help="exact untracked file paths included in working review")
    record = commands.add_parser("record")
    record.add_argument("--event", required=True, help="one typed event JSON file")
    record.add_argument("--revision", type=int, required=True, help="expected revision; reject stale writers")
    commands.add_parser("status")
    native_parent = commands.add_parser("native-parent", help="capture this native parent terminal's canonical cwd without a caller-supplied root")
    native_parent.add_argument("--revision", type=int, required=True)
    commands.add_parser("report", help="render recorded status and completion evidence as Markdown; no live checks")
    diff = commands.add_parser("diff", help="emit the frozen comparison through preparation's isolated Git configuration")
    diff.add_argument("--target", required=True)
    task = commands.add_parser("task", help="gate one native launch/follow-up and return its task; does not dispatch")
    task.add_argument("--target", required=True)
    task.add_argument("--assignment", required=True)
    task.add_argument("--capacity", help="fresh native active counts/evidence JSON; omit in explicit native admission mode")
    task.add_argument("--revision", type=int, required=True)
    for name in ("task-read", "author-report", "receive-report"):
        command = commands.add_parser(name, help="verified file-backed native task/report handoff")
        command.add_argument("--target", required=True)
        command.add_argument("--assignment", required=True)
        if name == "task-read":
            command.add_argument("--section", help="sealed evidence key/subkey/list-index; omit to read assignment controls")
            command.add_argument("--offset", type=int, default=0, help="byte offset in section ASCII JSON")
            command.add_argument("--length", type=int, default=8192, help="evidence page bytes, 1..32768")
        if name == "author-report":
            command.add_argument("--input", required=True, help="path to a reviewer-authored UTF-8 judgment JSON file; omit reserved binding fields")
        if name == "receive-report":
            command.add_argument("--runtime-handle", required=True)
            command.add_argument("--evidence", required=True, help="native completion and authorship evidence")
            command.add_argument("--revision", type=int, required=True)
    commands.add_parser("check", help="record actual source/parent/copy integrity, retaining failures")
    for name in ("preflight", "publish", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("--target", required=True)
        command.add_argument("--revision", type=int, required=True)
        if name in ("preflight", "publish"):
            command.add_argument("--draft", required=name == "publish", help="evidence-bound review prose JSON; preflight previews without posting")
    for name in ("prepare", "setup", "finalize", "baseline", "test-copy", "cleanup", "catalog"):
        command = commands.add_parser(name)
        command.add_argument("--target", required=True, help="owner/repo#number, or local")
        command.add_argument("--revision", type=int, required=True)
        if name == "catalog":
            command.add_argument("--roster", required=True, help="generated machine roster.md")
        if name == "prepare":
            command.add_argument("--source", help="verified checkout root; local runs reuse their explicit source")
        if name == "baseline":
            command.add_argument("--id", required=True, help="stable participating-copy ID")
            command.add_argument("--root", required=True, help="actual parent/spawn checkout root")
        if name == "setup":
            command.add_argument("--tools", choices=preparation.INDEX_TOOLS, required=True, nargs="+")
        if name == "finalize":
            command.add_argument("--static-fallback", choices=preparation.INDEX_TOOLS, nargs="*", default=[])
            command.add_argument("--evidence", help="why static source inspection covers failed/unavailable indexes")
    args = parser.parse_args()
    directory = Path(args.run).absolute()
    try:
        if args.command == "create":
            if args.review_own_pr and (not args.repo or not args.pr):
                raise ValueError("--review-own-pr requires --repo and explicit --pr")
            if directory.parent.resolve() != directory.parent or directory.exists() or directory.is_symlink():
                raise ValueError("run must be a new directory without symlink parents")
            checkout = subprocess.run(["rtk", "proxy", "git", "-C", str(directory.parent), "rev-parse", "--show-toplevel"], capture_output=True, check=False)
            if checkout.returncode == 0:
                raise ValueError("run records must stay outside Git checkouts")
            if args.org and args.pr:
                raise ValueError("--pr requires --repo")
            if args.source:
                if args.previous_run:
                    raise ValueError("--previous-run applies only to PR intake")
                collection = local_collection(args)
            else:
                if args.range or args.branch or args.working or args.base or args.untracked is not None:
                    raise ValueError("comparison flags require --source")
                baselines = discussion_baselines(load_run(Path(args.previous_run) / "run.json")[1]) if args.previous_run else None
                collection = (intake.organization_intake(args.org, baselines) if args.org else
                              intake.intake(args.repo, args.pr, baselines=baselines, review_own_pr=args.review_own_pr))
            initial = {"method": args.method, "intake": collection, "registry": read_json(args.registry),
                       "orchestrator": read_json(args.orchestrator), "thread_id": args.thread_id,
                       "admission_mode": args.admission_mode}
            if args.review_own_pr:
                initial["review_own_pr"] = True
            run = new_run(
                initial["method"], collection, initial["registry"], initial["orchestrator"], initial["thread_id"],
                review_own_pr=args.review_own_pr, admission_mode=args.admission_mode)
            initial["run_id"] = run["run_id"]
            directory.mkdir(mode=0o700, parents=False)
            write_json(directory / "run.json", {"version": 1, "initial": initial, "events": []})
        else:
            if directory.resolve() != directory or not directory.is_dir() or (directory / "run.json").is_symlink():
                raise ValueError("run must be an existing private directory without symlinks")
            descriptor = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                saved, run = load_run(directory / "run.json")
                if hasattr(args, "revision") and args.revision != run["revision"]:
                    raise ValueError(f"stale revision; current revision is {run['revision']}")
                event = fields(read_json(args.event), ("target", "operation", "data")) if args.command == "record" else None
                if args.command not in ("status", "report"):
                    settling = ((event["target"], event["data"].get("assignment_id")) if event is not None
                                and event["operation"] in ("started", "withdraw") and isinstance(event["data"], dict) else None)
                    require_registered_launch(run, settling)
                if event is not None:
                    if event["operation"] in ("native-parent", "prepare", "index-setup", "baseline", "integrity", "test-copy", "cleanup", "catalog", "task-ready", "report-draft", "report-authored", "publication-context", "publication-intent", "publication-outcome", "publication-reconciliation", "publication-receipt") or (event["operation"] == "freeze" and "preparation" in event["data"]):
                        raise ValueError("generated preparation/task/publication evidence requires its dedicated command")
                    if event["operation"] == "started":
                        row = run["targets"][event["target"]]["assignments"].get(event["data"].get("assignment_id"), {})
                        if row.get("task_ready", {}).get("revision") != run["revision"]:
                            raise NeedsDecision("task_preflight", "run task at the current revision before native launch/follow-up; record started immediately afterward")
                        data = event["data"]
                        approved_parent = row["task_ready"]["capacity"].get("runtime_parent_root")
                        if approved_parent is None:
                            raise NeedsDecision("runtime_parent", "run task with the actual native parent before launch/follow-up")
                        if data.get("runtime_parent_root", approved_parent) != approved_parent:
                            raise ValueError("native start must match the approved native parent")
                        if "runtime_copy" in data:
                            raise ValueError("native copy evidence is generated from runtime_root, not caller-supplied")
                        prior = next((unit["runtime_copy"] for unit in run["targets"][event["target"]]["assignments"].values()
                                      if unit.get("runtime_handle") == data.get("runtime_handle") and "runtime_copy" in unit), None)
                        if "runtime_root" not in data and prior:
                            data["runtime_root"] = prior["root"]
                        profile = run["targets"][event["target"]]["catalog"]["profiles"].get(row.get("profile"), {})
                        if profile.get("worktree") == "isolated" and "runtime_root" not in data:
                            raise NeedsDecision("runtime_copy", "supply the actual canonical runtime_root from this native handle's checkout metadata")
                        if "runtime_root" in data:
                            data["runtime_copy"] = prior or preparation.capture_runtime_copy(
                                directory, run["run_id"], event["target"], run["targets"][event["target"]]["frozen"],
                                Path(data["runtime_root"]), runtime_copies(run), parent_ledgers(directory, run), approved_parent)
                        if "handoff" in row["task_ready"]:
                            handoff_task(directory, run, event["target"], row["id"])
                        apply_event(run, event)
                    if event["operation"] in ("batch-wave", "assignment", "recover", "started", "withdraw", "runtime-outcome", "report-input", "report", "adjudication-report", "orchestrator-finding", "complete", "publication-intent"):
                        report_operation = event["operation"] in ("report-input", "report", "adjudication-report")
                        options = {"runtime_copy": event["data"]["runtime_copy"]} if event["operation"] == "started" and "runtime_copy" in event["data"] else {}
                        run = check_sources(directory, saved, run, stop=not (report_operation or event["operation"] in ("runtime-outcome", "withdraw")), **options)
                        if report_operation:
                            target = run["targets"][event["target"]]
                            if event["data"]["source_status"] != target["integrity"][-1]["status"]:
                                raise ValueError("report source status must match the actual baseline check")
                    run = append_record(directory, saved, run, event["target"], event["operation"], event["data"])
                elif args.command == "native-parent":
                    run = append_record(directory, saved, run, None, "native-parent", {
                        "root": str(Path.cwd().resolve(strict=True)), "thread_id": run["thread_id"], "source": "terminal-cwd"})
                elif args.command == "task":
                    run = check_sources(directory, saved, run)
                    if args.capacity:
                        capacity = read_json(args.capacity)
                    elif run.get("admission_mode") == "native":
                        capacity = {"parent_active": None, "process_active": None,
                                    "evidence": "Live counts unavailable; explicit native admission uses Delta's unchanged 15/30 limits.",
                                    "runtime_parent_root": (run.get("native_parent") or {}).get("root")}
                    else:
                        raise NeedsDecision("native_capacity", "observed admission requires fresh --capacity counts and evidence")
                    print(json.dumps(issue_handoff(directory, saved, run, args.target, args.assignment, capacity), indent=2))
                    return 0
                elif args.command in ("task-read", "author-report", "receive-report"):
                    key = args.target
                    row = run["targets"][key]["assignments"][args.assignment]
                    if args.command == "receive-report":
                        proof = row.get("authored_report")
                        if (not proof or args.runtime_handle != row.get("runtime_handle") or row.get("superseded_by")
                                or row.get("report") or row.get("rejected_report")):
                            raise ValueError("receive-report requires its exact original native handle and unreceived report")
                        handoff.read(directory, row["task_ready"]["handoff"])
                        handoff.read(directory, proof["input"])
                        raw = handoff.read(directory, proof["report"])
                        try:
                            original = raw.decode("utf-8")
                        except UnicodeError:
                            original = None
                        run = check_sources(directory, saved, run, stop=False)
                        run = append_record(directory, saved, run, key, "report-input", {
                            "assignment_id": args.assignment, "runtime_handle": args.runtime_handle,
                            "target": run["targets"][key]["frozen"], "report": original, "evidence": args.evidence,
                            "source_status": run["targets"][key]["integrity"][-1]["status"]})
                    else:
                        # Native clone creation is expected only after started has
                        # registered its exact evidence. Never fingerprint that
                        # pending clone as an unregistered source mutation.
                        handoff_task(directory, run, key, args.assignment)
                        if row["status"] != "running":
                            raise NeedsDecision("runtime_registration", "send the parent your native pwd -P result and wait for started registration before reading or authoring")
                        run = check_sources(directory, saved, run, stop=args.command == "task-read", record_unchanged=False)
                        if args.command == "task-read":
                            command = (f"rtk proxy python3 {shlex.quote(str(Path(__file__).absolute()))} --run {shlex.quote(str(directory))}"
                                       f" task-read --target {shlex.quote(key)} --assignment {shlex.quote(args.assignment)}")
                            print(handoff.task_view(handoff_task(directory, run, key, args.assignment), command,
                                                   section=args.section, offset=args.offset, length=args.length))
                        else:
                            result = author_report(directory, saved, run, key, args.assignment, Path(args.input))
                            print(json.dumps(result, indent=2))
                            return 2 if result.get("code") == "report_draft" else 0
                        return 0
                elif args.command == "check":
                    run = check_sources(directory, saved, run)
                elif args.command not in ("status", "report"):
                    key = args.target
                    if key not in run["targets"]:
                        raise ValueError("unknown target")
                    target = run["targets"][key]
                    if args.command == "diff":
                        if target["frozen"] is None or target["resources_removed"]:
                            raise NeedsDecision("prepare_source", "diff requires existing frozen preparation resources")
                        run = check_sources(directory, saved, run, record_unchanged=False)
                        frozen = target["frozen"]
                        output = preparation.git(Path(frozen["source_root"]), "diff", *preparation.DIFF_OPTIONS,
                                                 *preparation.PATCH_OPTIONS, frozen["diff_base_sha"], frozen["head_sha"], "--")
                        sys.stdout.buffer.write(output)
                        return 0
                    if args.command in ("preflight", "publish", "reconcile"):
                        run = publish_command(directory, saved, run, key, args.command,
                                              read_json(args.draft) if getattr(args, "draft", None) else None)
                        if args.command == "preflight" and args.draft:
                            print(json.dumps({"status": status(run), "review": {"event": review_event(run["targets"][key]),
                                              "body": publication.render_body(run["targets"][key], read_json(args.draft))}}, indent=2))
                            return 0
                    elif args.command == "catalog":
                        require_phase(target, "prepared")
                        run = check_sources(directory, saved, run)
                        data = contract.catalog(target["frozen"], Path(args.roster), run["registry"])
                        run = append_record(directory, saved, run, key, "catalog", data)
                    elif args.command == "prepare":
                        require_phase(target, "intake")
                        source_arg = args.source or run["intake"].get("local", {}).get("source_root")
                        if not source_arg:
                            raise NeedsDecision("source_checkout", "supply the verified source checkout with --source")
                        parent = Path(source_arg).resolve()
                        if "comparison" in target and str(parent) != target["repository"]:
                            raise ValueError("local source differs from the recorded comparison")
                        frozen = preparation.prepare(directory, run["run_id"], key, target, parent, runtime_copies(run))
                        run = append_record(directory, saved, run, key, "prepare", frozen)
                    elif args.command == "setup":
                        require_phase(target, "setup")
                        if target["resources_removed"]:
                            raise NeedsDecision("resources_removed", "review resources were cleaned; create a new run")
                        run = check_sources(directory, saved, run)
                        request = target["index_setup"]
                        if request is None:
                            request = preparation.setup_request(directory, run["run_id"], key, target["setup"], args.tools,
                                                                baselines=target["baselines"], native_copies=runtime_copies(run))
                            run = append_record(directory, saved, run, key, "index-setup", request)
                        elif args.tools != request["tools"]:
                            raise ValueError("setup retry must retain its recorded tools and intent")
                        preparation.run_setup(target["setup"], request)
                    elif args.command == "finalize":
                        require_phase(target, "setup")
                        if target["resources_removed"]:
                            raise NeedsDecision("resources_removed", "review resources were cleaned; create a new run")
                        run = check_sources(directory, saved, run)
                        try:
                            frozen = preparation.finalize(directory, run["run_id"], key, target["setup"],
                                                          target["index_setup"], args.static_fallback, args.evidence,
                                                          baselines=target["baselines"], native_copies=runtime_copies(run))
                        except NeedsDecision as error:
                            if error.observation is not None:
                                append_record(directory, saved, run, key, "integrity", error.observation)
                            raise
                        run = append_record(directory, saved, run, key, "freeze", frozen)
                    elif args.command == "baseline":
                        stable_id(args.id)
                        if target["frozen"] is None or target["resources_removed"] or args.id in target["baselines"]:
                            raise ValueError("baseline requires prepared resources and a new copy ID")
                        root = Path(args.root).absolute()
                        data = preparation.capture_baseline(directory, args.id, root, runtime_copies(run))
                        run = append_record(directory, saved, run, key, "baseline", data)
                    elif args.command == "test-copy":
                        if target["frozen"] is None or target["resources_removed"]:
                            raise NeedsDecision("prepare_source", "test copies require existing frozen preparation resources")
                        if not target["authorization"] or not target["authorization"]["test_execution"]:
                            raise NeedsDecision("test_authorization", "test execution requires explicit recorded authorization")
                        run = check_sources(directory, saved, run)
                        root = preparation.test_copy(directory, run["run_id"], key, target["frozen"])
                        run = append_record(directory, saved, run, key, "test-copy", {"root": str(root)})
                    elif args.command == "cleanup":
                        if any(row["status"] in ("queued", "running") for row in target["assignments"].values()):
                            raise NeedsDecision("active_assignments", "settle active assignments before cleaning review resources")
                        comparison = target["frozen"] or target["setup"]
                        removed = preparation.cleanup(directory, run["run_id"], key, target["index_setup"],
                                                      comparison["preparation"]["resource"] if comparison else None)
                        run = append_record(directory, saved, run, key, "cleanup", {"resources": removed})
        if args.command == "report":
            print(report(run), end="")
        else:
            print(json.dumps(status(run), indent=2))
        return 0
    except NeedsDecision as error:
        print(json.dumps({"status": "needs_decision", "code": error.code, "reason": str(error)}))
        return 2
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else "invalid or unreadable run input"
        print(json.dumps({"status": "invalid", "reason": message, "error_type": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
