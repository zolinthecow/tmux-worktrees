from __future__ import annotations

import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tests.test_model import TemporaryRepository, git
from tmux_worktrees.model import Repository
from tmux_worktrees.process import Runner
from tmux_worktrees.tmux import TmuxManager


class TmuxTests(unittest.TestCase):
    def setUp(self):
        self.repository = TemporaryRepository()
        repo = Repository.discover(self.repository.root)
        self.child = repo.add_worktree("feature", "main")
        self.repo = Repository.discover(self.repository.root)
        self.server = f"tmux-worktrees-{uuid.uuid4().hex}"
        self.tmux = TmuxManager(server_name=self.server)

    def tearDown(self):
        self.tmux.run(["kill-server"], check=False)
        self.repository.close()

    def test_ensure_session_tags_and_reuses_it(self):
        child = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(child)
        session, created = self.tmux.ensure_session(self.repo, child)
        self.assertTrue(created)
        reused, created_again = self.tmux.ensure_session(self.repo, child)
        self.assertFalse(created_again)
        self.assertEqual(session.id, reused.id)
        tagged = next(item for item in self.tmux.sessions() if item.id == session.id)
        self.assertEqual(str(child.path), tagged.path)
        self.assertEqual("feature", tagged.branch)

    def test_child_branch_switch_moves_original_session_to_child(self):
        feature = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(feature)
        original_session, _ = self.tmux.ensure_session(self.repo, feature)
        git(feature.path, "switch", "-c", "child")
        switched = Repository.discover(self.repository.root)

        reconciled = self.tmux.reconcile_session_switch(
            switched, original_session.id
        )

        child = reconciled.worktree_for_branch("child")
        restored_feature = reconciled.worktree_for_branch("feature")
        self.assertIsNotNone(child)
        self.assertIsNotNone(restored_feature)
        self.assertEqual(feature.id, child.id)
        self.assertNotEqual(feature.id, restored_feature.id)
        child_session = self.tmux.lookup_session(reconciled, child)
        feature_session = self.tmux.lookup_session(reconciled, restored_feature)
        self.assertEqual(original_session.id, child_session.id)
        self.assertNotEqual(original_session.id, feature_session.id)
        self.assertEqual("feature", reconciled.local_parent("child"))
        self.assertEqual("main", reconciled.local_parent("feature"))

    def test_clean_sibling_switch_keeps_original_session_on_old_branch(self):
        feature = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(feature)
        self.repository.add_commit(feature.path, "feature.txt", "feature\n")
        self.repo = Repository.discover(self.repository.root)
        feature = self.repo.worktree_for_branch("feature")
        original_session, _ = self.tmux.ensure_session(self.repo, feature)
        git(feature.path, "switch", "-c", "sibling", "main")
        switched = Repository.discover(self.repository.root)

        reconciled = self.tmux.reconcile_session_switch(
            switched, original_session.id
        )

        restored_feature = reconciled.worktree_for_branch("feature")
        sibling = reconciled.worktree_for_branch("sibling")
        self.assertIsNotNone(restored_feature)
        self.assertIsNotNone(sibling)
        self.assertEqual(feature.id, restored_feature.id)
        self.assertNotEqual(feature.id, sibling.id)
        feature_session = self.tmux.lookup_session(reconciled, restored_feature)
        sibling_session = self.tmux.lookup_session(reconciled, sibling)
        self.assertEqual(original_session.id, feature_session.id)
        self.assertNotEqual(original_session.id, sibling_session.id)
        self.assertEqual("main", reconciled.local_parent("sibling"))

    def test_dirty_sibling_switch_keeps_changes_with_new_branch(self):
        feature = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(feature)
        self.repository.add_commit(feature.path, "feature.txt", "feature\n")
        self.repo = Repository.discover(self.repository.root)
        feature = self.repo.worktree_for_branch("feature")
        original_session, _ = self.tmux.ensure_session(self.repo, feature)
        git(feature.path, "switch", "-c", "sibling", "main")
        dirty_path = feature.path / "dirty.txt"
        dirty_path.write_text("keep me\n")
        switched = Repository.discover(self.repository.root)

        reconciled = self.tmux.reconcile_session_switch(
            switched, original_session.id
        )

        sibling = reconciled.worktree_for_branch("sibling")
        restored_feature = reconciled.worktree_for_branch("feature")
        self.assertIsNotNone(sibling)
        self.assertIsNotNone(restored_feature)
        self.assertEqual(feature.id, sibling.id)
        self.assertNotEqual(feature.id, restored_feature.id)
        self.assertEqual("keep me\n", dirty_path.read_text())
        sibling_session = self.tmux.lookup_session(reconciled, sibling)
        feature_session = self.tmux.lookup_session(reconciled, restored_feature)
        self.assertEqual(original_session.id, sibling_session.id)
        self.assertNotEqual(original_session.id, feature_session.id)
        self.assertEqual("main", reconciled.local_parent("sibling"))

    def test_clean_switch_that_becomes_dirty_falls_back_to_new_branch(self):
        feature = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(feature)
        self.repository.add_commit(feature.path, "feature.txt", "feature\n")
        git(feature.path, "switch", "-c", "sibling", "main")

        class DirtyAfterSwitchRunner(Runner):
            def __init__(self, path: Path):
                super().__init__()
                self.path = path
                self.injected = False

            def run(self, args, **kwargs):
                result = super().run(args, **kwargs)
                command = tuple(str(item) for item in args)
                if (
                    not self.injected
                    and "switch" in command
                    and command[-1] == "feature"
                    and result.returncode == 0
                ):
                    self.injected = True
                    (self.path / "raced.txt").write_text("raced\n")
                return result

        runner = DirtyAfterSwitchRunner(feature.path)
        repo = Repository.discover(self.repository.root, runner)
        manager = TmuxManager(runner, server_name=self.server)
        stale_feature = repo.managed_worktree(feature.path)
        self.assertIsNotNone(stale_feature)
        git(feature.path, "switch", "feature")
        repo = Repository.discover(self.repository.root, runner)
        stale_feature = repo.managed_worktree(feature.path)
        original_session, _ = manager.ensure_session(repo, stale_feature)
        git(feature.path, "switch", "sibling")
        switched = Repository.discover(self.repository.root, runner)

        reconciled = manager.reconcile_session_switch(switched, original_session.id)

        sibling = reconciled.worktree_for_branch("sibling")
        restored_feature = reconciled.worktree_for_branch("feature")
        self.assertIsNotNone(sibling)
        self.assertIsNotNone(restored_feature)
        self.assertEqual(feature.id, sibling.id)
        self.assertEqual("raced\n", (feature.path / "raced.txt").read_text())
        self.assertEqual(
            original_session.id, manager.lookup_session(reconciled, sibling).id
        )

    def test_recreated_worktree_does_not_reuse_old_generation_session(self):
        child = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(child)
        old_session, _ = self.tmux.ensure_session(self.repo, child)
        old_generation = next(
            item.generation for item in self.tmux.sessions() if item.id == old_session.id
        )
        self.repo.remove_worktree(child)
        after_removal = Repository.discover(self.repository.root)
        recreated = after_removal.add_worktree("feature", "main")
        refreshed = Repository.discover(self.repository.root)
        recreated = refreshed.managed_worktree(recreated.path)
        self.assertIsNotNone(recreated)
        self.assertIsNone(self.tmux.lookup_session(refreshed, recreated))
        new_session, _ = self.tmux.ensure_session(refreshed, recreated)
        new_generation = next(
            item.generation for item in self.tmux.sessions() if item.id == new_session.id
        )
        self.assertNotEqual(old_generation, new_generation)

    def test_concurrent_first_use_creates_one_session(self):
        child = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(child)

        def ensure():
            manager = TmuxManager(server_name=self.server)
            return manager.ensure_session(self.repo, child)[0].id

        with ThreadPoolExecutor(max_workers=8) as executor:
            session_ids = list(executor.map(lambda _: ensure(), range(8)))
        self.assertEqual(1, len(set(session_ids)))
        tagged = [item for item in self.tmux.sessions() if item.path == child.id]
        self.assertEqual(1, len(tagged))

    def test_resume_project_switches_to_last_selected_worktree(self):
        child = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(child)
        child_session, _ = self.tmux.ensure_session(self.repo, child)
        switched: list[str] = []
        self.tmux.switch = lambda session: switched.append(session.id)

        self.tmux.switch_worktree(self.repo, child, child_session)
        resumed_session, resumed_worktree = self.tmux.resume_project(self.repo)

        self.assertEqual(child.id, resumed_worktree.id)
        self.assertEqual(child_session.id, resumed_session.id)
        self.assertEqual([child_session.id, child_session.id], switched)

    def test_resume_registers_explicit_collision_safe_root_session(self):
        collision_safe_name = f"{self.repo.root.name}-12345"
        self.tmux.run(
            [
                "new-session",
                "-d",
                "-s",
                collision_safe_name,
                "-c",
                str(self.repo.root),
            ]
        )
        switched: list[str] = []
        self.tmux.switch = lambda session: switched.append(session.id)

        session, worktree = self.tmux.resume_project(
            self.repo, root_session_name=collision_safe_name
        )

        self.assertTrue(worktree.is_root)
        self.assertEqual(collision_safe_name, session.name)
        self.assertEqual(self.repo.root_worktree.id, session.path)
        self.assertEqual([session.id], switched)

    def test_remember_failure_does_not_claim_durable_success(self):
        child = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(child)
        self.tmux.ensure_session(self.repo, child)
        config_lock = self.repository.root / ".git" / "config.lock"
        config_lock.write_text("locked\n")
        try:
            with self.assertRaises(RuntimeError):
                self.tmux.remember_worktree(self.repo, child)
        finally:
            config_lock.unlink()
        self.assertTrue(self.tmux.last_worktree(self.repo).is_root)

    def test_resume_project_falls_back_to_root_when_worktree_is_removed(self):
        child = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(child)
        child_session, _ = self.tmux.ensure_session(self.repo, child)
        self.tmux.remember_worktree(self.repo, child)
        self.repo.remove_worktree(child)
        refreshed = Repository.discover(self.repository.root)
        switched: list[str] = []
        self.tmux.switch = lambda session: switched.append(session.id)

        resumed_session, resumed_worktree = self.tmux.resume_project(refreshed)

        self.assertTrue(resumed_worktree.is_root)
        self.assertEqual(resumed_session.id, switched[-1])

    def test_resume_project_rejects_recreated_worktree_generation(self):
        child = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(child)
        self.tmux.ensure_session(self.repo, child)
        self.tmux.remember_worktree(self.repo, child)
        self.repo.remove_worktree(child)
        after_removal = Repository.discover(self.repository.root)
        after_removal.add_worktree("feature", "main")
        refreshed = Repository.discover(self.repository.root)

        self.assertTrue(self.tmux.last_worktree(refreshed).is_root)

    def test_remembered_worktree_survives_tmux_server_restart(self):
        child = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(child)
        self.tmux.ensure_session(self.repo, child)
        self.tmux.remember_worktree(self.repo, child)
        self.tmux.run(["kill-server"])
        refreshed = Repository.discover(self.repository.root)

        self.assertEqual(child.id, self.tmux.last_worktree(refreshed).id)

    def test_migration_finds_window_owned_by_child_worktree(self):
        self.tmux.run(
            [
                "new-session",
                "-d",
                "-s",
                self.repo.root.name,
                "-c",
                str(self.repo.root),
            ]
        )
        self.tmux.run(
            [
                "new-window",
                "-t",
                self.repo.root.name,
                "-n",
                "feature",
                "-c",
                str(self.child.path),
            ]
        )
        plan, ambiguous = self.tmux.migration_plan(self.repo)
        self.assertFalse(ambiguous)
        self.assertEqual(1, len(plan))
        self.assertEqual("feature", plan[0].worktree.branch)

        self.tmux.apply_migration(self.repo, plan)
        child = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(child)
        session = self.tmux.find_session(self.repo, child)
        self.assertIsNotNone(session)
        moved = [window for window in self.tmux.windows() if window.session_id == session.id]
        self.assertEqual(["feature"], [window.name for window in moved])
        root_session = next(item for item in self.tmux.sessions() if item.name == self.repo.root.name)
        self.assertEqual(self.repo.root_worktree.id, root_session.path)

    def test_migration_rejects_window_with_unmanaged_pane(self):
        self.tmux.run(
            [
                "new-session",
                "-d",
                "-s",
                "mixed",
                "-c",
                str(self.child.path),
            ]
        )
        self.tmux.run(["split-window", "-t", "mixed", "-c", str(self.repository.root.parent)])
        plan, ambiguous = self.tmux.migration_plan(self.repo)
        self.assertFalse(plan)
        self.assertEqual(1, len(ambiguous))

    def test_migration_splits_child_window_from_tagged_root_session(self):
        root_session, _ = self.tmux.ensure_session(self.repo, self.repo.root_worktree)
        self.tmux.run(
            [
                "new-window",
                "-t",
                root_session.id,
                "-n",
                "feature",
                "-c",
                str(self.child.path),
            ]
        )
        plan, ambiguous = self.tmux.migration_plan(self.repo)
        self.assertEqual(1, len(plan))
        self.assertEqual("feature", plan[0].worktree.branch)
        self.assertFalse(ambiguous)

    def test_external_nested_worktree_is_not_owned_by_root(self):
        external_path = self.repository.root / ".ide-worktrees" / "agent"
        external_path.parent.mkdir()
        from tests.test_model import git

        git(self.repository.root, "worktree", "add", "-b", "agent", str(external_path), "main")
        repo = Repository.discover(self.repository.root)
        self.assertIsNone(self.tmux.worktree_for_path(repo, external_path))

    def test_root_name_does_not_adopt_unrelated_session(self):
        self.tmux.run(
            [
                "new-session",
                "-d",
                "-s",
                self.repo.root.name,
                "-c",
                str(self.repository.root.parent),
            ]
        )
        self.assertIsNone(self.tmux.find_session(self.repo, self.repo.root_worktree))

    def test_sessionizer_root_session_is_canonical(self):
        self.tmux.run(
            [
                "new-session",
                "-d",
                "-s",
                self.repo.root.name,
                "-c",
                str(self.repo.root),
            ]
        )
        session, created = self.tmux.ensure_session(self.repo, self.repo.root_worktree)
        self.assertFalse(created)
        self.assertEqual(self.repo.root.name, session.name)
        self.assertEqual(self.repo.root_worktree.id, session.path)
        self.assertIsNotNone(session.generation)

    def test_duplicate_root_sessions_prefer_sessionizer_name(self):
        self.tmux.run(
            [
                "new-session",
                "-d",
                "-s",
                self.repo.root.name,
                "-c",
                str(self.repo.root),
            ]
        )
        canonical = next(item for item in self.tmux.sessions() if item.name == self.repo.root.name)
        self.tmux.tag_session(canonical.id, self.repo, self.repo.root_worktree)
        self.tmux.run(
            [
                "new-session",
                "-d",
                "-s",
                "generated-root",
                "-c",
                str(self.repo.root),
            ]
        )
        generated = next(item for item in self.tmux.sessions() if item.name == "generated-root")
        self.tmux.tag_session(generated.id, self.repo, self.repo.root_worktree)
        selected = self.tmux.lookup_session(self.repo, self.repo.root_worktree)
        self.assertIsNotNone(selected)
        self.assertEqual(self.repo.root.name, selected.name)

    def test_root_adoption_never_overwrites_child_identity(self):
        child = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(child)
        child_session, _ = self.tmux.ensure_session(self.repo, child)
        self.tmux.run(["rename-session", "-t", child_session.id, self.repo.root.name])
        self.tmux.run(
            ["new-window", "-t", child_session.id, "-c", str(self.repo.root)]
        )
        root_session, created = self.tmux.ensure_session(self.repo, self.repo.root_worktree)
        self.assertTrue(created)
        self.assertNotEqual(child_session.id, root_session.id)
        unchanged_child = next(
            item for item in self.tmux.sessions() if item.id == child_session.id
        )
        self.assertEqual(child.id, unchanged_child.path)
        self.assertEqual("feature", unchanged_child.branch)

    def test_root_adoption_rejects_stale_generation(self):
        self.tmux.run(
            [
                "new-session",
                "-d",
                "-s",
                self.repo.root.name,
                "-c",
                str(self.repo.root),
            ]
        )
        stale = next(item for item in self.tmux.sessions() if item.name == self.repo.root.name)
        self.tmux.tag_session(stale.id, self.repo, self.repo.root_worktree)
        self.tmux.run(
            ["set-option", "-t", stale.id, "@tmux-worktrees-generation", "stale-generation"]
        )

        root_session, created = self.tmux.ensure_session(self.repo, self.repo.root_worktree)

        self.assertTrue(created)
        self.assertNotEqual(stale.id, root_session.id)

    def test_root_adoption_rejects_foreign_repository_pane(self):
        other = TemporaryRepository()
        try:
            other_repo = Repository.discover(other.root)
            self.tmux.run(
                [
                    "new-session",
                    "-d",
                    "-s",
                    self.repo.root.name,
                    "-c",
                    str(self.repo.root),
                ]
            )
            self.tmux.run(
                [
                    "new-window",
                    "-t",
                    self.repo.root.name,
                    "-c",
                    str(other_repo.root),
                ]
            )

            root_session, created = self.tmux.ensure_session(
                self.repo, self.repo.root_worktree
            )

            self.assertTrue(created)
            self.assertNotEqual(self.repo.root.name, root_session.name)
            original = next(
                item for item in self.tmux.sessions() if item.name == self.repo.root.name
            )
            self.assertIsNone(original.repo)
        finally:
            other.close()

    def test_picker_does_not_implicitly_adopt_matching_legacy_session(self):
        self.tmux.run(
            [
                "new-session",
                "-d",
                "-s",
                "legacy-root",
                "-c",
                str(self.repo.root),
            ]
        )
        self.assertIsNone(self.tmux.find_session(self.repo, self.repo.root_worktree))

    def test_migration_revalidates_plan_before_moving(self):
        self.tmux.run(
            [
                "new-session",
                "-d",
                "-s",
                "legacy",
                "-c",
                str(self.child.path),
            ]
        )
        plan, _ = self.tmux.migration_plan(self.repo)
        self.assertEqual(1, len(plan))
        self.tmux.run(["split-window", "-t", "legacy", "-c", str(self.repository.root.parent)])
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.tmux.apply_migration(self.repo, plan)

    def test_session_names_remain_unique_after_sanitization_collisions(self):
        from tests.test_model import git

        branches = ["foo/bar", "foo.bar", "foo-bar"]
        sessions = []
        repo = self.repo
        for branch in branches:
            git(self.repository.root, "branch", branch, "main")
            worktree = repo.add_worktree(branch, "main")
            repo = Repository.discover(self.repository.root)
            managed = repo.managed_worktree(worktree.path)
            self.assertIsNotNone(managed)
            session, _ = self.tmux.ensure_session(repo, managed)
            sessions.append(session.name)
        self.assertEqual(3, len(set(sessions)))


if __name__ == "__main__":
    unittest.main()
