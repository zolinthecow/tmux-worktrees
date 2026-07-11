from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .process import CommandError, Runner


ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
IgnoredSnapshot = tuple[tuple[str, str, int, int], ...]


class ParentSource(str, Enum):
    ROOT = "root"
    GRAPHITE = "graphite"
    LOCAL = "local"
    INFERRED = "inferred"
    DETACHED = "detached"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str | None = None
    branch: str | None = None
    detached: bool = False
    locked: str | None = None
    prunable: str | None = None
    is_root: bool = False

    @property
    def id(self) -> str:
        return str(self.path)

    @property
    def label(self) -> str:
        return self.branch or f"detached@{(self.head or 'unknown')[:8]}"


@dataclass(frozen=True)
class ParentInfo:
    parent: str | None
    source: ParentSource
    warning: str | None = None


@dataclass
class HierarchyNode:
    worktree: Worktree
    direct_parent: str | None
    parent_id: str | None
    source: ParentSource
    skipped_parents: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Hierarchy:
    root_id: str
    nodes: dict[str, HierarchyNode]
    children: dict[str, list[str]]

    def ordered_ids(self) -> list[str]:
        ordered: list[str] = []

        def visit(node_id: str) -> None:
            ordered.append(node_id)
            for child_id in self.children.get(node_id, []):
                visit(child_id)

        visit(self.root_id)
        return ordered

    def depth(self, node_id: str) -> int:
        depth = 0
        seen: set[str] = set()
        current = self.nodes[node_id]
        while current.parent_id is not None and current.parent_id not in seen:
            seen.add(current.parent_id)
            depth += 1
            current = self.nodes[current.parent_id]
        return depth


@dataclass(frozen=True)
class GraphiteParent:
    tracked: bool
    parent: str | None = None
    error: str | None = None


