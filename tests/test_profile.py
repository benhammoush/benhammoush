"""Offline checks for aggregate counting, failure preservation, and Matrix output."""

import hashlib
import io
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import call, patch
from urllib.error import HTTPError

from PIL import Image

import generate_matrix as matrix
import update_profile as profile

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)
PERSON = {
    key: "test" for key in ("username", "title", "ide", "languages", "hobbies", "email")
}


def stats(**values):
    return {
        **dict.fromkeys(
            (
                "repos",
                "commits",
                "prs",
                "lines",
                "additions",
                "deletions",
                "recent_prs",
                "recent_commits",
                "recent_additions",
                "recent_deletions",
            ),
            0,
        ),
        **values,
    }


def repo(name, **values):
    return {
        "name": name,
        "private": False,
        "fork": False,
        "size": 1,
        "default_branch": "main",
        **values,
    }


def history(nodes, more=False):
    return {
        "data": {
            "repository": {
                "isPrivate": False,
                "isEmpty": False,
                "object": {
                    "oid": "TIP",
                    "history": {
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": more, "endCursor": "next"},
                    },
                },
            }
        }
    }


class ApiTests(unittest.TestCase):
    """Check bounded retries without contacting GitHub or waiting."""

    def test_transient_http_errors_retry_the_same_request(self):
        """Temporary server failures recover with exponential backoff."""
        for code in (500, 502, 503, 504):
            errors = [
                HTTPError("https://api.github.com/graphql", code, "error", {}, None)
                for _ in range(3)
            ]
            with (
                self.subTest(code=code),
                patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}),
                patch.object(
                    profile.urllib.request,
                    "urlopen",
                    side_effect=errors + [io.BytesIO(b'{"data": {}}')],
                ) as urlopen,
                patch.object(profile.time, "sleep") as sleep,
            ):
                self.assertEqual(profile.api("graphql", {"query": "test"}), {"data": {}})
                self.assertEqual(urlopen.call_count, 4)
                self.assertTrue(all(
                    item == urlopen.call_args_list[0]
                    for item in urlopen.call_args_list
                ))
                self.assertEqual(sleep.call_args_list, [call(2), call(4), call(8)])

    def test_persistent_or_nontransient_http_errors_raise(self):
        """Retries stop at four attempts; client errors are never retried."""
        for code in (500, 502, 503, 504, 400, 401, 403, 404, 429):
            error = HTTPError("https://api.github.com/graphql", code, "error", {}, None)
            with (
                self.subTest(code=code),
                patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}),
                patch.object(
                    profile.urllib.request, "urlopen", side_effect=error
                ) as urlopen,
                patch.object(profile.time, "sleep") as sleep,
                self.assertRaises(HTTPError) as raised,
            ):
                profile.api("graphql", {"query": "test"})
            self.assertIs(raised.exception, error)
            transient = code in (500, 502, 503, 504)
            self.assertEqual(urlopen.call_count, 4 if transient else 1)
            self.assertEqual(
                sleep.call_args_list,
                [call(2), call(4), call(8)] if transient else [],
            )

    def test_graphql_errors_are_not_retried(self):
        """Partial GraphQL results still fail closed without retries."""
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}),
            patch.object(
                profile.urllib.request,
                "urlopen",
                return_value=io.BytesIO(b'{"data": {}, "errors": [{}]}'),
            ) as urlopen,
            patch.object(profile.time, "sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "GraphQL errors"),
        ):
            profile.api("graphql", {"query": "test"})
        urlopen.assert_called_once()
        sleep.assert_not_called()

    def test_only_service_unavailable_errors_allow_fallback(self):
        """Never classify mixed authentication or rate-limit errors as recoverable."""
        for errors, expected in (
            ([{"type": "SERVICE_UNAVAILABLE"}], profile.GraphQLUnavailable),
            ([{"type": "SERVICE_UNAVAILABLE"}, {"type": "FORBIDDEN"}], RuntimeError),
            ([{"type": "RATE_LIMITED"}], RuntimeError),
            ([{}], RuntimeError),
        ):
            with (
                self.subTest(errors=errors),
                patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}),
                patch.object(
                    profile.urllib.request,
                    "urlopen",
                    return_value=io.BytesIO(
                        profile.json.dumps({"errors": errors}).encode()
                    ),
                ),
                self.assertRaises(expected) as raised,
            ):
                profile.api("graphql", {"query": "test"})
            self.assertIs(type(raised.exception), expected)


