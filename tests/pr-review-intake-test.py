#!/usr/bin/env python3
"""Offline CLI acceptance checks for shared PR eligibility."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "skills/pr-review/scripts/intake.py"
REPO = "fixture/project"
PULLS = f"repos/{REPO}/pulls"
HEAD = "a" * 40
OLD = "c" * 40


def pull(number, head=HEAD, repo=REPO):
    return {"number": number, "html_url": f"https://github.com/{repo}/pull/{number}",
            "title": f"PR {number}", "user": {"login": "author"},
            "base": {"sha": "b" * 40}, "head": {"sha": head}}


def review(review_id=1, commit=HEAD, state="APPROVED", user="owner", submitted="2026-10-09T10:00:00Z"):
    return {"id": review_id, "commit_id": commit, "state": state,
            "user": {"login": user}, "submitted_at": submitted}


class IntakeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="pr-intake.")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.replies = self.base / "replies.json"
        self.calls = self.base / "calls.jsonl"
        (self.base / "rtk").write_text('#!/bin/sh\n[ "$1" = proxy ] || exit 2\nshift\nexec "$@"\n')
        (self.base / "gh").write_text("#!" + sys.executable + "\n" + '''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["INTAKE_CALLS"], "a") as log:
    log.write(json.dumps(args) + "\\n")
assert args[:3] == ["api", "--method", "GET"], args
reply = json.load(open(os.environ["INTAKE_REPLIES"]))[args[3]]
if isinstance(reply, dict) and "_error" in reply:
    print(reply["_error"], file=sys.stderr)
    sys.exit(1)
if isinstance(reply, dict) and "_raw" in reply:
    print(reply["_raw"])
else:
    print(json.dumps(reply))
''')
        for name in ("gh", "rtk"):
            (self.base / name).chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.base) + ":" + os.environ["PATH"],
                        INTAKE_CALLS=str(self.calls), INTAKE_REPLIES=str(self.replies))

    def run_intake(self, replies, *args, repo=REPO, org=None):
        self.replies.write_text(json.dumps({"user": {"login": "owner"}, **replies}))
        self.calls.unlink(missing_ok=True)
        scope = ["--repo", repo] if org is None else ["--org", org]
        return subprocess.run([sys.executable, str(HELPER), *scope, *args],
                              env=self.env, text=True, capture_output=True)

    def explicit(self, reviews):
        return {f"{PULLS}/12": pull(12), f"{PULLS}/12/reviews?per_page=100": reviews}

    def assert_failure(self, result, message):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "", "failed intake must not emit a partial dispatch list")
        self.assertIn(message, result.stderr)

    def test_single_pr_new_unchanged_and_changed(self):
        for reviews, expected in (([[]], "new"), ([[review()]], "unchanged"), ([[review(commit=OLD)]], "changed")):
            with self.subTest(classification=expected):
                result = self.run_intake(self.explicit(reviews), "--pr", "12")
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual((data["owner"], data["repository"]), ("owner", REPO))
                entry = data["pull_requests"][0]
                self.assertEqual((entry["number"], entry["base_sha"], entry["head_sha"]), (12, "b" * 40, HEAD))
                self.assertEqual((entry["classification"], entry["review_required"]), (expected, expected != "unchanged"))

    def test_latest_completed_owner_review_across_pages_with_timestamp_and_id_order(self):
        pages = [[review(99, commit=OLD, submitted="2026-10-09T09:00:00Z"),
                  review(3, commit=OLD)],
                 [review(4, state="COMMENTED", user="OwNeR", submitted="2026-10-09T12:00:00+02:00"),
                  review(100, commit=OLD, user="someone-else"),
                  review(101, commit=None, state="PENDING", submitted=None),
                  review(102, commit=None, state="DISMISSED", submitted=None)]]
        result = self.run_intake(self.explicit(pages), "--pr", "12")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = json.loads(result.stdout)["pull_requests"][0]
        self.assertEqual(entry["classification"], "unchanged")
        self.assertEqual(entry["owner_review"]["id"], 4)
        self.assertEqual(entry["owner_review"]["state"], "COMMENTED")

    def test_other_authors_pending_and_dismissed_are_not_owner_reviews(self):
        pages = [[review(user="author"), review(state="PENDING", submitted=None),
                  review(state="DISMISSED", submitted=None)]]
        result = self.run_intake(self.explicit(pages), "--pr", "12")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(result.stdout)["pull_requests"][0]["owner_review"])

    def test_deleted_reviewers_do_not_change_owner_eligibility(self):
        deleted_before = dict(review(99, submitted="2026-10-09T09:00:00Z"), user=None)
        deleted_after = dict(review(100, submitted="2026-10-09T11:00:00Z"), user=None)
        for prior, expected in ((None, "new"), (review(), "unchanged"), (review(commit=OLD), "changed")):
            pages = [[deleted_before], [] if prior is None else [prior], [deleted_after]]
            replies = {**self.explicit(pages), PULLS + "?state=open&per_page=100": [[pull(12)]],
                       "orgs/fixture/repos?type=all&per_page=100": [[{"full_name": REPO}]]}
            for scope in ("explicit", "repository", "organization"):
                with self.subTest(classification=expected, scope=scope):
                    args = ("--pr", "12") if scope == "explicit" else ()
                    result = self.run_intake(replies, *args, org="fixture" if scope == "organization" else None)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    data = json.loads(result.stdout)
                    group = data["repositories"][0] if scope == "organization" else data
                    entry = group["pull_requests"][0]
                    self.assertEqual((entry["classification"], entry["review_required"]), (expected, expected != "unchanged"))
                    self.assertEqual(entry["owner_review"]["id"] if prior else entry["owner_review"], 1 if prior else None)

    def test_explicit_list_deduplicates_and_does_not_enumerate_repository(self):
        replies = self.explicit([[]])
        replies.update({f"{PULLS}/18": pull(18), f"{PULLS}/18/reviews?per_page=100": [[review(state="CHANGES_REQUESTED")]]})
        result = self.run_intake(replies, "--pr", "12", "18", "12")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p["number"] for p in json.loads(result.stdout)["pull_requests"]], [12, 18])
        endpoints = [json.loads(line)[3] for line in self.calls.read_text().splitlines()]
        self.assertEqual(endpoints, ["user", f"{PULLS}/12", f"{PULLS}/18",
                                     f"{PULLS}/12/reviews?per_page=100", f"{PULLS}/18/reviews?per_page=100"])

    def test_repository_inventory_and_reviews_use_all_pages_with_only_get_requests(self):
        replies = {PULLS + "?state=open&per_page=100": [[pull(12)], [pull(18)], [pull(23)]],
                   f"{PULLS}/12/reviews?per_page=100": [[]],
                   f"{PULLS}/18/reviews?per_page=100": [[], [review()]],
                   f"{PULLS}/23/reviews?per_page=100": [[review(commit=OLD)]]}
        result = self.run_intake(replies)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p["classification"] for p in json.loads(result.stdout)["pull_requests"]],
                         ["new", "unchanged", "changed"])
        for index, line in enumerate(self.calls.read_text().splitlines()):
            call = json.loads(line)
            self.assertEqual(call[:3], ["api", "--method", "GET"])
            self.assertEqual(call[4:], [] if index == 0 else ["--paginate", "--slurp"])

    def test_empty_repository_succeeds_without_review_lookups(self):
        result = self.run_intake({PULLS + "?state=open&per_page=100": [[]]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["pull_requests"], [])
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_org_paginates_repositories_and_keeps_same_pr_numbers_separate(self):
        repos = [REPO, "fixture/private", "fixture/archived", "fixture/fork"]
        replies = {"orgs/fixture/repos?type=all&per_page=100": [
            [{"full_name": repos[0]}, {"full_name": repos[1], "private": True}],
            [{"full_name": repos[2], "archived": True}, {"full_name": repos[3], "fork": True}]],
        }
        for index, repo in enumerate(repos):
            endpoint = f"repos/{repo}/pulls"
            replies[endpoint + "?state=open&per_page=100"] = [[], [pull(12, repo=repo)]] if index < 3 else [[]]
            if index < 3:
                replies[endpoint + "/12/reviews?per_page=100"] = [[], [] if index == 0 else [review(commit=HEAD if index == 1 else OLD)]]
        result = self.run_intake(replies, org="fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual((data["organization"], data["owner"]), ("fixture", "owner"))
        self.assertEqual([group["repository"] for group in data["repositories"]], repos)
        self.assertEqual([group["owner"] for group in data["repositories"]], ["owner"] * 4)
        self.assertEqual([group["pull_requests"][0]["classification"] for group in data["repositories"][:3]],
                         ["new", "unchanged", "changed"])
        self.assertEqual([group["pull_requests"][0]["url"] for group in data["repositories"][:3]],
                         [f"https://github.com/{repo}/pull/12" for repo in repos[:3]])
        self.assertEqual(data["repositories"][3]["pull_requests"], [])
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(sum(call[3] == "user" for call in calls), 1)
        for index, call in enumerate(calls):
            self.assertEqual(call[:3], ["api", "--method", "GET"])
            self.assertEqual(call[4:], [] if index == 0 else ["--paginate", "--slurp"])

    def test_empty_org_succeeds_without_repository_lookups(self):
        result = self.run_intake({"orgs/fixture/repos?type=all&per_page=100": [[]]}, org="fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["repositories"], [])
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_org_later_repository_or_review_failure_emits_no_partial_groups(self):
        next_repo = "fixture/later"
        next_pulls = f"repos/{next_repo}/pulls"
        replies = {"orgs/fixture/repos?type=all&per_page=100": [[{"full_name": REPO}], [{"full_name": next_repo}]],
                   PULLS + "?state=open&per_page=100": [[pull(12)]],
                   PULLS + "/12/reviews?per_page=100": [[]],
                   next_pulls + "?state=open&per_page=100": [[pull(12, repo=next_repo)]],
                   next_pulls + "/12/reviews?per_page=100": [[]]}
        for endpoint in (next_pulls + "?state=open&per_page=100", next_pulls + "/12/reviews?per_page=100"):
            with self.subTest(endpoint=endpoint):
                self.assert_failure(self.run_intake({**replies, endpoint: {"_error": "lookup failed"}}, org="fixture"),
                                    endpoint)

    def test_org_inventory_failure_or_invalid_metadata_holds_intake(self):
        for pages, message in (({"_error": "org unavailable"}, "org unavailable"),
                               ([], "all GitHub pages"), ([{}], "all GitHub pages"),
                               ([[None]], "repository metadata"), ([[{}]], "full_name"),
                               ([[{"full_name": "different/project"}]], "outside requested organization"),
                               ([[{"full_name": "fixture/../project"}]], "verified owner/repo")):
            with self.subTest(message=message):
                self.assert_failure(self.run_intake({"orgs/fixture/repos?type=all&per_page=100": pages}, org="fixture"), message)

    def test_duplicate_org_repository_inventory_holds_intake(self):
        replies = {"orgs/fixture/repos?type=all&per_page=100": [[{"full_name": REPO}], [{"full_name": REPO.upper()}]],
                   PULLS + "?state=open&per_page=100": [[]]}
        self.assert_failure(self.run_intake(replies, org="fixture"), "duplicate repository")

    def test_invalid_org_inputs_and_conflicting_scopes_fail_before_lookup(self):
        for org in ("", "fixture/project", "../fixture", "fixture project"):
            with self.subTest(org=org):
                self.assert_failure(self.run_intake({}, org=org), "explicit organization name")
                self.assertFalse(self.calls.exists())
        self.assert_failure(self.run_intake({}, "--pr", "12", org="fixture"), "--pr requires --repo")
        self.assertFalse(self.calls.exists())
        self.assert_failure(self.run_intake({}, "--repo", REPO, org="fixture"), "not allowed")
        self.assertFalse(self.calls.exists())

    def test_failed_lookup_on_later_pr_emits_no_partial_list(self):
        replies = self.explicit([[]])
        replies.update({f"{PULLS}/18": pull(18), f"{PULLS}/18/reviews?per_page=100": {"_error": "pagination failed"}})
        self.assert_failure(self.run_intake(replies, "--pr", "12", "18"), "pagination failed")

    def test_bad_owner_review_metadata_never_guesses_eligibility(self):
        for field, value, message in (("commit_id", None, "commit_id"), ("submitted_at", None, "submitted_at"),
                                      ("submitted_at", "bad", "submitted_at"), ("submitted_at", "2026-10-09T10:00:00", "timezone"),
                                      ("id", True, "review id"), ("state", "UNKNOWN", "review state")):
            with self.subTest(field=field, value=value):
                bad = dict(review(), **{field: value})
                self.assert_failure(self.run_intake(self.explicit([[bad]]), "--pr", "12"), message)

    def test_missing_or_malformed_review_authors_hold_intake(self):
        missing = review()
        del missing["user"]
        bad_reviews = [missing] + [dict(review(), user=user) for user in
                                   ({}, {"login": None}, {"login": ""}, "owner", [], True)]
        for bad in bad_reviews:
            with self.subTest(review=bad):
                self.assert_failure(self.run_intake(self.explicit([[bad]]), "--pr", "12"), "author")

    def test_auth_json_pagination_and_pr_metadata_failures_hold_intake(self):
        for replies, message in (({"user": {"_error": "not authenticated"}}, "not authenticated"),
                                  ({"user": {}}, "login"),
                                  ({"user": {"_raw": "not JSON"}}, "invalid GitHub JSON"),
                                  (self.explicit([]), "all GitHub pages"),
                                  (self.explicit([review()]), "all GitHub pages"),
                                  ({**self.explicit([[]]), f"{PULLS}/12": pull(13)}, "requested number"),
                                  ({**self.explicit([[]]), f"{PULLS}/12": dict(pull(12), head={"sha": "bad"})}, "head SHA")):
            with self.subTest(message=message):
                self.assert_failure(self.run_intake(replies, "--pr", "12"), message)

    def test_duplicate_inventory_holds_instead_of_silently_missing_changed_pr(self):
        replies = {PULLS + "?state=open&per_page=100": [[pull(12)], [pull(12, OLD)]],
                   f"{PULLS}/12/reviews?per_page=100": [[]]}
        self.assert_failure(self.run_intake(replies), "duplicate PR")

    def test_invalid_inputs_fail_before_github_lookup(self):
        for repo, numbers in (("fixture/project/extra", ["12"]), ("fixture/..", ["12"]), (REPO, ["0"]), (REPO, ["-1"])):
            with self.subTest(repo=repo, numbers=numbers):
                self.assert_failure(self.run_intake({}, "--pr", *numbers, repo=repo), "invalid" if repo == REPO else "verified owner/repo")
                self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
