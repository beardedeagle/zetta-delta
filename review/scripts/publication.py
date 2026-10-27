"""Frozen-head review payloads, live GitHub checks and receipt reconciliation."""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import intake
from review_contract import NeedsDecision, decisions, evidence, strings


def endpoint(target: dict) -> tuple[str, str]:
    """Bind requests to the captured PR's host, repository and number.

    Args:
        target (dict): Validated PR target.

    Returns:
        tuple[str, str]: Hostname and relative pull endpoint.

    Raises:
        NeedsDecision: Local comparisons cannot publish GitHub reviews.
        ValueError: The intake URL does not identify the recorded PR.
    """
    if "project" in target:
        raise NeedsDecision("local_publication", "project reviews have no GitHub publication target")
    if "comparison" in target:
        raise NeedsDecision("local_publication", "local comparison has no GitHub PR publication target")
    return intake.pr_endpoint(target["repository"], target["intake"]["number"], target["intake"]["url"])


def pull_binding(target: dict, pull: dict) -> dict:
    """Validate a live PR and extract its comparison endpoints.

    Args:
        target (dict): Frozen PR target.
        pull (dict): Live REST response.

    Returns:
        dict: PR state and base/head SHAs.

    Raises:
        ValueError: GitHub returned a different or malformed PR.
    """
    if pull.get("number") != target["intake"]["number"] or pull.get("html_url") != target["intake"]["url"]:
        raise ValueError("live PR does not match the captured target")
    return {"state": intake.text(pull.get("state"), "PR state"),
            "base_sha": intake.sha(pull["base"]["sha"], "PR base"),
            "head_sha": intake.sha(pull["head"]["sha"], "PR head")}


def authenticated_owner(run: dict, hostname: str) -> None:
    """Require the invoking GitHub account captured during intake.

    Args:
        run (dict): Validated run with its invoking account.
        hostname (str): Captured GitHub host.

    Raises:
        NeedsDecision: Authentication now identifies another account.
        ValueError: The authenticated account lookup is invalid.
        OSError: GitHub could not be queried.
    """
    user = intake.api("user", hostname=hostname)
    if intake.github_login(user.get("login"), "authenticated owner").casefold() != run["owner"].casefold():
        raise NeedsDecision("publication_account", "authenticated GitHub account differs from the invoking owner")


def live_context(run: dict, target: dict) -> dict:
    """Read complete discussion and bracket it with live comparison checks.

    Args:
        run (dict): Validated run.
        target (dict): Completed frozen PR target.

    Returns:
        dict: Snapshot, complete review IDs and comparison/account evidence.

    Raises:
        NeedsDecision: Account, PR comparison, author or concurrent owner review changed.
        ValueError: A response is incomplete or inconsistent.
        OSError: GitHub could not be queried.
    """
    hostname, path = endpoint(target)
    authenticated_owner(run, hostname)
    pull = intake.api(path, hostname=hostname)
    binding = pull_binding(target, pull)
    if binding["state"] != "open" or any(binding[key] != target["frozen"][key] for key in ("base_sha", "head_sha")):
        raise NeedsDecision("comparison_changed", "live PR closed or comparison moved; publication held")
    user = pull.get("user")
    if not isinstance(user, dict):
        raise ValueError("live PR lacks author metadata; publication held")  # noqa: TRY004 - retain external-response validation contract.
    author = intake.github_login(user.get("login"), "PR author")
    if author.casefold() == run["owner"].casefold() or author.casefold() != target["intake"]["author"].casefold():
        raise NeedsDecision("publication_author", "PR author differs from the eligible intake target")
    reviews = intake.api(path + "/reviews?per_page=100", paginate=True, hostname=hostname)
    comments = intake.api(f"repos/{target['repository']}/issues/{pull['number']}/comments?per_page=100", paginate=True, hostname=hostname)
    inline = intake.api(path + "/comments?per_page=100", paginate=True, hostname=hostname)
    discussion = intake.discussion_record(pull, reviews, comments, inline)
    ids = [intake.positive(row["id"], "review ID") for row in reviews]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate review IDs; publication held")
    latest = intake.latest_review(reviews, run["owner"])
    prior = target["intake"]["owner_review"]
    if (latest["id"] if latest else None) != (prior["id"] if prior else None):
        raise NeedsDecision("concurrent_review", "owner review changed since intake; reconcile it before another review")
    if any(row["state"] == "PENDING" and row["user"] and row["user"]["login"].casefold() == run["owner"].casefold() for row in reviews):
        raise NeedsDecision("pending_owner_review", "preserve the owner's existing pending GitHub review")
    after = intake.api(path, hostname=hostname)
    if pull_binding(target, after) != binding or any(after.get(key) != pull.get(key) for key in ("title", "body", "user")):
        raise NeedsDecision("comparison_changed", "PR changed during live preflight; publication held")
    return {**binding, "discussion": discussion, "review_ids": ids, "owner": run["owner"],
            "observed_at": datetime.now(UTC).isoformat(), "hostname": hostname}


