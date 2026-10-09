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

Use GitHub's native REST review endpoint to create and submit the review in one
request. Write a temporary JSON payload with `commit_id` equal to the frozen
head, `event` selected above, and `body` only when needed. Binding `commit_id`
prevents a concurrent push from silently retargeting the review.

```sh
rtk proxy gh api --method POST 'repos/<owner>/<repo>/pulls/<n>/reviews' --input '<payload.json>'; echo "exit=$?"
```

Require command success and a returned review ID, URL, the expected `commit_id`,
and state (`APPROVED` or `CHANGES_REQUESTED` matching the selected event). Do not
claim publication based on attempted submission or a locally prepared payload.

After a timeout, interrupted response, or otherwise uncertain result, read all
pages of the PR's reviews before retrying. Match the authenticated author,
frozen commit, state, and body against this attempted review. If it was submitted,
report its receipt and do not duplicate it. If the outcome remains unknown,
hold and report that uncertainty. An explicit rejection is a failure to resolve,
not permission to substitute a comment or another event.

A later push leaves the review attached to its recorded head. Report it as
superseded; never claim that the new head was reviewed. Include the final event,
body (if any), review URL, reviewed head, and actual coverage limits in the
completion report. Remove only temporary resources created for this run.
