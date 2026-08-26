# Host Bridge Security Invariants

1. Requesters may select only a hard-coded profile and exact SHA.
2. Requesters may not provide commands, paths, URLs, models, tests, duration, repositories, remotes, branches, or refspecs.
3. Candidate SHAs must be the exact tip of `origin/autonomous/*` and descend from current `origin/main`; current `origin/main` itself is also eligible.
4. Candidate code runs in a detached temporary worktree, never by checking the candidate into the production checkout.
5. PASS is revision-specific. Any new commit requires a new request and receipt.
6. A candidate branch moving during verification invalidates the run.
7. Reusing a request ID with different immutable inputs is rejected.
8. Credentials and private media remain host-only.
9. The web app remains usable if Google/host-worker startup fails.
10. Host results are evidence, not permission to bypass backlog acceptance criteria.