class StatsTests(unittest.TestCase):
    def setUp(self):
        counter = patch.object(profile, "snapshot_lines", return_value=100)
        self.snapshot = counter.start()
        self.addCleanup(counter.stop)

    def test_unavailable_line_counts_use_rest_with_pinned_pagination(self):
        """Recover only the failed page, retaining dates, tip and SHA deduplication."""
        for failure in (
            profile.GraphQLUnavailable("unavailable"),
            *(HTTPError("https://api.github.com/graphql", code, "error", {}, None)
              for code in (500, 502, 503, 504)),
        ):
            calls = []
            recent = {"oid": "B", "committedDate": "2026-10-22T11:00:00Z"}
            old = {
                "oid": "A", "additions": 10, "deletions": 3,
                "committedDate": "2026-10-20T12:00:00Z",
            }

            def request(
                path, payload=None, calls=calls, recent=recent, old=old, failure=failure
            ):
                """Return a good first page, failing second page and REST recovery."""
                calls.append((path, payload))
                if path == "user":
                    return {"login": "test", "node_id": "USER"}
                if path.startswith("user/repos?"):
                    return [repo("one"), repo("two")]
                if path.startswith("search/"):
                    return {"total_count": 2, "incomplete_results": False}
                if path.startswith("repos/"):
                    self.assertEqual(path, "repos/test/one/commits/B")
                    return {
                        "sha": "B", "stats": {"additions": 50, "deletions": 5},
                        "files": [{"additions": 999}],
                    }
                variables = payload["variables"]
                if variables["name"] == "two":
                    return history([{**recent, "additions": 50, "deletions": 5}])
                if variables["cursor"] is None:
                    self.assertEqual(variables["ref"], "refs/heads/main")
                    return history([old], more=True)
                self.assertEqual(variables, {
                    "owner": "test", "name": "one", "author": "USER",
                    "cursor": "next", "ref": "TIP",
                })
                if "additions deletions" in payload["query"]:
                    raise failure
                self.assertIn("history(first: 10,", payload["query"])
                return history([old, recent, recent])

            with self.subTest(failure=type(failure).__name__):
                self.assertEqual(profile.collect("test", request, NOW), stats(
                    repos=2, commits=2, prs=2, recent_prs=2, recent_commits=1,
                    additions=60, deletions=8, lines=200,
                    recent_additions=50, recent_deletions=5,
                ))
                self.assertEqual(
                    len([path for path, _ in calls if path.startswith("repos/")]), 1
                )

    def test_fallback_failures_preserve_outputs(self):
        """Unavailable metadata, REST errors and invalid counts never publish totals."""
        cases = (
            "metadata", "rest", "missing", "null", "negative", "boolean",
            "string", "wrong_sha",
        )
        for failure in cases:
            def request(path, payload=None, failure=failure):
                """Fail the recovery stage selected by the current fixture."""
                if path == "user":
                    return {"login": "test", "node_id": "USER"}
                if path.startswith("user/repos?"):
                    return [repo("one")]
                if path == "graphql":
                    if "additions deletions" in payload["query"] or failure == "metadata":
                        raise profile.GraphQLUnavailable("unavailable")
                    return history([{
                        "oid": "A", "committedDate": "2026-10-22T11:00:00Z",
                    }])
                if failure == "rest":
                    raise HTTPError("https://api.github.com", 503, "error", {}, None)
                counts = {"additions": 5, "deletions": 1}
                if failure == "missing":
                    counts.pop("additions")
                elif failure in ("null", "negative", "boolean", "string"):
                    counts["additions"] = {
                        "null": None, "negative": -1, "boolean": True, "string": "5",
                    }[failure]
                return {
                    "sha": "OTHER" if failure == "wrong_sha" else "A",
                    "stats": counts,
                }

            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "profile.json").write_text('{"username":"test"}')
                (root / "assets").mkdir()
                for name in ("profile.gif", "profile.png", "stats.json"):
                    (root / "assets" / name).write_text("previous")
                with self.assertRaises((RuntimeError, HTTPError)):
                    profile.refresh(root, request)
                self.assertTrue(all(
                    path.read_text() == "previous"
                    for path in (root / "assets").iterdir()
                ))

    def test_auth_and_other_errors_do_not_trigger_fallback(self):
        """Do not conceal permission, rate-limit or unrelated GraphQL failures."""
        for failure in (
            RuntimeError("GraphQL error"),
            *(HTTPError("https://api.github.com/graphql", code, "error", {}, None)
              for code in (400, 401, 403, 404, 429)),
        ):
            graphql_calls = []

            def request(path, payload=None, graphql_calls=graphql_calls, failure=failure):
                """Allow discovery and fail the original history query."""
                if path == "user":
                    return {"login": "test", "node_id": "USER"}
                if path.startswith("user/repos?"):
                    return [repo("one")]
                self.assertEqual(path, "graphql")
                graphql_calls.append(payload)
                raise failure

            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises(type(failure)):
                    profile.collect("test", request, NOW)
                self.assertEqual(len(graphql_calls), 1)

    def test_pagination_visibility_empty_and_zero_size_repos(self):
        calls = []

        def request(path, payload=None):
            calls.append((path, payload))
            if path == "user":
                return {"login": "TEST", "node_id": "USER"}
            if path.startswith("user/repos?"):
                self.assertIn("affiliation=owner&visibility=all", path)
                if "&page=1" in path:
                    return [
                        repo("one"),
                        repo("test"),
                        repo("empty", size=0),
                        repo("secret", private=True),
                        repo("fork", fork=True, private=True),
                    ] + [repo(f"f{i}", fork=True) for i in range(95)]
                return [repo("two", size=0)]
            if path.startswith("search/"):
                self.assertNotIn("is%3Apublic", path)
                self.assertNotIn("is%3Aprivate", path)
                self.assertIn("is%3Apr%20author%3Atest", path)
                return {"total_count": 9, "incomplete_results": False}
            self.assertIn("history(first: 10,", payload["query"])
            self.assertIn("oid additions deletions committedDate", payload["query"])
            variables = payload["variables"]
            self.assertEqual(variables["author"], "USER")
            if variables["name"] == "secret":
                result = history(
                    [{
                        "oid": "PRIVATE",
                        "additions": 50,
                        "deletions": 5,
                        "committedDate": "2026-10-22T11:00:00Z",
                    }]
                )
                result["data"]["repository"]["isPrivate"] = True
                return result
            if variables["name"] == "empty":
                return {
                    "data": {
                        "repository": {
                            "isPrivate": False,
                            "isEmpty": True,
                            "object": None,
                        }
                    }
                }
            second = variables["cursor"] is not None
            if second:
                self.assertEqual(variables["ref"], "TIP")
            node = {
                "oid": "B" if second else "A",
                "additions": 10,
                "deletions": 3,
                "committedDate": "2026-10-20T12:00:00Z"
                if second
                else "2026-10-22T11:00:00Z",
            }
            return history([node], variables["name"] == "one" and not second)

        self.assertEqual(
            profile.collect("test", request, NOW),
            stats(
                repos=5,
                commits=3,
                prs=9,
                recent_prs=9,
                recent_commits=2,
                additions=70,
                deletions=11,
                lines=300,
                recent_additions=60,
                recent_deletions=8,
            ),
        )
        self.assertEqual(len([call for call in calls if call[0] == "graphql"]), 5)
        self.assertEqual(self.snapshot.call_args_list, [
            call("test", name, "TIP") for name in ("one", "secret", "two")
        ])

    def test_recent_boundaries_future_dates_offsets_and_deduplication(self):
        dates = [
            "2026-10-21T11:59:59Z",
            "2026-10-21T12:00:00Z",
            "2026-10-22T12:00:00Z",
            "2026-10-22T12:00:01Z",
            "2026-10-22T13:00:00+02:00",
        ]
        nodes = [
            {"oid": str(i), "additions": i + 1, "deletions": i, "committedDate": date}
            for i, date in enumerate(dates)
        ]

        def request(path, payload=None):
            if path == "user":
                return {"login": "test", "node_id": "USER"}
            if path.startswith("user/repos?"):
                return [repo("one")]
            if path == "graphql":
                return history(nodes + [nodes[1]])
            if "created%3A" in path:
                self.assertIn(
                    "2026-10-21T12%3A00%3A00Z..2026-10-22T12%3A00%3A00Z", path
                )
                return {"incomplete_results": False, "total_count": 2}
            return {"incomplete_results": False, "total_count": 9}

        result = profile.collect("test", request, NOW)
        self.assertEqual(
            (result["commits"], result["recent_commits"], result["recent_prs"]),
            (5, 3, 2),
        )
        self.assertEqual(
            (result["lines"], result["recent_additions"], result["recent_deletions"]),
            (100, 10, 7),
        )

    def test_rejects_partial_search_and_unavailable_repository(self):
        for unavailable in (True, False):

            def request(path, payload=None, unavailable=unavailable):
                if path == "user":
                    return {"login": "test", "node_id": "USER"}
                if path.startswith("user/repos?"):
                    return [repo("one")] if unavailable else []
                if path == "graphql":
                    return {"data": {"repository": None}}
                return {"total_count": 3, "incomplete_results": True}

            with self.assertRaises(RuntimeError):
                profile.collect("test", request, NOW)

    def test_wrong_account_preserves_outputs_before_listing_repositories(self):
        """Reject another user's token before accessing or publishing their stats."""
        calls = []

        def request(path, payload=None):
            calls.append(path)
            return {"login": "someone-else", "node_id": "OTHER"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "profile.json").write_text('{"username":"test"}')
            (root / "assets").mkdir()
            for name in ("profile.gif", "profile.png", "stats.json"):
                (root / "assets" / name).write_text("previous")
            with self.assertRaisesRegex(RuntimeError, "must belong"):
                profile.refresh(root, request)
            self.assertEqual(calls, ["user"])
            self.assertTrue(
                all(
                    path.read_text() == "previous"
                    for path in (root / "assets").iterdir()
                )
            )

    def test_snapshot_failure_preserves_outputs(self):
        def request(path, payload=None):
            if path == "user":
                return {"login": "test", "node_id": "USER"}
            if path.startswith("user/repos?"):
                return [repo("one")]
            return history([])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "profile.json").write_text('{"username":"test"}')
            (root / "assets").mkdir()
            for name in ("profile.gif", "profile.png", "stats.json"):
                (root / "assets" / name).write_text("previous")
            self.snapshot.side_effect = RuntimeError("snapshot unavailable")
            with self.assertRaisesRegex(RuntimeError, "snapshot unavailable"):
                profile.refresh(root, request)
            self.assertTrue(all(
                path.read_text() == "previous" for path in (root / "assets").iterdir()
            ))

    def test_api_and_render_failures_preserve_all_outputs(self):
        for failure in ("fetch", "render"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "profile.json").write_text('{"username":"test"}')
                (root / "assets").mkdir()
                for name in ("profile.gif", "profile.png", "stats.json"):
                    (root / "assets" / name).write_text("previous")
                with (
                    patch.object(
                        profile,
                        "collect",
                        side_effect=RuntimeError("rate limit")
                        if failure == "fetch"
                        else None,
                        return_value=stats(),
                    ),
                    patch.object(
                        profile, "generate", side_effect=RuntimeError("render failed")
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    profile.refresh(root)
                self.assertTrue(
                    all(
                        path.read_text() == "previous"
                        for path in (root / "assets").iterdir()
                    )
                )

    def test_incomplete_recent_search_preserves_outputs(self):
        searches = 0

        def request(path, payload=None):
            nonlocal searches
            if path == "user":
                return {"login": "test", "node_id": "USER"}
            if path.startswith("user/repos?"):
                return []
            searches += 1
            return {"total_count": 9, "incomplete_results": searches == 2}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "profile.json").write_text('{"username":"test"}')
            with self.assertRaises(RuntimeError):
                profile.refresh(root, request)
            self.assertEqual(searches, 2)
            self.assertFalse((root / "assets").exists())


class SnapshotTests(unittest.TestCase):
    def test_git_and_cloc_count_code_without_comments_blanks_or_generated_files(self):
        """Exercise real Git blobs and cloc, including duplicate paths and MQL5."""
        with tempfile.TemporaryDirectory() as directory:
            env = {
                **{key: value for key, value in profile.os.environ.items()
                   if not key.upper().startswith("GIT_")},
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": profile.os.devnull,
            }

            def run(*args, input=None):
                return profile.subprocess.run(
                    ["git", "-C", directory, *args], input=input, check=True, env=env,
                    stdout=profile.subprocess.PIPE, stderr=profile.subprocess.PIPE,
                    timeout=30,
                ).stdout

            run("init", "--bare")
            entries = []
            for name, mode, content in (
                ("source.py", "100644", b"# comment\r\n\r\nx = 1\r\ny = 2"),
                ("duplicate.py", "100644", b"# comment\r\n\r\nx = 1\r\ny = 2"),
                ("generated.js", "100755", b"// generated\n\ncode\n"),
                ("robot.mq5", "100644", b"// comment\nvoid OnTick() {}\n"),
                ("README.md", "100644", b"# Documentation\n\nText\n"),
                ("config.json", "100644", b'{"key": 1}\n'),
                ("api.py", "100644", b"# @generated\nx = 1\n"),
                ("binary.dat", "100644", b"\x00binary\n\n"),
                ("empty.txt", "100644", b""),
                ("symlink", "120000", b"source.py"),
            ):
                oid = run("hash-object", "-w", "--stdin", input=content).strip()
                entries.append(mode.encode() + b" blob " + oid + b"\t" + name.encode())
            tree = run("mktree", input=b"\n".join(entries) + b"\n").strip().decode()
            self.assertEqual(profile.tracked_lines(tree, run), 5)
            for suffix in (b"\tREADME.md", b"\tbinary.dat"):
                empty_source = [entry for entry in entries if entry.endswith(suffix)]
                tree = run(
                    "mktree", input=b"\n".join(empty_source) + b"\n"
                ).strip().decode()
                self.assertEqual(profile.tracked_lines(tree, run), 0)

    def test_dependency_build_generated_links_and_submodules_are_excluded(self):
        """Do not export excluded paths or indirect filesystem objects."""
        paths = [
            b"100644 blob SHA\tnode_modules/dependency.js\0",
            b"100644 blob SHA\tvendor/library.py\0",
            b"100644 blob SHA\tdist/bundle.js\0",
            b"100644 blob SHA\tgenerated/api.py\0",
            b"100644 blob SHA\tsrc/api.generated.ts\0",
            b"100644 blob SHA\tsrc/types.d.ts\0",
            b"120000 blob SHA\tsymlink.py\0",
            b"160000 commit SHA\tsubmodule\0",
        ]
        run = unittest.mock.Mock(return_value=b"".join(paths))
        self.assertEqual(profile.tracked_lines("TIP", run), 0)
        run.assert_called_once_with("ls-tree", "-r", "-z", "TIP")

    def test_empty_tree_and_invalid_batch(self):
        """Never use partial blob data as a valid snapshot."""
        self.assertEqual(profile.tracked_lines(
            "TIP", unittest.mock.Mock(return_value=b"")
        ), 0)
        for response in (b"SHA missing\n", b"OTHER blob 1\nx\n", b"SHA blob 10\nx\n"):
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                profile.tracked_lines("TIP", unittest.mock.Mock(side_effect=[
                    b"100644 blob SHA\tfile.py\0", response,
                ]))

    def test_cloc_failures_and_invalid_reports_raise(self):
        """Missing tools, timeouts and invalid counts must fail closed."""
        for result in (
            FileNotFoundError("private-path"),
            profile.subprocess.TimeoutExpired("cloc", 900),
            profile.subprocess.CalledProcessError(1, "cloc"),
            b"not json", b'{"SUM":{"code":-1},"header":{"n_files":1}}',
            b'{"SUM":{"code":true},"header":{"n_files":1}}',
            b'{"SUM":{"code":"1"},"header":{"n_files":1}}',
            b'{"header":{"n_files":1}}', b'{"header":{"n_files":0}}',
            b'{"header":null,"SUM":{}}',
            b'{"header":{"n_files":0},"SUM":{}}',
            b'{"header":{"n_files":0},"Python":{"code":100}}',
        ):
            run = unittest.mock.Mock(side_effect=[
                b"100644 blob SHA\tfile.py\0", b"SHA blob 4\nx=1\n\n",
            ])
            with (
                self.subTest(result=result),
                patch.object(
                    profile.subprocess, "run",
                    side_effect=result if isinstance(result, Exception) else None,
                    return_value=profile.subprocess.CompletedProcess([], 0, stdout=result),
                ),
                self.assertRaisesRegex(RuntimeError, "Source counting failed"),
            ):
                profile.tracked_lines("TIP", run)

    def test_export_is_isolated_and_cloc_keeps_duplicate_paths(self):
        """Flatten untrusted directories and never pass credentials to cloc."""
        run = unittest.mock.Mock(side_effect=[
            b"100644 blob SHA\t../../code.py\0", b"SHA blob 4\nx=1\n\n",
        ])

        def count(command, **kwargs):
            """Inspect exports while their temporary directory still exists."""
            root = Path(command[-1])
            self.assertEqual((root / "0" / "code.py").read_bytes(), b"x=1\n")
            self.assertIn("--skip-uniqueness", command)
            self.assertIn("--config", command)
            self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
            return profile.subprocess.CompletedProcess(
                [], 0, stdout=b'{"header":{"n_files":1},"SUM":{"code":1}}'
            )

        with patch.object(profile.subprocess, "run", side_effect=count):
            self.assertEqual(profile.tracked_lines("TIP", run), 1)

    def test_unsafe_basenames_are_rejected_without_exposing_paths(self):
        """Reject Windows aliases and separators rather than exporting ambiguously."""
        for name in (b"..", b"CON.py", b"file.py.", b"file\\other.py", b"file:stream.py"):
            run = unittest.mock.Mock(side_effect=[
                b"100644 blob SHA\t" + name + b"\0", b"SHA blob 4\nx=1\n\n",
            ])
            with self.subTest(name=name), self.assertRaisesRegex(
                RuntimeError, "^Unsupported source filename; refusing a partial refresh.$"
            ):
                profile.tracked_lines("TIP", run)

    def test_source_free_cloc_report_counts_zero(self):
        """A valid report with no recognized code is a legitimate zero."""
        run = unittest.mock.Mock(side_effect=[
            b"100644 blob SHA\tREADME.md\0", b"SHA blob 4\ntext\n",
        ])
        with patch.object(
            profile.subprocess, "run",
            return_value=profile.subprocess.CompletedProcess([], 0, stdout=b'{}\n'),
        ):
            self.assertEqual(profile.tracked_lines("TIP", run), 0)

    def test_fetch_is_pinned_and_credentials_are_not_command_arguments(self):
        sha = "a" * 40
        inherited = {
            "GIT_DIR": "caller-repository",
            "GIT_WORK_TREE": "caller-worktree",
            "GIT_OBJECT_DIRECTORY": "caller-objects",
            "GIT_CONFIG_PARAMETERS": "caller-config",
            "GIT_TRACE": "caller-trace",
        }
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "private-token", **inherited}),
            patch.object(profile.subprocess, "run", side_effect=[
                profile.subprocess.CompletedProcess([], 0, stdout=output)
                for output in (b"", b"", sha.encode() + b"\n")
            ]) as run,
            patch.object(profile, "tracked_lines", return_value=42) as count,
        ):
            self.assertEqual(profile.snapshot_lines("test", "repo", sha), 42)
        commands = [item.args[0] for item in run.call_args_list]
        self.assertEqual(commands[1][-2:], ["https://github.com/test/repo.git", sha])
        self.assertNotIn("private-token", str(commands))
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertTrue(all(key not in env for key in inherited))
        self.assertEqual(count.call_args.args[0], sha)

    def test_git_failures_are_sanitized_and_bad_revisions_rejected(self):
        for failure in (
            FileNotFoundError("private-token"),
            profile.subprocess.CalledProcessError(1, ["private-token"]),
            profile.subprocess.TimeoutExpired(["private-token"], 900),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                patch.dict("os.environ", {"GITHUB_TOKEN": "private-token"}),
                patch.object(profile.subprocess, "run", side_effect=failure),
                self.assertRaisesRegex(RuntimeError, "Git snapshot counting failed") as raised,
            ):
                profile.snapshot_lines("test", "repo", "a" * 40)
            self.assertNotIn("private-token", str(raised.exception))
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "private-token"}),
            self.assertRaisesRegex(RuntimeError, "Invalid snapshot revision"),
        ):
            profile.snapshot_lines("test", "repo", "--bad")


