#!/usr/bin/env python3
"""
board_state.py — read a whole claims folder, compute LOCKED, pick your task.

The eligibility rule this implements is the entire reason parallel agents can
write to one repository without destroying each other's work. A task is
claimable only when ALL THREE hold at once:

  1. no claim file exists for it, or its claim is `status: abandoned`
  2. every id in `depends_on` is `done` AND its PR is merged into the base
     branch — `done` alone is worthless, an unmerged dependency does not exist
     as far as your branch is concerned
  3. its `owns:` paths do not intersect LOCKED (the union of `owns:` across
     every `in_progress` claim)

Rule 3 is the one agents skip, and skipping it is how two chats end up editing
the same file and silently overwriting each other.

Usage
-----
  board_state.py --owner O --repo R --board-branch B --claims board/exec/claims \\
                 [--tasks board/exec/tasks] [--stale-minutes 90] [--json]

Write `--repo=-godrive` with an equals sign when the repo name starts with a
dash, or argparse reads it as a flag.

Reads are unauthenticated (public repos). Everything printed is derived from
the files themselves, not from any summary.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UA = {"User-Agent": "board-state-script", "Accept": "application/vnd.github+json"}


def _api(url: str):
    request = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()
    except Exception as err:  # noqa: BLE001
        return 0, str(err).encode()


def list_dir(owner: str, repo: str, ref: str, path: str) -> list[dict]:
    status, body = _api(
        f"{API}/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}"
    )
    if status == 404:
        return []
    if status != 200:
        raise SystemExit(f"list {path} failed ({status}): {body.decode('utf-8', 'replace')[:300]}")
    return [
        e
        for e in json.loads(body)
        if e.get("type") == "file" and e["name"].endswith(".md") and not e["name"].startswith(".")
    ]


def get_file(owner: str, repo: str, ref: str, path: str) -> str:
    """Contents API, never raw — the board is written to during the run and raw is CDN-stale."""
    status, body = _api(
        f"{API}/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}"
    )
    if status != 200:
        return ""
    payload = json.loads(body)
    return base64.b64decode(payload["content"]).decode("utf-8", "replace")


FRONT_MATTER = re.compile(r"^-{3,}\s*$", re.M)


def parse_front_matter(text: str) -> dict:
    """Parse the small YAML subset these board files use.

    Deliberately hand-rolled: the files carry `key: value`, list blocks of
    `  - item`, and `[a, b]` inline lists. Backticks around paths are stripped
    because briefs quote them and claims usually do not — an `owns:` set that
    compares unequal because of a backtick is a lock that silently fails open.
    """
    body = text.lstrip()
    if not body.startswith("---"):
        parts = FRONT_MATTER.split(text, 2)
        body = parts[1] if len(parts) >= 3 else ""
    else:
        parts = body.split("---", 2)
        body = parts[1] if len(parts) >= 3 else ""

    data: dict = {}
    current_list = None
    for raw_line in body.splitlines():
        if not raw_line.strip() or raw_line.strip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s+(.*)$", raw_line)
        if item and current_list is not None:
            data[current_list].append(item.group(1).strip().strip("`").strip())
            continue
        pair = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", raw_line)
        if not pair:
            continue
        key, value = pair.group(1), pair.group(2).strip()
        if value == "":
            data[key] = []
            current_list = key
            continue
        current_list = None
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            data[key] = [v.strip().strip("`\"'") for v in inner.split(",") if v.strip()]
        else:
            data[key] = value.strip("`\"'")
    return data


def age_minutes(stamp: str) -> float | None:
    if not stamp:
        return None
    try:
        when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 60.0


def merged_prs(owner: str, repo: str) -> dict[int, bool]:
    """Merge state straight from the pulls API.

    Never trust a claim file's word that its PR merged. The claim says `done`
    the moment its author stops typing; merging is somebody else's action and
    frequently has not happened yet.
    """
    state: dict[int, bool] = {}
    for page in (1, 2, 3):
        status, body = _api(f"{API}/repos/{owner}/{repo}/pulls?state=all&per_page=100&page={page}")
        if status != 200:
            break
        rows = json.loads(body)
        if not rows:
            break
        for row in rows:
            state[row["number"]] = bool(row.get("merged_at"))
    return state


NONE_TOKENS = {"", "-", "—", "–", "none", "None", "n/a", "[]", "null", "no"}


def as_list(value) -> list[str]:
    """Normalise a front-matter field into a list of real ids.

    Board files write "no dependencies" half a dozen ways — an em dash, a
    hyphen, `none`, an empty inline list. Treating any of those as a dependency
    id makes every task look permanently blocked, which is worse than useless:
    it tells an agent to stop when work is available.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(v).strip() for v in items if str(v).strip() not in NONE_TOKENS]


