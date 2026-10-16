---
name: pr-review
description: Thorough Delta review of ONE PR or commit range using one finder, one vetter, and the orchestrator from three different model families, with workload-sized assignments and adjudication of every finding. Runs only when explicitly invoked with /pr-review or "multi-model PR review" for a single target. Multiple PRs, repository-only requests, or explicit org:name targets route to the pr-review-batch skill instead.
disable-model-invocation: true
---

# PR Review for Delta

You are the top-level review orchestrator. Use one primary finder, one primary
vetter, and the orchestrator from three different actual model families. Size
units and follow-ups to the workload while retaining those participants.
Every original and spillover finding receives independent discovery, cross-family
vetting, and a separate evidence-based adjudication. Findings are claims, not votes
or a promise that code is bug-free.

## Finding routing

The selected finder and vetter may perform both roles when vetting spillover.
A role swap does not change who actually discovered a finding.

| Actual finder | Vetter | Adjudicator |
|---|---|---|
| primary finder | primary vetter | orchestrator |
| primary vetter (spillover) | primary finder | orchestrator |

Reserve the orchestrator's actual family for adjudication. Choose the finder and
vetter for capability and the measured change, recording selection and omission
reasons for all configured profiles. Requested models take precedence within the
three-family separation; disclose conflicts or unavailable requests. Different
models or provider routes within one family do not provide independence.
Orchestrator-originated findings retain their provenance and hold for an agreed
independent route; do not invent another participant or adjudicate your own claim.

## Shared tool contract

Read `references/repositories.md` to resolve the target. Multiple PRs,
repository-only requests, or explicit `org:<name>` targets use `pr-review-batch`.
This skill reviews one PR or explicit local comparison.

Read `references/review-workflow.md` for commands and record fields, and
`references/review-intake.md` for eligibility and discussion evidence. Use one
private run record. Tooling owns intake, frozen preparation, catalogs, task blocks,
structural report validation, bounded recovery, completion, publication and factual
reports. Use its next decision; supply the stated missing judgment or evidence
instead of guessing, refreshing the comparison or bypassing a hold.

`/pr-review --review-own-pr <PR>` explicitly requests a local self-authored
review. Forward the flag to shared intake and workflow creation. It requires
explicit PR numbers, retains full discussion and permanently disables GitHub
publication for self-authored targets. Naming your own PR alone does not opt in.

Read `references/roster.md` relative to this skill before selecting participants.
Pinned profiles take no model override; for user-requested unprofiled models,
follow `references/effective-identity.md` before dispatch. Never switch the top-level model. Unknown
identity or family overlap holds the affected work. Self-reported identity text
is not evidence of the served model; retain Delta's native identity evidence.

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

Use Delta's actual subagent tools and exact configured profile names; do not
substitute Codex agent APIs or invent tool arguments. Generated `REVIEW-PR`, `VET`
and recovery tasks are read-only analysis. A `candidate` role does not invoke the
separate orchestrate implementation procedure. Finish startup/index setup before
sealing. After sealing, use the generated non-writing Git/search prefixes; do not
run the maintainer, create caches or refresh the Git index against the source.
Preserve unexpected changes. Check the first real bounded assignment's role
adherence, served identity and source integrity before queuing
more work for that profile/block pair; discovery success does not prove vetting.
Reuse a coherent unit rather than adding a dummy smoke-test spawn. Unsupported
role behavior requires corrected routing and a retained gap.

Register additional actual runtime parent/child copies with `baseline` before execution and
run `check` after native completion. Delta isolated copies may merge changes back;
never treat a blocked report as proof that no merge occurred. Keep source and user
work unchanged. Fetch/setup/test-copy exceptions belong to the shared contract;
never run tests in a source or merge-back checkout, borrow live settings, or erase
unexpected user changes. Use enforced read-only access or merge suppression only
when Delta actually exposes it. Fingerprints detect writes afterward; they do not
prevent them. Native 15-per-parent / 30-per-process controls exclude the parent.
Use only authorized models; no provider/token/spending budget management.

## Review responsibilities

Apply the shared workflow reference's analytical responsibilities and generated
category checklist. Plan from changed-file and hunk metadata first. Choose coherent
boundaries, useful risk lenses and interaction questions, with room for targeted
code retrieval and output. The finder owns the complete change, including
end-to-end interactions across partitioned units. A large orchestrator context
is not a reason to front-load whole-file reads or overload a reviewer.

Give a provisional vetting estimate when presenting the generated plan report;
the exact vet-batch count follows discovery. Keep required work explicit; failed
work cannot become an intentional omission. Large read-only reviews do not
require new approval solely for size.

### Independent discovery

Withhold other discovery reports, consensus and prior review conclusions while
supplying PR intent, discussion evidence and project rules. Evaluate explanations
against the frozen code. Risk acceptance or a declined fix alone does not erase a
supported finding. Start at assigned changed hunks, then trace concrete triggers,
callers, contracts and consequences. Use targeted context-router reads; inspect
whole files only when the investigation needs them. Record meaningful test
evidence or the reason execution was omitted. There is no finding quota.

### Cross-family vetting

Independently reproduce or trace every assigned claim, including those likely to
be rejected. Give your own decisive source evidence or a precise gap, assess
severity and the proposed fix, and check whether that fix creates another problem.
A passing test does not refute an unexercised trigger. Record new findings as
spillover with their actual finder identity; the other participant vets them.
Agreement and duplicate discovery are not vet verdicts.

### Orchestrator adjudication

Personally adjudicate every raw ID after its vet pass, including rejected and
unverifiable claims. Compare the original trigger, impact and fix against the
vetter's evidence and refutation. Inspect decisive frozen-source lines and needed
callers/contracts when reports do not establish the conclusion. Explain how each
material argument affects the final claim, severity, category and fix assessment.
Agreement, majority and populated fields do not prove correctness.

A retained finding needs its own supporting cross-family vet. Rejection requires
a concrete refutation; missing evidence remains unresolved. For a real dispute,
identify new decisive evidence and both arguments for the tool's bounded focused
follow-up, then re-adjudicate. Do not restart an already covered chunk without a
concrete evidence gap. If investigation raises another question, assign bounded
discovery to the selected finder. Preserve any already-recorded orchestrator claim.

Only after separate adjudications, identify shared root causes and demonstrated
triggers for consolidation. Location alone is insufficient. Preserve all raw IDs,
rejected duplicates, disagreements and precise evidence gaps.

## Voice, publication and reporting

Read `references/review-voice.md` before composing progress, review bodies or
completion prose. Read `references/review-publication.md` after completion for the
shared authorized one-review contract. Use only `preflight`/`publish`/`reconcile`
for GitHub reviews; local comparisons remain in-thread. No other PR update or fix
is authorized by review publication.

Use `report` for the factual plan/status/completion report. Summarize its recorded
recommendation, finding ledger, identities, coverage, verification limits and
publication receipt in the user's voice. Keep a local recommendation distinct
from verified publication, and preserve rejected/unresolved claims and holds.
Tooling cannot establish natural tone, reasoning quality or live runtime identity
from filled fields. Ask before filing learned facts in the llm-wiki unless ongoing
wiki updates are already authorized.

Use `cleanup` when reports no longer depend on disposable resources. Retain the
journal and reproducible evidence; leave user work and machine settings untouched.
