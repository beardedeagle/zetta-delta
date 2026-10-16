---
name: pr-review-batch
description: >-
  Batch review of PRs: an explicit list (space/comma-separated or bulleted numbers/URLs), a bare $ORG/$REPO string for all open PRs in that repo, or an explicit org:name target for all open PRs across visible organization repositories. Skip PRs already reviewed by the owner at their current head. One strong reviewer per eligible PR, different-family vetting, orchestrator adjudication with a parallel high-end stand-in for family overlap, then one authorized GitHub review per PR (approve / request changes). Use for /pr-review with multiple PRs, repository or organization targets, "review these PRs", or any multi-PR review request. Single PR or commit range routes to the pr-review skill.
---

# Batch PR Review

You are the top-level batch review orchestrator. Use one strongest eligible
finder per PR, a different-family vetter, and separate adjudication of every
finding. Read `references/notes.md` for the parallel stand-in exception when the
orchestrator's family participated on that PR. Family independence applies per
PR; families may work on other PRs in the batch. Empty finding reports need no
finding-vetting or stand-in spawn. Evidence establishes claims, not votes.

## Shared tool contract

Read `references/repositories.md` to resolve an explicit PR list, a repository's
open PRs or an explicit `org:<name>` collection. Normalize numbers/URLs from
space-separated, comma-separated, bulleted or mixed lists using verified repository
identity. Single PRs or local comparisons use `pr-review`. Ask only when material
target semantics remain ambiguous; do not claim access to hidden repositories.

Read `references/review-workflow.md` for commands and record fields, and
`references/review-intake.md` for eligibility and discussion evidence. Use one
private batch record with independent PR targets. Tooling owns intake, frozen
preparation, catalogs, discovery-wave allocation, task blocks, structural result
validation, bounded recovery, completion, publication and factual reports.
Follow its next decision; supply missing judgments or evidence instead of guessing,
refreshing comparisons or bypassing holds. A hold on one PR does not prevent
unrelated PRs from proceeding unless a shared source-integrity failure holds the run.

`/pr-review-batch --review-own-pr <explicit PR list>` opts named self-authored
PRs into full local review. Forward the flag to shared intake and workflow
creation. It is unavailable for repository-wide or organization requests and
disables GitHub publication for those targets. Other authors' eligibility and
publication requirements stay unchanged.

Read `references/roster.md` relative to this skill before choosing participants. Record capability and
eligibility judgments for the configured profiles; Python allocates discovery
families and binds each plan to its finder. Prefer suitable non-orchestrator
families before using the orchestrator's. Pinned profiles take no override;
for user-requested unprofiled models, follow `references/effective-identity.md` before dispatch. Never
switch the top-level model. Unknown or mismatched served identity holds work;
self-reported identity text is not proof. Use only authorized models; no
provider quotas, token/spending budgets or fixed two-per-model/family cap.

### Delta dispatch contract

Before every spawn or follow-up, call the shared workflow's `task` command with
the current revision and fresh native active counts. For new runs, first capture
the actual parent terminal's cwd once with `native-parent`, without `cd`, then
register its baseline. `runtime_parent_root` must match that capture. An explicit
`create --admission-mode native` records unavailable live counts as null and lets
Delta own admission under unchanged 15/30 limits; omit `task --capacity` in that
mode. Never substitute persisted thread lists or invented zero for live counts.
An issued task must be registered before another task or fresh scan. While waiting
for its native cwd, use only `status`/`report`; other commands hold without journal
changes. Settle that exact launch with `started` or a verified no-start withdrawal.
Proceed only on success;
use its exact profile, identity, role, worktree mode, override and file-reading task.
Forward that small task verbatim; never retype the sealed payload or its hashes.
The reviewer first uses `task-read` for the role, assigned raw IDs, exact expected
coverage paths/units/interactions, test
authorization and report receipt instructions. Read sealed evidence with its
`--section <key/subkey/index> --offset <byte> --length <1..32768>` commands;
historical PR comments are context, not substitute vet assignments. No evidence
is discarded. Then run `author-report --input '<judgments.json>'` on the reviewer's
own UTF-8 judgment file. Invalid drafts return `report_draft` with all independently
detectable errors and expected coverage; their bytes are retained without sealing
a final or consuming a submitted correction. The reviewer corrects their own
file and reruns on the same assignment. A valid final seals once; attestations,
materiality and judgments remain reviewer-owned.
On a permitted provider retry, retained claims keep their recorded original finder;
omit `finder` for all claims and preserve retained judgments exactly. Code binds
only new claims to the current reviewer.
Use `receive-report` with the actual native handle and completion
evidence to retain the sealed report bytes; never reconstruct received reports.
A returned
`runtime_handle` requires a follow-up to that handle. Launch once, then immediately
record `started` with native identity/handle evidence and the returned revision.
For isolated work, include its actual canonical `runtime_root`; Python captures
the copy and validates its exact bookkeeping against the protected parent.
Any intervening journal change requires another `task` check. Never bypass a
Python hold or launch from a cached block. Delta performs the launch and enforces
15/30; skill adherence supplies the connection to Python until a supported native
integration exists. Record native outcomes and original replies through the tool.
Startup/index setup precedes source sealing. No maintainer, cache writes or Git
index refresh may run against a sealed source; use the generated non-writing
Git/search prefixes and preserve unexpected changes.

