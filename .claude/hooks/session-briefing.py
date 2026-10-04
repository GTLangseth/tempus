#!/usr/bin/env python3
"""SessionStart hook: inject a "where we left off" briefing for tempus.

Gathers local state (git, current branch's Linear issue, DB, stack review) and, when
LINEAR_API_KEY is set (env or .env), the Linear project's open work. Prints Claude Code
hook JSON with the briefing as additionalContext. Never fails the session: any error
degrades to a shorter briefing.
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "tempus"
LINEAR_URL = "https://linear.app/spaceman-ventures/project/tempus-78819de7e2db"
STATE_ORDER = {"started": 0, "unstarted": 1, "backlog": 2}


def sh(*args: str, timeout: float = 5) -> str:
    try:
        out = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return out.stdout.rstrip() if out.returncode == 0 else ""
    except Exception:
        return ""


def env_value(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"') or None
    return None


def git_section() -> tuple[list[str], str | None]:
    branch = sh("git", "branch", "--show-current") or "(detached)"
    lines = [f"- Branch: `{branch}`"]
    counts = sh("git", "rev-list", "--left-right", "--count", "@{u}...HEAD")
    if counts:
        behind, ahead = counts.split()
        if ahead != "0" or behind != "0":
            lines.append(f"- vs upstream: {ahead} ahead, {behind} behind")
    status = [s for s in sh("git", "status", "--porcelain").splitlines() if s]
    if status:
        shown = ", ".join(s[3:] for s in status[:8]) + (" …" if len(status) > 8 else "")
        lines.append(f"- Uncommitted: {len(status)} file(s): {shown}")
    else:
        lines.append("- Working tree clean")
    log = sh("git", "log", "-3", "--format=%h %s (%cr)")
    if log:
        lines.append("- Recent commits:\n" + "\n".join(f"  - {c}" for c in log.splitlines()))
    match = re.search(r"spa-(\d+)", branch, re.IGNORECASE)
    return lines, f"SPA-{match.group(1)}" if match else None


def env_section() -> list[str]:
    lines = []
    db = sh("docker", "inspect", "-f", "{{.State.Health.Status}}", "tempus-db", timeout=3)
    lines.append(f"- DB container: {db or 'not running (mise run db:up)'}")
    stack = ROOT / "docs" / "STACK.md"
    if stack.exists():
        m = re.search(r"\*\*Last reviewed:\*\* (\d{4}-\d{2}-\d{2})", stack.read_text())
        if m:
            days = (date.today() - date.fromisoformat(m.group(1))).days
            if days > 90:
                lines.append(f"- ⚠️ Stack review overdue ({days} days; see docs/STACK.md → Review)")
    return lines


QUERY = """
query($project: String!) {
  issues(first: 100, filter: {
    project: { name: { eq: $project } },
    state: { type: { in: ["started", "unstarted", "backlog"] } }
  }) {
    nodes {
      identifier title priority url
      state { name type }
      projectMilestone { name }
      inverseRelations { nodes { type issue { identifier state { type } } } }
      relations { nodes { type } }
    }
  }
}
"""


def linear_section(api_key: str, branch_issue: str | None) -> list[str]:
    body = json.dumps({"query": QUERY, "variables": {"project": PROJECT}}).encode()
    req = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=body,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=6) as resp:
        issues = json.load(resp)["data"]["issues"]["nodes"]

    def blockers(i: dict) -> list[str]:
        return [
            r["issue"]["identifier"]
            for r in i["inverseRelations"]["nodes"]
            if r["type"] == "blocks"
            and r["issue"]["state"]["type"] not in ("completed", "canceled")
        ]

    def sort_key(i: dict):
        prio = i["priority"] or 5  # 0 = no priority → last
        unblocks = sum(r["type"] == "blocks" for r in i["relations"]["nodes"])
        milestone = i["projectMilestone"]["name"] if i["projectMilestone"] else "z"
        number = int(i["identifier"].split("-")[1])
        # Ties: issues that unblock others first (critical path), then oldest.
        return (STATE_ORDER.get(i["state"]["type"], 9), milestone, prio, -unblocks, number)

    issues.sort(key=sort_key)
    in_progress = [i for i in issues if i["state"]["type"] == "started"]
    ready = [i for i in issues if i["state"]["type"] != "started" and not blockers(i)]

    def fmt(i: dict) -> str:
        ms = i["projectMilestone"]["name"] if i["projectMilestone"] else "no milestone"
        b = blockers(i)
        return f"  - {i['identifier']} {i['title']} [{i['state']['name']} · {ms}]" + (
            f" (blocked by {', '.join(b)})" if b else ""
        )

    lines = [f"- Open issues: {len(issues)} ({len(in_progress)} in progress)"]
    if branch_issue:
        match = next((i for i in issues if i["identifier"] == branch_issue), None)
        lines.append(
            f"- Current branch is for {branch_issue}"
            + (f": {fmt(match).strip()[len(branch_issue) + 3 :]}" if match else "")
        )
    if in_progress:
        lines.append("- In progress:\n" + "\n".join(fmt(i) for i in in_progress))
    if ready:
        lines.append(
            "- Next ready (unblocked, by milestone then priority):\n"
            + "\n".join(fmt(i) for i in ready[:5])
        )
    return lines


def main() -> None:
    git_lines, branch_issue = git_section()
    parts = [
        "# tempus — where we left off (SessionStart briefing)",
        "",
        "## Repo",
        *git_lines,
        *env_section(),
        "",
    ]

    api_key = env_value("LINEAR_API_KEY")
    parts.append(f"## Linear ([project]({LINEAR_URL}))")
    if api_key:
        try:
            parts += linear_section(api_key, branch_issue)
        except Exception as exc:
            parts.append(
                f"- Linear fetch failed ({type(exc).__name__}); query the Linear MCP tools instead."
            )
    else:
        parts.append(
            "- No LINEAR_API_KEY: before answering, list the project's in-progress and next "
            f'unblocked issues with the Linear MCP tools (project "{PROJECT}", team Spaceman).'
            + (f" The current branch is for {branch_issue}." if branch_issue else "")
        )

    parts += [
        "",
        "## How to use this",
        "Open your first reply with a short briefing (3–6 lines): what's in flight (uncommitted "
        "work, branch issue, in-progress issues) and the recommended next issue with a one-line "
        "reason. Then handle the user's message. If the message is just a greeting or 'go', offer "
        "to start the recommended issue. When starting an issue, move it to In Progress in Linear "
        "and use its suggested git branch name.",
    ]
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "\n".join(parts),
                }
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # never block a session
        print(json.dumps({"systemMessage": f"tempus briefing hook failed: {exc}"}))
    sys.exit(0)
