from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult):
        self.result = result
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        super().__init__(f"{_display_command(result.args)}: {detail}")


def _display_command(args: Sequence[str]) -> str:
    return " ".join(args)


class Runner:
    def __init__(self, extra_env: Mapping[str, str] | None = None):
        self.env = os.environ.copy()
        for key in (
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_CONFIG",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_SYSTEM",
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_PREFIX",
            "GIT_WORK_TREE",
        ):
            self.env.pop(key, None)
        for key in list(self.env):
            if key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
                self.env.pop(key, None)
        self.env.update(
            {
                "CLICOLOR": "0",
                "GIT_PAGER": "cat",
                "NO_COLOR": "1",
                "PAGER": "cat",
            }
        )
        if extra_env:
            self.env.update(extra_env)

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str | Path | None = None,
        check: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        command = tuple(str(arg) for arg in args)
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd) if cwd is not None else None,
                env=self.env,
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            result = CommandResult(
                command,
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )
        except FileNotFoundError as error:
            result = CommandResult(command, 127, "", str(error))

        if check and result.returncode != 0:
            raise CommandError(result)
        return result

    def run_bytes(
        self,
        args: Sequence[str],
        *,
        cwd: str | Path | None = None,
        check: bool = True,
    ) -> tuple[int, bytes, bytes]:
        command = tuple(str(arg) for arg in args)
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd) if cwd is not None else None,
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError as error:
            if check:
                raise RuntimeError(str(error)) from error
            return 127, b"", str(error).encode()

        if check and completed.returncode != 0:
            result = CommandResult(
                command,
                completed.returncode,
                os.fsdecode(completed.stdout),
                os.fsdecode(completed.stderr),
            )
            raise CommandError(result)
        return completed.returncode, completed.stdout, completed.stderr
