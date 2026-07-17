from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from tests.test_model import TemporaryRepository, git
from tmux_worktrees.model import Repository
from tmux_worktrees.cli import main
from tmux_worktrees.process import Runner


class CliTests(unittest.TestCase):
    def setUp(self):
        self.repository = TemporaryRepository()

    def tearDown(self):
        self.repository.close()

    def test_no_subcommand_opens_picker_without_argument_error(self):
        with (
            mock.patch("tmux_worktrees.cli.Path.cwd", return_value=self.repository.root),
            mock.patch("tmux_worktrees.cli.Picker.run") as run_picker,
        ):
            self.assertEqual(0, main([]))
        run_picker.assert_called_once()

    def test_launcher_is_executable(self):
        launcher = Path(__file__).resolve().parents[1] / "tmux-worktrees"
        self.assertTrue(os.access(launcher, os.X_OK))

    def test_git_optional_locks_are_always_disabled(self):
        runner = Runner({"GIT_OPTIONAL_LOCKS": "1"})

        self.assertEqual("0", runner.env["GIT_OPTIONAL_LOCKS"])
        result = runner.run(["env"])
        self.assertIn("GIT_OPTIONAL_LOCKS=0", result.stdout.splitlines())

    def test_register_rejects_trunk_without_pushing(self):
        with mock.patch("tmux_worktrees.model.GitHubProvider.register") as register:
            self.assertEqual(
                1,
                main(["register", "--cwd", str(self.repository.root)]),
            )
        register.assert_not_called()

    def test_local_registration_does_not_create_pr(self):
        git(self.repository.root, "branch", "child", "main")
        with mock.patch("tmux_worktrees.model.GitHubProvider.register") as register:
            self.assertEqual(
                0,
                main(
                    [
                        "register",
                        "--cwd",
                        str(self.repository.root),
                        "--branch",
                        "child",
                        "--parent",
                        "main",
                        "--local",
                    ]
                ),
            )
        register.assert_not_called()
        repo = Repository.discover(self.repository.root)
        self.assertEqual("main", repo.local_parent("child"))
        self.assertIn("child", repo.registered_branches())

    def test_unregister_keeps_branch(self):
        git(self.repository.root, "branch", "child", "main")
        repo = Repository.discover(self.repository.root)
        repo.register_local("child", "main")

        self.assertEqual(
            0,
            main(
                [
                    "unregister",
                    "--cwd",
                    str(self.repository.root),
                    "--branch",
                    "child",
                ]
            ),
        )

        repo = Repository.discover(self.repository.root)
        self.assertTrue(repo.branch_exists("child"))
        self.assertIsNone(repo.local_parent("child"))
        self.assertNotIn("child", repo.registered_branches())


if __name__ == "__main__":
    unittest.main()
