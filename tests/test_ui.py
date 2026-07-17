from __future__ import annotations

import json
import shutil
import unittest
import uuid
from unittest import mock

from tests.test_model import FakeGitHubRunner, TemporaryRepository, git
from tmux_worktrees.model import ParentSource, Repository
from tmux_worktrees.tmux import TmuxManager
from tmux_worktrees.ui import (
    Picker,
    VirtualBranchNode,
    doctor,
    hierarchy_as_json,
    render_hierarchy,
)


class VirtualBranchTests(unittest.TestCase):
    def setUp(self):
        self.repository = TemporaryRepository()

    def tearDown(self):
        self.repository.close()

    def test_render_includes_explicit_branch_only_stack(self):
        git(self.repository.root, "branch", "parent", "main")
        git(self.repository.root, "branch", "child", "parent")
        git(self.repository.root, "branch", "other-stack", "main")
        repo = Repository.discover(self.repository.root)
        repo.add_worktree("child", "parent")
        repo.set_local_parent("parent", "main")
        repo.set_local_parent("child", "parent")
        repo.set_local_parent("other-stack", "main")
        repo.set_registered("parent", True)
        repo.set_registered("child", True)
        repo.set_registered("other-stack", True)
        repo = Repository.discover(self.repository.root)

        rows = render_hierarchy(repo, repo.hierarchy(), include_inactive=True)
        displays = [display for display, _ in rows]
        parent_index = next(index for index, value in enumerate(displays) if "parent  [L branch]" in value)
        child_index = next(index for index, value in enumerate(displays) if "child  [L]" in value)

        self.assertLess(parent_index, child_index)
        self.assertIn("└─ child", displays[child_index])
        self.assertTrue(any("other-stack  [L branch]" in value for value in displays))
        parent_node = rows[parent_index][1]
        self.assertIsInstance(parent_node, VirtualBranchNode)
        self.assertEqual("main", parent_node.direct_parent)
        recovery_json = json.loads(
            hierarchy_as_json(repo, repo.hierarchy(), include_inactive=True)
        )
        parent_json_index = next(
            index
            for index, item in enumerate(recovery_json)
            if item["id"] == "branch:parent"
        )
        child_json_index = next(
            index
            for index, item in enumerate(recovery_json)
            if item["branch"] == "child"
        )
        self.assertLess(parent_json_index, child_json_index)
        self.assertEqual(
            "branch:parent", recovery_json[child_json_index]["visible_parent_id"]
        )

    def test_default_render_only_includes_active_managed_worktrees(self):
        git(self.repository.root, "branch", "inactive", "main")
        git(
            self.repository.root,
            "config",
            "branch.inactive.tmux-worktrees-parent",
            "main",
        )
        repo = Repository.discover(self.repository.root)

        rows = render_hierarchy(repo, repo.hierarchy())

        self.assertFalse(any("inactive" in display for display, _ in rows))
        self.assertEqual(1, len(rows))

    def test_active_unregistered_worktree_remains_visible(self):
        repo = Repository.discover(self.repository.root)
        worktree = repo.add_worktree("feature", "main")
        repo.unset_local_parent("feature")
        repo = Repository.discover(self.repository.root)

        rows = render_hierarchy(repo, repo.hierarchy())

        self.assertTrue(
            any(
                node.worktree.id == worktree.id and "feature  [U]" in display
                for display, node in rows
                if hasattr(node, "worktree")
            )
        )

    def test_default_render_keeps_inactive_bridge_between_active_worktrees(self):
        git(self.repository.root, "branch", "bridge", "main")
        git(self.repository.root, "branch", "child", "bridge")
        repo = Repository.discover(self.repository.root)
        child = repo.add_worktree("child", "bridge")
        repo.set_local_parent("bridge", "main")
        repo.set_local_parent("child", "bridge")
        repo.set_registered("bridge", True)
        repo.set_registered("child", True)
        repo = Repository.discover(self.repository.root)

        rows = render_hierarchy(repo, repo.hierarchy())
        displays = [display for display, _ in rows]

        bridge_index = next(
            index for index, display in enumerate(displays) if "bridge  [L branch]" in display
        )
        child_index = next(
            index for index, display in enumerate(displays) if "child  [L]" in display
        )
        self.assertLess(bridge_index, child_index)
        self.assertIn("└─ child", displays[child_index])
        self.assertEqual(child.id, rows[child_index][1].worktree.id)

    def test_default_render_hides_missing_registered_worktree(self):
        repo = Repository.discover(self.repository.root)
        worktree = repo.add_worktree("missing", "main")
        shutil.rmtree(worktree.path)
        repo = Repository.discover(self.repository.root)

        active_rows = render_hierarchy(repo, repo.hierarchy())
        recovery_rows = render_hierarchy(
            repo, repo.hierarchy(), include_inactive=True
        )

        self.assertFalse(any("missing" in display for display, _ in active_rows))
        self.assertTrue(any("missing" in display for display, _ in recovery_rows))

        active_json = json.loads(hierarchy_as_json(repo, repo.hierarchy()))
        recovery_json = json.loads(
            hierarchy_as_json(repo, repo.hierarchy(), include_inactive=True)
        )
        self.assertFalse(any(item["branch"] == "missing" for item in active_json))
        self.assertTrue(any(item["branch"] == "missing" for item in recovery_json))

    def test_nearest_active_parent_skips_missing_registration(self):
        repo = Repository.discover(self.repository.root)
        parent = repo.add_worktree("parent", "main")
        repo = Repository.discover(self.repository.root)
        child = repo.add_worktree("child", "parent")
        shutil.rmtree(parent.path)
        repo = Repository.discover(self.repository.root)
        node = repo.hierarchy().nodes[child.id]

        selected_parent = Picker(repo)._nearest_active_parent(repo.hierarchy(), node)

        self.assertEqual(repo.root_worktree.id, selected_parent.id)

    def test_detached_worktree_cannot_be_deactivated(self):
        path = self.repository.root / ".worktrees" / "detached"
        git(self.repository.root, "worktree", "add", "--detach", str(path), "main")
        repo = Repository.discover(self.repository.root)
        worktree = repo.managed_worktree(path)
        self.assertIsNotNone(worktree)
        node = repo.hierarchy().nodes[worktree.id]

        with self.assertRaisesRegex(RuntimeError, "create a branch"):
            Picker(repo)._remove(node, repo.hierarchy(), delete_branch=False)

    def test_missing_locked_worktree_rejects_actions(self):
        repo = Repository.discover(self.repository.root)
        worktree = repo.add_worktree("locked-missing", "main")
        git(self.repository.root, "worktree", "lock", str(worktree.path))
        shutil.rmtree(worktree.path)
        repo = Repository.discover(self.repository.root)
        worktree = repo.worktree_for_branch("locked-missing")
        self.assertIsNotNone(worktree)
        self.assertIsNotNone(worktree.locked)
        node = repo.hierarchy().nodes[worktree.id]

        with self.assertRaisesRegex(RuntimeError, "path is missing"):
            Picker(repo)._handle("ctrl-a", node, repo.hierarchy())

    def test_opening_virtual_branch_creates_worktree_and_session(self):
        git(self.repository.root, "branch", "parent", "main")
        repo = Repository.discover(self.repository.root)
        tmux = TmuxManager(server_name=f"virtual-{uuid.uuid4().hex}")
        switched: list[str] = []
        original_switch = tmux.switch_worktree

        def record_switch(repo, worktree, session):
            tmux.remember_worktree(repo, worktree)
            switched.append(session.id)

        tmux.switch_worktree = record_switch
        try:
            Picker(repo, tmux=tmux)._open_virtual_branch(
                VirtualBranchNode("parent", "main", ParentSource.UNREGISTERED)
            )
            refreshed = Repository.discover(self.repository.root)
            worktree = refreshed.worktree_for_branch("parent")
            self.assertIsNotNone(worktree)
            self.assertIsNotNone(refreshed.managed_worktree(worktree.path))
            self.assertIsNone(refreshed.local_parent("parent"))
            self.assertEqual(1, len(switched))
        finally:
            tmux.switch_worktree = original_switch
            tmux.run(["kill-server"], check=False)

    def test_registered_pr_seed_renders_remote_descendants(self):
        repo = Repository.discover(self.repository.root)
        search = repo.add_worktree("search", "main")
        repo.set_registered("search", True)
        runner = FakeGitHubRunner(
            [
                {"number": 1, "title": "search", "headRefName": "search", "baseRefName": "main", "isDraft": True, "url": "https://example/1"},
                {"number": 2, "title": "child", "headRefName": "search-child", "baseRefName": "search", "isDraft": False, "url": "https://example/2"},
                {"number": 3, "title": "leaf", "headRefName": "search-leaf", "baseRefName": "search-child", "isDraft": False, "url": "https://example/3"},
                {"number": 4, "title": "other", "headRefName": "other", "baseRefName": "main", "isDraft": False, "url": "https://example/4"},
            ]
        )
        repo = Repository.discover(self.repository.root, runner)

        displays = [display for display, _ in render_hierarchy(repo, repo.hierarchy())]

        self.assertTrue(any("search  [P]" in display for display in displays))
        self.assertTrue(any("search-child  [P branch]" in display for display in displays))
        self.assertTrue(any("search-leaf  [P branch]" in display for display in displays))
        self.assertFalse(any("other" in display for display in displays))
        self.assertIsNotNone(repo.managed_worktree(search.path))

        all_rows = render_hierarchy(repo, repo.hierarchy(), include_inactive=True)
        other = next(
            node
            for _, node in all_rows
            if isinstance(node, VirtualBranchNode) and node.branch == "other"
        )
        refreshed, _ = Picker(repo)._refresh_picker_node(other, exact=False)
        self.assertEqual("other", refreshed.branch)

    def test_removed_local_worktree_remains_as_branch_only_node(self):
        repo = Repository.discover(self.repository.root)
        worktree = repo.add_worktree("local-feature", "main")
        repo.set_registered("local-feature", True)
        repo = Repository.discover(self.repository.root)
        worktree = repo.managed_worktree(worktree.path)
        self.assertIsNotNone(worktree)
        repo.remove_worktree(worktree)
        refreshed = Repository.discover(self.repository.root)

        rows = render_hierarchy(refreshed, refreshed.hierarchy(), include_inactive=True)
        default_rows = render_hierarchy(refreshed, refreshed.hierarchy())

        self.assertTrue(
            any("local-feature  [L branch]" in display for display, _ in rows)
        )
        self.assertTrue(
            any("local-feature  [L branch]" in display for display, _ in default_rows)
        )

    def test_disappearing_virtual_branch_is_not_recreated(self):
        git(self.repository.root, "branch", "victim", "main")
        victim_tip = git(self.repository.root, "rev-parse", "victim")
        git(
            self.repository.root,
            "update-ref",
            "refs/remotes/origin/victim",
            victim_tip,
        )
        repo = Repository.discover(self.repository.root)
        picker = Picker(repo, tmux=TmuxManager(server_name=f"race-{uuid.uuid4().hex}"))
        original_exists = repo.branch_exists
        first_call = True

        def remove_after_check(branch: str) -> bool:
            nonlocal first_call
            exists = original_exists(branch)
            if branch == "victim" and first_call and exists:
                first_call = False
                git(self.repository.root, "update-ref", "-d", "refs/heads/victim")
            return exists

        repo.branch_exists = remove_after_check
        try:
            with self.assertRaises(RuntimeError):
                picker._open_virtual_branch(
                    VirtualBranchNode("victim", "main", ParentSource.LOCAL)
                )
            self.assertFalse(original_exists("victim"))
            self.assertIsNone(Repository.discover(self.repository.root).worktree_for_branch("victim"))
        finally:
            picker.tmux.run(["kill-server"], check=False)

    def test_virtual_parent_cycle_is_broken_and_remains_visible(self):
        git(self.repository.root, "branch", "a", "main")
        git(self.repository.root, "branch", "b", "main")
        git(self.repository.root, "branch", "child", "main")
        repo = Repository.discover(self.repository.root)
        repo.add_worktree("child", "a")
        repo.set_local_parent("a", "b")
        repo.set_local_parent("b", "a")
        repo.set_local_parent("child", "a")
        repo.set_registered("a", True)
        repo.set_registered("b", True)
        repo.set_registered("child", True)
        refreshed = Repository.discover(self.repository.root)

        rows = render_hierarchy(refreshed, refreshed.hierarchy(), include_inactive=True)
        displays = [display for display, _ in rows]

        self.assertTrue(any("a  [L branch]" in value for value in displays))
        self.assertTrue(any("b  [L branch]" in value for value in displays))
        self.assertTrue(any("child  [L]" in value for value in displays))

    def test_external_worktree_branch_is_marked_non_actionable(self):
        external_path = self.repository.root.parent / "external"
        git(self.repository.root, "branch", "external", "main")
        git(self.repository.root, "config", "branch.external.tmux-worktrees-parent", "main")
        git(self.repository.root, "config", "branch.external.tmux-worktrees-registered", "true")
        git(self.repository.root, "worktree", "add", str(external_path), "external")
        repo = Repository.discover(self.repository.root)

        rows = render_hierarchy(repo, repo.hierarchy(), include_inactive=True)
        external = next(
            node for display, node in rows if "external  [L external]" in display
        )

        self.assertIsInstance(external, VirtualBranchNode)
        self.assertEqual(external_path.resolve(), external.external_path)

    def test_untracked_external_worktree_appears_in_recovery_view(self):
        repo = Repository.discover(self.repository.root)
        parent = repo.add_worktree("managed-parent", "main")
        self.repository.add_commit(parent.path, "parent.txt", "parent\n")
        external_path = self.repository.root.parent / "ordinary-external"
        git(self.repository.root, "branch", "ordinary-external", "managed-parent")
        git(
            self.repository.root,
            "worktree",
            "add",
            str(external_path),
            "ordinary-external",
        )
        repo = Repository.discover(self.repository.root)

        rows = render_hierarchy(repo, repo.hierarchy(), include_inactive=True)
        recovery_json = json.loads(
            hierarchy_as_json(repo, repo.hierarchy(), include_inactive=True)
        )

        self.assertTrue(
            any("ordinary-external  [U external]" in display for display, _ in rows)
        )
        external_node = next(
            node
            for _, node in rows
            if isinstance(node, VirtualBranchNode)
            and node.branch == "ordinary-external"
        )
        self.assertEqual("main", external_node.direct_parent)
        self.assertTrue(
            any(
                item["kind"] == "external"
                and item["branch"] == "ordinary-external"
                for item in recovery_json
            )
        )

    def test_unregistered_external_stack_is_flat(self):
        parent_path = self.repository.root.parent / "external-parent"
        child_path = self.repository.root.parent / "external-child"
        git(self.repository.root, "branch", "external-parent", "main")
        git(self.repository.root, "worktree", "add", str(parent_path), "external-parent")
        self.repository.add_commit(parent_path, "parent.txt", "parent\n")
        git(self.repository.root, "branch", "external-child", "external-parent")
        git(self.repository.root, "worktree", "add", str(child_path), "external-child")
        self.repository.add_commit(child_path, "child.txt", "child\n")
        repo = Repository.discover(self.repository.root)

        rows = render_hierarchy(repo, repo.hierarchy(), include_inactive=True)
        child = next(
            node
            for _, node in rows
            if isinstance(node, VirtualBranchNode) and node.branch == "external-child"
        )
        recovery_json = json.loads(
            hierarchy_as_json(repo, repo.hierarchy(), include_inactive=True)
        )
        child_json = next(
            item for item in recovery_json if item["branch"] == "external-child"
        )

        self.assertEqual("main", child.direct_parent)
        self.assertEqual(str(self.repository.root.resolve()), child_json["visible_parent_id"])

    def test_missing_locked_external_is_warned_and_diagnosed(self):
        external_path = self.repository.root.parent / "locked-external"
        git(self.repository.root, "branch", "locked-external", "main")
        git(self.repository.root, "worktree", "add", str(external_path), "locked-external")
        git(self.repository.root, "worktree", "lock", str(external_path))
        shutil.rmtree(external_path)
        repo = Repository.discover(self.repository.root)

        rows = render_hierarchy(repo, repo.hierarchy(), include_inactive=True)
        issues = doctor(
            repo,
            tmux=TmuxManager(server_name=f"doctor-{uuid.uuid4().hex}"),
        )

        self.assertTrue(
            any("locked-external  [U external] !" in display for display, _ in rows)
        )
        self.assertTrue(
            any("missing external worktree" in issue and "locked-external" in issue for issue in issues)
        )

    def test_duplicate_branch_registration_is_diagnosed(self):
        external_path = self.repository.root.parent / "duplicate-main"
        git(
            self.repository.root,
            "worktree",
            "add",
            "--force",
            str(external_path),
            "main",
        )
        repo = Repository.discover(self.repository.root)

        rows = render_hierarchy(repo, repo.hierarchy(), include_inactive=True)
        recovery_json = json.loads(
            hierarchy_as_json(repo, repo.hierarchy(), include_inactive=True)
        )
        issues = doctor(
            repo,
            tmux=TmuxManager(server_name=f"duplicate-{uuid.uuid4().hex}"),
        )
        main_json = next(item for item in recovery_json if item["branch"] == "main")

        self.assertTrue(rows[0][0].endswith("[ ] !"))
        self.assertEqual([str(external_path.resolve())], main_json["external_paths"])
        self.assertTrue(any("multiple worktree registrations" in issue for issue in issues))

    def test_virtual_open_rolls_back_when_remembering_fails(self):
        git(self.repository.root, "branch", "locked", "main")
        repo = Repository.discover(self.repository.root)
        tmux = TmuxManager(server_name=f"locked-{uuid.uuid4().hex}")
        picker = Picker(repo, tmux=tmux)
        config_lock = self.repository.root / ".git" / "config.lock"
        config_lock.write_text("locked\n")
        try:
            with self.assertRaises(RuntimeError):
                picker._open_virtual_branch(
                    VirtualBranchNode("locked", "main", ParentSource.LOCAL)
                )
        finally:
            config_lock.unlink()
            tmux.run(["kill-server"], check=False)
        refreshed = Repository.discover(self.repository.root)
        self.assertIsNone(refreshed.worktree_for_branch("locked"))

    def test_unregistered_branch_is_shown_flat(self):
        git(self.repository.root, "branch", "stack-only", "main")
        repo = Repository.discover(self.repository.root)

        rows = render_hierarchy(repo, repo.hierarchy(), include_inactive=True)

        self.assertTrue(
            any("stack-only  [U branch]" in display for display, _ in rows)
        )

    def test_unregistered_branch_preserves_parentheses_in_branch_name(self):
        branch = "feat(test)"
        git(self.repository.root, "branch", branch, "main")
        repo = Repository.discover(self.repository.root)

        rows = render_hierarchy(repo, repo.hierarchy(), include_inactive=True)

        self.assertTrue(any(f"{branch}  [U branch]" in display for display, _ in rows))

    def test_stale_local_metadata_branch_is_not_rendered(self):
        git(
            self.repository.root,
            "config",
            "branch.stale.tmux-worktrees-parent",
            "main",
        )
        repo = Repository.discover(self.repository.root)

        rows = render_hierarchy(repo, repo.hierarchy(), include_inactive=True)

        self.assertFalse(any("stale" in display for display, _ in rows))

    def test_stale_explicit_parent_of_managed_child_is_not_rendered(self):
        git(self.repository.root, "branch", "child", "main")
        repo = Repository.discover(self.repository.root)
        repo.add_worktree("child", "main")
        repo.set_local_parent("child", "stale-parent")
        refreshed = Repository.discover(self.repository.root)

        rows = render_hierarchy(refreshed, refreshed.hierarchy(), include_inactive=True)

        self.assertFalse(any("stale-parent" in display for display, _ in rows))
        self.assertTrue(any("child" in display for display, _ in rows))

    def test_referenced_local_parent_endpoint_is_rendered(self):
        git(self.repository.root, "branch", "parent", "main")
        git(self.repository.root, "branch", "child", "parent")
        git(
            self.repository.root,
            "config",
            "branch.child.tmux-worktrees-parent",
            "parent",
        )
        git(
            self.repository.root,
            "config",
            "branch.child.tmux-worktrees-registered",
            "true",
        )
        repo = Repository.discover(self.repository.root)

        rows = render_hierarchy(repo, repo.hierarchy(), include_inactive=True)
        displays = [display for display, _ in rows]
        parent_index = next(index for index, value in enumerate(displays) if "parent  [U branch]" in value)
        child_index = next(index for index, value in enumerate(displays) if "child  [L branch]" in value)

        self.assertLess(parent_index, child_index)
        self.assertIn("└─ child", displays[child_index])

    def test_session_tag_failure_rolls_back_virtual_worktree(self):
        class FailingTagTmux(TmuxManager):
            def tag_session(self, session_id, repo, worktree):
                self.run(
                    [
                        "set-option",
                        "-t",
                        session_id,
                        "@tmux-worktrees-common-dir",
                        str(repo.common_dir),
                    ]
                )
                raise RuntimeError("injected tag failure")

        git(self.repository.root, "branch", "tag-failure", "main")
        repo = Repository.discover(self.repository.root)
        tmux = FailingTagTmux(server_name=f"tag-failure-{uuid.uuid4().hex}")
        try:
            with self.assertRaisesRegex(RuntimeError, "tag failure"):
                Picker(repo, tmux=tmux)._open_virtual_branch(
                    VirtualBranchNode("tag-failure", "main", ParentSource.LOCAL)
                )
            refreshed = Repository.discover(self.repository.root)
            self.assertIsNone(refreshed.worktree_for_branch("tag-failure"))
            self.assertFalse(tmux.is_running())
        finally:
            tmux.run(["kill-server"], check=False)

    def test_failed_switch_restores_previous_remembered_worktree(self):
        repo = Repository.discover(self.repository.root)
        old = repo.add_worktree("old", "main")
        repo = Repository.discover(self.repository.root)
        old = repo.managed_worktree(old.path)
        self.assertIsNotNone(old)
        git(self.repository.root, "branch", "victim", "main")
        tmux = TmuxManager(server_name=f"switch-failure-{uuid.uuid4().hex}")
        tmux.ensure_session(repo, old)
        tmux.remember_worktree(repo, old)
        original_switch = tmux.switch
        tmux.switch = lambda session: (_ for _ in ()).throw(RuntimeError("switch failed"))
        try:
            with self.assertRaisesRegex(RuntimeError, "switch failed"):
                Picker(repo, tmux=tmux)._open_virtual_branch(
                    VirtualBranchNode("victim", "main", ParentSource.LOCAL)
                )
            refreshed = Repository.discover(self.repository.root)
            self.assertEqual(old.id, tmux.last_worktree(refreshed).id)
            self.assertIsNone(refreshed.worktree_for_branch("victim"))
        finally:
            tmux.switch = original_switch
            tmux.run(["kill-server"], check=False)


if __name__ == "__main__":
    unittest.main()
