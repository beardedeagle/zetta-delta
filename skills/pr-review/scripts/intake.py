#!/usr/bin/env python3
"""Classify PRs before reviewer dispatch; only read GitHub metadata."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from typing import Literal, overload
from urllib.parse import urlsplit


@overload
def api(endpoint: str, paginate: Literal[False] = False, *, hostname: str | None = None) -> dict: ...


@overload
def api(endpoint: str, paginate: Literal[True], *, hostname: str | None = None) -> list: ...


def api(endpoint: str, paginate: bool = False, *, hostname: str | None = None) -> dict | list:
    """Read a GitHub object or every list page through the invoking account.

    Args:
        endpoint (str): Relative REST endpoint.
        paginate (bool): Collect and flatten all response pages.
        hostname (str | None): Explicit GitHub host; otherwise use gh's default.

    Returns:
        dict | list: Parsed object or complete paginated inventory.

    Raises:
        ValueError: GitHub failed or returned an invalid response.
        OSError: The command could not execute.
    """
    command = ["rtk", "proxy", "gh", "api", "--method", "GET", endpoint]
    if hostname is not None:
        command += ["--hostname", hostname]
    if paginate:
        command += ["--paginate", "--slurp"]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"GitHub lookup failed for {endpoint}: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid GitHub JSON for {endpoint}") from error
    if not paginate:
        if not isinstance(data, dict):
            raise ValueError(f"expected a GitHub object for {endpoint}")
        return data
    if not isinstance(data, list) or not data or any(not isinstance(page, list) for page in data):
        raise ValueError(f"expected all GitHub pages for {endpoint}")
    return [item for page in data for item in page]


def pr_endpoint(repository: str, number: int, url: str) -> tuple[str, str]:
    """Bind a PR lookup to its captured host, repository and number.

    Args:
        repository (str): Validated owner/repository slug.
        number (int): Positive PR number.
        url (str): Captured HTTPS PR URL without credentials or query data.

    Returns:
        tuple[str, str]: Hostname and relative REST endpoint.

    Raises:
        ValueError: The URL does not identify the captured PR.
    """
    positive(number, "PR number")
    parsed = urlsplit(text(url, "PR URL"))
    if (parsed.scheme != "https" or not parsed.hostname or parsed.netloc != parsed.hostname
            or parsed.path.casefold() != f"/{repository}/pull/{number}".casefold() or parsed.query or parsed.fragment):
        raise ValueError("intake URL does not match the captured PR target")
    return parsed.hostname, f"repos/{repository}/pulls/{number}"


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing or invalid {field}")
    return value


def github_login(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?", value):
        raise ValueError(f"missing or invalid {field}")
    return value


def positive(value, field):
    if type(value) is not int or value < 1:
        raise ValueError(f"missing or invalid {field}")
    return value


def sha(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise ValueError(f"missing or invalid {field}")
    return value.lower()


def latest_review(reviews, owner):
    completed = []
    for review in reviews:
        if not isinstance(review, dict) or "user" not in review:
            raise ValueError("invalid review or review author")
        if review["user"] is None:
            continue
        if not isinstance(review["user"], dict):
            raise ValueError("invalid review author")  # noqa: TRY004 - intake preserves its ValueError validation contract.
        login = github_login(review["user"].get("login"), "review author")
        if login.casefold() != owner.casefold():
            continue
        state = review.get("state")
        if state in ("PENDING", "DISMISSED"):
            continue
        if state not in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
            raise ValueError("invalid owner review state")
        submitted = text(review.get("submitted_at"), "owner review submitted_at")
        try:
            timestamp = datetime.fromisoformat(submitted)
        except ValueError as error:
            raise ValueError("invalid owner review submitted_at") from error
        if timestamp.tzinfo is None:
            raise ValueError("owner review submitted_at lacks timezone")
        review_id = positive(review.get("id"), "owner review id")
        completed.append((timestamp, review_id, review))
    if not completed:
        return None
    review = max(completed, key=lambda item: item[:2])[2]
    return {
        "id": review["id"],
        "state": review["state"],
        "submitted_at": review["submitted_at"],
        "commit_id": sha(review.get("commit_id"), "latest owner review commit_id"),
    }


def discussion_record(pull: dict, reviews: list, comments: list, review_comments: list) -> dict:
    """Capture complete discussion content with stable IDs and a canonical hash.

    Args:
        pull (dict): PR description and title.
        reviews (list): All paginated review records, including prior owner reviews.
        comments (list): All paginated issue comments.
        review_comments (list): All paginated inline comments and replies.

    Returns:
        dict: Normalized snapshot and SHA-256; pending reviews are excluded.

    Raises:
        ValueError: Content, authors or IDs are missing, malformed or duplicated.
    """
    def body(value):
        if value is not None and not isinstance(value, str):
            raise ValueError("invalid discussion body")
        return value or ""

    if not isinstance(pull, dict) or "body" not in pull:
        raise ValueError("missing PR description")
    snapshot = {"description": {"title": text(pull.get("title"), "PR title"), "body": body(pull["body"])}}
    common = ("html_url", "created_at", "updated_at")
    groups = (("reviews", reviews, ("html_url", "state", "submitted_at", "commit_id")),
              ("comments", comments, common),
              ("review_comments", review_comments, (*common, "path", "line", "original_line", "start_line",
               "original_start_line", "side", "start_side", "diff_hunk", "commit_id", "original_commit_id",
               "in_reply_to_id", "pull_request_review_id")))
    for name, rows, keys in groups:
        if not isinstance(rows, list):
            raise ValueError("discussion records must be lists")  # noqa: TRY004 - intake preserves its ValueError validation contract.
        captured = {}
        for row in rows:
            if not isinstance(row, dict) or "user" not in row or "body" not in row:
                raise ValueError("incomplete discussion record")
            if name == "reviews" and row.get("state") == "PENDING":
                continue
            key = positive(row.get("id"), "discussion ID")
            if key in captured:
                raise ValueError("duplicate discussion ID; rerun intake")
            user = row["user"]
            if user is not None:
                if not isinstance(user, dict):
                    raise ValueError("invalid discussion author")
                user = {"login": github_login(user.get("login"), "discussion author")}
            captured[key] = {"id": key, "user": user, "body": body(row["body"]),
                             **{field: row.get(field) for field in keys}}
        snapshot[name] = [captured[key] for key in sorted(captured)]
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    return {"snapshot": snapshot, "sha256": digest}


def validate_discussion(record: object) -> None:
    """Reject incomplete, modified or noncanonical discussion evidence.

    Args:
        record (object): Untrusted snapshot and its recorded SHA-256.

    Raises:
        ValueError: Snapshot fields, content or fingerprint are inconsistent.
    """
    if not isinstance(record, dict) or set(record) != {"snapshot", "sha256"}:
        raise ValueError("missing discussion snapshot")
    snapshot = record["snapshot"]
    if not isinstance(snapshot, dict) or set(snapshot) != {"description", "reviews", "comments", "review_comments"}:
        raise ValueError("incomplete discussion snapshot")
    if record != discussion_record(snapshot["description"], snapshot["reviews"], snapshot["comments"], snapshot["review_comments"]):
        raise ValueError("discussion snapshot or fingerprint changed")


def intake(repo: str, numbers: list[int] | None = None, owner: str | None = None,
           baselines: dict | None = None, *, review_own_pr: bool = False) -> dict:
    """Classify PRs and capture discussion before any reviewer launch.

    Args:
        repo (str): Verified owner/repository slug.
        numbers (list[int] | None): Explicit PRs; None collects all open PRs.
        owner (str | None): Authenticated account reused by organization intake.
        baselines (dict | None): Evaluated snapshots/history from a replayed run.
        review_own_pr (bool): Review explicitly named self-authored PRs locally.

    Returns:
        dict: Complete repository intake, including pending materiality decisions.

    Raises:
        ValueError: GitHub lookup, metadata or discussion validation failed.
        OSError: The read-only GitHub command could not execute.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo) or repo.split("/")[1] in (".", ".."):
        raise ValueError("repo must be a verified owner/repo slug")
    if numbers is not None:
        numbers = list(dict.fromkeys(positive(number, "PR number") for number in numbers))
    if type(review_own_pr) is not bool or (review_own_pr and not numbers):
        raise ValueError("review-own-pr requires a boolean override and explicit PR numbers")
    owner = github_login(api("user").get("login") if owner is None else owner, "authenticated owner login")
    endpoint = f"repos/{repo}/pulls"
    if numbers is None:
        pulls = api(endpoint + "?state=open&per_page=100", paginate=True)
    else:
        pulls = [api(f"{endpoint}/{number}") for number in numbers]
    classified = []
    seen = set()
    for index, pull in enumerate(pulls):
        if not isinstance(pull, dict):
            raise ValueError("invalid PR metadata")  # noqa: TRY004 - intake preserves its ValueError validation contract.
        number = positive(pull.get("number"), "PR number")
        if numbers is not None and number != numbers[index]:
            raise ValueError("PR metadata does not match requested number")
        if number in seen:
            raise ValueError(f"duplicate PR #{number} in paginated inventory; rerun intake")
        seen.add(number)
        if any(not isinstance(pull.get(field), dict) for field in ("base", "head", "user")):
            raise ValueError(f"PR #{number} lacks base/head/author metadata")
        author = github_login(pull["user"].get("login"), f"PR #{number} author")
        base = sha(pull["base"].get("sha"), f"PR #{number} base SHA")
        head = sha(pull["head"].get("sha"), f"PR #{number} head SHA")
        prior = None
        discussion = baseline = history = None
        decision_required = False
        own_pr = author.casefold() == owner.casefold()
        if own_pr and not review_own_pr:
            classification = "self-authored"
        else:
            reviews = api(f"{endpoint}/{number}/reviews?per_page=100", paginate=True)
            prior = latest_review(reviews, owner)
            classification = ("self-authored-review" if own_pr else
                              "new" if prior is None else "unchanged" if prior["commit_id"] == head else "changed")
            discussion = discussion_record(pull, reviews,
                api(f"repos/{repo}/issues/{number}/comments?per_page=100", paginate=True),
                api(f"{endpoint}/{number}/comments?per_page=100", paginate=True))
            previous = (baselines or {}).get(f"{repo}#{number}")
            if previous and previous["owner"].casefold() == owner.casefold():
                history = previous.get("prior_review")
                if (previous["base_sha"] == base and previous["head_sha"] == head
                        and prior and previous["owner_review_id"] == prior["id"]):
                    baseline = previous["discussion"]
                    if baseline is not None:
                        validate_discussion(baseline)
            decision_required = classification == "unchanged" and (baseline is None or baseline != discussion)
        classified.append({
            "number": number,
            "url": text(pull.get("html_url"), f"PR #{number} URL"),
            "title": text(pull.get("title"), f"PR #{number} title"),
            "author": author,
            "base_sha": base,
            "head_sha": head,
            "classification": classification,
            "review_required": classification in ("new", "changed", "self-authored-review"),
            "owner_review": prior,
            "discussion": discussion,
            "discussion_baseline": baseline,
            "discussion_decision_required": decision_required,
            "prior_review": history,
        })
    return {"repository": repo, "owner": owner, "pull_requests": classified}


