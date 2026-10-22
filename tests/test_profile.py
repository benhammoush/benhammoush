"""Offline checks for public counting, failure preservation, and Matrix output."""

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

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


class StatsTests(unittest.TestCase):
    def test_pagination_visibility_empty_and_zero_size_repos(self):
        calls = []

        def request(path, payload=None):
            calls.append((path, payload))
            if path == "users/test":
                return {"node_id": "USER"}
            if path.startswith("users/"):
                if "&page=1" in path:
                    return [
                        repo("one"),
                        repo("test"),
                        repo("empty", size=0),
                        repo("secret", private=True),
                        repo("fork", fork=True),
                    ] + [repo(f"f{i}", fork=True) for i in range(95)]
                return [repo("two", size=0)]
            if path.startswith("search/"):
                self.assertIn("is%3Apublic", path)
                return {"total_count": 9, "incomplete_results": False}
            variables = payload["variables"]
            self.assertEqual(variables["author"], "USER")
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
                repos=4,
                commits=2,
                prs=9,
                recent_prs=9,
                recent_commits=1,
                additions=20,
                deletions=6,
                lines=14,
                recent_additions=10,
                recent_deletions=3,
            ),
        )
        self.assertEqual(len([call for call in calls if call[0] == "graphql"]), 4)

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
            if path == "users/test":
                return {"node_id": "USER"}
            if path.startswith("users/"):
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
            (5, 10, 7),
        )

    def test_rejects_partial_search_and_changed_visibility(self):
        for private in (True, False):

            def request(path, payload=None, private=private):
                if path == "users/test":
                    return {"node_id": "USER"}
                if path.startswith("users/"):
                    return [repo("one")] if private else []
                if path == "graphql":
                    return {"data": {"repository": {"isPrivate": True}}}
                return {"total_count": 3, "incomplete_results": True}

            with self.assertRaises(RuntimeError):
                profile.collect("test", request, NOW)

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
            if path == "users/test":
                return {"node_id": "USER"}
            if path.startswith("users/"):
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
        self.assertIn(" · 24h)", contents)
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
