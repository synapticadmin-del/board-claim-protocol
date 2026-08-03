---
name: board-claim-protocol
description: >-
  Coordinate many independent agents writing to one repository, using claim files on a board
  branch as atomic file-level locks. Covers claim/lock mechanics, path-ownership intersection,
  merged-not-just-done dependency gates, wave scheduling, staleness takeover, and globally
  serialised resources like migration numbers.
whenToUse: >-
  Use when several agents or chats work the same repository in parallel and must not overwrite
  each other — distributed code review, a multi-task execution plan, any fan-out where each
  worker ships its own PR. Also use when joining an existing board: to read the claims folder,
  compute what is locked, and decide whether any task is actually claimable.
tags: [multi-agent, coordination, github, locking, protocol, orchestration]
scripts:
  - path: board_state.py
    description: >-
      Read an entire claims folder, compute the LOCKED path set from in-progress claims, check
      depends_on against real PR merge state from the pulls API, and report which tasks are
      actually claimable and why the rest are not.
  - path: make_claim.py
    description: >-
      Generate a timestamped CHAT_ID, build a well-formed claim file, stage the
      create-without-sha params that make the claim atomic, and rewrite a claim to done or
      abandoned.
credentials: none
---

# The board-claim protocol

How to let N agents write to one repository at the same time without silently destroying each
other's work. Derived from a real programme: 28 review agents, then 21 execution agents, on one
repo, across six rounds.

## Why a board at all

Parallelism is safe only when two agents cannot touch the same file. Everything below is
machinery for enforcing that with nothing but git and an HTTP API — no lock server, no
scheduler, no shared memory.

The board lives on its **own branch** (`review-board`), separate from the code branch (`main`).
It holds three folders and two files:

```
board/
  PROTOCOL.md              the rules, read first, followed literally
  REVIEW_PLAN.md           the index of tasks
  tasks/TNN.md             one brief per task: scope, owns, depends_on, acceptance
  claims/TNN.md            one claim per task — this is the lock
  MIGRATION-LOCK.md        globally serialised resource counter
```

---

## 1. Constants you must fix before anything else

| Key | Example |
|---|---|
| `OWNER` / `REPO` | `synapticadmin-del` / `-godrive` (the leading dash is part of the name) |
| `BOARD_BRANCH` | `review-board` |
| `BASE_BRANCH` | `main` |
| Task briefs | `board/exec/tasks/ENN.md` |
| Claims | `board/exec/claims/ENN.md` |
| Staleness window | 90 minutes |

## 2. Generate a CHAT_ID — first action of the run

```
CHAT_ID = chat-<UTC yyyymmdd-HHMM>-<4 hex>     e.g. chat-20260802-0726-6f1e
```

```bash
python3 make_claim.py id
```

The timestamp is load-bearing. It goes in the claim, the commits and the PR body. A board can
detect a copied or reused id by comparing it against the claim's own `claimed_at_utc` — and a
duplicated id destroys the "a *different* agent verifies" guarantee in §7, because the board can
no longer tell author from verifier. **Never reuse another claim's id, and never mint a fresh id
to disguise that you are the same agent doing a second task.** Say it plainly instead.

## 3. Claim before you read a line of code

### 3.1 Read the WHOLE claims folder

Not just the task you want. Collect the union of `owns:` across every claim whose status is
`in_progress`. That set is **LOCKED**.

```bash
python3 board_state.py \
    --owner synapticadmin-del --repo=-godrive --board-branch review-board \
    --claims board/exec/claims --tasks board/exec/tasks
```

### 3.2 The three conditions — all of them, together

Take the **lowest-numbered** task where every one of these holds:

1. **No claim exists for it, or its claim is `abandoned`.**
2. **Every id in `depends_on` is `done` AND its PR is merged into the base branch.**
   `done` alone is worthless. A dependency that is `done` but unmerged does not exist as far as
   your branch is concerned. Verify with the pulls API, not with the claim file's word for it —
   a claim says `done` the moment its author stops typing, and merging is somebody else's action.
3. **Its `owns:` does not intersect LOCKED.**

Condition 3 is the one agents skip. Skipping it is exactly how two agents end up editing the
same file and one of them loses everything.

If nothing qualifies: report `no unblocked task — N in progress, waiting on <ids>` and **stop**.
Do not invent a task. Do not take one whose dependency is unmerged. Do not branch off the
dependency's branch to get around it.

### 3.3 Take the lock

`github__create_or_update_file` on `board/exec/claims/ENN.md` **with no `sha`**.

The Contents API rejects a create when the path already exists. **That rejection is the lock.**
Adding a `sha` converts an atomic claim into a silent overwrite of whoever won the race.

```bash
python3 make_claim.py new \
    --task E10 --title "Rider client: active-trip recovery" \
    --branch exec/10-rider-recovery \
    --owns apps/rider/lib/main.dart apps/rider/lib/services/app_state.dart \
    --out claim.md

python3 make_claim.py params \
    --owner O --repo=-godrive --board-branch review-board \
    --path board/exec/claims/E10.md --file claim.md --out /tmp/claim-params.json
```

