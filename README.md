# board-claim-protocol

**Let N agents write to one repository at the same time without silently destroying each other's
work — using nothing but git and an HTTP API.**

No lock server. No scheduler. No shared memory. A claim file on a board branch is the lock, and
the atomicity comes from a single property of the GitHub Contents API: a create without a `sha`
is rejected when the path already exists. **That rejection is the lock.**

Extracted from a real programme: 28 review agents, then 21 execution agents, on one monorepo,
across six rounds.

## Tags

`multi-agent` · `coordination` · `github` · `locking` · `protocol` · `orchestration`

## The rule everything rests on

A task is claimable only when **all three** hold at once:

1. No claim file exists for it, or its claim is `abandoned`.
2. Every id in `depends_on` is `done` **and its PR is merged into the base branch.** `done` alone
   is worthless — an unmerged dependency does not exist as far as your branch is concerned.
3. Its `owns:` paths **do not intersect LOCKED** (the union of `owns:` across every `in_progress`
   claim).

Condition 3 is the one agents skip, and skipping it is exactly how two agents edit the same file
and one of them loses everything.

## What's in here

| File | Purpose |
|---|---|
| `SKILL.md` | The protocol: claim mechanics, ownership, waves, verification, prohibitions |
| `board_state.py` | Reads the whole claims folder, computes LOCKED, checks `depends_on` against **real** PR merge state, and reports what is claimable and why the rest isn't |
| `make_claim.py` | Mints a timestamped CHAT_ID, builds a valid claim, stages the create-without-sha params, and transitions a claim to `done` / `abandoned` |

## Quickstart

Python 3 standard library only. Reads are unauthenticated.

```bash
# what is locked right now, and is anything actually claimable?
python3 board_state.py --owner OWNER --repo=REPO --board-branch review-board \
    --claims board/exec/claims --tasks board/exec/tasks

# mint an identity and build a claim
python3 make_claim.py id
python3 make_claim.py new --task E10 --title "Rider client: active-trip recovery" \
    --owns apps/rider/lib/main.dart apps/rider/lib/services/app_state.dart --out claim.md

# stage the atomic claim (no --sha on purpose), then send it as paramsFile
python3 make_claim.py params --owner OWNER --repo=REPO --board-branch review-board \
    --path board/exec/claims/E10.md --file claim.md --out /tmp/claim.json

# release it — an unreleased claim looks like a crash and holds the lock for 90 minutes
python3 make_claim.py done --file claim.md --pr https://github.com/o/r/pull/107
```

> Repo names may start with a dash (`-godrive` is real). Always write `--repo=-godrive` with an
> equals sign.

## Board layout

```
board/
  PROTOCOL.md              the rules — read first, follow literally
  tasks/ENN.md             brief: scope, owns, depends_on, round, acceptance
  claims/ENN.md            the lock
  MIGRATION-LOCK.md        the only authority on globally serialised numbers
```

## Three lessons that cost the most to learn

- **`owns:` describes intent, not consequence.** A brief lists the files you mean to edit — not
  the lockfile a dependency bump rewrites, nor the three places a shared string catalogue must
  change. Every post-audit collision was a consequence file.
- **A placeholder in `owns:` is not a path.** `migrations/@NEXT_xxx.sql` cannot be intersected
  against another claim, so the lock silently fails *open* for exactly the tasks that touch the
  schema.
- **Somebody must own the merge.** Round 1 once ended with five tasks `done`, five green PRs and
  zero effect on `main` — and every later task then *correctly* refused to start.

## Related skills

- [`github-sandbox-ops`](https://github.com/synapticadmin-del/github-sandbox-ops) — reading and writing the repo without corrupting it
- [`evidence-grade-review`](https://github.com/synapticadmin-del/evidence-grade-review) — whether the work is real
- [`distributed-repo-programme`](https://github.com/synapticadmin-del/distributed-repo-programme) — the umbrella playbook

## License

MIT
