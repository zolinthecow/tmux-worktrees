from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .process import CommandError, Runner


IgnoredSnapshot = tuple[tuple[str, str, int, int], ...]


class ParentSource(str, Enum):
    ROOT = "root"
    GITHUB = "github"
    LOCAL = "local"
    UNREGISTERED = "unregistered"
    DETACHED = "detached"


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


@dataclass(frozen=True)
class PullRequest:
    number: int
    head: str
    base: str
    title: str
    url: str
    draft: bool


class GitHubProvider:
    def __init__(self, repo: Repository):
        self.repo = repo
        self._pull_requests: dict[str, PullRequest] | None = None
        self._involved_branches: set[str] | None = None
        self.error: str | None = None

    def pull_requests(self, *, force: bool = False) -> dict[str, PullRequest]:
        if self._pull_requests is not None and not force:
            return self._pull_requests
        result = self.repo.runner.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,title,headRefName,baseRefName,isDraft,url",
            ],
            cwd=self.repo.root,
            check=False,
        )
        if result.returncode != 0:
            self.error = result.stderr.strip() or result.stdout.strip() or "GitHub PR lookup failed"
            self._pull_requests = {}
            return self._pull_requests
        try:
            rows = json.loads(result.stdout)
            self._pull_requests = {
                row["headRefName"]: PullRequest(
                    number=int(row["number"]),
                    head=row["headRefName"],
                    base=row["baseRefName"],
                    title=row["title"],
                    url=row["url"],
                    draft=bool(row["isDraft"]),
                )
                for row in rows
            }
            self.error = None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.error = f"invalid GitHub PR response: {error}"
            self._pull_requests = {}
        return self._pull_requests

    def involved_branches(self) -> set[str]:
        if self._involved_branches is not None:
            return self._involved_branches
        result = self.repo.runner.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                "1000",
                "--search",
                "involves:@me",
                "--json",
                "headRefName",
            ],
            cwd=self.repo.root,
            check=False,
        )
        if result.returncode != 0:
            self._involved_branches = set()
            return self._involved_branches
        try:
            rows = json.loads(result.stdout)
            self._involved_branches = {row["headRefName"] for row in rows}
        except (KeyError, TypeError, json.JSONDecodeError):
            self._involved_branches = set()
        return self._involved_branches

    def registered_pull_requests(self) -> dict[str, PullRequest]:
        registered = self.repo.registered_branches()
        pull_requests = self.pull_requests()
        included = set(registered)
        included.update(self.involved_branches())
        included.update(
            worktree.branch
            for worktree in self.repo.managed_worktrees
            if worktree.branch in pull_requests
        )
        if not included:
            return {}
        changed = True
        while changed:
            changed = False
            for branch in list(included):
                pull_request = pull_requests.get(branch)
                if (
                    pull_request is not None
                    and pull_request.base in pull_requests
                    and pull_request.base not in included
                ):
                    included.add(pull_request.base)
                    changed = True
            for branch, pull_request in pull_requests.items():
                if pull_request.base in included and branch not in included:
                    included.add(branch)
                    changed = True
        registered_pull_requests = {
            branch: pull_request
            for branch, pull_request in pull_requests.items()
            if branch in included
        }
        for branch, pull_request in registered_pull_requests.items():
            if (
                self.repo.local_parent(branch) != pull_request.base
                or branch not in self.repo.registered_branches()
            ):
                self.repo.set_local_parent(branch, pull_request.base)
                self.repo.set_registered(branch, True)
        return registered_pull_requests

    def register(self, branch: str, parent: str) -> PullRequest:
        existing = self.pull_requests(force=True).get(branch)
        if existing is not None:
            self.repo.set_registered(branch, True)
            self.repo.set_local_parent(branch, existing.base)
            return existing
        self.repo.git(["push", "--set-upstream", "origin", branch])
        title = self.repo.git(["log", "-1", "--format=%s", branch]).stdout.strip() or branch
        result = self.repo.runner.run(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--head",
                branch,
                "--base",
                parent,
                "--title",
                title,
                "--body",
                "Registered by tmux-worktrees.",
            ],
            cwd=self.repo.root,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "failed to create draft PR")
        pull_request = self.pull_requests(force=True).get(branch)
        if pull_request is None:
            raise RuntimeError("draft PR was created but could not be rediscovered")
        self.repo.set_registered(branch, True)
        self.repo.set_local_parent(branch, pull_request.base)
        return pull_request

    def reparent(self, pull_request: PullRequest, parent: str) -> None:
        result = self.repo.runner.run(
            ["gh", "pr", "edit", str(pull_request.number), "--base", parent],
            cwd=self.repo.root,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "failed to update PR base")
        self._pull_requests = None
        self._involved_branches = None
        self.repo.set_local_parent(pull_request.head, parent)


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
        self.github = GitHubProvider(self)

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
    def prunable_worktrees(self) -> list[Worktree]:
        return [item for item in self.all_worktrees if item.prunable is not None]

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

    def ensure_local_branch(self, branch: str, *, pull_request: int | None = None) -> None:
        if self.branch_exists(branch):
            return
        if pull_request is not None:
            remote_ref = f"refs/remotes/origin/tmux-worktrees-pr/{pull_request}"
            self.git(
                [
                    "fetch",
                    "origin",
                    f"+refs/pull/{pull_request}/head:{remote_ref}",
                ]
            )
            self.git(["branch", branch, remote_ref])
            return
        remote_ref = f"refs/remotes/origin/{branch}"
        self.git(
            [
                "fetch",
                "origin",
                f"+refs/heads/{branch}:{remote_ref}",
            ]
        )
        self.git(["branch", "--track", branch, f"origin/{branch}"])

    def branch_tip(self, branch: str) -> str:
        result = self.git(["rev-parse", "--verify", f"refs/heads/{branch}"])
        return result.stdout.strip()

    def branch_is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self.git(
            ["merge-base", "--is-ancestor", ancestor, descendant], check=False
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise CommandError(result)

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
        result = self.git(
            ["config", "--local", "--null", "--get", f"branch.{branch}.tmux-worktrees-parent"],
            check=False,
        )
        return result.stdout.removesuffix("\0") if result.returncode == 0 else None

    def set_local_parent(self, branch: str, parent: str) -> None:
        self.git(["config", "--local", f"branch.{branch}.tmux-worktrees-parent", parent])
        self._parent_cache.pop(branch, None)

    def unset_local_parent(self, branch: str) -> None:
        self.git(
            ["config", "--local", "--unset", f"branch.{branch}.tmux-worktrees-parent"],
            check=False,
        )
        self._parent_cache.pop(branch, None)

    def registered_branches(self) -> set[str]:
        result = self.git(
            ["config", "--local", "--get-regexp", r"^branch\..*\.tmux-worktrees-registered$"],
            check=False,
        )
        registered: set[str] = set()
        prefix = "branch."
        suffix = ".tmux-worktrees-registered"
        for line in result.stdout.splitlines():
            key, separator, value = line.partition(" ")
            if (
                separator
                and value.strip().lower() == "true"
                and key.startswith(prefix)
                and key.endswith(suffix)
            ):
                registered.add(key[len(prefix) : -len(suffix)])
        return registered

    def set_registered(self, branch: str, registered: bool) -> None:
        key = f"branch.{branch}.tmux-worktrees-registered"
        if registered:
            self.git(["config", "--local", key, "true"])
        else:
            self.git(["config", "--local", "--unset", key], check=False)
        self._parent_cache.pop(branch, None)

    def register_local(self, branch: str, parent: str) -> None:
        if branch == self.trunk_branch:
            raise RuntimeError("the trunk branch is already registered")
        if branch == parent:
            raise RuntimeError("a branch cannot be its own parent")
        if not self.branch_exists(branch):
            raise RuntimeError(f"branch does not exist locally: {branch}")
        if not self.branch_exists(parent) and parent not in self.github.pull_requests():
            raise RuntimeError(f"parent branch does not exist: {parent}")

        visited: set[str] = set()
        current = parent
        pull_requests = self.github.pull_requests()
        while current and current not in visited:
            if current == branch:
                raise RuntimeError("branch registration would create a parent cycle")
            visited.add(current)
            pull_request = pull_requests.get(current)
            current = (
                pull_request.base
                if pull_request is not None
                else self.local_parent(current) or ""
            )

        previous_parent = self.local_parent(branch)
        was_registered = branch in self.registered_branches()
        try:
            self.set_local_parent(branch, parent)
            self.set_registered(branch, True)
        except (CommandError, RuntimeError):
            if previous_parent is None:
                self.unset_local_parent(branch)
            else:
                self.set_local_parent(branch, previous_parent)
            if not was_registered:
                self.set_registered(branch, False)
            raise

    def unregister(self, branch: str) -> None:
        self.set_registered(branch, False)
        self.unset_local_parent(branch)

    @property
    def trunk_branch(self) -> str | None:
        return self.config_value("tmux-worktrees.trunk", self.root_worktree.branch)

    def direct_parent(self, worktree: Worktree) -> ParentInfo:
        if worktree.is_root:
            return ParentInfo(None, ParentSource.ROOT)
        if worktree.branch is None:
            return ParentInfo(self.root_worktree.branch, ParentSource.DETACHED)
        if worktree.branch in self._parent_cache:
            return self._parent_cache[worktree.branch]

        registered_pull_request = self.github.registered_pull_requests().get(worktree.branch)
        local = self.local_parent(worktree.branch)
        if registered_pull_request is not None:
            info = ParentInfo(registered_pull_request.base, ParentSource.GITHUB)
        elif local and worktree.branch in self.registered_branches():
            info = ParentInfo(local, ParentSource.LOCAL)
        else:
            info = ParentInfo(
                self.trunk_branch,
                ParentSource.UNREGISTERED,
                "branch is not registered; use ctrl-g to create or import a draft PR",
            )
        self._parent_cache[worktree.branch] = info
        return info

    def parent_for_virtual_branch(self, branch: str) -> ParentInfo:
        if branch == self.root_worktree.branch:
            return ParentInfo(None, ParentSource.ROOT)
        if branch in self._parent_cache:
            return self._parent_cache[branch]

        registered_pull_request = self.github.registered_pull_requests().get(branch)
        local = self.local_parent(branch)
        if registered_pull_request is not None:
            info = ParentInfo(registered_pull_request.base, ParentSource.GITHUB)
        elif local and branch in self.registered_branches():
            info = ParentInfo(local, ParentSource.LOCAL)
        else:
            info = ParentInfo(
                self.trunk_branch,
                ParentSource.UNREGISTERED,
                "branch is not registered; use ctrl-g to create or import a draft PR",
            )
        self._parent_cache[branch] = info
        return info

    def local_branch_tips(self) -> dict[str, str]:
        result = self.git(
            ["for-each-ref", "--format=%(refname:short)%00%(objectname)", "refs/heads"],
            check=False,
        )
        tips: dict[str, str] = {}
        for line in result.stdout.splitlines():
            branch, separator, head = line.partition("\0")
            if separator and branch and head:
                tips[branch] = head
        return tips

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

    def switch_worktree_branch(
        self,
        worktree: Worktree,
        *,
        expected_branch: str,
        target_branch: str,
        expected_generation: str,
        require_clean: bool = True,
    ) -> None:
        current = self.managed_worktree(worktree.path)
        if current is None or current.branch != expected_branch:
            raise RuntimeError("worktree branch changed during reconciliation")
        if self.worktree_generation(current, create=False) != expected_generation:
            raise RuntimeError("worktree generation changed during reconciliation")
        if require_clean and not self.is_clean(current):
            raise RuntimeError("worktree became dirty during reconciliation")
        occupied = self.worktree_for_branch(target_branch)
        if occupied is not None and occupied.id != current.id:
            raise RuntimeError(f"branch {target_branch} is already checked out at {occupied.path}")
        self.git(["switch", "--no-guess", target_branch], cwd=current.path)
        refreshed = Repository.discover(current.path, self.runner)
        switched = refreshed.managed_worktree(current.path)
        if (
            switched is None
            or switched.branch != target_branch
            or refreshed.worktree_generation(switched, create=False) != expected_generation
        ):
            raise RuntimeError("worktree branch switch could not be verified")

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
        preserve_parent: str | None = None,
    ) -> None:
        if worktree.is_root:
            raise RuntimeError("the main worktree cannot be removed")
        if worktree.locked is not None:
            detail = f": {worktree.locked}" if worktree.locked else ""
            raise RuntimeError(f"worktree is locked{detail}")
        if not self.is_clean(worktree):
            raise RuntimeError("worktree is dirty; commit, stash, or clean it before removal")
        parent_added = bool(
            worktree.branch
            and preserve_parent
            and self.local_parent(worktree.branch) is None
        )
        if parent_added:
            self.set_local_parent(worktree.branch, preserve_parent)
        try:
            ignored = self.ignored_paths(worktree)
            current_snapshot = self.ignored_snapshot(worktree, ignored)
            if current_snapshot != (confirmed_ignored or ()):
                raise RuntimeError(
                    "ignored worktree content changed or was not confirmed; removal aborted"
                )
            self.git(["worktree", "remove", str(worktree.path)])
        except (CommandError, RuntimeError):
            if (
                parent_added
                and worktree.branch
                and self.local_parent(worktree.branch) == preserve_parent
            ):
                self.git(
                    ["config", "--local", "--unset", f"branch.{worktree.branch}.tmux-worktrees-parent"],
                    check=False,
                )
            raise

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
        self.git(["config", "--local", "--remove-section", f"branch.{branch}"], check=False)

    def local_children(self, parent: str) -> list[str]:
        result = self.git(
            ["config", "--local", "--get-regexp", r"^branch\..*\.tmux-worktrees-parent$"],
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
            ["config", "--local", "--get-regexp", r"^branch\..*\.tmux-worktrees-parent$"],
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
