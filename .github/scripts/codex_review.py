"""Codex (OpenAI) PR reviewer.

Reads the PR diff, asks the model for findings, and posts them as inline
comments on the PR via the `gh` CLI (pre-installed on GitHub-hosted runners).

Env (set by the workflow):
  OPENAI_API_KEY  — OpenAI key (read implicitly by the OpenAI client)
  OPENAI_MODEL    — model name (default: gpt-4o-mini)
  GH_TOKEN        — token for `gh` (GITHUB_TOKEN from the workflow)
  PR_NUMBER, REPO, BASE_REF, HEAD_SHA — PR context
"""

import json
import os
import subprocess

from openai import OpenAI

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

PROMPT = """You are a senior code reviewer. Review this PR diff and return ONLY a JSON array.
Each item: {"path": "<file path>", "line": <line number in the new file>, \
"severity": "high|medium|low", "category": "bug|security|performance|style", \
"comment": "<concise explanation>"}
Focus on bugs, security, performance. Skip trivial style nits.
If nothing is notable, return []."""

EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout


def post_inline(repo: str, pr: str, commit_id: str, path: str, line: int, body: str) -> None:
    subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/pulls/{pr}/comments",
            "-f",
            f"body={body}",
            "-f",
            f"commit_id={commit_id}",
            "-f",
            f"path={path}",
            "-F",
            f"line={line}",
            "-f",
            "side=RIGHT",
        ],
        check=False,
    )


def main() -> None:
    base = os.environ["BASE_REF"]
    repo = os.environ["REPO"]
    pr = os.environ["PR_NUMBER"]
    commit_id = os.environ["HEAD_SHA"]

    subprocess.run(["git", "fetch", "origin", base], check=False)
    diff = sh("git", "diff", f"origin/{base}...HEAD", "--", "*.py").strip()
    if not diff:
        print("No Python changes to review.")
        return

    client = OpenAI()
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": PROMPT + "\n\n" + diff}],
    )
    content = (resp.choices[0].message.content or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content[content.find("[") :]

    try:
        findings = json.loads(content)
    except json.JSONDecodeError:
        print("Could not parse model output:\n", content)
        return

    posted = 0
    for f in findings:
        if f.get("severity") == "low":
            continue
        body = (
            f"{EMOJI.get(f.get('severity'), '⚪')} **{f.get('category', '').upper()}**: "
            f"{f.get('comment', '')}\n\n*Codex review — проверь перед тем как принять*"
        )
        post_inline(repo, pr, commit_id, f["path"], int(f["line"]), body)
        posted += 1

    high = sum(1 for f in findings if f.get("severity") == "high")
    summary = f"🤖 Codex review: {len(findings)} находок ({high} high), {posted} inline-комментариев."
    subprocess.run(["gh", "pr", "comment", pr, "--repo", repo, "--body", summary], check=False)


if __name__ == "__main__":
    main()
