from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from tests.test_model import TemporaryRepository
from tmux_worktrees.cli import main


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


if __name__ == "__main__":
    unittest.main()
