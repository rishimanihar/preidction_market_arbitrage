"""Publish reviewed Markdown to GitHub. Default: preview; --publish writes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
TRACKER = "<!-- pmarb-review-tracker:v1 -->"


def github_token():
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    result = subprocess.run(
        ["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
        text=True, capture_output=True, timeout=30, cwd=ROOT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"},
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode or not values.get("password"):
        raise RuntimeError("GitHub sign-in required; configure Git credentials or GH_TOKEN.")
    return values["password"]


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("repository must be owner/name")
        self.base = "https://api.github.com/repos/" + repository
        self.token = token

    def request(self, method, path, body=None):
        request = Request(self.base + path, method=method,
            data=None if body is None else json.dumps(body).encode(), headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json", "User-Agent": "pmarb-review",
                "Content-Type": "application/json",
            })
        try:
            with urlopen(request, timeout=45) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"GitHub {method} failed: HTTP {exc.code}") from None

    def pages(self, path):
        page = 1
        separator = "&" if "?" in path else "?"
        while True:
            rows = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            yield from rows
            if len(rows) < 100:
                return
            page += 1


def render_report(report):
    normalized = report.replace("\r\n", "\n").strip() + "\n"
    if not normalized.strip() or len(normalized) > 58000:
        raise ValueError("report must contain 1 to 58000 characters")
    marker = "<!-- pmarb-assessment:" + hashlib.sha256(normalized.encode()).hexdigest() + " -->"
    return marker, marker + "\n@avyukthNarra — assessment ready for review.\n\n" + normalized


def publish(api, report):
    marker, body = render_report(report)
    issues = [i for i in api.pages("/issues?state=all")
              if "pull_request" not in i and TRACKER in (i.get("body") or "")]
    if len(issues) > 1:
        raise RuntimeError("Multiple tracking issues found; resolve them before publishing.")
    issue = issues[0] if issues else api.request("POST", "/issues", {
        "title": "PR assessments and implementation-plan gaps",
        "body": TRACKER + "\n\nDated assessments of Avyukth's PRs, plan alignment, and independent tests.\n\n"
        "Reply with each finding ID and a fix commit, explanation, or proposed deferral. "
        "Subsequent reviews retest changes and track open/resolved/deferred findings. "
        "Each report identifies exact commits; publication does not approve merging.",
    })
    path = f"/issues/{issue['number']}/comments"
    for comment in api.pages(path):
        if marker in (comment.get("body") or ""):
            return {"status": "already-published", "issue": issue["html_url"], "report": comment["html_url"]}
    comment = api.request("POST", path, {"body": body})
    return {"status": "published", "issue": issue["html_url"], "report": comment["html_url"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--repo", default="rishimanihar/preidction_market_arbitrage")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    path = args.report.resolve()
    if not path.is_relative_to((ROOT / "reviews").resolve()) or path.suffix != ".md":
        parser.error("report must be Markdown under reviews/")
    report = path.read_text(encoding="utf-8")
    if args.publish:
        print(json.dumps(publish(GitHub(args.repo, github_token()), report), indent=2))
    else:
        print(render_report(report)[1])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never print credentials, raw API error bodies, or proxy URLs.
        message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
        raise SystemExit("Review publication failed: " + message) from None
