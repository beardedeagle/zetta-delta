# Post-adjudication PR publication

Both review skills use this contract after their own review, vetting, coverage,
and orchestrator-adjudication requirements are complete. The review method
differs; the GitHub outcome does not. A commit-range review without a resolved
PR ends with the in-thread report.

## Publication gates

- Publish only when the user has explicitly authorized review submission for
  this PR or batch. A request to review and post/submit the results is sufficient;
  do not ask again after completing that authorized work. A review-only request
  does not authorize posting. Honor any standing prohibition or narrower scope.
- Require coverage of the complete frozen PR comparison; a commit range alone
  does not establish that coverage. Require complete assessment of paths/hunks,
  relevant interactions, and applicable categories, plus completed adjudication
  of every raw finding.
  Apply the invoking skill's additional requirements, including cross-family
  vetting for `pr-review`. Partial, blocked, or failed work, missing required
  vetting/adjudication, unresolved material disputes, and material evidence gaps
  hold submission. Zero confirmed findings alone is not proof of completion.
- Immediately before submission, re-fetch the PR's state and base/head OIDs.
  The PR must be open and match the frozen comparison. A changed base or head
  requires a new frozen comparison and re-review before any submission.
- Re-fetch complete PR discussion. Changed discussion requires a recorded
  judgment about finding additions, removals or severity changes. Nonmaterial
  changes permit the completed review; material changes hold for re-review.
- Read `review-voice.md` for the review body and ordinary review-flow messages.
  If the user requested review-before-post, show the exact body and event and
  wait for approval. Existing authorization for direct posting needs no extra
  confirmation.

## Publication decisions

These are the three authorized outcomes. A hold performs no GitHub update.
Use adjudicated, deduplicated findings only; rejected, unrelated pre-existing,
and unsupported claims are not findings to publish.

| Review status | Findings/evidence | Event | Body |
|---|---|---|---|
| complete | confirmed actionable blocker/major/minor | REQUEST_CHANGES | verdict and findings |
| complete | no findings, no material gaps | APPROVE | omit unless user supplied one |
| complete | non-blocking nits only, no material gaps | APPROVE | nits and explicit non-blocking statement |
| partial/blocked/failed | any | hold | none |
| any | unresolved material evidence gap | hold | none |
| any | missing publication authorization | hold | none |
| any | closed PR or changed base/head | hold | none |

- **Actionable findings:** create and submit a REQUEST_CHANGES review. Put the
  findings in that review body: verdict first, then one hyphen bullet per root
  cause with exact code evidence, trigger, impact, and direct corrective action.
  Include material verification limits. Any retained nits stay in this same
  body and are explicitly non-blocking; they never determine the event.
- **No findings:** create and submit an APPROVE review, normally without a body.
  State the verdict and actual verification limits in the in-thread report.
- **Nits only:** create and submit an APPROVE review with the nits in the review
  body. Lead with wording such as "Approved. These nits do not block merging."
  Label each nit and keep the requested polish optional.

Submit one review per PR for this adjudicated result. Keep findings and nits in
its body; do not split them into standalone PR comments, inline comments, or a
COMMENT review. This flow makes no other PR updates. Review bodies contain no
attribution, model identities, orchestration metadata, signoff, footer, or links
to internal skill outputs unless the user explicitly instructs otherwise.

## Submission and receipt

Use only the shared workflow's `preflight`/`publish`/`reconcile` commands,
documented in `review-workflow.md`. The publisher owns completion and live gates,
body bindings, private payload cleanup, durable intent before POST, and exact
receipt readback. It creates and submits a review in one native REST request
with the frozen `commit_id`; a concurrent push cannot retarget that review.

When exact wording approval is required, show `preflight --draft`'s event/body
and record `authorization.approved_review`. Direct-post authorization needs no
additional approval. Models remain responsible for evidence, shared-root-cause
judgments, natural prose, and clean language; structural checks do not prove them.

Only `receipt_verified` establishes the tool's independent live readback.
An attempted POST or legacy `receipt_recorded` does not establish it. Uncertain
responses are reconciled against every review page and a unique new matching
ID, followed by a GET of that exact ID. Pending attempts cannot POST again.
Absence, ambiguity or unreadable evidence holds publication; an error never
authorizes a comment or different event as a substitute.

A later push leaves the review attached to its recorded head. Report it as
superseded; never claim that the new head was reviewed. Include the final event,
body (if any), review URL, reviewed head, and actual coverage limits in the
completion report. Remove only temporary resources created for this run.