def pr_number(value: str) -> int | None:
    match = re.search(r"/pull/(\d+)", value or "")
    return int(match.group(1)) if match else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for flag in ("--owner", "--repo", "--board-branch"):
        parser.add_argument(flag, required=True)
    parser.add_argument("--claims", required=True, help="e.g. board/exec/claims")
    parser.add_argument("--tasks", default="", help="e.g. board/exec/tasks (for owns: of unclaimed ids)")
    parser.add_argument("--stale-minutes", type=int, default=90)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    entries = list_dir(args.owner, args.repo, args.board_branch, args.claims)
    if not entries:
        print(f"no claim files under {args.claims} — nothing is claimed yet", file=sys.stderr)

    def load(entry):
        text = get_file(args.owner, args.repo, args.board_branch, entry["path"])
        meta = parse_front_matter(text)
        meta["_file"] = entry["name"]
        meta["_id"] = meta.get("task") or entry["name"].rsplit(".", 1)[0]
        return meta

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(load, entries))

    locked: set[str] = set()
    stale: list[str] = []
    for claim in claims:
        if claim.get("status") != "in_progress":
            continue
        locked.update(as_list(claim.get("owns")))
        age = age_minutes(claim.get("claimed_at_utc", ""))
        if age is not None and age > args.stale_minutes:
            stale.append(f"{claim['_id']} ({age:.0f}m — takeover eligible)")

    merged = merged_prs(args.owner, args.repo)

    def dep_ok(dep_id: str) -> tuple[bool, str]:
        match = next((c for c in claims if c["_id"] == dep_id), None)
        if match is None:
            return False, "no claim"
        if match.get("status") != "done":
            return False, f"status={match.get('status')}"
        number = pr_number(match.get("pr", ""))
        if number is None:
            return False, "done but no PR link"
        if not merged.get(number, False):
            return False, f"done but PR #{number} NOT merged"
        return True, f"merged #{number}"

    report = {
        "locked_paths": sorted(locked),
        "in_progress": [c["_id"] for c in claims if c.get("status") == "in_progress"],
        "done": [c["_id"] for c in claims if c.get("status") == "done"],
        "abandoned": [c["_id"] for c in claims if c.get("status") == "abandoned"],
        "stale_takeover_eligible": stale,
        "eligibility": [],
    }

    task_ids: list[str] = []
    if args.tasks:
        task_ids = [
            e["name"].rsplit(".", 1)[0]
            for e in list_dir(args.owner, args.repo, args.board_branch, args.tasks)
        ]
    candidates = sorted({t for t in set(task_ids) | {c["_id"] for c in claims} if t})

    for task_id in candidates:
        claim = next((c for c in claims if c["_id"] == task_id), None)
        row = {"task": task_id, "eligible": False, "reasons": []}

        if claim and claim.get("status") in ("in_progress", "done"):
            row["reasons"].append(f"claimed ({claim.get('status')})")

        brief = {}
        if args.tasks:
            brief = parse_front_matter(
                get_file(args.owner, args.repo, args.board_branch, f"{args.tasks}/{task_id}.md")
            )
        owns = as_list(brief.get("owns")) or as_list(claim.get("owns") if claim else None)
        clash = sorted(set(owns) & locked)
        if clash:
            row["reasons"].append(f"owns intersects LOCKED: {clash}")

        deps = as_list(brief.get("depends_on"))
        for dep in deps:
            ok, why = dep_ok(dep)
            if not ok:
                row["reasons"].append(f"depends_on {dep}: {why}")

        row["eligible"] = not row["reasons"]
        row["owns"] = owns
        report["eligibility"].append(row)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"claims read: {len(claims)}")
    print(f"in_progress: {report['in_progress'] or '—'}")
    print(f"done:        {len(report['done'])}   abandoned: {report['abandoned'] or '—'}")
    if stale:
        print(f"STALE (> {args.stale_minutes}m): {stale}")
    print(f"\nLOCKED ({len(locked)} paths):")
    for path in sorted(locked):
        print(f"  {path}")
    eligible = [r for r in report["eligibility"] if r["eligible"]]
    print(f"\nELIGIBLE: {[r['task'] for r in eligible] or 'NONE — report `no unblocked task` and stop'}")
    for row in report["eligibility"]:
        if not row["eligible"] and row["reasons"]:
            print(f"  {row['task']}: {'; '.join(row['reasons'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