def organization_intake(org: str, baselines: dict | None = None) -> dict:
    """Collect repository-separated intake across an explicit organization.

    Args:
        org (str): Verified organization name.
        baselines (dict | None): Evaluated per-PR discussion/history from a run.

    Returns:
        dict: Complete visible repository groups, including empty groups.

    Raises:
        ValueError: Organization, inventory or repository intake is invalid.
        OSError: The read-only GitHub command could not execute.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", org):
        raise ValueError("org must be an explicit organization name")
    owner = github_login(api("user").get("login"), "authenticated owner login")
    repositories = api(f"orgs/{org}/repos?type=all&per_page=100", paginate=True)
    groups = []
    seen = set()
    for repository in repositories:
        if not isinstance(repository, dict):
            raise ValueError("invalid organization repository metadata")  # noqa: TRY004 - intake preserves its ValueError validation contract.
        repo = text(repository.get("full_name"), "organization repository full_name")
        if repo.split("/")[0].casefold() != org.casefold():
            raise ValueError(f"repository {repo} is outside requested organization {org}")
        if repo.casefold() in seen:
            raise ValueError(f"duplicate repository {repo} in paginated inventory; rerun intake")
        seen.add(repo.casefold())
        groups.append(intake(repo, owner=owner, baselines=baselines))
    return {"organization": org, "owner": owner, "repositories": groups}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--repo", help="verified owner/repo slug")
    scope.add_argument("--org", help="organization name; all visible repositories and their open PRs")
    parser.add_argument("--pr", nargs="+", type=int, help="explicit PR numbers; omit for all open PRs")
    parser.add_argument("--review-own-pr", action="store_true", help="review explicitly named self-authored PRs locally; no publication")
    args = parser.parse_args()
    if args.review_own_pr and (args.repo is None or not args.pr):
        parser.error("--review-own-pr requires --repo and explicit --pr")
    if args.org is not None and args.pr is not None:
        parser.error("--pr requires --repo; organization intake collects all open PRs")
    try:
        result = organization_intake(args.org) if args.org is not None else intake(args.repo, args.pr, review_own_pr=args.review_own_pr)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"PR intake failed: {error}", file=sys.stderr)
        return 1
    # Publish no partial dispatch list if any requested lookup failed.
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
