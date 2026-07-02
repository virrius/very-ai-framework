"""Codex PR reviewer — posts a live status comment, then fills it with the verdict.

Flow: post placeholder → diff PR against base → `codex exec` for JSON findings →
keep only findings anchorable to a diff line → post one PR review (verdict+summary in
body, inline threads) and drop the placeholder, or edit the placeholder into the summary
if there are no inline findings. Any parse/post failure degrades into the summary comment.

Env: GH_TOKEN, REPO (owner/repo), PR_NUMBER, BASE_REF, HEAD_SHA
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
SEV_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}
MAX_DIFF = 60000  # держит вход в пределах контекста модели

# Добавляется в промпт ТОЛЬКО при поднятом worktree — иначе codex примет файлы
# default-ветки за PR и выдаст ревью не по тому коду.
PR_CONTEXT_NOTE = (
    "The full PR branch is checked out in your working directory — read any files you "
    "need for context (imports, callers, type definitions, neighbouring code).\n\n"
)

PROMPT = """\
You are a senior code reviewer. Review the changes in the git diff below and reply with
ONLY a JSON object (no markdown, no prose, no code fences). Schema:

{
  "verdict": "lgtm" | "needs_changes",
  "summary": "<one or two sentence overall assessment>",
  "findings": [
    {
      "path": "<file path exactly as in the diff>",
      "line": <line number in the NEW file>,
      "severity": "high" | "medium" | "low",
      "category": "bug" | "security" | "performance" | "style",
      "comment": "<concise, actionable explanation>"
    }
  ]
}

Focus on bugs, security and performance. Skip trivial style nits. Only report findings
on lines that appear in the diff (changed lines). Do NOT modify any files. If nothing
is notable, return an empty "findings" array and verdict "lgtm".