class MatrixTests(unittest.TestCase):
    def test_order_and_recent_line_colors_not_lifetime(self):
        with patch.object(matrix.ImageDraw, "Draw") as draw:
            matrix.information(
                PERSON,
                stats(
                    lines=999,
                    additions=9999,
                    deletions=9000,
                    recent_additions=12,
                    recent_deletions=5,
                ),
            )
        calls = draw.return_value.text.call_args_list
        contents = [call.args[1] for call in calls]
        self.assertLess(contents.index("Repos"), contents.index("PRs"))
        self.assertLess(contents.index("PRs"), contents.index("Commits"))
        self.assertIn("999 (", contents)
        self.assertNotIn(" · 24h)", contents)
        self.assertIn(
            ("+12", "#54ff89"), [(call.args[1], call.kwargs["fill"]) for call in calls]
        )
        self.assertIn(
            ("-5", "#ff7b81"), [(call.args[1], call.kwargs["fill"]) for call in calls]
        )
        self.assertNotIn("+9,999", contents)

    def test_activity_badges_hidden_when_zero(self):
        for prs, commits in ((0, 0), (5, 0), (0, 3), (5, 3)):
            with patch.object(matrix.ImageDraw, "Draw") as draw:
                matrix.information(
                    PERSON, stats(recent_prs=prs, recent_commits=commits)
                )
            badges = [
                call
                for call in draw.return_value.text.call_args_list
                if call.args[1].startswith(" (+")
            ]
            self.assertEqual(len(badges), int(prs > 0) + int(commits > 0))
            self.assertTrue(all(call.kwargs["fill"] == "#54ff89" for call in badges))

    def test_gif_png_valid_and_deterministic(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(matrix, "FRAMES", 4),
        ):
            destination = Path(directory) / "profile.gif"
            matrix.generate(PERSON, stats(), destination)
            first = hashlib.sha256(destination.read_bytes()).digest()
            with Image.open(destination) as image:
                self.assertEqual(image.n_frames, 4)
                self.assertEqual(image.size, (900, 610))
                self.assertEqual(image.info["loop"], 0)
            with Image.open(destination.with_suffix(".png")) as image:
                self.assertEqual(image.size, (900, 610))
            matrix.generate(PERSON, stats(), destination)
            self.assertEqual(hashlib.sha256(destination.read_bytes()).digest(), first)


if __name__ == "__main__":
    unittest.main()
