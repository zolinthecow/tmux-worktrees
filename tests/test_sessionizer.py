from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path


SESSIONIZER = Path(__file__).resolve().parents[4] / "dots" / ".scripts" / "tmux-sessionizer.sh"


class SessionizerTests(unittest.TestCase):
    def test_new_session_failure_is_propagated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            binary_dir = root / "bin"
            binary_dir.mkdir()
            tmux_stub = binary_dir / "tmux"
            tmux_stub.write_text(
                "#!/bin/zsh\n"
                "[[ $1 == has-session ]] && exit 1\n"
                "[[ $1 == new-session ]] && exit 7\n"
                "exit 0\n"
            )
            tmux_stub.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(root / "home"),
                    "PATH": f"{binary_dir}:/usr/bin:/bin",
                    "TMUX": "test-client",
                }
            )
            result = subprocess.run(
                ["/bin/zsh", str(SESSIONIZER), str(project)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(7, result.returncode)

    def test_basename_collision_creates_path_qualified_session(self):
        real_tmux = shutil.which("tmux")
        self.assertIsNotNone(real_tmux)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "one" / "repo"
            second = root / "two" / "repo"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            binary_dir = root / "bin"
            binary_dir.mkdir()
            wrapper = binary_dir / "tmux"
            wrapper.write_text(
                "#!/bin/zsh\n"
                "if [[ $1 == switch-client || $1 == attach-session ]]; then exit 0; fi\n"
                "exec \"$REAL_TMUX\" -L \"$TMUX_TEST_SERVER\" \"$@\"\n"
            )
            wrapper.chmod(0o755)
            server = f"sessionizer-{uuid.uuid4().hex}"
            subprocess.run(
                [real_tmux, "-L", server, "new-session", "-ds", "repo", "-c", str(first)],
                check=True,
            )
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(root / "home"),
                    "PATH": f"{binary_dir}:{env['PATH']}",
                    "REAL_TMUX": real_tmux,
                    "TMUX": "test-client",
                    "TMUX_TEST_SERVER": server,
                }
            )
            try:
                result = subprocess.run(
                    ["/bin/zsh", str(SESSIONIZER), str(second)],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(0, result.returncode)
                sessions = subprocess.run(
                    [
                        real_tmux,
                        "-L",
                        server,
                        "list-sessions",
                        "-F",
                        "#{session_name}|#{pane_current_path}",
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    check=True,
                ).stdout.splitlines()
                matching = [
                    line
                    for line in sessions
                    if Path(line.split("|", 1)[1]).resolve() == second.resolve()
                ]
                self.assertEqual(1, len(matching))
                self.assertNotEqual("repo", matching[0].split("|", 1)[0])
            finally:
                subprocess.run(
                    [real_tmux, "-L", server, "kill-server"], check=False
                )

    def test_git_project_never_falls_back_after_root_registration_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            subprocess.run(["git", "-C", str(project), "init", "-b", "main"], check=True)
            home = root / "home"
            navigator = home / "src" / "personal" / "tmux-worktrees" / "tmux-worktrees"
            navigator.parent.mkdir(parents=True)
            navigator_log = root / "navigator.log"
            navigator.write_text(
                "#!/bin/zsh\n"
                "print -r -- \"$*\" >> \"$NAVIGATOR_LOG\"\n"
                "[[ \"$*\" == *--root-session* ]] && exit 1\n"
                "exit 0\n"
            )
            navigator.chmod(0o755)
            binary_dir = root / "bin"
            binary_dir.mkdir()
            tmux_log = root / "tmux.log"
            tmux_stub = binary_dir / "tmux"
            tmux_stub.write_text(
                "#!/bin/zsh\n"
                "print -r -- \"$*\" >> \"$TMUX_LOG\"\n"
                "[[ $1 == has-session ]] && exit 1\n"
                "exit 0\n"
            )
            tmux_stub.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "NAVIGATOR_LOG": str(navigator_log),
                    "PATH": f"{binary_dir}:{env['PATH']}",
                    "TMUX": "test-client",
                    "TMUX_LOG": str(tmux_log),
                }
            )
            result = subprocess.run(
                ["/bin/zsh", str(SESSIONIZER), str(project)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, result.returncode)
            navigator_calls = navigator_log.read_text().splitlines()
            self.assertEqual(2, len(navigator_calls))
            self.assertIn("--root-session", navigator_calls[0])
            self.assertNotIn("--root-session", navigator_calls[1])
            tmux_calls = tmux_log.read_text()
            self.assertNotIn("switch-client", tmux_calls)
            self.assertNotIn("attach-session", tmux_calls)


if __name__ == "__main__":
    unittest.main()
