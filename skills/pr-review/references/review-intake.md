# Shared PR review intake

Run this before launching any review subagent for actual PR targets: one PR,
an explicit list, all open PRs in a repository, or an explicit `org:<name>` target.
Resolve inputs through `repositories.md` first. Commit ranges, branches, and working changes without a
PR bypass this check; do not invent a PR association.

## Collect and classify

Run the installed helper for each verified repository group, or once with `--org`
for an explicit organization target. Replace and POSIX-quote the scope and
numbers. Supply all explicit numbers after `--pr`; omit `--pr` for whole-repo
and organization requests. `--repo` and `--org` are mutually exclusive, and PR
numbers cannot be applied to an organization:

```sh
rtk proxy python3 {{INTAKE_SH}} --repo <owner/repo> --pr <number>; echo "exit=$?"
rtk proxy python3 {{INTAKE_SH}} --repo <owner/repo>; echo "exit=$?"
rtk proxy python3 {{INTAKE_SH}} --org <organization>; echo "exit=$?"
```

Require exit 0 and complete JSON from every group before any dispatch. A lookup
or classification failure holds the requested collection; do not dispatch a
partial result or guess that an unread PR is new or unchanged. The helper only
reads metadata through `gh api --method GET`. Organization intake paginates
`orgs/<org>/repos?type=all&per_page=100`, then reuses repository intake for each
returned `full_name`. All visible repositories are included, including forks,
private repositories, and archived repositories returned by GitHub; none are
silently filtered. Coverage is limited to repositories visible to the
authenticated account and its credentials. Report this scope in the thread;
unlisted repositories are not evidence that the organization has no other PRs.
The helper paginates all open PRs and all reviews, deduplicates explicit numbers,
and emits JSON only after all lookups succeed, including every organization
repository. A failed repository or review lookup holds the entire organization
run. An empty collection means no work.

Owner means the authenticated invoking user returned by `gh api user`, not the
repository owner or PR author. Select that user's latest submitted, non-dismissed
review by `submitted_at`, breaking ties with review ID. APPROVED,
CHANGES_REQUESTED, and COMMENTED count; PENDING and DISMISSED do not. Reviews
with an explicitly null `user` (such as a deleted reviewer) cannot be owner
reviews and are ignored. Missing or malformed review authors and invalid owner
review metadata hold intake rather than silently skipping.

The helper returns the repository, authenticated owner, and each PR's number,
URL, title, author, base/head SHAs, classification, `review_required`, and latest
owner review metadata. Organization results contain `organization`, `owner`,
and a `repositories` list of those repository results; keep empty repository
groups too. PR numbers and frozen comparisons remain scoped to their repository.
Keep this evidence in the thread.

| Latest completed owner review | Classification | Action |
|---|---|---|
| None | new | Full review |
| `commit_id` equals current head SHA | unchanged | Skip; no reviewer or GitHub activity |
| `commit_id` differs from current head SHA | changed | Full review of the complete current comparison |

Show a table for the entire requested collection (repository/PR, head SHA, prior
review commit or none, classification) before dispatch. Only `review_required:
true` entries may proceed. If all are unchanged, stop without subagents,
comments, or review submissions. Explicitly naming an unchanged PR does not
bypass eligibility. New/changed PRs still use the normal publication contract.

## Bind intake to review setup

Freeze only eligible PRs, retaining intake's base/head SHAs. Verify source and
live GitHub metadata against those SHAs before reviewer dispatch. If either
endpoint moved during setup, rerun intake for that repository group and rebuild
affected snapshots/plans before dispatch; never silently carry eligibility to a
different head. Later movement follows the skill's frozen-target rules and holds
publication. Changed PRs receive the complete current merge-base-to-head review,
not only the commits added since the owner's earlier review.

GitHub reviews record the reviewed head in `commit_id`, not the historical base.
Eligibility therefore detects head changes; current base/head checks still bind
setup and publication to the frozen comparison. This helper computes eligibility;
the skill must enforce its result before calling Delta's subagent tools.
