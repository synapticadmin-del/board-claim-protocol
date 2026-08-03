#!/usr/bin/env python3
"""
make_claim.py — generate a CHAT_ID and build a well-formed claim file.

Emits the claim markdown and (optionally) the params JSON for
`github__create_or_update_file`. Two details that are load-bearing:

  * NO `sha` on a fresh claim. The Contents API rejects a create when the path
    already exists, and that rejection IS the lock. Adding a sha turns an
    atomic claim into a silent overwrite of whoever won the race.
  * `owns:` is copied from the brief VERBATIM. Shortening it — dropping a path
    you "won't really touch" — breaks the lock for every other agent, because
    their intersection test against LOCKED will pass on a file you then edit.

Subcommands
-----------
  id
  new       --task E10 --title T --owns PATH... [--branch B] [--chat-id ID]
            [--migration no] [--takeover-of ID] [--out claim.md]
  params    --owner O --repo R --board-branch B --path board/exec/claims/E10.md
            --file claim.md --out params.json [--sha SHA]
  done      --file claim.md --pr URL --progress LINE... [--out claim2.md]
  abandon   --file claim.md --note LINE [--out claim2.md]

`done` and `abandon` rewrite an existing claim's front matter. Releasing a claim
matters: an untouched `in_progress` looks like a crashed agent and holds the
file lock for the full staleness window (90 minutes on most boards).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import sys


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_chat_id() -> str:
    """chat-<UTC yyyymmdd-HHMM>-<4 hex>.

    The timestamp is not decoration — a board can spot a duplicated or copied
    id by checking it against the claim's own claimed_at_utc, and a duplicate
    id destroys the 'a different agent verifies' guarantee.
    """
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M")
    return f"chat-{stamp}-{secrets.token_hex(2)}"


def build_claim(args) -> str:
    chat_id = args.chat_id or new_chat_id()
    branch = args.branch or f"exec/{re.sub(r'[^0-9]', '', args.task) or args.task}-task"
    lines = [
        "---",
        f"task: {args.task}",
        f"title: {args.title}",
        "status: in_progress",
        f"claimed_by: {chat_id}",
        f"claimed_at_utc: {now_utc()}",
        f"branch: {branch}",
        "pr:",
        "owns:",
    ]
    lines += [f"  - {path}" for path in args.owns] or ["  []"]
    if args.takeover_of:
        lines.append(f"takeover_of: {args.takeover_of}")
    lines += [
        f"migration: {args.migration}",
        "finished_at_utc:",
        "---",
        "",
        "## Progress",
        "- claimed",
        "",
    ]
    print(f"CHAT_ID: {chat_id}", file=sys.stderr)
    return "\n".join(lines)


def set_field(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}\s*:.*$", re.M)
    if pattern.search(text):
        return pattern.sub(f"{key}: {value}", text, count=1)
    return text.replace("---\n\n", f"{key}: {value}\n---\n\n", 1)


def append_progress(text: str, lines: list[str]) -> str:
    block = "".join(f"- {line}\n" for line in lines)
    if "## Progress" in text:
        return text.rstrip("\n") + "\n" + block
    return text.rstrip("\n") + "\n\n## Progress\n" + block


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("id")

    p_new = sub.add_parser("new")
    p_new.add_argument("--task", required=True)
    p_new.add_argument("--title", required=True)
    p_new.add_argument("--owns", nargs="*", default=[])
    p_new.add_argument("--branch", default="")
    p_new.add_argument("--chat-id", default="")
    p_new.add_argument("--migration", default="no")
    p_new.add_argument("--takeover-of", default="")
    p_new.add_argument("--out", default="")

    p_par = sub.add_parser("params")
    for flag in ("--owner", "--repo", "--board-branch", "--path", "--file", "--out"):
        p_par.add_argument(flag, required=True)
    p_par.add_argument("--sha", default="")

    p_done = sub.add_parser("done")
    p_done.add_argument("--file", required=True)
    p_done.add_argument("--pr", required=True)
    p_done.add_argument("--progress", nargs="*", default=[])
    p_done.add_argument("--out", default="")

    p_ab = sub.add_parser("abandon")
    p_ab.add_argument("--file", required=True)
    p_ab.add_argument("--note", required=True)
    p_ab.add_argument("--out", default="")

    args = parser.parse_args()

    if args.cmd == "id":
        print(new_chat_id())
        return 0

    if args.cmd == "new":
        text = build_claim(args)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(text)
            print(f"wrote {args.out}")
        else:
            print(text)
        return 0

    if args.cmd == "params":
        with open(args.file, encoding="utf-8") as handle:
            content = handle.read()
        params = {
            "owner": args.owner,
            "repo": args.repo,
            "branch": args.board_branch,
            "path": args.path,
            "message": f"claim({os.path.basename(args.path).rsplit('.', 1)[0]})",
            "content": content,
        }
        if args.sha:
            params["sha"] = args.sha
            print("WITH sha: this is an update (takeover of an abandoned claim, or a release)")
        else:
            print("NO sha: atomic create — rejection on an existing path means you lost the race")
        if os.path.exists(args.out):
            raise SystemExit(f"refusing to overwrite {args.out} — use a fresh name")
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(params, handle, ensure_ascii=False)
        print(f"staged {args.out} — pass as paramsFile")
        print("after the call: re-read the claim and confirm claimed_by is YOUR id")
        return 0

    with open(args.file, encoding="utf-8") as handle:
        text = handle.read()

    if args.cmd == "done":
        text = set_field(text, "status", "done")
        text = set_field(text, "pr", args.pr)
        text = set_field(text, "finished_at_utc", now_utc())
        text = append_progress(text, args.progress or ["done — awaiting verification by a different agent"])
    else:
        text = set_field(text, "status", "abandoned")
        text = append_progress(text, [f"abandoned — {args.note}"])

    out = args.out or args.file
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
