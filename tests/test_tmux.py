from __future__ import annotations

import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tests.test_model import TemporaryRepository, git
from tmux_worktrees.model import Repository
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

    def test_repository_reconciliation_is_noop_without_tmux_server(self):
        reconciled = self.tmux.reconcile_repository_sessions(self.repo)

        self.assertEqual(self.repo.root, reconciled.root)
        self.assertFalse(self.tmux.is_running())

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
        self.assertIsNotNone(child)
        self.assertEqual(feature.id, child.id)
        self.assertIsNone(reconciled.worktree_for_branch("feature"))
        child_session = self.tmux.lookup_session(reconciled, child)
        self.assertEqual(original_session.id, child_session.id)
        self.assertEqual(1, len(self.tmux.sessions()))
        self.assertIsNone(reconciled.local_parent("child"))
        self.assertEqual("main", reconciled.local_parent("feature"))
        parent = reconciled.parent_for_virtual_branch("child")
        self.assertEqual("main", parent.parent)

    def test_clean_sibling_switch_moves_session_to_new_branch(self):
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

        sibling = reconciled.worktree_for_branch("sibling")
        self.assertIsNotNone(sibling)
        self.assertEqual(feature.id, sibling.id)
        self.assertIsNone(reconciled.worktree_for_branch("feature"))
        sibling_session = self.tmux.lookup_session(reconciled, sibling)
        self.assertEqual(original_session.id, sibling_session.id)
        self.assertEqual(1, len(self.tmux.sessions()))
        self.assertIsNone(reconciled.local_parent("sibling"))

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
        self.assertIsNotNone(sibling)
        self.assertEqual(feature.id, sibling.id)
        self.assertIsNone(reconciled.worktree_for_branch("feature"))
        self.assertEqual("keep me\n", dirty_path.read_text())
        sibling_session = self.tmux.lookup_session(reconciled, sibling)
        self.assertEqual(original_session.id, sibling_session.id)
        self.assertIsNone(reconciled.local_parent("sibling"))

    def test_session_follows_multiple_in_place_branch_switches(self):
        feature = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(feature)
        original_session, _ = self.tmux.ensure_session(self.repo, feature)
        git(feature.path, "switch", "-c", "branch-b")
        branch_b_repo = self.tmux.reconcile_session_switch(
            Repository.discover(self.repository.root), original_session.id
        )
        branch_b_session = next(
            item for item in self.tmux.sessions() if item.id == original_session.id
        )
        git(feature.path, "switch", "-c", "branch-c")

        reconciled = self.tmux.reconcile_session_switch(
            Repository.discover(self.repository.root), branch_b_session.id
        )

        branch_c = reconciled.worktree_for_branch("branch-c")
        self.assertIsNotNone(branch_c)
        self.assertEqual(feature.id, branch_c.id)
        self.assertIsNone(reconciled.worktree_for_branch("feature"))
        self.assertIsNone(reconciled.worktree_for_branch("branch-b"))
        self.assertIsNone(branch_b_repo.local_parent("branch-b"))
        self.assertIsNone(reconciled.local_parent("branch-c"))
        self.assertEqual(
            original_session.id, self.tmux.lookup_session(reconciled, branch_c).id
        )
        self.assertEqual(1, len(self.tmux.sessions()))

    def test_repository_refresh_reconciles_inactive_session(self):
        feature = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(feature)
        feature_session, _ = self.tmux.ensure_session(self.repo, feature)
        other = self.repo.add_worktree("other", "main")
        repo = Repository.discover(self.repository.root)
        other = repo.managed_worktree(other.path)
        self.assertIsNotNone(other)
        other_session, _ = self.tmux.ensure_session(repo, other)
        git(feature.path, "switch", "-c", "background-child")

        reconciled = self.tmux.reconcile_repository_sessions(repo)

        background_child = reconciled.worktree_for_branch("background-child")
        self.assertIsNotNone(background_child)
        self.assertEqual(
            feature_session.id,
            self.tmux.lookup_session(reconciled, background_child).id,
        )
        unchanged_other = self.tmux.lookup_session(reconciled, other)
        self.assertEqual(other_session.id, unchanged_other.id)
        self.assertIsNone(reconciled.worktree_for_branch("feature"))

    def test_repository_refresh_reconciles_root_session(self):
        root_session, _ = self.tmux.ensure_session(
            self.repo, self.repo.root_worktree
        )
        original_name = root_session.name
        git(self.repository.root, "switch", "-c", "root-new")

        reconciled = self.tmux.reconcile_repository_sessions(self.repo)

        current = next(
            item for item in self.tmux.sessions() if item.id == root_session.id
        )
        self.assertEqual("root-new", current.branch)
        self.assertEqual(original_name, current.name)
        self.assertEqual("root-new", reconciled.root_worktree.branch)

    def test_repository_refresh_reconciles_chained_stale_session_tags(self):
        repo = self.repo
        alpha = repo.add_worktree("alpha", "main")
        repo = Repository.discover(self.repository.root)
        beta = repo.add_worktree("beta", "main")
        repo = Repository.discover(self.repository.root)
        alpha = repo.managed_worktree(alpha.path)
        beta = repo.managed_worktree(beta.path)
        alpha_session, _ = self.tmux.ensure_session(repo, alpha)
        beta_session, _ = self.tmux.ensure_session(repo, beta)
        git(beta.path, "switch", "-c", "gamma")
        git(alpha.path, "switch", "beta")

        reconciled = self.tmux.reconcile_repository_sessions(repo)

        beta_worktree = reconciled.worktree_for_branch("beta")
        gamma_worktree = reconciled.worktree_for_branch("gamma")
        self.assertEqual(alpha.id, beta_worktree.id)
        self.assertEqual(beta.id, gamma_worktree.id)
        self.assertEqual(
            alpha_session.id,
            self.tmux.lookup_session(reconciled, beta_worktree).id,
        )
        self.assertEqual(
            beta_session.id,
            self.tmux.lookup_session(reconciled, gamma_worktree).id,
        )

    def test_reconciliation_does_not_mutate_parent_metadata(self):
        feature = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(feature)
        session, _ = self.tmux.ensure_session(self.repo, feature)
        git(feature.path, "switch", "-c", "child")
        switched = Repository.discover(self.repository.root)
        switched.set_local_parent("feature", "child")

        reconciled = self.tmux.reconcile_session_switch(switched, session.id)

        self.assertIsNone(reconciled.local_parent("child"))
        self.assertEqual("child", reconciled.local_parent("feature"))

    def test_root_reconciliation_does_not_persist_self_parent(self):
        root_session, _ = self.tmux.ensure_session(
            self.repo, self.repo.root_worktree
        )
        git(self.repository.root, "switch", "-c", "root-child")
        git(self.repository.root, "branch", "-D", "main")

        reconciled = self.tmux.reconcile_repository_sessions(self.repo)

        self.assertEqual("root-child", reconciled.root_worktree.branch)
        self.assertIsNone(reconciled.local_parent("root-child"))
        current = next(
            item for item in self.tmux.sessions() if item.id == root_session.id
        )
        self.assertEqual("root-child", current.branch)

    def test_ensure_session_rejects_stale_worktree_branch(self):
        feature = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(feature)
        git(feature.path, "switch", "-c", "child")

        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.tmux.ensure_session(self.repo, feature)

        self.assertFalse(self.tmux.is_running())

    def test_reconciliation_rejects_generation_change_after_retag(self):
        feature = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(feature)
        session, _ = self.tmux.ensure_session(self.repo, feature)
        git(feature.path, "switch", "-c", "child")
        original_retag = self.tmux._retag_session_branch

        def retag_then_change_generation(expected, repo, worktree):
            rebound = original_retag(expected, repo, worktree)
            git_dir = Path(git(worktree.path, "rev-parse", "--git-dir"))
            (git_dir / "tmux-worktrees-generation").write_text("changed\n")
            return rebound

        self.tmux._retag_session_branch = retag_then_change_generation
        try:
            with self.assertRaisesRegex(RuntimeError, "changed after"):
                self.tmux.reconcile_session_switch(
                    Repository.discover(self.repository.root), session.id
                )
        finally:
            self.tmux._retag_session_branch = original_retag

        restored = next(
            item for item in self.tmux.sessions() if item.id == session.id
        )
        self.assertEqual("feature", restored.branch)

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

    def test_resume_reconciles_remembered_worktree_branch_switch(self):
        child = self.repo.worktree_for_branch("feature")
        self.assertIsNotNone(child)
        original_session, _ = self.tmux.ensure_session(self.repo, child)
        self.tmux.remember_worktree(self.repo, child)
        git(child.path, "switch", "-c", "renamed-in-place")
        switched: list[str] = []
        self.tmux.switch = lambda session: switched.append(session.id)

        resumed_session, resumed_worktree = self.tmux.resume_project(self.repo)

        self.assertEqual("renamed-in-place", resumed_worktree.branch)
        self.assertEqual(original_session.id, resumed_session.id)
        self.assertEqual([original_session.id], switched)
        self.assertEqual(1, len(self.tmux.sessions()))

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
