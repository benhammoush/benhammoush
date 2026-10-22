"""Collect public-only GitHub stats and refresh the Matrix profile."""

import json
import os
import tempfile
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from generate_matrix import generate

ROOT = Path(__file__).resolve().parent
HISTORY = """
query($owner: String!, $name: String!, $author: ID!, $cursor: String, $ref: String!) {
  repository(owner: $owner, name: $name) {
    isPrivate
    isEmpty
    object(expression: $ref) { ... on Commit {
      oid
      history(first: 100, author: {id: $author}, after: $cursor) {
        nodes { oid additions deletions committedDate }
        pageInfo { hasNextPage endCursor }
      }
    } }
  }
}
"""


def api(path, payload=None):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("Set GITHUB_TOKEN to an authenticated GitHub token.")
    request = urllib.request.Request(
        "https://api.github.com/" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "benhammoush-profile",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.load(response)
    if isinstance(result, dict) and result.get("errors"):
        raise RuntimeError("GitHub returned GraphQL errors; keeping previous panels.")
    return result


def collect(username, request=api, now=None):
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    cutoff = now - timedelta(hours=24)
    author = request(f"users/{username}")["node_id"]
    repos = []
    page = 1
    while True:
        batch = request(f"users/{username}/repos?type=owner&per_page=100&page={page}")
        repos.extend(repo for repo in batch if not repo["private"] and not repo["fork"])
        if len(batch) < 100:
            break
        page += 1

    seen = {}
    for repo in repos:
        # Generated profile assets must not inflate their own commit/line stats.
        if repo["name"].lower() == username.lower():
            continue
        cursor = None
        ref = "refs/heads/" + (repo["default_branch"] or "main")
        while True:
            data = request(
                "graphql",
                {
                    "query": HISTORY,
                    "variables": {
                        "owner": username,
                        "name": repo["name"],
                        "author": author,
                        "cursor": cursor,
                        "ref": ref,
                    },
                },
            )["data"]["repository"]
            if data is None or data["isPrivate"]:
                raise RuntimeError(
                    "Repository visibility changed; refusing a partial refresh."
                )
            if data["isEmpty"]:
                break
            commit = data["object"]
            if commit is None:
                raise RuntimeError(
                    "Default branch unavailable; refusing a partial refresh."
                )
            # Pin the branch tip so pagination cannot shift during a refresh.
            ref = commit["oid"]
            history = commit["history"]
            for node in history["nodes"]:
                seen[node["oid"]] = (
                    node["additions"],
                    node["deletions"],
                    datetime.fromisoformat(node["committedDate"]),
                )
            info = history["pageInfo"]
            if not info["hasNextPage"]:
                break
            if not info["endCursor"] or info["endCursor"] == cursor:
                raise RuntimeError("Invalid pagination cursor.")
            cursor = info["endCursor"]

    def pr_count(query):
        encoded = urllib.parse.quote(query)
        result = request(f"search/issues?q={encoded}&per_page=1")
        if result["incomplete_results"]:
            raise RuntimeError("Incomplete PR search; refusing a partial refresh.")
        return result["total_count"]

    base_query = f"is:pr author:{username} is:public"
    prs = pr_count(base_query)
    start = cutoff.isoformat().replace("+00:00", "Z")
    end = now.isoformat().replace("+00:00", "Z")
    recent_prs = pr_count(f"{base_query} created:{start}..{end}")
    additions = sum(pair[0] for pair in seen.values())
    deletions = sum(pair[1] for pair in seen.values())
    return {
        "repos": len(repos),
        "commits": len(seen),
        "prs": prs,
        "recent_prs": recent_prs,
        "recent_commits": sum(cutoff <= pair[2] <= now for pair in seen.values()),
        "recent_additions": sum(
            pair[0] for pair in seen.values() if cutoff <= pair[2] <= now
        ),
        "recent_deletions": sum(
            pair[1] for pair in seen.values() if cutoff <= pair[2] <= now
        ),
        "additions": additions,
        "deletions": deletions,
        "lines": additions - deletions,
    }


def refresh(root=ROOT, request=api):
    profile = json.loads((root / "profile.json").read_text(encoding="utf-8"))
    stats = collect(profile["username"], request)
    assets = root / "assets"
    assets.mkdir(exist_ok=True)
    # Fetch, render and validate all outputs before replacing the published assets.
    with tempfile.TemporaryDirectory(dir=assets) as directory:
        temporary = Path(directory)
        generate(profile, stats, temporary / "profile.gif")
        (temporary / "stats.json").write_text(
            json.dumps(stats, indent=2) + "\n", encoding="utf-8"
        )
        for name in ("profile.gif", "profile.png", "stats.json"):
            (temporary / name).replace(assets / name)
    print("Updated public stats:", json.dumps(stats))


if __name__ == "__main__":
    refresh()