| Result | Meaning |
|---|---|
| success | you may own it — still verify by read-back |
| `File already exists` / `422` / mentions `sha` | another agent got there first; next candidate |
| `409 expected <sha> but was <sha>` | the branch ref moved; wait 3–10s, re-read, retry |

**Claim file shape:**

```markdown
---
task: E09
title: Trip lifecycle — expiry sweeper, active-trip recovery
status: in_progress          # in_progress | done | abandoned
claimed_by: chat-20260801-1812-4b7a
claimed_at_utc: 2026-08-01T18:12:04Z
branch: exec/09-trip-lifecycle
pr:
owns:
  - apps/api/src/routes/trips.ts
  - apps/api/src/lib/dispatch.ts
migration: no
finished_at_utc:
---

## Progress
- claimed
```

**Copy `owns:` from the brief verbatim.** Shortening it — dropping a path you think you "won't
really touch" — breaks the lock for everyone else, because their intersection test passes on a
file you then edit.

### 3.4 Read-back is mandatory

Re-read the claim and confirm `claimed_by` is *your* CHAT_ID. Error strings vary; the file does
not. **Read it back through the contents API, never through raw.githubusercontent.com** — raw is
CDN-cached and will report your own write as missing.

### 3.5 Re-read the claims before you push

Between claiming and pushing, someone may have claimed a file adjacent to yours. If they have,
do not merge over them: say so on both PRs and let a human decide.

### 3.6 Staleness and heartbeats

An `in_progress` claim untouched for **90 minutes** is abandonable. Take it *with* its sha and
add `takeover_of:`. Write a heartbeat line into `## Progress` at each milestone so a slow task
isn't mistaken for a dead one.

---

## 4. `owns` is not the whole story — consequence files

Your `owns` names the files you set out to edit. It does not name the files that move as a **side
effect**. On a real board, every collision found after the first audit was one of these, because
a brief describes intent and a lockfile is a consequence.

| If your change… | …it also rewrites |
|---|---|
| adds or bumps a dependency | the root lockfile (`package-lock.json`, `pubspec.lock`, …) |
| adds a script another task will call | that package's manifest |
| adds a user-facing string via a shared catalogue | the abstract class **and** every locale subclass — a three-place edit two agents cannot make at once |
| adds a DB migration | the number comes from the lock file, never a directory listing |

**Reading a shared catalogue is free. Adding to it is not.**

If your work requires a consequence file you do not own: **stop and say so on the PR.** Do not
edit it, and do not work around it by duplicating the thing elsewhere.

## 5. Globally serialised resources

Anything with a global counter — migration numbers, port assignments, sequence ids — cannot be
derived from a directory listing, because two agents will read the same listing and pick the same
number. One lock file is the only authority:

- Take a number by editing the lock file **with** its sha, appending your row and bumping the
  "next free" line.
- On `409`: re-read, take the next number, and **rename your file in the same commit** — a claim
  whose `owns:` names a file that doesn't exist locks nothing.
- Never put a placeholder like `@NEXT_xxx.sql` in `owns:`. A placeholder cannot be intersected
  against another claim, so the file lock silently fails open for exactly the tasks that need it
  most.

## 6. Waves

Each brief carries `round: N`. The number is not advice. A task whose dependencies aren't merged
has files still moving underneath it. Rounds exist so the dependency graph is respected without
central scheduling.

Some gate items **split across tasks** — half the item ships in one PR, half in another. If your
task is half an item, say so explicitly on the PR, or a verifier will close a half-implemented
item.

## 7. Verification — the part that is not optional

- The author moves the finding to `awaiting-verification` and **stops**. The author never closes
  their own finding and never merges their own PR.
- **A human (or a designated owner task) merges.** If nobody in the board owns the merge, round 1
  ends with five green PRs and zero effect on the base branch, and every later task correctly
  refuses to start. Name the merge owner explicitly.
- A **different agent** verifies against the base branch *after* the merge, re-runs the original
  reproduction, and posts the result.
- **The verifier does not read the author's summary before forming a view.**
- Verification claims take `owns: []` and a `VNN.md` claim file, claimed the same way.

## 8. Never

- push to the base branch (sole exception: your own block in the shared registry file)
- merge or close any PR, including your own
- edit a file outside your `owns:`
- edit another agent's claim, brief, or registry block
- ask the user a question mid-run — decide, write the assumption into the PR, keep going

## 9. If your run stops early

Set your claim to `status: abandoned` with one line saying how far you got and what is actually
on the branch.

```bash
python3 make_claim.py abandon --file claim.md \
    --note "branch pushed, PR not opened; two of three files edited"
```

An unreleased claim looks like a crash and holds the file lock for the full staleness window.

---

## 10. Review-phase variant

For a **read-only** review phase (each agent produces a document, nobody edits product code) the
protocol simplifies: no `owns` intersection is needed, because the only shared write is the
registry file. Claims still exist — they prevent two agents writing the same document — but the
eligibility rule collapses to "lowest-numbered unclaimed task".

**Do not reuse the review protocol for an execution phase.** It was built for independent
read-only work, and it has no answer for two agents editing one source file. That distinction is
the single most important thing on this page.