class GraphiteProvider:
    def __init__(self, common_dir: Path, runner: Runner):
        self.common_dir = common_dir
        self.runner = runner
        self._cache: dict[str, GraphiteParent] = {}
        self.configured = (common_dir / ".graphite_repo_config").exists()
        self._database_invalid: dict[str, str] = {}
        self._database_parents = self._read_database()

    def trunks(self) -> list[str]:
        config_path = self.common_dir / ".graphite_repo_config"
        if not config_path.exists():
            return []
        try:
            data = json.loads(config_path.read_text())
        except (OSError, ValueError):
            return []
        trunks = [item.get("name") for item in data.get("trunks", [])]
        trunks = [name for name in trunks if isinstance(name, str)]
        if not trunks and isinstance(data.get("trunk"), str):
            trunks.append(data["trunk"])
        return trunks

    def checked_descendants(self, branch: str, worktrees: list[Worktree]) -> list[Worktree] | None:
        if self._database_parents is None:
            return None
        descendants: set[str] = set()
        pending = [branch]
        while pending:
            parent = pending.pop()
            children = [
                child
                for child, child_parent in self._database_parents.items()
                if child_parent == parent and child not in descendants
            ]
            descendants.update(children)
            pending.extend(children)
        return [item for item in worktrees if item.branch in descendants]

    def tracked_parents(self) -> dict[str, str | None]:
        if self._database_parents is not None:
            return {
                branch: parent
                for branch, parent in self._database_parents.items()
                if self.common_dir.parent.joinpath(".git").exists()
                and self.runner.run(
                    [
                        "git",
                        "-C",
                        str(self.common_dir.parent),
                        "show-ref",
                        "--verify",
                        "--quiet",
                        f"refs/heads/{branch}",
                    ],
                    check=False,
                ).returncode
                == 0
            }
        if not self.configured:
            return {}
        result = self.runner.run(
            ["gt", "log", "short", "--classic", "--all", "--no-interactive"],
            cwd=self.common_dir.parent,
            check=False,
        )
        if result.returncode != 0:
            return {}
        branches = set(re.findall(r"\$\s+(\S+)", _clean_output(result.stdout)))
        parents: dict[str, str | None] = {}
        for branch in branches:
            info = self.parent(branch)
            if info.tracked:
                parents[branch] = info.parent
        return parents

    def require_supported_version(self) -> None:
        result = self.runner.run(["gt", "--version"], check=False)
        if result.returncode != 0:
            raise RuntimeError(_command_detail(result.stdout, result.stderr))
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout + result.stderr)
        if not match or tuple(int(part) for part in match.groups()) < (1, 8, 4):
            raise RuntimeError("Graphite 1.8.4 or newer is required for worktree-safe mutations")

    def validate_tracked_parent(self, branch: str, path: Path) -> str | None:
        self.require_supported_version()
        result = self.runner.run(
            ["gt", "parent", "--cwd", str(path), "--no-interactive"], check=False
        )
        if result.returncode != 0:
            raise RuntimeError(_command_detail(result.stdout, result.stderr))
        return _first_nonempty_line(result.stdout)

    def parent(self, branch: str, worktree_path: Path | None = None) -> GraphiteParent:
        if branch in self._cache:
            return self._cache[branch]
        if not self.configured:
            result = GraphiteParent(False)
            self._cache[branch] = result
            return result

        if self._database_parents is not None:
            if branch in self._database_parents:
                result = GraphiteParent(True, self._database_parents[branch])
            elif branch in self._database_invalid:
                result = GraphiteParent(
                    False,
                    error=f"Graphite metadata is invalid: {self._database_invalid[branch]}",
                )
            else:
                result = GraphiteParent(False)
            self._cache[branch] = result
            return result

        if worktree_path is not None and worktree_path.exists():
            command = ["gt", "parent", "--cwd", str(worktree_path), "--no-interactive"]
            result = self.runner.run(command, check=False)
            if result.returncode == 0:
                parent = _first_nonempty_line(result.stdout)
                value = GraphiteParent(True, parent)
            elif _is_untracked_graphite_error(result.stdout + result.stderr):
                value = GraphiteParent(False)
            else:
                value = GraphiteParent(False, error=_command_detail(result.stdout, result.stderr))
        else:
            command = ["gt", "info", branch, "--no-interactive"]
            result = self.runner.run(command, cwd=self.common_dir.parent, check=False)
            if result.returncode == 0:
                parent = None
                for line in _clean_output(result.stdout).splitlines():
                    if line.startswith("Parent:"):
                        parent = line.partition(":")[2].strip() or None
                        break
                value = GraphiteParent(True, parent)
            elif _is_untracked_graphite_error(result.stdout + result.stderr):
                value = GraphiteParent(False)
            else:
                value = GraphiteParent(False, error=_command_detail(result.stdout, result.stderr))

        self._cache[branch] = value
        return value

    def _read_database(self) -> dict[str, str | None] | None:
        database_path = self.common_dir / ".graphite_metadata.db"
        if not self.configured or not database_path.exists():
            return None
        try:
            connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
            try:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(branch_metadata)")
                }
                if not {"branch_name", "parent_branch_name", "validation_result"}.issubset(columns):
                    return None
                rows = connection.execute(
                    "SELECT branch_name, parent_branch_name, validation_result FROM branch_metadata"
                )
                parents: dict[str, str | None] = {}
                for branch, parent, validation in rows:
                    if validation in {"VALID", "TRUNK"}:
                        parents[branch] = parent
                    elif parent is not None:
                        self._database_invalid[branch] = validation or "unknown state"
                return parents
            finally:
                connection.close()
        except sqlite3.Error:
            return None