Diff:
"""


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"missing env: {name}")
    return val


def gh_api(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {}


def post_comment(repo: str, pr: str, token: str, body: str) -> int | None:
    status, data = gh_api("POST", f"/repos/{repo}/issues/{pr}/comments", token, {"body": body})
    return data.get("id") if status < 300 else None


def edit_comment(repo: str, token: str, comment_id: int, body: str) -> None:
    gh_api("PATCH", f"/repos/{repo}/issues/comments/{comment_id}", token, {"body": body})


def delete_comment(repo: str, token: str, comment_id: int) -> None:
    gh_api("DELETE", f"/repos/{repo}/issues/comments/{comment_id}", token)


def run(*args: str, **kwargs) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} exited {proc.returncode}: {proc.stderr.strip()[:1000]}")
    return proc.stdout


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """Map each file to the set of new-file line numbers present in the diff."""
    lines: dict[str, set[int]] = {}
    path: str | None = None
    new_ln: int | None = None
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            path = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            if path:
                lines.setdefault(path, set())
            new_ln = None
        elif raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            new_ln = int(match.group(1)) if match else None
        elif path and new_ln is not None:
            if raw.startswith("+"):
                lines[path].add(new_ln)
                new_ln += 1
            elif raw.startswith(" "):
                new_ln += 1
    return lines


def parse_codex_json(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def build_summary(verdict: str, summary: str, orphans: list[dict]) -> str:
    verdict_label = "✅ LGTM" if verdict == "lgtm" else "⚠️ Needs changes"
    out = [f"🤖 **Codex review** — {verdict_label}", "", summary or ""]
    if orphans:
        out += ["", "Не удалось привязать к строке diff'а:"]
        out += [
            f"- {SEV_EMOJI.get(f.get('severity'), '⚪')} `{f.get('path')}:{f.get('line')}` "
            f"**{f.get('category', '').upper()}** — {f.get('comment', '')}"
            for f in orphans
        ]
    return "\n".join(out).strip()


def main() -> None:
    token, repo, pr = env("GH_TOKEN"), env("REPO"), env("PR_NUMBER")
    base, head_sha = os.environ.get("BASE_REF"), os.environ.get("HEAD_SHA")
    if not base or not head_sha:
        # путь «по комментарию» не несёт base/head в событии
        _, info = gh_api("GET", f"/repos/{repo}/pulls/{pr}", token)
        base = base or info["base"]["ref"]
        head_sha = head_sha or info["head"]["sha"]

    progress_id = post_comment(repo, pr, token, "🤖 **Codex review** — анализирую изменения, подождите… ⏳")

    def finalize(text: str) -> None:
        if progress_id is not None:
            edit_comment(repo, token, progress_id, text)
        else:
            post_comment(repo, pr, token, text)

    # PR-head берём через refs/pull/<n>/head — скрипт исполняется из default-ветки.
    fetch_base = subprocess.run(["git", "fetch", "origin", base], capture_output=True, text=True)
    fetch_head = subprocess.run(["git", "fetch", "origin", f"refs/pull/{pr}/head"], capture_output=True, text=True)
    if fetch_base.returncode or fetch_head.returncode:
        err = (fetch_base.stderr + fetch_head.stderr).strip()[:1000]
        finalize(f"🤖 Codex review: не смог получить изменения (git fetch упал).\n\n```\n{err}\n```")
        sys.exit("git fetch failed")
    diff_pathspec = [
        ".",
        ":(exclude)**/*.lock",
        ":(exclude)**/*-lock.json",
        ":(exclude)**/*.lockb",
        ":(exclude)**/*.min.*",
        ":(exclude)**/*.snap",
    ]
    diff = run("git", "diff", f"origin/{base}...FETCH_HEAD", "--", *diff_pathspec).strip()
    if not diff:
        finalize(f"🤖 Codex review: изменений относительно `{base}` нет.")
        return
    if len(diff) > MAX_DIFF:
        diff = diff[:MAX_DIFF] + "\n\n[diff обрезан по лимиту размера; полный код ветки доступен в рабочей папке]"

    codex_env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}  # секрет tool-capable CLI не нужен

    # Код ветки PR — в detached worktree на чтение; на self-hosted раннере нельзя
    # исполнять CI из присланного кода. Раннер персистентный — чистим остаток прошлого прогона.
    wt = "_pr_src"
    subprocess.run(["git", "worktree", "remove", "--force", wt], capture_output=True, text=True)
    subprocess.run(["git", "worktree", "prune"], capture_output=True, text=True)
    add = subprocess.run(["git", "worktree", "add", "--detach", wt, "FETCH_HEAD"], capture_output=True, text=True)
    pr_cwd = wt if add.returncode == 0 else None
    if pr_cwd is None:
        print(f"worktree add failed, fallback diff-only: {add.stderr.strip()[:300]}")

    # stdin, не argv: большой дифф в argv упирается в ARG_MAX (E2BIG). --sandbox read-only
    # обезвреживает prompt-injection из кода PR.
    codex_argv = ["codex", "exec", "--sandbox", "read-only"]
    prompt = PROMPT + diff
    if pr_cwd:
        codex_argv += ["--cd", pr_cwd]
        prompt = PR_CONTEXT_NOTE + prompt
    try:
        raw = run(*codex_argv, "-", input=prompt, timeout=600, env=codex_env)
    except Exception as exc:  # noqa: BLE001 — сбой codex не должен оставить заглушку висеть
        finalize(f"🤖 Codex review: не удалось выполнить codex.\n\n```\n{str(exc)[:1000]}\n```")
        sys.exit(f"codex exec failed: {exc}")
    finally:
        if pr_cwd:
            subprocess.run(["git", "worktree", "remove", "--force", wt], capture_output=True, text=True)

    parsed = parse_codex_json(raw)
    if parsed is None:
        finalize("🤖 **Codex review**\n\n" + raw.strip()[:60000])
        return

    verdict = parsed.get("verdict", "lgtm")
    findings = parsed.get("findings", []) or []
    valid = commentable_lines(diff)

    inline, orphans = [], []
    for f in findings:
        path, line = f.get("path"), f.get("line")
        if path in valid and isinstance(line, int) and line in valid[path]:
            inline.append(
                {
                    "path": path,
                    "line": line,
                    "side": "RIGHT",
                    "body": f"{SEV_EMOJI.get(f.get('severity'), '⚪')} **{f.get('category', '').upper()}**: "
                    f"{f.get('comment', '')}",
                }
            )
        else:
            orphans.append(f)

    summary = build_summary(verdict, parsed.get("summary", ""), orphans)

    # Inline-замечания требуют review; вердикт+summary кладём в его body и удаляем заглушку.
    # Без inline-находок ревью не создаём — итог пишем в заглушку.
    review_status = 200
    if inline:
        review = {"commit_id": head_sha, "body": summary, "event": "COMMENT", "comments": inline}
        review_status, _ = gh_api("POST", f"/repos/{repo}/pulls/{pr}/reviews", token, review)

    if inline and review_status < 300:
        if progress_id is not None:
            delete_comment(repo, token, progress_id)
        print(f"posted review: {len(inline)} inline, {len(orphans)} in summary, verdict={verdict}")
    elif review_status >= 300:
        all_findings = orphans + [
            {"path": c["path"], "line": c["line"], "severity": "", "category": "", "comment": c["body"]} for c in inline
        ]
        finalize(build_summary(verdict, parsed.get("summary", ""), all_findings))
        print(f"reviews API returned {review_status}; put everything in summary comment")
    else:
        finalize(summary)
        print(f"no inline findings; summary comment only, verdict={verdict}")


if __name__ == "__main__":
    main()
