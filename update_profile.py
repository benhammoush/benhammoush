"""Collect token-visible GitHub stats and publish aggregate profile totals."""

import base64
import io
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from generate_matrix import generate

ROOT = Path(__file__).resolve().parent
TRANSIENT_HTTP_ERRORS = (500, 502, 503, 504)
EXCLUDED_DIRECTORIES = {
    "node_modules", "vendor", ".venv", "venv", "__pycache__", ".next", ".nuxt",
    ".output", "dist", "build", "out", "coverage", "htmlcov", "target",
    "generated", "__generated__", ".git", ".cache", ".turbo",
}
GENERATED_FILE = re.compile(
    r"(?:^|[._-])(?:generated|gen|min)(?:[._-]|$)|"
    r"\.(?:g|designer)\.[^.]+$|\.d\.ts$|"
    r"^(?:package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|"
    r"uv\.lock|Cargo\.lock|composer\.lock|go\.sum)$",
    re.IGNORECASE,
)
HISTORY = """
query($owner: String!, $name: String!, $author: ID!, $cursor: String, $ref: String!) {
  repository(owner: $owner, name: $name) {
    isEmpty
    object(expression: $ref) { ... on Commit {
      oid
      history(first: 10, author: {id: $author}, after: $cursor) {
        nodes { oid additions deletions committedDate }
        pageInfo { hasNextPage endCursor }
      }
    } }
  }
}
"""


class GraphQLUnavailable(RuntimeError):
    """GitHub could not resolve a query because its service was unavailable."""


def api(path, payload=None):
    """Read GitHub data, retrying temporary HTTP failures up to three times."""
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
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.load(response)
            break
        except urllib.error.HTTPError as error:
            if error.code not in TRANSIENT_HTTP_ERRORS or attempt == 3:
                raise
            error.close()
            time.sleep(2 ** (attempt + 1))
    if isinstance(result, dict) and result.get("errors"):
        if all(error.get("type") == "SERVICE_UNAVAILABLE" for error in result["errors"]):
            raise GraphQLUnavailable("GitHub GraphQL service unavailable.")
        raise RuntimeError("GitHub returned GraphQL errors; keeping previous panels.")
    return result


