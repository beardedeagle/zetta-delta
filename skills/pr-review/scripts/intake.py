#!/usr/bin/env python3
"""Classify PRs before reviewer dispatch; only read GitHub metadata."""

import argparse
from datetime import datetime
import json
import re
import subprocess
import sys


def api(endpoint, paginate=False):
    command = ["rtk", "proxy", "gh", "api", "--method", "GET", endpoint]
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
            raise ValueError("invalid review author")
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
            timestamp = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
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


def intake(repo, numbers=None, owner=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo) or repo.split("/")[1] in (".", ".."):
        raise ValueError("repo must be a verified owner/repo slug")
    if numbers is not None:
        numbers = list(dict.fromkeys(positive(number, "PR number") for number in numbers))
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
            raise ValueError("invalid PR metadata")
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
        if author.casefold() == owner.casefold():
            classification = "self-authored"
        else:
            prior = latest_review(api(f"{endpoint}/{number}/reviews?per_page=100", paginate=True), owner)
            classification = "new" if prior is None else "unchanged" if prior["commit_id"] == head else "changed"
        classified.append({
            "number": number,
            "url": text(pull.get("html_url"), f"PR #{number} URL"),
            "title": text(pull.get("title"), f"PR #{number} title"),
            "author": author,
            "base_sha": base,
            "head_sha": head,
            "classification": classification,
            "review_required": classification in ("new", "changed"),
            "owner_review": prior,
        })
    return {"repository": repo, "owner": owner, "pull_requests": classified}


def organization_intake(org):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", org):
        raise ValueError("org must be an explicit organization name")
    owner = github_login(api("user").get("login"), "authenticated owner login")
    repositories = api(f"orgs/{org}/repos?type=all&per_page=100", paginate=True)
    groups = []
    seen = set()
    for repository in repositories:
        if not isinstance(repository, dict):
            raise ValueError("invalid organization repository metadata")
        repo = text(repository.get("full_name"), "organization repository full_name")
        if repo.split("/")[0].casefold() != org.casefold():
            raise ValueError(f"repository {repo} is outside requested organization {org}")
        if repo.casefold() in seen:
            raise ValueError(f"duplicate repository {repo} in paginated inventory; rerun intake")
        seen.add(repo.casefold())
        groups.append(intake(repo, owner=owner))
    return {"organization": org, "owner": owner, "repositories": groups}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--repo", help="verified owner/repo slug")
    scope.add_argument("--org", help="organization name; all visible repositories and their open PRs")
    parser.add_argument("--pr", nargs="+", type=int, help="explicit PR numbers; omit for all open PRs")
    args = parser.parse_args()
    if args.org is not None and args.pr is not None:
        parser.error("--pr requires --repo; organization intake collects all open PRs")
    try:
        result = organization_intake(args.org) if args.org is not None else intake(args.repo, args.pr)
    except (ValueError, OSError) as error:
        print(f"PR intake failed: {error}", file=sys.stderr)
        return 1
    # Publish no partial dispatch list if any requested lookup failed.
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