def render_body(target: dict, draft: dict) -> str | None:
    """Render one evidence-bound bullet per adjudicated shared root cause.

    Args:
        target (dict): Completed target and its final adjudications.
        draft (dict): Model-authored groups and explicit verification limits.

    Returns:
        str | None: Complete review body, or a bodyless clean approval.

    Raises:
        NeedsDecision: Publication wording or finding coverage needs a decision.
        ValueError: Groups, evidence excerpts or root-cause bindings are invalid.
    """
    decisions(draft, ("groups", "limitations"), "publication_draft", ("clean_body", "user_body_evidence"), nullable=("limitations",))
    retained = {key: row for key, row in target["adjudications"].items() if row["disposition"] in ("CONFIRMED", "ADJUSTED")}
    if not isinstance(draft["groups"], list):
        raise ValueError("publication groups must be a list")  # noqa: TRY004 - journal validation uses ValueError.
    seen = set()
    roots = set()
    bullets = []
    for group in draft["groups"]:
        decisions(group, ("finding_ids", "text", "trigger", "impact", "action"), "publication_evidence")
        ids = strings(group["finding_ids"], "publication finding ID")
        if seen.intersection(ids) or not set(ids) <= retained.keys():
            raise ValueError("publication groups require unique retained finding IDs")
        judgments = [retained[key] for key in ids]
        causes = {row["root_cause"] for row in judgments}
        if len(causes) != 1 or roots.intersection(causes):
            raise ValueError("each shared adjudicated root cause requires exactly one bullet")
        roots.update(causes)
        seen.update(ids)
        text = evidence(group["text"])
        if "\n" in text or "\r" in text or text.startswith("- "):
            raise ValueError("publication group text must be one unprefixed paragraph")
        for field in ("trigger", "impact", "action"):
            if evidence(group[field]) not in text:
                raise ValueError(f"publication bullet lacks its recorded {field} excerpt")
        for row in judgments:
            if not any(f"{ref['path']}:{ref['line']}" in text for ref in row["references"]):
                raise ValueError("publication bullet lacks adjudicated path-and-line evidence")
        nits = [row["severity"] == "nit" for row in judgments]
        if all(nits) and not text.startswith("Nit: "):
            raise ValueError("optional nit bullets must start with 'Nit: '")
        bullets.append("- " + text)
    if seen != retained.keys():
        raise NeedsDecision("publication_findings", "publication body must include every retained finding exactly once")
    limits = draft["limitations"]
    if limits is not None:
        evidence(limits)
        if "\n" in limits or "\r" in limits:
            raise ValueError("verification limits must be one paragraph")
    if retained:
        actionable = any(row["severity"] != "nit" for row in retained.values())
        lead = "Changes requested." if actionable else "Approved."
        if any(row["severity"] == "nit" for row in retained.values()):
            lead += " These nits do not block merging."
        body = "\n".join([lead, *bullets])
        if limits:
            body += "\n\n" + limits
        if draft.get("clean_body") is not None:
            raise ValueError("clean_body applies only to an approval without findings")
    else:
        if limits is not None:
            raise ValueError("clean approval verification limits belong in the completion report")
        body = draft.get("clean_body")
        if body is not None:
            evidence(body)
            evidence(draft.get("user_body_evidence"))
    if body is not None:
        check_wording(body, target)
    return body


