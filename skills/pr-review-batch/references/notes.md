# Notes

## Batch routing

Use one discovery reviewer per eligible PR, then a different model family for
vetting its findings. The orchestrator normally adjudicates. Track actual served
families per PR; different models, profiles or provider routes within one family
do not provide family separation. Each independent pass on a PR uses a different
family. A family may handle other PRs in the same batch.

Choose the strongest eligible reviewer for the assigned family and PR, retaining
the capability judgment. Prefer suitable families other than the orchestrator's
before assigning its family; avoid a stand-in when another suitable family has
not yet been used in the batch. For larger batches, all suitable families may be
in use, including the orchestrator's.

The shared `review-workflow.md` contract owns discovery-wave allocation and
15/30 capacity checks. Models supply eligibility/capability judgments and native
observations; Python determines the allocated finder. Two jobs using one model
may handle different PRs; there is no fixed two-per-model/family cap or provider
budget management. Native admission remains Delta's responsibility.

If the orchestrator's family participated in discovery or vetting on a PR,
delegate adjudication of the affected findings to a high-end model from another
family eligible for that PR. Its family can already be active on other PRs in the
batch; it need not be unused across the batch. Record why its capability is near,
equivalent to, or higher than the orchestrator's. Unknown identity, unresolved
capability or no suitable family holds that PR; do not silently use a weaker model.

Run that stand-in in parallel with the orchestrator adjudicating the other
findings, within 15/30. The stand-in returns its decisions and decisive evidence.
The orchestrator checks the result contract and incorporates those decisions with
their actual adjudicator provenance, without adjudicating the same findings again.
The exception does not delegate the other findings or the whole batch. Preserve
original and spillover IDs and every finding's required evidence and stages.
An empty finding report requires no finding-vetting or adjudication spawn.
The shared contract retains vetter-originated spillover and returns
`needs_decision: batch_spillover_route`; an independent route that preserves the
PR's family separation must be agreed before that finding can proceed.

## Batch adjudication decisions

"Eligible" means the stand-in's actual family has not already performed an
independent pass on this PR. "Capable" means the recorded assessment establishes
near/equivalent/higher capability than the orchestrator. These judgments do not
prove reasoning quality. Activity on another PR does not disqualify the family;
native concurrency still applies.

A hold blocks the affected PR until a suitable alternative or the missing evidence
is recorded; unrelated PRs can continue.

| Orchestrator family participated on this PR | Stand-in assessment | Adjudicator | Action |
|---|---|---|---|
| no | not needed | orchestrator | adjudicate |
| yes | eligible and capable; idle | stand-in | adjudicate-in-parallel |
| yes | eligible and capable; active on another PR | stand-in | adjudicate-in-parallel |
| yes | family already participated on this PR | none | hold affected PR |
| yes | lower or unknown capability | none | hold affected PR |
| yes | unavailable or unknown identity | none | hold affected PR |

## Publication

Posting actions are never implicit. The user must explicitly authorize approval/request-changes publication for each PR or batch.

GitHub review bodies must not carry attribution, model identity, process metadata, footers, or signoffs unless the owner explicitly instructs it.