Use Delta's actual subagent tools and configured profile names; do not substitute
Codex agent APIs or invent arguments. `reviewer` and `candidate` tasks are read-only
analysis, not best-of-N implementation. Verify the first real assignment's served
identity, role adherence and source integrity before queuing more for that pair;
discovery success does not prove vetting. Do not add dummy probe work.

Register additional native parent/child copies with `baseline` before execution and run
`check` after native completion. Preserve source and user work; isolated copies
may merge back even when a report is blocked. Use only the shared contract's
disposable fetch/setup/test-copy exceptions. Never execute tests in source or
merge-back checkouts or use live settings. Unexpected changes require preserving
evidence, not undoing user work. Fingerprints detect observed writes, not prevention.
Delta's 15-per-parent / 30-per-process controls exclude parent inference.

## Review responsibilities

Apply the shared workflow reference's analytical responsibilities and generated
category checklist. Plan from frozen changed-file and hunk metadata, with coherent
scope and interaction questions. Start at changed hunks and retrieve enclosing
code, callers and contracts as needed; whole-file reads are not the default.
Each PR's finder assesses the complete change independently, without other
reviewers' conclusions or consensus. Consider PR intent, discussion and prior
finding history as evidence; risk acceptance or refusing a fix alone does not
erase a supported finding. Record meaningful tests or why they were omitted.

The vetter independently checks every claim, concrete trigger, consequence and
proposed fix, giving decisive references or a precise gap. Preserve separate vet
and adjudication evidence, including rejected claims. Vetter-originated spillover
retains its finder identity and holds as `batch_spillover_route` until an independent
route is agreed; do not silently use reciprocal non-batch routing.

Personally adjudicate ordinary findings against both evidence trails. When your
family participated on this PR, run its capable third-family stand-in in parallel
with your adjudication of the other PRs. Incorporate its exact decisions and actual
identity without adjudicating those findings again. Explain material disagreements;
missing evidence remains unresolved. Use the tool's bounded recovery contract
for failures, report corrections and focused disputes.

Consolidate only after separate adjudications establish a shared root cause and
trigger. Preserve raw IDs, refutations, unresolved claims and demonstrated merge
risks; do not infer correctness from agreement or populated fields.

## Voice, publication and reporting

Read `references/review-voice.md` before composing progress, review bodies or
completion prose. After completion, read `references/review-publication.md` and
use only the shared `preflight`/`publish`/`reconcile` commands for each authorized
PR's one GitHub review. Review authorization does not grant fixes or other PR
updates. Holds perform no GitHub update.

Use `report` for factual batch status and completion evidence: requested scope,
authenticated account, collected visible repositories (including empty groups),
classifications, each frozen comparison and recommendation, actual participants,
coverage, rejected/unresolved claims, verification limits, holds and receipts.
Summarize in the user's voice; keep recommendations distinct from verified GitHub
publication. Process identities belong in the thread, not review bodies. Filled
fields do not establish reasoning quality, runtime identity or natural tone.
Ask before recording durable wiki facts unless ongoing updates are authorized.

Use `cleanup` after reports no longer depend on snapshots. Preserve the journal,
user work and machine-specific configuration, authentication and profiles.