class Repository:
    def __init__(
        self,
        root: Path,
        common_dir: Path,
        worktrees: list[Worktree],
        runner: Runner | None = None,
    ):
        self.root = root
        self.common_dir = common_dir
        self.runner = runner or Runner()
        self.all_worktrees = worktrees
        configured_directory = Path(
            self.config_value("tmux-worktrees.directory", ".worktrees") or ".worktrees"
        ).expanduser()
        if not configured_directory.is_absolute():
            configured_directory = root / configured_directory
        self.worktrees_dir = _canonical_path(configured_directory)
        self._validate_managed_directory()
        self.graphite = GraphiteProvider(common_dir, self.runner)
        self._parent_cache: dict[str, ParentInfo] = {}

        root_worktree = next((item for item in worktrees if item.path == root), None)
        if root_worktree is None:
            raise RuntimeError(f"main worktree {root} was not found in git worktree list")
        self.root_worktree = Worktree(**{**root_worktree.__dict__, "is_root": True})

        self.managed_worktrees = [self.root_worktree]
        for worktree in worktrees:
            if worktree.path == root:
                continue
            if _is_relative_to(worktree.path, self.worktrees_dir):
                _validate_protocol_path(worktree.path)
                self.managed_worktrees.append(worktree)

    @classmethod
    def discover(cls, cwd: str | Path, runner: Runner | None = None) -> Repository:
        runner = runner or Runner()
        cwd_path = Path(cwd).expanduser()
        bare_result = runner.run(
            ["git", "-C", str(cwd_path), "rev-parse", "--is-bare-repository"]
        )
        if bare_result.stdout.strip() == "true":
            raise RuntimeError("bare repositories are not supported")
        common_result = runner.run(
            ["git", "-C", str(cwd_path), "rev-parse", "--path-format=absolute", "--git-common-dir"]
        )
        common_dir = Path(common_result.stdout.strip()).resolve()
        if not common_dir.is_dir():
            raise RuntimeError(f"Git common directory does not exist: {common_dir}")

        _, output, _ = runner.run_bytes(
            ["git", "-C", str(cwd_path), "worktree", "list", "--porcelain", "-z"]
        )
        worktrees = parse_worktree_porcelain(output)
        if not worktrees:
            raise RuntimeError("git returned no worktrees")
        root = worktrees[0].path
        return cls(root, common_dir, worktrees, runner)

    @property
    def external_worktrees(self) -> list[Worktree]:
        managed_paths = {item.path for item in self.managed_worktrees}
        return [item for item in self.all_worktrees if item.path not in managed_paths]

    @property
    def managed_directory_is_internal(self) -> bool:
        return _is_relative_to(self.worktrees_dir, self.root)

    def git(self, args: list[str], *, check: bool = True, cwd: Path | None = None):
        return self.runner.run(["git", "-C", str(cwd or self.root), *args], check=check)

    def config_value(self, key: str, default: str | None = None) -> str | None:
        result = self.runner.run(
            ["git", "-C", str(self.root), "config", "--null", "--get", key], check=False
        )
        return result.stdout.removesuffix("\0") if result.returncode == 0 else default

    def set_config(self, key: str, value: str) -> None:
        self.git(["config", key, value])

    def branch_exists(self, branch: str) -> bool:
        return (
            self.git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False).returncode
            == 0
        )

    def worktree_for_branch(self, branch: str) -> Worktree | None:
        return next((item for item in self.all_worktrees if item.branch == branch), None)

    def managed_worktree(self, path: str | Path) -> Worktree | None:
        target = _canonical_path(Path(path))
        return next((item for item in self.managed_worktrees if item.path == target), None)

    def worktree_generation(self, worktree: Worktree, *, create: bool) -> str | None:
        if not worktree.path.exists():
            return None
        result = self.git(
            ["rev-parse", "--path-format=absolute", "--git-dir"],
            check=False,
            cwd=worktree.path,
        )
        if result.returncode != 0:
            return None
        token_path = Path(result.stdout.strip()) / "tmux-worktrees-generation"
        try:
            token = token_path.read_text().strip()
            return token or None
        except FileNotFoundError:
            if not create:
                return None
        except OSError as error:
            raise RuntimeError(f"failed to read worktree generation token: {error}") from error

        token = uuid.uuid4().hex
        try:
            with token_path.open("x") as file:
                file.write(token + "\n")
            return token
        except FileExistsError:
            try:
                return token_path.read_text().strip() or None
            except OSError as error:
                raise RuntimeError(f"failed to read worktree generation token: {error}") from error
        except OSError as error:
            raise RuntimeError(f"failed to create worktree generation token: {error}") from error

    def local_parent(self, branch: str) -> str | None:
        return self.config_value(f"branch.{branch}.tmux-worktrees-parent")

    def set_local_parent(self, branch: str, parent: str) -> None:
        self.set_config(f"branch.{branch}.tmux-worktrees-parent", parent)
        self._parent_cache.pop(branch, None)

    def direct_parent(self, worktree: Worktree) -> ParentInfo:
        if worktree.is_root:
            return ParentInfo(None, ParentSource.ROOT)
        if worktree.branch is None:
            return ParentInfo(self.root_worktree.branch, ParentSource.DETACHED)
        if worktree.branch in self._parent_cache:
            return self._parent_cache[worktree.branch]

        graphite = self.graphite.parent(worktree.branch, worktree.path)
        if graphite.tracked:
            info = ParentInfo(graphite.parent, ParentSource.GRAPHITE)
        else:
            local = self.local_parent(worktree.branch)
            if graphite.error:
                parent = local or self.infer_parent(worktree.branch)
                info = ParentInfo(parent, ParentSource.UNRESOLVED, graphite.error)
            elif local:
                info = ParentInfo(local, ParentSource.LOCAL, graphite.error)
            else:
                inferred = self.infer_parent(worktree.branch)
                warning = graphite.error
                info = ParentInfo(inferred, ParentSource.INFERRED, warning)
        self._parent_cache[worktree.branch] = info
        return info

    def parent_for_virtual_branch(self, branch: str) -> ParentInfo:
        if branch == self.root_worktree.branch:
            return ParentInfo(None, ParentSource.ROOT)
        if branch in self._parent_cache:
            return self._parent_cache[branch]

        graphite = self.graphite.parent(branch)
        if graphite.tracked:
            info = ParentInfo(graphite.parent, ParentSource.GRAPHITE)
        else:
            local = self.local_parent(branch)
            if graphite.error:
                info = ParentInfo(local, ParentSource.UNRESOLVED, graphite.error)
            elif local:
                info = ParentInfo(local, ParentSource.LOCAL, graphite.error)
            else:
                inferred = self.infer_parent(branch, allow_equal=True)
                info = ParentInfo(inferred, ParentSource.INFERRED)
        self._parent_cache[branch] = info
        return info

    def infer_parent(self, branch: str, *, allow_equal: bool = False) -> str | None:
        candidates_by_head: dict[str, list[str]] = {}
        target = self.worktree_for_branch(branch)
        target_head = target.head if target else None
        for worktree in self.managed_worktrees:
            candidate = worktree.branch
            if not candidate or candidate == branch or not worktree.head:
                continue
            if not allow_equal and target_head and worktree.head == target_head:
                continue
            candidates_by_head.setdefault(worktree.head, []).append(candidate)

        history = self.git(["rev-list", "--topo-order", branch], check=False)
        if history.returncode == 0:
            for commit in history.stdout.splitlines():
                candidates = candidates_by_head.get(commit)
                if not candidates:
                    continue
                if len(candidates) == 1:
                    return candidates[0]
                if self.root_worktree.branch in candidates:
                    return self.root_worktree.branch
                return None
        return self.root_worktree.branch

    def hierarchy(self) -> Hierarchy:
        nodes: dict[str, HierarchyNode] = {}
        by_branch = {
            item.branch: item
            for item in self.managed_worktrees
            if item.branch is not None
        }

        for worktree in self.managed_worktrees:
            info = self.direct_parent(worktree)
            warnings = [info.warning] if info.warning else []
            nodes[worktree.id] = HierarchyNode(
                worktree=worktree,
                direct_parent=info.parent,
                parent_id=None,
                source=info.source,
                warnings=warnings,
            )

        root_id = self.root_worktree.id
        for node_id, node in nodes.items():
            if node_id == root_id:
                continue
            parent_branch = node.direct_parent
            visited = {node.worktree.branch} if node.worktree.branch else set()
            while parent_branch:
                if parent_branch in visited:
                    node.warnings.append(f"parent cycle detected at {parent_branch}")
                    parent_branch = None
                    break
                visited.add(parent_branch)

                visible_parent = by_branch.get(parent_branch)
                if visible_parent is not None and visible_parent.id != node_id:
                    node.parent_id = visible_parent.id
                    break

                node.skipped_parents.append(parent_branch)
                virtual = self.parent_for_virtual_branch(parent_branch)
                if virtual.warning:
                    node.warnings.append(virtual.warning)
                if virtual.source == ParentSource.UNRESOLVED:
                    node.warnings.append(f"parent branch {parent_branch} could not be resolved")
                parent_branch = virtual.parent

            if node.parent_id is None:
                node.parent_id = root_id

        self._break_hierarchy_cycles(nodes, root_id)

        children: dict[str, list[str]] = {node_id: [] for node_id in nodes}
        for node_id, node in nodes.items():
            if node.parent_id is not None:
                children[node.parent_id].append(node_id)
        for child_ids in children.values():
            child_ids.sort(key=lambda item: nodes[item].worktree.label.casefold())
        return Hierarchy(root_id, nodes, children)

    def status(self, worktree: Worktree) -> str:
        if not worktree.path.exists():
            return "path is missing"
        result = self.git(
            ["status", "--short", "--branch", "--untracked-files=normal"],
            check=False,
            cwd=worktree.path,
        )
        return result.stdout.strip() or "clean"

    def is_clean(self, worktree: Worktree) -> bool:
        if not worktree.path.exists():
            return False
        result = self.git(
            ["status", "--porcelain", "--untracked-files=normal"],
            check=False,
            cwd=worktree.path,
        )
        return result.returncode == 0 and not result.stdout.strip()

    def ignored_paths(self, worktree: Worktree) -> list[str]:
        if not worktree.path.exists():
            return []
        result = self.git(
            ["status", "--porcelain=v1", "-z", "--ignored", "--untracked-files=normal"],
            check=False,
            cwd=worktree.path,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "failed to inspect ignored worktree content")
        return [entry[3:] for entry in result.stdout.split("\0") if entry.startswith("!! ")]

    def ignored_snapshot(
        self,
        worktree: Worktree,
        ignored_paths: list[str] | None = None,
    ) -> IgnoredSnapshot:
        roots = ignored_paths if ignored_paths is not None else self.ignored_paths(worktree)
        entries: list[tuple[str, str, int, int]] = []

        def add_path(path: Path) -> None:
            try:
                metadata = path.lstat()
            except OSError as error:
                raise RuntimeError(f"failed to inspect ignored path {path}: {error}") from error
            relative = path.relative_to(worktree.path).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                digest = hashlib.sha256(target.encode(errors="surrogateescape")).hexdigest()
                entries.append((relative, f"symlink:{digest}", metadata.st_size, metadata.st_mtime_ns))
            elif path.is_file():
                digest = hashlib.sha256()
                try:
                    with path.open("rb") as file:
                        for chunk in iter(lambda: file.read(1024 * 1024), b""):
                            digest.update(chunk)
                except OSError as error:
                    raise RuntimeError(f"failed to read ignored path {path}: {error}") from error
                entries.append((relative, f"file:{digest.hexdigest()}", metadata.st_size, metadata.st_mtime_ns))
            elif path.is_dir():
                entries.append((relative, "directory", metadata.st_size, metadata.st_mtime_ns))
                try:
                    children = sorted(path.iterdir(), key=lambda item: os.fsencode(item.name))
                except OSError as error:
                    raise RuntimeError(f"failed to list ignored path {path}: {error}") from error
                for child in children:
                    add_path(child)
            else:
                entries.append((relative, f"mode:{metadata.st_mode}", metadata.st_size, metadata.st_mtime_ns))

        for relative in sorted(roots, key=os.fsencode):
            path = worktree.path / relative.rstrip("/")
            if not path.exists() and not path.is_symlink():
                raise RuntimeError(f"ignored path changed during inspection: {relative}")
            add_path(path)
        return tuple(entries)

    def path_for_branch(self, branch: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", branch).strip("-") or "worktree"
        digest = hashlib.sha256(branch.encode()).hexdigest()[:8]
        if len(slug.encode()) > 180:
            slug = f"{slug[:170].rstrip('-')}-{digest}"
        candidate = self.worktrees_dir / slug
        registered = {item.path for item in self.all_worktrees}
        if candidate.exists() or candidate in registered:
            candidate = self.worktrees_dir / f"{slug}-{digest}"
        counter = 2
        while candidate.exists() or candidate in registered:
            candidate = self.worktrees_dir / f"{slug[:180]}-{digest}-{counter}"
            counter += 1
        return candidate

    def add_worktree(self, branch: str, parent: str) -> Worktree:
        validation = self.git(["check-ref-format", "--branch", branch], check=False)
        if validation.returncode != 0:
            raise RuntimeError(validation.stderr.strip() or f"invalid branch name: {branch}")
        existing = self.worktree_for_branch(branch)
        if existing is not None:
            raise RuntimeError(f"branch {branch} is already checked out at {existing.path}")

        path = self.path_for_branch(branch)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_managed_directory_ignored()
        branch_existed = self.branch_exists(branch)
        try:
            if branch_existed:
                self.git(["worktree", "add", str(path), branch])
            else:
                self.git(["worktree", "add", "-b", branch, str(path), parent])
        except CommandError:
            if not branch_existed and self.branch_exists(branch) and self.worktree_for_branch(branch) is None:
                self.git(["branch", "-D", branch], check=False)
            raise
        self.set_local_parent(branch, parent)
        return self._worktree_from_path(path)

    def add_existing_worktree(
        self,
        branch: str,
        parent: str,
        *,
        persist_parent: bool,
    ) -> Worktree:
        validation = self.git(["check-ref-format", "--branch", branch], check=False)
        if validation.returncode != 0:
            raise RuntimeError(validation.stderr.strip() or f"invalid branch name: {branch}")
        existing = self.worktree_for_branch(branch)
        if existing is not None:
            raise RuntimeError(f"branch {branch} is already checked out at {existing.path}")
        path = self.path_for_branch(branch)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_managed_directory_ignored()
        self.git(["worktree", "add", "--no-guess-remote", str(path), branch])
        try:
            if persist_parent:
                self.set_local_parent(branch, parent)
        except (CommandError, RuntimeError):
            self.git(["worktree", "remove", str(path)], check=False)
            raise
        return self._worktree_from_path(path)

    def ensure_managed_directory_ignored(self) -> None:
        if not _is_relative_to(self.worktrees_dir, self.root):
            return
        ignored = self.git(["check-ignore", "--quiet", str(self.worktrees_dir)], check=False)
        if ignored.returncode == 0:
            return
        relative = self.worktrees_dir.relative_to(self.root).as_posix().rstrip("/") + "/"
        exclude_path = self.common_dir / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text() if exclude_path.exists() else ""
        escaped_relative = _escape_gitignore_pattern(relative)
        patterns = set(existing.splitlines())
        if escaped_relative in patterns or f"/{escaped_relative}" in patterns:
            return
        separator = "" if not existing or existing.endswith("\n") else "\n"
        with exclude_path.open("a") as exclude:
            exclude.write(f"{separator}{escaped_relative}\n")

    def remove_worktree(
        self,
        worktree: Worktree,
        *,
        confirmed_ignored: IgnoredSnapshot | None = None,
    ) -> None:
        if worktree.is_root:
            raise RuntimeError("the main worktree cannot be removed")
        if worktree.locked is not None:
            detail = f": {worktree.locked}" if worktree.locked else ""
            raise RuntimeError(f"worktree is locked{detail}")
        if not self.is_clean(worktree):
            raise RuntimeError("worktree is dirty; commit, stash, or clean it before removal")
        ignored = self.ignored_paths(worktree)
        current_snapshot = self.ignored_snapshot(worktree, ignored)
        if current_snapshot != (confirmed_ignored or ()):
            raise RuntimeError("ignored worktree content changed or was not confirmed; removal aborted")
        self.git(["worktree", "remove", str(worktree.path)])

    def branch_is_retained(self, branch: str, parent: str | None) -> bool:
        if not parent or not self.branch_exists(parent):
            return False
        return self.git(["merge-base", "--is-ancestor", branch, parent], check=False).returncode == 0

    def delete_local_branch(self, branch: str, parent: str | None) -> None:
        children = self.local_children(branch)
        object_id = self.git(["rev-parse", f"refs/heads/{branch}"]).stdout.strip()
        if not self.branch_is_retained(branch, parent):
            raise RuntimeError(f"branch {branch} is not retained by {parent or 'a parent'}")
        updated_children: list[str] = []
        try:
            if parent:
                for child in children:
                    self.set_local_parent(child, parent)
                    updated_children.append(child)
            self.git(["update-ref", "-d", f"refs/heads/{branch}", object_id])
        except (CommandError, RuntimeError):
            for child in updated_children:
                try:
                    self.set_local_parent(child, branch)
                except (CommandError, RuntimeError):
                    pass
            raise
        self.git(["config", "--remove-section", f"branch.{branch}"], check=False)

    def local_children(self, parent: str) -> list[str]:
        result = self.git(
            ["config", "--get-regexp", r"^branch\..*\.tmux-worktrees-parent$"],
            check=False,
        )
        children: list[str] = []
        for line in result.stdout.splitlines():
            key, separator, value = line.partition(" ")
            if not separator or value.strip() != parent:
                continue
            prefix = "branch."
            suffix = ".tmux-worktrees-parent"
            if key.startswith(prefix) and key.endswith(suffix):
                children.append(key[len(prefix) : -len(suffix)])
        return children

    def local_parents(self) -> dict[str, str]:
        result = self.git(
            ["config", "--get-regexp", r"^branch\..*\.tmux-worktrees-parent$"],
            check=False,
        )
        parents: dict[str, str] = {}
        prefix = "branch."
        suffix = ".tmux-worktrees-parent"
        for line in result.stdout.splitlines():
            key, separator, value = line.partition(" ")
            if not separator or not key.startswith(prefix) or not key.endswith(suffix):
                continue
            branch = key[len(prefix) : -len(suffix)]
            if branch and value and self.branch_exists(branch):
                parents[branch] = value.strip()
        root_branch = self.root_worktree.branch
        for parent in list(parents.values()):
            if (
                parent
                and parent != root_branch
                and parent not in parents
                and self.branch_exists(parent)
            ):
                parents[parent] = root_branch or ""
        return parents

    def _validate_managed_directory(self) -> None:
        _validate_protocol_path(self.root)
        _validate_protocol_path(self.worktrees_dir)
        if self.worktrees_dir == self.root or _is_relative_to(self.root, self.worktrees_dir):
            raise RuntimeError("managed worktree directory cannot contain the repository root")
        if self.worktrees_dir == self.common_dir or _is_relative_to(
            self.worktrees_dir, self.common_dir
        ):
            raise RuntimeError("managed worktree directory cannot be inside the Git common directory")

    def _break_hierarchy_cycles(
        self,
        nodes: dict[str, HierarchyNode],
        root_id: str,
    ) -> None:
        for node_id in nodes:
            if node_id == root_id:
                continue
            path: list[str] = []
            positions: dict[str, int] = {}
            current_id: str | None = node_id
            while current_id is not None and current_id != root_id:
                if current_id in positions:
                    cycle = path[positions[current_id] :]
                    labels = ", ".join(nodes[item].worktree.label for item in cycle)
                    for cycle_id in cycle:
                        nodes[cycle_id].parent_id = root_id
                        nodes[cycle_id].warnings.append(f"hierarchy cycle broken: {labels}")
                    break
                positions[current_id] = len(path)
                path.append(current_id)
                current_id = nodes[current_id].parent_id

    def _worktree_from_path(self, path: Path) -> Worktree:
        refreshed = Repository.discover(path, self.runner)
        worktree = refreshed.managed_worktree(path)
        if worktree is None:
            raise RuntimeError(f"new worktree was not registered at {path}")
        return worktree


def parse_worktree_porcelain(output: bytes) -> list[Worktree]:
    records: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for token in output.split(b"\0"):
        if not token:
            if current:
                records.append(current)
                current = {}
            continue
        key_bytes, separator, value_bytes = token.partition(b" ")
        key = os.fsdecode(key_bytes)
        value = os.fsdecode(value_bytes) if separator else True
        if key == "worktree" and current:
            records.append(current)
            current = {}
        current[key] = value
    if current:
        records.append(current)

    worktrees: list[Worktree] = []
    for record in records:
        raw_path = record.get("worktree")
        if not isinstance(raw_path, str):
            continue
        branch = record.get("branch")
        if isinstance(branch, str) and branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/") :]
        elif not isinstance(branch, str):
            branch = None
        locked = record.get("locked")
        prunable = record.get("prunable")
        worktrees.append(
            Worktree(
                path=_canonical_path(Path(raw_path)),
                head=record.get("HEAD") if isinstance(record.get("HEAD"), str) else None,
                branch=branch,
                detached=bool(record.get("detached", False)),
                locked=locked if isinstance(locked, str) else "" if locked else None,
                prunable=prunable if isinstance(prunable, str) else "" if prunable else None,
            )
        )
    return worktrees


def _canonical_path(path: Path) -> Path:
    return Path(os.path.realpath(path))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_protocol_path(path: Path) -> None:
    if any(character in str(path) for character in ("\n", "\r", "\t", "\x1e", "\x1f")):
        raise RuntimeError(f"paths containing control separators are not supported: {path!s}")


def _escape_gitignore_pattern(value: str) -> str:
    trailing_spaces = len(value) - len(value.rstrip(" "))
    if trailing_spaces:
        value = value[:-trailing_spaces]
    escaped = re.sub(r"([\\*?\[\]])", r"\\\1", value)
    if escaped.startswith(("#", "!")):
        escaped = "\\" + escaped
    return escaped + "\\ " * trailing_spaces


def _clean_output(value: str) -> str:
    return ANSI_ESCAPE.sub("", value)


def _first_nonempty_line(value: str) -> str | None:
    for line in _clean_output(value).splitlines():
        if line.strip():
            return line.strip()
    return None


def _is_untracked_graphite_error(value: str) -> bool:
    return "untracked branch" in _clean_output(value).lower()


def _command_detail(stdout: str, stderr: str) -> str:
    detail = _clean_output(stderr).strip() or _clean_output(stdout).strip()
    return detail.splitlines()[0] if detail else "Graphite command failed"
