# Managed Worktree Ownership Finding

Date: 2026-07-23

Status: secondary study passed; evidence only, not an approved production contract

## Hypothesis

A workstream-scoped managed worktree can be created from a recorded commit,
selected as the provider working directory, inspected for divergence and
dirtiness, and retired only when its exact ownership and clean merged state
still agree. Shared, external, mismatched, dirty, unmerged, and active
worktrees must not be removed.

## Procedure

The automated study used Git `2.55.0` and a marker-verified disposable
repository with no remotes. It:

1. discovered the primary checkout with
   `git worktree list --porcelain -z`;
2. requested the same managed-workstream slug twice;
3. created collision-free branches and worktrees from one recorded commit;
4. switched between the shared checkout and both managed worktrees;
5. confirmed the selected process working directory;
6. committed one managed branch, observed it ahead and unmerged, and rejected
   retirement;
7. fast-forwarded the disposable primary branch, observed the managed branch
   merged, and retired the exact clean managed worktree;
8. left the second managed worktree dirty and behind, then rejected retirement;
9. observed an externally created worktree and rejected its retirement;
10. rejected shared and forged/mismatched claims; and
11. verified the managed code contains neither stash nor forced-removal paths.

## Observed Contract

The 94 ms study passed all 15 assertions:

- same-slug creation was collision-free;
- workstream switching and the provider working directory were exact;
- clean ahead, merged, dirty, and behind states were surfaced;
- unmerged, dirty, external, shared, and mismatched retirement was rejected;
- exact clean merged retirement removed only the managed worktree and its
  branch;
- dirty and external worktrees remained present; and
- no stash or forced removal was available.

The behavioral fingerprint was
`03f865fd3dc8a5337bd98c9bcdbab3d39a5453e597390260880d89eb9baf2923`.
The sanitized fixture is
`spikes/fixtures/thread-workstream/git/managed-worktree.json`.

## Limitations

This study proves local Git worktree ownership mechanics only. It does not
define production branch naming, persistence, multi-repository workstreams,
remote divergence policy, merge UX, crash recovery, or a public retirement
command. The primary branch was advanced only inside the disposable
repository.

## Decision Impact

Managed worktrees remain feasible as the default isolation boundary for
explicit parallel workstreams. Production design must retain the exact
ownership, repository, branch, location, clean, merged, and inactive gates
proved here. No production Git behavior or public interface changed.
