from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tmux_worktrees.model import (
    ParentSource,
    Repository,
    parse_worktree_porcelain,
)
from tmux_worktrees.process import CommandError, CommandResult, Runner


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


class TemporaryRepository:
    def __init__(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        self.root.mkdir()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Test User")
        (self.root / "README.md").write_text("root\n")
        git(self.root, "add", "README.md")
        git(self.root, "commit", "-m", "initial")
        info_exclude = self.root / ".git" / "info" / "exclude"
        with info_exclude.open("a") as file:
            file.write("\n.worktrees/\n")

    def close(self):
        self.tempdir.cleanup()

    def add_commit(self, worktree: Path, filename: str, contents: str) -> None:
        (worktree / filename).write_text(contents)
        git(worktree, "add", filename)
        git(worktree, "commit", "-m", f"add {filename}")


class IgnoredStatusFailureRunner(Runner):
    def run(self, args, **kwargs):
        command = tuple(str(item) for item in args)
        if command and command[0] == "git" and "--ignored" in command:
            return CommandResult(command, 128, "", "ignored scan failed")
        return super().run(args, **kwargs)


class FakeGitHubRunner(Runner):
    def __init__(self, pull_requests: list[dict], involved_heads: set[str] | None = None):
        super().__init__()
        self.pull_requests = pull_requests
        self.involved_heads = involved_heads or set()

    def run(self, args, **kwargs):
        command = tuple(str(item) for item in args)
        if command[:3] == ("gh", "pr", "list"):
            rows = self.pull_requests
            if "--search" in command:
                rows = [
                    {"headRefName": row["headRefName"]}
                    for row in rows
                    if row["headRefName"] in self.involved_heads
                ]
            return CommandResult(command, 0, json.dumps(rows), "")
        return super().run(args, **kwargs)


class WorktreePorcelainTests(unittest.TestCase):
    def test_parses_attached_detached_and_flags(self):
        output = (
            b"worktree /tmp/repo\0HEAD abc\0branch refs/heads/main\0\0"
            b"worktree /tmp/repo/.worktrees/child\0HEAD def\0detached\0locked reason\0\0"
        )
        worktrees = parse_worktree_porcelain(output)
        self.assertEqual(2, len(worktrees))
        self.assertEqual("main", worktrees[0].branch)
        self.assertTrue(worktrees[1].detached)
        self.assertEqual("reason", worktrees[1].locked)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = TemporaryRepository()

    def tearDown(self):
        self.repository.close()

    def test_local_metadata_builds_nested_hierarchy(self):
        repo = Repository.discover(self.repository.root)
        feature_a = repo.add_worktree("feature-a", "main")
        self.repository.add_commit(feature_a.path, "a.txt", "a\n")

        repo = Repository.discover(self.repository.root)
        feature_b = repo.add_worktree("feature-b", "feature-a")
        repo.set_registered("feature-a", True)
        repo.set_registered("feature-b", True)
        repo = Repository.discover(self.repository.root)
        hierarchy = repo.hierarchy()

        self.assertEqual(feature_a.id, hierarchy.nodes[feature_b.id].parent_id)
        self.assertEqual(ParentSource.LOCAL, hierarchy.nodes[feature_b.id].source)

    def test_virtual_parent_is_projected_to_visible_worktree(self):
        repo = Repository.discover(self.repository.root)
        feature_a = repo.add_worktree("feature-a", "main")
        self.repository.add_commit(feature_a.path, "a.txt", "a\n")
        git(self.repository.root, "branch", "bridge", "feature-a")
        repo.set_local_parent("bridge", "feature-a")
        repo.set_registered("feature-a", True)
        repo.set_registered("bridge", True)
        git(self.repository.root, "branch", "feature-b", "bridge")
        feature_b = repo.add_worktree("feature-b", "bridge")
        repo.set_registered("feature-b", True)

        repo = Repository.discover(self.repository.root)
        node = repo.hierarchy().nodes[feature_b.id]
        self.assertEqual(feature_a.id, node.parent_id)
        self.assertEqual(["bridge"], node.skipped_parents)

    def test_explicit_parent_survives_parent_branch_advancing(self):
        repo = Repository.discover(self.repository.root)
        feature_a = repo.add_worktree("feature-a", "main")
        self.repository.add_commit(feature_a.path, "a.txt", "a\n")
        repo = Repository.discover(self.repository.root)
        feature_b = repo.add_worktree("feature-b", "feature-a")
        repo.set_registered("feature-a", True)
        repo.set_registered("feature-b", True)
        self.repository.add_commit(feature_a.path, "later.txt", "later\n")

        repo = Repository.discover(self.repository.root)
        node = repo.hierarchy().nodes[feature_b.id]
        self.assertEqual(ParentSource.LOCAL, node.source)
        self.assertEqual(feature_a.id, node.parent_id)

    def test_unregistered_branch_is_flat_without_persisting_parent(self):
        git(self.repository.root, "branch", "parent", "main")
        git(self.repository.root, "branch", "child", "parent")
        repo = Repository.discover(self.repository.root)

        info = repo.parent_for_virtual_branch("child")

        self.assertEqual("main", info.parent)
        self.assertEqual(ParentSource.UNREGISTERED, info.source)
        self.assertIsNone(repo.local_parent("child"))

    def test_registered_pr_seed_imports_open_descendant_stack(self):
        git(self.repository.root, "branch", "search", "main")
        runner = FakeGitHubRunner(
            [
                {"number": 1, "title": "search", "headRefName": "search", "baseRefName": "main", "isDraft": True, "url": "https://example/1"},
                {"number": 2, "title": "child", "headRefName": "search-child", "baseRefName": "search", "isDraft": False, "url": "https://example/2"},
                {"number": 3, "title": "leaf", "headRefName": "search-leaf", "baseRefName": "search-child", "isDraft": False, "url": "https://example/3"},
                {"number": 4, "title": "unrelated", "headRefName": "other", "baseRefName": "main", "isDraft": False, "url": "https://example/4"},
            ]
        )
        repo = Repository.discover(self.repository.root, runner)
        repo.set_registered("search", True)

        registered = repo.github.registered_pull_requests()

        self.assertEqual({"search", "search-child", "search-leaf"}, set(registered))
        self.assertEqual(ParentSource.GITHUB, repo.parent_for_virtual_branch("search-leaf").source)
        self.assertEqual("search-child", repo.parent_for_virtual_branch("search-leaf").parent)

    def test_managed_pr_leaf_imports_ancestors_to_trunk(self):
        repo = Repository.discover(self.repository.root)
        leaf = repo.add_worktree("search-leaf", "main")
        runner = FakeGitHubRunner(
            [
                {"number": 1, "title": "root", "headRefName": "search-root", "baseRefName": "main", "isDraft": False, "url": "https://example/1"},
                {"number": 2, "title": "child", "headRefName": "search-child", "baseRefName": "search-root", "isDraft": False, "url": "https://example/2"},
                {"number": 3, "title": "leaf", "headRefName": "search-leaf", "baseRefName": "search-child", "isDraft": False, "url": "https://example/3"},
                {"number": 4, "title": "other", "headRefName": "other", "baseRefName": "main", "isDraft": False, "url": "https://example/4"},
            ]
        )
        repo = Repository.discover(self.repository.root, runner)

        registered = repo.github.registered_pull_requests()

        self.assertEqual({"search-root", "search-child", "search-leaf"}, set(registered))
        self.assertEqual(leaf.id, repo.worktree_for_branch("search-leaf").id)

    def test_involved_pr_imports_its_full_stack(self):
        runner = FakeGitHubRunner(
            [
                {"number": 1, "title": "root", "headRefName": "search-root", "baseRefName": "main", "isDraft": False, "url": "https://example/1"},
                {"number": 2, "title": "child", "headRefName": "search-child", "baseRefName": "search-root", "isDraft": False, "url": "https://example/2"},
                {"number": 3, "title": "leaf", "headRefName": "search-leaf", "baseRefName": "search-child", "isDraft": False, "url": "https://example/3"},
                {"number": 4, "title": "other", "headRefName": "other", "baseRefName": "main", "isDraft": False, "url": "https://example/4"},
            ],
            involved_heads={"search-child"},
        )
        repo = Repository.discover(self.repository.root, runner)

        registered = repo.github.registered_pull_requests()

        self.assertEqual({"search-root", "search-child", "search-leaf"}, set(registered))
        self.assertEqual("search-root", repo.local_parent("search-child"))
        self.assertIn("search-child", repo.registered_branches())

    def test_registered_worktree_survives_pr_closure_as_local(self):
        repo = Repository.discover(self.repository.root)
        feature = repo.add_worktree("feature", "main")
        runner = FakeGitHubRunner(
            [
                {"number": 1, "title": "feature", "headRefName": "feature", "baseRefName": "main", "isDraft": False, "url": "https://example/1"},
            ],
            involved_heads={"feature"},
        )
        open_repo = Repository.discover(self.repository.root, runner)
        self.assertEqual(
            ParentSource.GITHUB,
            open_repo.hierarchy().nodes[feature.id].source,
        )

        closed_repo = Repository.discover(
            self.repository.root,
            FakeGitHubRunner([]),
        )

        self.assertEqual(
            ParentSource.LOCAL,
            closed_repo.hierarchy().nodes[feature.id].source,
        )
        self.assertIn("feature", closed_repo.registered_branches())

    def test_global_parent_metadata_is_ignored(self):
        config = Path(self.repository.tempdir.name) / "global.gitconfig"
        config.write_text(
            '[branch "feature"]\n\ttmux-worktrees-parent = unrelated\n'
        )
        repo = Repository.discover(
            self.repository.root,
            Runner({"GIT_CONFIG_GLOBAL": str(config)}),
        )

        self.assertIsNone(repo.local_parent("feature"))

    def test_remove_refuses_dirty_worktree(self):
        repo = Repository.discover(self.repository.root)
        feature = repo.add_worktree("feature", "main")
        (feature.path / "dirty.txt").write_text("dirty\n")
        repo = Repository.discover(self.repository.root)
        feature = repo.worktree_for_branch("feature")
        self.assertIsNotNone(feature)
        with self.assertRaisesRegex(RuntimeError, "dirty"):
            repo.remove_worktree(feature)

    def test_safe_remove_keeps_branch(self):
        repo = Repository.discover(self.repository.root)
        feature = repo.add_worktree("feature", "main")
        repo = Repository.discover(self.repository.root)
        feature = repo.worktree_for_branch("feature")
        self.assertIsNotNone(feature)
        repo.remove_worktree(feature)
        self.assertFalse(feature.path.exists())
        self.assertTrue(repo.branch_exists("feature"))

    def test_remove_does_not_persist_unregistered_parent(self):
        repo = Repository.discover(self.repository.root)
        feature = repo.add_worktree("feature", "main")
        git(self.repository.root, "config", "--unset", "branch.feature.tmux-worktrees-parent")
        repo = Repository.discover(self.repository.root)
        feature = repo.worktree_for_branch("feature")
        self.assertIsNotNone(feature)
        node = repo.hierarchy().nodes[feature.id]
        self.assertEqual(ParentSource.UNREGISTERED, node.source)

        repo.remove_worktree(feature)

        refreshed = Repository.discover(self.repository.root)
        self.assertIsNone(refreshed.local_parent("feature"))
        self.assertTrue(refreshed.branch_exists("feature"))

    def test_failed_remove_rolls_back_recovery_parent(self):
        info_exclude = self.repository.root / ".git" / "info" / "exclude"
        with info_exclude.open("a") as file:
            file.write("*.cache\n")
        repo = Repository.discover(self.repository.root)
        feature = repo.add_worktree("feature", "main")
        git(self.repository.root, "config", "--unset", "branch.feature.tmux-worktrees-parent")
        (feature.path / "build.cache").write_text("generated\n")
        repo = Repository.discover(self.repository.root)
        feature = repo.worktree_for_branch("feature")
        self.assertIsNotNone(feature)

        with self.assertRaisesRegex(RuntimeError, "ignored"):
            repo.remove_worktree(feature, preserve_parent="main")

        self.assertIsNone(repo.local_parent("feature"))
        self.assertTrue(feature.path.exists())

    def test_branch_deletion_requires_tip_to_be_retained(self):
        repo = Repository.discover(self.repository.root)
        feature = repo.add_worktree("feature", "main")
        self.repository.add_commit(feature.path, "feature.txt", "feature\n")
        repo = Repository.discover(self.repository.root)
        self.assertFalse(repo.branch_is_retained("feature", "main"))
        git(self.repository.root, "merge", "--ff-only", "feature")
        self.assertTrue(repo.branch_is_retained("feature", "main"))

    def test_nested_local_branch_deletes_against_logical_parent(self):
        repo = Repository.discover(self.repository.root)
        parent = repo.add_worktree("feature-a", "main")
        self.repository.add_commit(parent.path, "a.txt", "a\n")
        repo = Repository.discover(self.repository.root)
        child = repo.add_worktree("feature-b", "feature-a")
        self.repository.add_commit(child.path, "b.txt", "b\n")
        git(parent.path, "merge", "--ff-only", "feature-b")
        repo = Repository.discover(self.repository.root)
        child = repo.worktree_for_branch("feature-b")
        self.assertIsNotNone(child)
        repo.remove_worktree(child)
        repo.delete_local_branch("feature-b", "feature-a")
        self.assertFalse(repo.branch_exists("feature-b"))

    def test_metadata_failure_keeps_branch_before_local_delete(self):
        repo = Repository.discover(self.repository.root)
        repo.add_worktree("feature", "main")
        git(self.repository.root, "branch", "child", "feature")
        repo.set_local_parent("child", "feature")
        config_lock = self.repository.root / ".git" / "config.lock"
        config_lock.write_text("locked\n")
        try:
            with self.assertRaises(CommandError):
                repo.delete_local_branch("feature", "main")
        finally:
            config_lock.unlink()
        self.assertTrue(repo.branch_exists("feature"))
        self.assertEqual("feature", repo.local_parent("child"))

    def test_remove_requires_confirmation_for_ignored_paths(self):
        info_exclude = self.repository.root / ".git" / "info" / "exclude"
        with info_exclude.open("a") as file:
            file.write("*.cache\n")
        repo = Repository.discover(self.repository.root)
        feature = repo.add_worktree("feature", "main")
        (feature.path / "build.cache").write_text("generated\n")
        repo = Repository.discover(self.repository.root)
        feature = repo.worktree_for_branch("feature")
        self.assertIsNotNone(feature)
        self.assertEqual(["build.cache"], repo.ignored_paths(feature))
        with self.assertRaisesRegex(RuntimeError, "ignored"):
            repo.remove_worktree(feature)
        snapshot = repo.ignored_snapshot(feature, ["build.cache"])
        repo.remove_worktree(feature, confirmed_ignored=snapshot)
        self.assertFalse(feature.path.exists())

    def test_remove_rejects_changed_ignored_snapshot(self):
        info_exclude = self.repository.root / ".git" / "info" / "exclude"
        with info_exclude.open("a") as file:
            file.write("*.cache\n")
        repo = Repository.discover(self.repository.root)
        feature = repo.add_worktree("feature", "main")
        (feature.path / "known.cache").write_text("known\n")
        confirmed = repo.ignored_snapshot(feature, repo.ignored_paths(feature))
        (feature.path / "late.cache").write_text("late\n")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            repo.remove_worktree(feature, confirmed_ignored=confirmed)
        self.assertTrue(feature.path.exists())

    def test_remove_detects_new_file_inside_confirmed_ignored_directory(self):
        info_exclude = self.repository.root / ".git" / "info" / "exclude"
        with info_exclude.open("a") as file:
            file.write("cache/\n")
        repo = Repository.discover(self.repository.root)
        feature = repo.add_worktree("feature", "main")
        cache = feature.path / "cache"
        cache.mkdir()
        (cache / "known").write_text("known\n")
        confirmed = repo.ignored_snapshot(feature, repo.ignored_paths(feature))
        (cache / "late-secret").write_text("secret\n")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            repo.remove_worktree(feature, confirmed_ignored=confirmed)
        self.assertTrue((cache / "late-secret").exists())

    def test_remove_fails_closed_when_ignored_scan_fails(self):
        repo = Repository.discover(self.repository.root)
        feature = repo.add_worktree("feature", "main")
        failing = Repository.discover(
            self.repository.root, IgnoredStatusFailureRunner()
        )
        failing_feature = failing.worktree_for_branch("feature")
        self.assertIsNotNone(failing_feature)
        with self.assertRaisesRegex(RuntimeError, "ignored scan failed"):
            failing.remove_worktree(failing_feature)
        self.assertTrue(feature.path.exists())

    def test_custom_managed_directory_is_canonicalized(self):
        git(self.repository.root, "config", "tmux-worktrees.directory", "../managed")
        repo = Repository.discover(self.repository.root)
        feature = repo.add_worktree("feature", "main")
        expected = self.repository.root.parent / "managed" / "feature"
        self.assertEqual(expected.resolve(), feature.path)
        refreshed = Repository.discover(self.repository.root)
        self.assertIsNotNone(refreshed.managed_worktree(expected))

    def test_gitignore_metacharacters_are_escaped(self):
        git(self.repository.root, "config", "tmux-worktrees.directory", "#trees")
        repo = Repository.discover(self.repository.root)
        repo.add_worktree("feature", "main")
        status = git(self.repository.root, "status", "--porcelain")
        self.assertNotIn("#trees", status)

    def test_equal_tip_siblings_do_not_form_cycle(self):
        repo = Repository.discover(self.repository.root)
        first = repo.add_worktree("feature-a", "main")
        repo = Repository.discover(self.repository.root)
        second = repo.add_worktree("feature-b", "main")
        git(self.repository.root, "config", "--unset", "branch.feature-a.tmux-worktrees-parent")
        git(self.repository.root, "config", "--unset", "branch.feature-b.tmux-worktrees-parent")
        self.repository.add_commit(self.repository.root, "main.txt", "main\n")
        repo = Repository.discover(self.repository.root)
        hierarchy = repo.hierarchy()
        self.assertEqual(repo.root_worktree.id, hierarchy.nodes[first.id].parent_id)
        self.assertEqual(repo.root_worktree.id, hierarchy.nodes[second.id].parent_id)
        self.assertEqual(3, len(hierarchy.ordered_ids()))

    def test_explicit_parent_cycle_is_broken(self):
        repo = Repository.discover(self.repository.root)
        first = repo.add_worktree("feature-a", "main")
        repo = Repository.discover(self.repository.root)
        second = repo.add_worktree("feature-b", "main")
        repo.set_local_parent("feature-a", "feature-b")
        repo.set_local_parent("feature-b", "feature-a")
        repo = Repository.discover(self.repository.root)
        hierarchy = repo.hierarchy()
        self.assertEqual(3, len(hierarchy.ordered_ids()))
        self.assertTrue(hierarchy.nodes[first.id].warnings)
        self.assertTrue(hierarchy.nodes[second.id].warnings)

    def test_inherited_git_directory_does_not_redirect_discovery(self):
        other = self.repository.root.parent / "other"
        other.mkdir()
        git(other, "init", "-b", "main")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}, clear=False):
            repo = Repository.discover(self.repository.root, Runner())
        self.assertEqual(self.repository.root.resolve(), repo.root)

    def test_bare_repository_is_rejected(self):
        bare = self.repository.root.parent / "bare.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
        with self.assertRaisesRegex(RuntimeError, "bare"):
            Repository.discover(bare)


if __name__ == "__main__":
    unittest.main()