def tracked_lines(revision, run):
    """Count source code in regular tracked blobs, without executing checkout filters."""
    tree = run("ls-tree", "-r", "-z", revision)
    files = []
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        parts = name.decode("utf-8").split("/")
        if (
            mode not in (b"100644", b"100755") or kind != b"blob"
            or any(part.lower() in EXCLUDED_DIRECTORIES for part in parts[:-1])
            or GENERATED_FILE.search(parts[-1])
        ):
            continue
        files.append((oid, parts[-1]))
    if not files:
        return 0
    # Export only regular blobs; flatten directories to avoid symlink/path traversal.
    blobs = io.BytesIO(run(
        "cat-file", "--batch", input=b"".join(oid + b"\n" for oid, _ in files)
    ))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for index, (oid, name) in enumerate(files):
            header = blobs.readline().split()
            if len(header) != 3 or header[:2] != [oid, b"blob"] or not header[2].isdigit():
                raise RuntimeError("Invalid snapshot blob; refusing a partial refresh.")
            size = int(header[2])
            if size > 1_000_000_000:
                raise RuntimeError("Source file too large; refusing a partial refresh.")
            content = blobs.read(size)
            if len(content) != size or blobs.read(1) != b"\n":
                raise RuntimeError("Incomplete snapshot blob; refusing a partial refresh.")
            if b"\0" in content or content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                continue
            if (
                name in (".", "..") or name.endswith((".", " "))
                or any(char in name for char in '\\/:*?"<>|\r\n\t')
                or re.match(r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", name)
            ):
                raise RuntimeError("Unsupported source filename; refusing a partial refresh.")
            destination = root / str(index)
            try:
                destination.mkdir()
                (destination / name).write_bytes(content)
            except OSError:
                raise RuntimeError("Source export failed; keeping previous panels.") from None
        try:
            output = subprocess.run(
                [
                    "cloc", "--json", "--quiet", "--skip-uniqueness", "--no-autogen",
                    "--config", os.devnull,
                    "--timeout=0", "--max-file-size=1000",
                    "--exclude-lang=Text,Markdown,reStructuredText,JSON,YAML,TOML,INI,XML,CSV,SVG",
                    "--exclude-content=(?i)(auto[- ]generated|automatically generated|@generated|<auto-generated)",
                    "--force-lang=C++,mq5", "--force-lang=C++,mqh",
                    str(root),
                ],
                capture_output=True, check=True, timeout=900,
                env={key: value for key, value in os.environ.items()
                     if key != "GITHUB_TOKEN" and not key.upper().startswith("GIT_")},
            ).stdout
            report = json.loads(output)
            # cloc 2.08 emits exactly {} when no recognized source remains.
            if report == {}:
                return 0
            file_count = report.get("header", {}).get("n_files")
            count = report.get("SUM", {}).get("code")
            if (
                type(file_count) is not int or file_count < 0
                or type(count) is not int or count < 0
                or (file_count == 0 and count != 0)
            ):
                raise ValueError("Invalid cloc report")
            return count
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            raise RuntimeError("Source counting failed; keeping previous panels.") from None


def snapshot_lines(username, name, revision):
    """Fetch one pinned snapshot without checkout, filters, submodules or LFS downloads."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("Set GITHUB_TOKEN to an authenticated GitHub token.")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("Invalid snapshot revision; refusing a partial refresh.")
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", value) for value in (username, name)):
        raise RuntimeError("Invalid repository identity; refusing a partial refresh.")
    authorization = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    env = {
        **{key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_")},
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {authorization}",
    }
    with tempfile.TemporaryDirectory() as directory:
        def run(*args, input=None):
            try:
                return subprocess.run(
                    ["git", "-C", directory, *args], input=input, env=env,
                    capture_output=True, check=True,
                    timeout=900,
                ).stdout
            except (OSError, subprocess.SubprocessError):
                # Do not expose credentials, private names or Git's stderr in public logs.
                raise RuntimeError("Git snapshot counting failed; keeping previous panels.") from None

        run("init", "--bare")
        run("fetch", "--depth=1", "--no-tags", "--no-recurse-submodules",
            f"https://github.com/{username}/{name}.git", revision)
        if run("rev-parse", "FETCH_HEAD").strip().decode("ascii") != revision:
            raise RuntimeError("Snapshot revision mismatch; refusing a partial refresh.")
        try:
            return tracked_lines(revision, run)
        except (OSError, UnicodeError, ValueError):
            raise RuntimeError("Source counting failed; keeping previous panels.") from None


def collect(username, request=api, now=None):
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    cutoff = now - timedelta(hours=24)
    user = request("user")
    if user["login"].lower() != username.lower():
        raise RuntimeError("GITHUB_TOKEN must belong to the profile's GitHub account.")
    author = user["node_id"]
    repos = []
    page = 1
    while True:
        batch = request(
            f"user/repos?affiliation=owner&visibility=all&per_page=100&page={page}"
        )
        repos.extend(repo for repo in batch if not repo["fork"])
        if len(batch) < 100:
            break
        page += 1

    seen = {}
    lines = 0
    for repo in repos:
        # Keep generated profile activity and its own source out of account totals.
        if repo["name"].lower() == username.lower():
            continue
        cursor = None
        ref = "refs/heads/" + (repo["default_branch"] or "main")
        while True:
            payload = {
                "query": HISTORY,
                "variables": {
                    "owner": username,
                    "name": repo["name"],
                    "author": author,
                    "cursor": cursor,
                    "ref": ref,
                },
            }
            use_rest = False
            try:
                data = request("graphql", payload)["data"]["repository"]
            except (urllib.error.HTTPError, GraphQLUnavailable) as error:
                if isinstance(error, urllib.error.HTTPError):
                    if error.code not in TRANSIENT_HTTP_ERRORS:
                        raise
                    error.close()
                # Recover the same page without the unreliable line-count fields.
                payload = {
                    **payload,
                    "query": HISTORY.replace(
                        "oid additions deletions committedDate", "oid committedDate"
                    ),
                }
                data = request("graphql", payload)["data"]["repository"]
                use_rest = True
            if data is None:
                raise RuntimeError(
                    "Repository unavailable; refusing a partial refresh."
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
            if cursor is None:
                lines += snapshot_lines(username, repo["name"], ref)
            history = commit["history"]
            for node in history["nodes"]:
                if node["oid"] in seen:
                    continue
                counts = node
                if use_rest:
                    details = request(
                        f"repos/{username}/{repo['name']}/commits/{node['oid']}"
                    )
                    counts = details.get("stats") or {}
                    if details.get("sha") != node["oid"] or any(
                        type(counts.get(field)) is not int or counts[field] < 0
                        for field in ("additions", "deletions")
                    ):
                        raise RuntimeError(
                            "Invalid REST commit statistics; refusing a partial refresh."
                        )
                seen[node["oid"]] = (
                    counts["additions"],
                    counts["deletions"],
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

    base_query = f"is:pr author:{username}"
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
        "lines": lines,
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
    print("Updated aggregate stats:", json.dumps(stats))


if __name__ == "__main__":
    refresh()
