from __future__ import annotations

import os
import fcntl
import hashlib
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .model import Repository, Worktree, _canonical_path, _is_relative_to
from .process import CommandError, Runner


SESSION_REPO_OPTION = "@tmux-worktrees-common-dir"
SESSION_PATH_OPTION = "@tmux-worktrees-path"
SESSION_BRANCH_OPTION = "@tmux-worktrees-branch"
SESSION_GENERATION_OPTION = "@tmux-worktrees-generation"
FIELD_SEPARATOR = "\x1f"


@dataclass(frozen=True)
class TmuxSession:
    id: str
    name: str
    repo: str | None
    path: str | None
    branch: str | None
    generation: str | None
    attached: bool


@dataclass(frozen=True)
class TmuxWindow:
    id: str
    session_id: str
    session_name: str
    name: str
    active: bool
    pane_paths: tuple[Path, ...]


@dataclass(frozen=True)
class MigrationItem:
    window: TmuxWindow
    worktree: Worktree


class TmuxManager:
    def __init__(self, runner: Runner | None = None, server_name: str | None = None):
        self.runner = runner or Runner()
        self.server_name = server_name

    def run(self, args: list[str], *, check: bool = True):
        command = ["tmux"]
        if self.server_name:
            command.extend(["-L", self.server_name])
        return self.runner.run([*command, *args], check=check)

    def is_running(self) -> bool:
        return self.run(["list-sessions"], check=False).returncode == 0

    def sessions(self) -> list[TmuxSession]:
        result = self.run(
            [
                "list-sessions",
                "-F",
                FIELD_SEPARATOR.join(
                    [
                        "#{session_id}",
                        "#{session_name}",
                        f"#{{{SESSION_REPO_OPTION}}}",
                        f"#{{{SESSION_PATH_OPTION}}}",
                        f"#{{{SESSION_BRANCH_OPTION}}}",
                        f"#{{{SESSION_GENERATION_OPTION}}}",
                        "#{session_attached}",
                    ]
                ),
            ],
        )
        sessions: list[TmuxSession] = []
        for line in result.stdout.splitlines():
            fields = line.split(FIELD_SEPARATOR)
            if len(fields) != 7:
                continue
            sessions.append(
                TmuxSession(
                    id=fields[0],
                    name=fields[1],
                    repo=fields[2] or None,
                    path=fields[3] or None,
                    branch=fields[4] or None,
                    generation=fields[5] or None,
                    attached=fields[6] != "0",
                )
            )
        return sessions

    def windows(self) -> list[TmuxWindow]:
        panes_result = self.run(
            [
                "list-panes",
                "-a",
                "-F",
                FIELD_SEPARATOR.join(["#{window_id}", "#{pane_current_path}"]),
            ],
        )
        pane_paths: dict[str, list[Path]] = {}
        for line in panes_result.stdout.splitlines():
            window_id, separator, path = line.partition(FIELD_SEPARATOR)
            if separator and path:
                pane_paths.setdefault(window_id, []).append(_canonical_path(Path(path)))

        windows_result = self.run(
            [
                "list-windows",
                "-a",
                "-F",
                FIELD_SEPARATOR.join(
                    [
                        "#{window_id}",
                        "#{session_id}",
                        "#{session_name}",
                        "#{window_name}",
                        "#{window_active}",
                    ]
                ),
            ],
        )
        windows: list[TmuxWindow] = []
        for line in windows_result.stdout.splitlines():
            fields = line.split(FIELD_SEPARATOR)
            if len(fields) != 5:
                continue
            windows.append(
                TmuxWindow(
                    id=fields[0],
                    session_id=fields[1],
                    session_name=fields[2],
                    name=fields[3],
                    active=fields[4] != "0",
                    pane_paths=tuple(pane_paths.get(fields[0], [])),
                )
            )
        return windows

    def current_session_id(self) -> str | None:
        if not os.environ.get("TMUX"):
            return None
        result = self.run(["display-message", "-p", "#{session_id}"], check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def current_pane_path(self) -> Path | None:
        if not os.environ.get("TMUX"):
            return None
        result = self.run(["display-message", "-p", "#{pane_current_path}"], check=False)
        return _canonical_path(Path(result.stdout.strip())) if result.returncode == 0 else None

    def find_session(self, repo: Repository, worktree: Worktree) -> TmuxSession | None:
        if worktree.is_root:
            sessionizer_session = self._sessionizer_root_session(repo)
            if sessionizer_session is not None:
                expected_generation = repo.worktree_generation(worktree, create=False)
                if (
                    sessionizer_session.repo != str(repo.common_dir)
                    or sessionizer_session.path != worktree.id
                    or sessionizer_session.branch != worktree.branch
                    or expected_generation is None
                    or sessionizer_session.generation != expected_generation
                ):
                    self.tag_session(sessionizer_session.id, repo, worktree)
                    sessionizer_session = next(
                        item for item in self.sessions() if item.id == sessionizer_session.id
                    )
                return sessionizer_session
        return self.lookup_session(repo, worktree)

    def lookup_session(self, repo: Repository, worktree: Worktree) -> TmuxSession | None:
        repo_value = str(repo.common_dir)
        path_value = str(worktree.path)
        generation = repo.worktree_generation(worktree, create=False)
        if generation is None:
            return None
        sessions = self.sessions()
        exact = [
            item
            for item in sessions
            if item.repo == repo_value
            and item.path == path_value
            and item.branch == worktree.branch
            and item.generation == generation
        ]
        if worktree.is_root:
            preferred = [item for item in exact if item.name == _sessionizer_name(repo.root.name)]
            if len(preferred) == 1:
                return preferred[0]
        if len(exact) > 1:
            names = ", ".join(item.name for item in exact)
            raise RuntimeError(f"duplicate tmux sessions for {worktree.path}: {names}")
        if len(exact) == 1:
            return exact[0]
        return None

    def ensure_session(self, repo: Repository, worktree: Worktree) -> tuple[TmuxSession, bool]:
        lock_name = "tmux-worktrees-" + hashlib.sha256(
            f"{repo.common_dir}\0{worktree.path}".encode()
        ).hexdigest()[:16]
        lock_path = Path(tempfile.gettempdir()) / f"{lock_name}.lock"
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            server_running = self.is_running()
            existing = self.find_session(repo, worktree) if server_running else None
            if existing is not None:
                return existing, False
            if not worktree.path.exists():
                raise RuntimeError(f"worktree path does not exist: {worktree.path}")

            name = self.available_name(repo, worktree)
            result = self.run(
                [
                    "new-session",
                    "-d",
                    "-P",
                    "-F",
                    "#{session_id}",
                    "-s",
                    name,
                    "-c",
                    str(worktree.path),
                ],
                check=False,
            )
            if result.returncode != 0:
                for _ in range(10):
                    time.sleep(0.02)
                    existing = self.lookup_session(repo, worktree)
                    if existing is not None:
                        return existing, False
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            session_id = result.stdout.strip()
            try:
                self.tag_session(session_id, repo, worktree)
                created = next((item for item in self.sessions() if item.id == session_id), None)
                if created is None:
                    raise RuntimeError("tmux created a session but did not return it")
                return created, True
            except (RuntimeError, CommandError):
                self.run(["kill-session", "-t", session_id], check=False)
                raise

    def tag_session(self, session_id: str, repo: Repository, worktree: Worktree) -> None:
        generation = repo.worktree_generation(worktree, create=True)
        if generation is None:
            raise RuntimeError(f"could not create generation token for {worktree.path}")
        values = {
            SESSION_REPO_OPTION: str(repo.common_dir),
            SESSION_PATH_OPTION: str(worktree.path),
            SESSION_BRANCH_OPTION: worktree.branch or "",
            SESSION_GENERATION_OPTION: generation,
        }
        for option, value in values.items():
            self.run(["set-option", "-t", session_id, option, value])

    def available_name(self, repo: Repository, worktree: Worktree) -> str:
        if worktree.is_root:
            base = repo.root.name
        else:
            base = f"{repo.root.name}--{worktree.label}"
        base = re.sub(r"[^A-Za-z0-9_-]+", "-", base).strip("-")[:68] or "worktree"
        digest = hashlib.sha256(f"{repo.common_dir}\0{worktree.path}".encode()).hexdigest()[:8]
        base = f"{base}-{digest}"
        names = {item.name for item in self.sessions()} if self.is_running() else set()
        if base not in names:
            return base
        counter = 2
        while f"{base}-{counter}" in names:
            counter += 1
        return f"{base}-{counter}"

    def switch(self, session: TmuxSession) -> None:
        if os.environ.get("TMUX"):
            self.run(["switch-client", "-t", session.id])
            return
        command = ["tmux"]
        if self.server_name:
            command.extend(["-L", self.server_name])
        os.execvp("tmux", [*command, "attach-session", "-t", session.id])

    def switch_worktree(
        self,
        repo: Repository,
        worktree: Worktree,
        session: TmuxSession,
    ) -> None:
        previous = self._remembered_value(repo)
        try:
            self.remember_worktree(repo, worktree)
            self.switch(session)
        except (RuntimeError, CommandError):
            self._restore_remembered_value(repo, previous)
            raise

    def remember_worktree(self, repo: Repository, worktree: Worktree) -> None:
        generation = repo.worktree_generation(worktree, create=True)
        if generation is None:
            raise RuntimeError(f"could not read generation token for {worktree.path}")
        remembered_value = generation + FIELD_SEPARATOR + worktree.id
        repo.set_config("tmux-worktrees.last", remembered_value)
        self.run(
            [
                "set-option",
                "-g",
                self._last_worktree_option(repo),
                remembered_value,
            ]
        )

    def last_worktree(self, repo: Repository) -> Worktree:
        remembered_value = self._remembered_value(repo)
        if remembered_value:
            generation, separator, raw_path = remembered_value.partition(FIELD_SEPARATOR)
            remembered = repo.managed_worktree(raw_path) if separator else None
            if (
                remembered is not None
                and remembered.path.exists()
                and repo.worktree_generation(remembered, create=False)
                == generation
            ):
                return remembered
        return repo.root_worktree

    def _remembered_value(self, repo: Repository) -> str:
        result = self.run(
            ["show-option", "-gv", self._last_worktree_option(repo)],
            check=False,
        )
        value = result.stdout.rstrip("\n") if result.returncode == 0 else ""
        if FIELD_SEPARATOR not in value:
            value = repo.config_value("tmux-worktrees.last", "") or ""
        return value

    def _restore_remembered_value(self, repo: Repository, value: str) -> None:
        if value:
            repo.git(["config", "tmux-worktrees.last", value], check=False)
            self.run(
                ["set-option", "-g", self._last_worktree_option(repo), value],
                check=False,
            )
            return
        repo.git(["config", "--unset", "tmux-worktrees.last"], check=False)
        self.run(
            ["set-option", "-gu", self._last_worktree_option(repo)],
            check=False,
        )

    def resume_project(
        self,
        repo: Repository,
        root_session_name: str | None = None,
    ) -> tuple[TmuxSession, Worktree]:
        if root_session_name:
            self.register_root_session(repo, root_session_name)
        worktree = self.last_worktree(repo)
        session, _ = self.ensure_session(repo, worktree)
        self.switch_worktree(repo, worktree, session)
        return session, worktree

    def register_root_session(self, repo: Repository, session_name: str) -> TmuxSession:
        session = self._root_session_named(repo, session_name)
        if session is None:
            raise RuntimeError(
                f"tmux session {session_name} does not exclusively belong to {repo.root}"
            )
        expected_generation = repo.worktree_generation(repo.root_worktree, create=False)
        if (
            session.repo != str(repo.common_dir)
            or session.path != repo.root_worktree.id
            or session.branch != repo.root_worktree.branch
            or expected_generation is None
            or session.generation != expected_generation
        ):
            self.tag_session(session.id, repo, repo.root_worktree)
            session = next(item for item in self.sessions() if item.id == session.id)
        return session

    def kill_session(self, session_id: str) -> None:
        self.run(["kill-session", "-t", session_id])

    def pane_commands(self, session_id: str) -> list[str]:
        result = self.run(
            [
                "list-panes",
                "-s",
                "-t",
                session_id,
                "-F",
                "#{window_name}:#{pane_index} #{pane_current_command}",
            ],
            check=False,
        )
        return [line for line in result.stdout.splitlines() if line]

    def migration_plan(self, repo: Repository) -> tuple[list[MigrationItem], list[TmuxWindow]]:
        plan: list[MigrationItem] = []
        ambiguous: list[TmuxWindow] = []
        sessions = {item.id: item for item in self.sessions()}
        for window in self.windows():
            session = sessions.get(window.session_id)
            tagged_root_session = (
                session is not None
                and session.repo == str(repo.common_dir)
                and session.path == repo.root_worktree.id
            )
            if session and (session.repo is not None or session.path is not None) and not tagged_root_session:
                continue
            resolved_owners = [self.worktree_for_path(repo, path) for path in window.pane_paths]
            owners = {owner.id for owner in resolved_owners if owner is not None}
            if not resolved_owners or any(owner is None for owner in resolved_owners) or len(owners) != 1:
                if owners:
                    ambiguous.append(window)
                continue
            owner_id = next(iter(owners))
            worktree = next(item for item in repo.managed_worktrees if item.id == owner_id)
            if worktree.is_root and (
                window.session_name == _sessionizer_name(repo.root.name)
                or tagged_root_session
            ):
                continue
            plan.append(MigrationItem(window, worktree))
        return plan, ambiguous

    def apply_migration(self, repo: Repository, plan: list[MigrationItem]) -> None:
        current_plan, _ = self.migration_plan(repo)
        if {_migration_key(item) for item in current_plan} != {
            _migration_key(item) for item in plan
        }:
            raise RuntimeError("tmux windows changed after confirmation; migration aborted")
        by_worktree: dict[str, list[MigrationItem]] = {}
        for item in plan:
            by_worktree.setdefault(item.worktree.id, []).append(item)

        for items in by_worktree.values():
            worktree = items[0].worktree
            session, created = self.ensure_session(repo, worktree)
            original_windows = {window.id for window in self.windows() if window.session_id == session.id}
            for item in items:
                self.run(["move-window", "-s", item.window.id, "-t", f"{session.id}:"])
            if created:
                for window_id in original_windows:
                    self.run(["kill-window", "-t", window_id], check=False)

        for session in self.sessions():
            if (
                session.repo is None
                and session.path is None
                and session.name == _sessionizer_name(repo.root.name)
                and self._session_owned_by(repo, session.id, repo.root_worktree)
            ):
                self.tag_session(session.id, repo, repo.root_worktree)

    def worktree_for_path(self, repo: Repository, path: Path) -> Worktree | None:
        matches = [
            item
            for item in repo.all_worktrees
            if path == item.path or _is_relative_to(path, item.path)
        ]
        if not matches:
            return None
        owner = max(matches, key=lambda item: len(item.path.parts))
        managed_ids = {item.id for item in repo.managed_worktrees}
        return owner if owner.id in managed_ids else None

    def orphaned_sessions(self, repo: Repository) -> list[TmuxSession]:
        valid = {item.id for item in repo.managed_worktrees}
        return [
            item
            for item in self.sessions()
            if item.repo == str(repo.common_dir) and item.path and _canonical_path(Path(item.path)).as_posix() not in valid
        ]

    def _session_owned_by(
        self,
        repo: Repository,
        session_id: str,
        worktree: Worktree,
    ) -> bool:
        result = self.run(
            [
                "list-panes",
                "-s",
                "-t",
                session_id,
                "-F",
                "#{pane_current_path}",
            ],
            check=False,
        )
        if result.returncode != 0:
            return False
        paths = [line for line in result.stdout.splitlines() if line]
        return bool(paths) and all(
            (owner := self.worktree_for_path(repo, _canonical_path(Path(path)))) is not None
            and owner.id == worktree.id
            for path in paths
        )

    def _sessionizer_root_session(self, repo: Repository) -> TmuxSession | None:
        return self._root_session_named(repo, _sessionizer_name(repo.root.name))

    def _root_session_named(
        self,
        repo: Repository,
        session_name: str,
    ) -> TmuxSession | None:
        candidates = [item for item in self.sessions() if item.name == session_name]
        if len(candidates) != 1:
            return None
        session = candidates[0]
        has_identity = any(
            value is not None
            for value in (session.repo, session.path, session.branch, session.generation)
        )
        expected_generation = repo.worktree_generation(repo.root_worktree, create=False)
        if has_identity and not (
            session.repo == str(repo.common_dir)
            and session.path == repo.root_worktree.id
            and session.branch == repo.root_worktree.branch
            and expected_generation is not None
            and session.generation == expected_generation
        ):
            return None
        result = self.run(
            [
                "list-panes",
                "-s",
                "-t",
                session.id,
                "-F",
                "#{pane_current_path}",
            ],
            check=False,
        )
        if result.returncode != 0:
            return None
        has_root_pane = False
        for raw_path in result.stdout.splitlines():
            if not raw_path:
                continue
            owner = self.worktree_for_path(repo, _canonical_path(Path(raw_path)))
            if owner is None:
                return None
            if owner.id == repo.root_worktree.id:
                has_root_pane = True
        return session if has_root_pane else None

    def _last_worktree_option(self, repo: Repository) -> str:
        digest = hashlib.sha256(str(repo.common_dir).encode()).hexdigest()[:16]
        return f"@tmux-worktrees-last-{digest}"

def _migration_key(item: MigrationItem) -> tuple[str, str, str, tuple[Path, ...]]:
    return (
        item.window.id,
        item.window.session_id,
        item.worktree.id,
        item.window.pane_paths,
    )


def _sessionizer_name(directory_name: str) -> str:
    return directory_name.replace(".", "_").replace(":", "_")
