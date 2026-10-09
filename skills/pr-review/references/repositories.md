# Resolve the repository

Normalize each PR target to `owner/repo#number` before freezing source. Accept:

- A PR URL: use its repository and number, verified through GitHub metadata.
- An explicit `owner/repo` slug: use it for `gh --repo`; locate the local clone
  under `~/vcs/github/` or use a standalone disposable clone when needed.
- An absolute or `~/`-relative path to a clone: read its `origin` (or only source)
  remote to derive `owner/repo`. A local folder name need not match the GitHub org.
- A repository shorthand: resolve it through the user's stated org/project root
  or an unambiguous known local clone, then verify its source remote. For example,
  `~/vcs/github/igo/home_binder_ex` can map to `InspectionGo/home_binder_ex` through
  its remote; never infer the GitHub owner from the local `igo` folder alone.
- PR numbers with no repository: use the attached checkout's verified source
  remote when the request unambiguously refers to that repository; otherwise ask.
- An explicit `org:<name>` target: route to `pr-review-batch` and use shared
  intake's `--org <name>` command to enumerate all visible organization
  repositories and their open PRs. A bare name remains repository shorthand;
  never infer an organization-wide request from it. PR numbers require a
  repository and cannot be combined with an organization target.

Group mixed-repository requests by verified repository; never apply one repo's PR
numbers to another. A single PR or commit range uses `pr-review`. Multiple PRs,
or a repository slug/path with no PR list, use `pr-review-batch`; the latter means
all open PRs needing review at their current head.
`org:<name>` also uses batch review, even if it discovers only one eligible PR.

Resolving a repository grants no publication permission. Ask only when the
repository, PR association, or comparison semantics remain ambiguous.
