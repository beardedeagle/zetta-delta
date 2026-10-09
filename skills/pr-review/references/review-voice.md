## Review voice (when posting as the user)

Reviews and comments posted under the user's account are written in their
posted voice. Use the user's supplied style sample or approved corpus when
available; otherwise use these rules:

- Clean and professional (this is their job; no profanity), but direct:
  hyphen bullets, one issue per bullet, present tense.
- Name the concrete mechanism and link the exact code (path + line); state
  the trigger and impact in plain terms. Say what to do directly; no
  "suggested fix:" ceremony, and give a clear answer rather than ending
  findings with questions. Cut hedging filler, but keep uncertainty where
  the evidence genuinely requires it.
- Lead with the verdict when there is one ("Resolved. No outstanding
  findings from this review."). No praise, no filler.
- State the verification limits fitted to the review — what was actually
  checked vs not. No stock caveats when a different limit is the one that
  matters.
- Publication constraints, not mannerisms: no agentic plan/tooling files in
  PRs. Preserve user-approved publication text verbatim, or supplied text
  explicitly marked verbatim. Never manufacture typos.
- Nits are called nits and marked non-blocking; a nit-only review is an
  approval with the nits as comments, not a changes-request.