def check_wording(body: str, target: dict) -> None:
    """Reject obvious prohibited wording without claiming natural-tone assurance.

    Args:
        body (str): Final published prose.
        target (dict): Target with internal finding IDs and model identities.

    Raises:
        NeedsDecision: Wording contains profanity, attribution or internal metadata.
    """
    prohibited = (r"\b(?:fuck\w*|shit\w*|bullshit\w*|asshole\w*|bitch\w*|cunt\w*|motherfuck\w*)\b"
                  r"|\bf(?:[*#]+(?:ck|k)|(?:u|[*#])(?:c|[*#])(?:k|[*#]))\w*"
                  r"|\bs(?:h|[*#])(?:i|[*#])(?:t|[*#])\w*")
    if re.search(prohibited, body, re.IGNORECASE):
        raise NeedsDecision("publication_wording", "remove profanity from the review body")
    prose = re.sub(r"`[^`\n]+`", "", body)
    attribution = (r"co-authored-by:|signed-off-by:|\b(?:finder|vetter|adjudicator|orchestrator):"
                   r"|(?m:^\s*[*_>#-]*\s*(?:generated|reviewed) by\b)"
                   r"|(?:generated|reviewed) by(?!\s+(?:the|a|each)\s+caller\b)")
    if re.search(attribution, prose, re.IGNORECASE):
        raise NeedsDecision("publication_wording", "remove attribution and orchestration metadata from the review body")
    identities = [row["finder"] for row in target["findings"].values()]
    identities += [row["adjudicator"] for row in target["adjudications"].values()]
    internal = [person[field] for person in identities for field in ("model_id", "provider_id", "family")]
    labels = r"\b(?:model|provider|family)\s*(?:[:=]|\b(?:is|was|used|using)\b)\s*"
    citations_removed = body
    for row in (*target["findings"].values(), *target["adjudications"].values()):
        for reference in row.get("references", []):
            citations_removed = citations_removed.replace(f"{reference['path']}:{reference['line']}", "")
    if (any(re.search(labels + re.escape(value) + r"(?![\w-])", prose, re.IGNORECASE) for value in internal)
            or any(re.search(r"(?<![\w-])" + re.escape(key) + r"(?![\w-])", citations_removed) for key in target["findings"])):
        raise NeedsDecision("publication_wording", "keep finding IDs and model identities in the private evidence report")


def response_diagnostic(stderr: str) -> dict:
    """Retain bounded failure categories without storing untrusted response text.

    Args:
        stderr (str): Captured CLI error output, potentially containing secrets.

    Returns:
        dict: Optional HTTP status and a fixed, safe diagnostic category. This
            observation never proves non-submission or permits a retry.
    """
    match = re.search(r"\bHTTP[ /]+([45][0-9]{2})\b", stderr[:8192], re.IGNORECASE)
    status = int(match[1]) if match else None
    categories = {401: "authentication", 403: "authorization", 404: "not_found",
                  409: "conflict", 422: "validation", 429: "rate_limit"}
    return {"http_status": status, "reason": categories.get(status, "server" if status and status >= 500 else "request_failed")}


def submit(directory: Path, target: dict) -> dict:
    """Make one POST after its intent is durably persisted by the caller.

    Args:
        directory (Path): Locked private run directory for the temporary payload.
        target (dict): Publication-pending target with an immutable intent.

    Returns:
        dict: Response observation; success still requires independent receipt reads.

    Raises:
        OSError: The private payload could not be written or removed.
        ValueError: The captured publication endpoint is invalid.
    """
    hostname, path = endpoint(target)
    intent = target["publication"]
    payload = {"commit_id": intent["commit_id"], "event": intent["event"]}
    if intent["body"] is not None:
        payload["body"] = intent["body"]
    descriptor, filename = tempfile.mkstemp(prefix=".review-payload-", suffix=".json", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=True)
            output.flush()
            os.fsync(output.fileno())
        try:
            result = subprocess.run(["rtk", "proxy", "gh", "api", "--hostname", hostname, "--method", "POST",
                                     path + "/reviews", "--input", filename], text=True, capture_output=True, check=False, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "unknown", "returncode": None, "response_sha256": None,
                    "evidence": "POST did not return a usable response; reconcile all live review pages"}
        return {"status": "responded", "returncode": result.returncode,
                "response_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
                **({"diagnostic": response_diagnostic(result.stderr)} if result.returncode else {}),
                "evidence": "POST returned; the response is not a verified publication receipt"}
    finally:
        os.unlink(filename)


def review_matches(review: dict, owner: str, intent: dict) -> bool:
    """Compare a submitted live review with the exact recorded intent.

    Args:
        review (dict): One live GitHub review.
        owner (str): Captured invoking account.
        intent (dict): Immutable expected head, event and body.

    Returns:
        bool: Whether author, commit, submitted state and body match exactly.
    """
    state = "APPROVED" if intent["event"] == "APPROVE" else "CHANGES_REQUESTED"
    user = review.get("user")
    return bool(isinstance(user, dict) and isinstance(user.get("login"), str)
                and user["login"].casefold() == owner.casefold() and review.get("commit_id") == intent["commit_id"]
                and review.get("state") == state and review.get("body") == (intent["body"] or "") and review.get("submitted_at"))


def reconcile(run: dict, target: dict) -> dict:
    """Read all review pages and independently verify a unique new receipt.

    Args:
        run (dict): Captured account and run state.
        target (dict): Publication-pending target.

    Returns:
        dict: Complete observed IDs, matching IDs and a verified receipt or null.

    Raises:
        NeedsDecision: Attempt provenance is missing or a receipt changes during verification.
        ValueError: GitHub returns invalid inventory or receipt data.
        OSError: GitHub could not be queried.
    """
    hostname, path = endpoint(target)
    authenticated_owner(run, hostname)
    intent = target["publication"]
    if "prior_review_ids" not in intent:
        raise NeedsDecision("publication_provenance", "legacy pending intent lacks the pre-submit review inventory; do not retry it")
    reviews = intake.api(path + "/reviews?per_page=100", paginate=True, hostname=hostname)
    ids = [intake.positive(row["id"], "review ID") for row in reviews]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate review IDs during reconciliation")
    matches = [row for row in reviews if row["id"] not in intent["prior_review_ids"] and review_matches(row, run["owner"], intent)]
    observation = {"review_ids": ids, "matching_ids": [row["id"] for row in matches], "receipt": None,
                   "observed_at": datetime.now(UTC).isoformat()}
    if len(matches) != 1:
        return observation
    key = matches[0]["id"]
    review = intake.api(f"{path}/reviews/{key}", hostname=hostname)
    if review.get("id") != key or not review_matches(review, run["owner"], intent):
        raise NeedsDecision("publication_receipt", "matching review changed during independent receipt verification")
    if review.get("html_url") != target["intake"]["url"] + f"#pullrequestreview-{key}":
        raise ValueError("receipt URL does not identify this PR's review")
    submitted = datetime.fromisoformat(review["submitted_at"])
    if submitted.tzinfo is None:
        raise ValueError("receipt submission time must include its timezone")
    live = pull_binding(target, intake.api(path, hostname=hostname))
    observation["receipt"] = {"id": key, "html_url": review["html_url"], "commit_id": review["commit_id"],
                              "state": review["state"], "body": review["body"], "author": review["user"]["login"],
                              "submitted_at": review["submitted_at"], "live": live,
                              "evidence": "Read all live review pages, then fetched this exact review ID and PR independently",
                              "verification": "github-rest-readback"}
    return observation
