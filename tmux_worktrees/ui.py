from __future__ import annotations

import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from .model import Hierarchy, HierarchyNode, ParentSource, Repository, Worktree
from .process import CommandError, Runner
from .tmux import TmuxManager


EXPECTED_KEYS = {
    "enter",
    "ctrl-a",
    "ctrl-t",
    "ctrl-p",
    "ctrl-x",
    "alt-x",
    "ctrl-b",
    "ctrl-r",
}


@dataclass(frozen=True)
class VirtualBranchNode:
    branch: str
    direct_parent: str | None
    source: ParentSource
    warnings: tuple[str, ...] = ()
    external_path: Path | None = None

    @property
    def id(self) -> str:
        return f"branch:{self.branch}"


PickerNode = HierarchyNode | VirtualBranchNode


@dataclass
class _DisplayNode:
    id: str
    label: str
    parent_id: str | None
    source: ParentSource
    picker_node: PickerNode


def source_badge(source: ParentSource) -> str:
    return {
        ParentSource.ROOT: " ",
        ParentSource.GRAPHITE: "G",
        ParentSource.LOCAL: "L",
        ParentSource.INFERRED: "?",
        ParentSource.DETACHED: "D",
        ParentSource.UNRESOLVED: "!",
    }[source]


def _hierarchy_rows(
    repo: Repository,
    hierarchy: Hierarchy,
    *,
    active_path: str | None = None,
    include_inactive: bool = False,
) -> tuple[list[tuple[str, PickerNode]], dict[str, str | None]]:
    display_nodes: dict[str, _DisplayNode] = {}
    for node_id, node in hierarchy.nodes.items():
        if (
            not include_inactive
            and not node.worktree.is_root
            and (not node.worktree.path.exists() or node.worktree.prunable is not None)
        ):
            continue
        display_nodes[node_id] = _DisplayNode(
            id=node_id,
            label=node.worktree.label,
            parent_id=node.parent_id,
            source=node.source,
            picker_node=node,
        )

    for display_node in display_nodes.values():
        while display_node.parent_id and display_node.parent_id not in display_nodes:
            display_node.parent_id = hierarchy.nodes[display_node.parent_id].parent_id

    external_worktrees_by_branch: dict[str, list[Worktree]] = {}
    for worktree in repo.external_worktrees:
        if worktree.branch is not None:
            external_worktrees_by_branch.setdefault(worktree.branch, []).append(worktree)
    for worktrees in external_worktrees_by_branch.values():
        worktrees.sort(key=lambda item: item.id)
    external_by_branch = {
        branch: worktrees[0].path
        for branch, worktrees in external_worktrees_by_branch.items()
    }
    stale_external_branches = {
        worktree.branch
        for worktree in repo.external_worktrees
        if worktree.branch is not None
        and (worktree.prunable is not None or not worktree.path.exists())
    }

    def external_warnings(branch: str) -> tuple[str, ...]:
        warnings: list[str] = []
        if branch in stale_external_branches:
            warnings.append("registered worktree path is missing")
        count = len(external_worktrees_by_branch.get(branch, []))
        if count > 1:
            warnings.append(f"{count} external registrations share this branch")
        return tuple(warnings)

    for node_id, node in hierarchy.nodes.items():
        if not include_inactive or node_id not in display_nodes:
            continue
        if node_id == hierarchy.root_id or not node.skipped_parents:
            continue
        display_parent = node.parent_id or hierarchy.root_id
        for branch in reversed(node.skipped_parents):
            if not repo.branch_exists(branch):
                continue
            virtual_id = f"branch:{branch}"
            parent_info = repo.parent_for_virtual_branch(branch)
            virtual = display_nodes.get(virtual_id)
            if virtual is None:
                warnings = (parent_info.warning,) if parent_info.warning else ()
                picker_node = VirtualBranchNode(
                    branch=branch,
                    direct_parent=parent_info.parent,
                    source=parent_info.source,
                    warnings=warnings + external_warnings(branch),
                    external_path=external_by_branch.get(branch),
                )
                virtual = _DisplayNode(
                    id=virtual_id,
                    label=branch,
                    parent_id=display_parent,
                    source=parent_info.source,
                    picker_node=picker_node,
                )
                display_nodes[virtual_id] = virtual
            display_parent = virtual_id
        display_nodes[node_id].parent_id = display_parent

    managed_by_branch = {
        node.worktree.branch: node_id
        for node_id, node in hierarchy.nodes.items()
        if node.worktree.branch is not None
    }
    graphite_parents = repo.graphite.tracked_parents() if include_inactive else {}
    for branch, parent in graphite_parents.items():
        if branch in managed_by_branch:
            continue
        virtual_id = f"branch:{branch}"
        if virtual_id not in display_nodes:
            display_nodes[virtual_id] = _DisplayNode(
                id=virtual_id,
                label=branch,
                parent_id=None,
                source=ParentSource.GRAPHITE,
                picker_node=VirtualBranchNode(
                    branch=branch,
                    direct_parent=parent,
                    source=ParentSource.GRAPHITE,
                    warnings=external_warnings(branch),
                    external_path=external_by_branch.get(branch),
                ),
            )

    local_parents = repo.local_parents() if include_inactive else {}
    for branch, parent in local_parents.items():
        if branch in managed_by_branch:
            continue
        virtual_id = f"branch:{branch}"
        if virtual_id not in display_nodes:
            display_nodes[virtual_id] = _DisplayNode(
                id=virtual_id,
                label=branch,
                parent_id=None,
                source=ParentSource.LOCAL,
                picker_node=VirtualBranchNode(
                    branch=branch,
                    direct_parent=parent,
                    source=ParentSource.LOCAL,
                    warnings=external_warnings(branch),
                    external_path=external_by_branch.get(branch),
                ),
            )

    if include_inactive:
        for branch, path in external_by_branch.items():
            if branch in managed_by_branch:
                continue
            virtual_id = f"branch:{branch}"
            if virtual_id in display_nodes:
                continue
            parent = (
                repo.infer_parent(
                    branch,
                    allow_equal=True,
                    include_external=True,
                )
                or repo.root_worktree.branch
            )
            display_nodes[virtual_id] = _DisplayNode(
                id=virtual_id,
                label=branch,
                parent_id=None,
                source=ParentSource.INFERRED,
                picker_node=VirtualBranchNode(
                    branch=branch,
                    direct_parent=parent,
                    source=ParentSource.INFERRED,
                    warnings=external_warnings(branch),
                    external_path=path,
                ),
            )

    for display_node in display_nodes.values():
        if not isinstance(display_node.picker_node, VirtualBranchNode):
            continue
        parent = display_node.picker_node.direct_parent
        if parent in managed_by_branch:
            display_node.parent_id = managed_by_branch[parent]
        elif parent and f"branch:{parent}" in display_nodes:
            display_node.parent_id = f"branch:{parent}"
        else:
            display_node.parent_id = hierarchy.root_id

    for node_id, node in hierarchy.nodes.items():
        if node_id == hierarchy.root_id or not node.direct_parent:
            continue
        virtual_parent_id = f"branch:{node.direct_parent}"
        if virtual_parent_id in display_nodes:
            display_nodes[node_id].parent_id = virtual_parent_id

    for node_id in display_nodes:
        if node_id == hierarchy.root_id:
            continue
        path: list[str] = []
        positions: dict[str, int] = {}
        current_id: str | None = node_id
        while current_id is not None and current_id != hierarchy.root_id:
            if current_id in positions:
                cycle = path[positions[current_id] :]
                labels = ", ".join(display_nodes[item].label for item in cycle)
                for cycle_id in cycle:
                    cycle_node = display_nodes[cycle_id]
                    cycle_node.parent_id = hierarchy.root_id
                    if isinstance(cycle_node.picker_node, VirtualBranchNode):
                        cycle_node.picker_node = replace(
                            cycle_node.picker_node,
                            warnings=cycle_node.picker_node.warnings
                            + (f"display cycle broken: {labels}",),
                        )
                    else:
                        cycle_node.picker_node.warnings.append(
                            f"display cycle broken: {labels}"
                        )
                break
            positions[current_id] = len(path)
            path.append(current_id)
            current_id = display_nodes[current_id].parent_id

    children: dict[str, list[str]] = {node_id: [] for node_id in display_nodes}
    for node_id, node in display_nodes.items():
        if node.parent_id is not None:
            children.setdefault(node.parent_id, []).append(node_id)
    for child_ids in children.values():
        child_ids.sort(key=lambda item: display_nodes[item].label.casefold())

    rows: list[tuple[str, PickerNode]] = []

    visited: set[str] = set()

    def visit(node_id: str, prefix: str, connector: str) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        display_node = display_nodes[node_id]
        picker_node = display_node.picker_node
        badge = source_badge(display_node.source)
        if isinstance(picker_node, HierarchyNode):
            worktree = picker_node.worktree
            active = "*" if active_path == worktree.id else " "
            path_label = worktree.path.name
            secondary = f" ({path_label}/)" if path_label != worktree.label else ""
            warning = (
                " !"
                if picker_node.warnings
                or worktree.prunable is not None
                or worktree.branch in external_worktrees_by_branch
                else ""
            )
            suffix = f"[{badge}]{warning}"
        else:
            active = " "
            secondary = ""
            warning = " !" if picker_node.warnings else ""
            kind = "external" if picker_node.external_path else "branch"
            suffix = f"[{badge} {kind}]{warning}"
        display = f"{active} {prefix}{connector}{display_node.label}{secondary}  {suffix}"
        rows.append((display.rstrip(), picker_node))

        child_ids = children.get(node_id, [])
        child_prefix = prefix + ("   " if connector == "└─ " else "│  " if connector else "")
        for index, child_id in enumerate(child_ids):
            child_connector = "└─ " if index == len(child_ids) - 1 else "├─ "
            visit(child_id, child_prefix, child_connector)

    visit(hierarchy.root_id, "", "")
    return rows, {
        node_id: display_node.parent_id
        for node_id, display_node in display_nodes.items()
    }


def render_hierarchy(
    repo: Repository,
    hierarchy: Hierarchy,
    *,
    active_path: str | None = None,
    include_inactive: bool = False,
) -> list[tuple[str, PickerNode]]:
    rows, _ = _hierarchy_rows(
        repo,
        hierarchy,
        active_path=active_path,
        include_inactive=include_inactive,
    )
    return rows


def hierarchy_as_json(
    repo: Repository,
    hierarchy: Hierarchy,
    *,
    include_inactive: bool = False,
) -> str:
    data = []
    rows, visible_parents = _hierarchy_rows(
        repo, hierarchy, include_inactive=include_inactive
    )
    for _, node in rows:
        if isinstance(node, HierarchyNode):
            node_id = node.worktree.id
            visible_parent = visible_parents[node_id]
            external_paths = [
                str(worktree.path)
                for worktree in repo.external_worktrees
                if worktree.branch and worktree.branch == node.worktree.branch
            ]
            data.append(
                {
                    "id": node_id,
                    "kind": "worktree",
                    "path": str(node.worktree.path),
                    "external_paths": external_paths,
                    "branch": node.worktree.branch,
                    "head": node.worktree.head,
                    "source": node.source.value,
                    "direct_parent": node.direct_parent,
                    "visible_parent_id": visible_parent,
                    "visible_parent_path": (
                        visible_parent
                        if visible_parent and not visible_parent.startswith("branch:")
                        else None
                    ),
                    "skipped_parents": node.skipped_parents,
                    "warnings": node.warnings,
                    "locked": node.worktree.locked,
                    "prunable": node.worktree.prunable,
                    "detached": node.worktree.detached,
                }
            )
            continue

        existing = repo.worktree_for_branch(node.branch)
        external_paths = [
            str(worktree.path)
            for worktree in repo.external_worktrees
            if worktree.branch == node.branch
        ]
        data.append(
            {
                "id": node.id,
                "kind": "external" if node.external_path else "branch",
                "path": str(node.external_path) if node.external_path else None,
                "external_paths": external_paths,
                "branch": node.branch,
                "head": existing.head if existing else None,
                "source": node.source.value,
                "direct_parent": node.direct_parent,
                "visible_parent_id": visible_parents[node.id],
                "visible_parent_path": None,
                "skipped_parents": [],
                "warnings": list(node.warnings),
                "locked": existing.locked if existing else None,
                "prunable": existing.prunable if existing else None,
                "detached": False,
            }
        )
    return json.dumps(data, indent=2)


class Picker:
    def __init__(
        self,
        repo: Repository,
        tmux: TmuxManager | None = None,
        runner: Runner | None = None,
        executable: str | None = None,
    ):
        self.repo = repo
        self.runner = runner or repo.runner
        self.tmux = tmux or TmuxManager(self.runner)
        self.executable = executable or str(Path(sys.argv[0]).resolve())

    def run(self) -> None:
        include_inactive = False
        while True:
            current_session_id = self.tmux.current_session_id()
            try:
                if current_session_id is not None:
                    self.repo = self.tmux.reconcile_session_switch(
                        self.repo, current_session_id
                    )
                else:
                    self.repo = Repository.discover(self.repo.root, self.runner)
            except (RuntimeError, CommandError) as error:
                self._pause(f"Error reconciling branch switch: {error}")
                return
            hierarchy = self.repo.hierarchy()
            selected = self._select(hierarchy, include_inactive=include_inactive)
            if selected is None:
                return
            key, node = selected
            if key == "ctrl-b":
                include_inactive = not include_inactive
                continue
            try:
                if key != "ctrl-r":
                    node, hierarchy = self._refresh_picker_node(node, exact=False)
                should_exit = self._handle(key, node, hierarchy)
            except (RuntimeError, CommandError) as error:
                self._pause(f"Error: {error}")
                continue
            if should_exit:
                return

    def _select(
        self, hierarchy: Hierarchy, *, include_inactive: bool
    ) -> tuple[str, PickerNode] | None:
        current_path = self.tmux.current_pane_path()
        active_worktree = (
            self.tmux.worktree_for_path(self.repo, current_path) if current_path is not None else None
        )
        active_path = active_worktree.id if active_worktree else None
        rows = render_hierarchy(
            self.repo,
            hierarchy,
            active_path=active_path,
            include_inactive=include_inactive,
        )
        payload_rows: list[str] = []
        selections: dict[tuple[str, str], PickerNode] = {}
        for display, node in rows:
            if isinstance(node, HierarchyNode):
                kind = "worktree"
                target = str(node.worktree.path)
            else:
                kind = "branch"
                target = node.branch
            payload_rows.append(f"{display}\t{kind}\t{target}")
            selections[(kind, target)] = node
        payload = "\n".join(payload_rows)
        self.runner.env["TMUX_WORKTREES_EXECUTABLE"] = self.executable
        self.runner.env["TMUX_WORKTREES_REPO"] = str(self.repo.root)
        preview = (
            '"$TMUX_WORKTREES_EXECUTABLE" preview '
            '--repo "$TMUX_WORKTREES_REPO" --kind {2} --target {3}'
        )
        command = [
            "fzf",
            "--ansi",
            "--reverse",
            "--delimiter=\t",
            "--with-nth=1",
            "--expect=" + ",".join(sorted(EXPECTED_KEYS)),
            "--header=enter open | ctrl-a add | ctrl-b "
            + ("active only" if include_inactive else "show inactive")
            + " | ctrl-x deactivate | alt-x delete | ctrl-r refresh",
            "--preview",
            preview,
            "--preview-window=right,55%,wrap",
        ]
        result = self.runner.run(command, check=False, input_text=payload)
        if result.returncode not in (0, 1, 130) and result.stderr.strip():
            raise RuntimeError(result.stderr.strip())
        if result.returncode != 0 or not result.stdout:
            return None

        lines = result.stdout.rstrip("\n").splitlines()
        if not lines:
            return None
        if lines[0] in EXPECTED_KEYS:
            key = lines[0]
            selected_line = lines[1] if len(lines) > 1 else ""
        else:
            key = "enter"
            selected_line = lines[0]
        fields = selected_line.split("\t")
        if len(fields) < 3:
            return None
        node = selections.get((fields[1], fields[2]))
        return (key, node) if node is not None else None

    def _handle(self, key: str, node: PickerNode, hierarchy: Hierarchy) -> bool:
        if key == "ctrl-r":
            return False
        if isinstance(node, VirtualBranchNode):
            if key == "enter":
                return self._open_virtual_branch(node)
            raise RuntimeError(
                f"{node.branch} has no managed worktree; press Enter to create and open it"
            )
        if not node.worktree.path.exists() or node.worktree.prunable is not None:
            raise RuntimeError(
                "worktree path is missing; run doctor and inspect stale registrations before pruning"
            )
        if key == "enter":
            session, _ = self.tmux.ensure_session(self.repo, node.worktree)
            self.tmux.switch_worktree(self.repo, node.worktree, session)
            return True
        if key == "ctrl-a":
            return self._add(node)
        if key == "ctrl-t":
            self._track(node)
            return False
        if key == "ctrl-p":
            self._reparent(node, hierarchy)
            return False
        if key == "ctrl-x":
            return self._remove(node, hierarchy, delete_branch=False)
        if key == "alt-x":
            return self._remove(node, hierarchy, delete_branch=True)
        return False

    def _open_virtual_branch(self, node: VirtualBranchNode) -> bool:
        if not self.repo.branch_exists(node.branch):
            raise RuntimeError(f"branch no longer exists: {node.branch}")
        existing = self.repo.worktree_for_branch(node.branch)
        if existing is not None:
            detail = (
                "stale registration"
                if existing.prunable is not None or not existing.path.exists()
                else "external worktree"
            )
            raise RuntimeError(
                f"branch is already checked out in a {detail}: {existing.path}"
            )
        parent = node.direct_parent or self.repo.root_worktree.branch
        if parent is None:
            raise RuntimeError(f"cannot determine a parent for {node.branch}")
        worktree = self.repo.add_existing_worktree(
            node.branch,
            parent,
            persist_parent=node.source not in {ParentSource.GRAPHITE, ParentSource.LOCAL},
        )
        refreshed = Repository.discover(worktree.path, self.runner)
        managed = refreshed.managed_worktree(worktree.path)
        if managed is None:
            raise RuntimeError("created worktree is outside the managed directory")
        session = None
        session_created = False
        try:
            session, session_created = self.tmux.ensure_session(refreshed, managed)
            self.tmux.switch_worktree(refreshed, managed, session)
        except (CommandError, RuntimeError):
            if session_created and session is not None:
                try:
                    self.tmux.kill_session(session.id)
                except (CommandError, RuntimeError):
                    pass
            try:
                refreshed.remove_worktree(managed)
            except (CommandError, RuntimeError):
                pass
            raise
        return True

    def _add(self, parent_node: HierarchyNode) -> bool:
        parent = parent_node.worktree
        if parent.branch is None:
            raise RuntimeError("cannot create a child from a detached worktree")
        branch = input(f"New branch from {parent.branch}: ").strip()
        if not branch:
            return False
        if not self.repo.is_clean(parent):
            print("Parent is dirty. The new worktree starts from its committed branch HEAD.")
            if not self._confirm("Continue?"):
                return False
        if self.repo.branch_exists(branch):
            ancestry = self.repo.git(
                ["merge-base", "--is-ancestor", parent.branch, branch], check=False
            )
            if ancestry.returncode != 0 and not self._confirm(
                f"Existing branch {branch} is not based on {parent.branch}. Add it anyway?"
            ):
                return False
        parent_node, _ = self._refresh_selected(parent_node, exact=True)
        parent = parent_node.worktree
        worktree = self.repo.add_worktree(branch, parent.branch)
        refreshed = Repository.discover(worktree.path, self.runner)
        managed = refreshed.managed_worktree(worktree.path)
        if managed is None:
            raise RuntimeError("created worktree is outside the managed directory")
        session, _ = self.tmux.ensure_session(refreshed, managed)
        self.tmux.switch_worktree(refreshed, managed, session)
        return True

    def _track(self, node: HierarchyNode) -> None:
        worktree = node.worktree
        if worktree.branch is None:
            raise RuntimeError("detached worktrees cannot be tracked by Graphite")
        if node.source == ParentSource.GRAPHITE:
            self._pause(f"{worktree.branch} is already tracked by Graphite.")
            return
        if not node.direct_parent:
            raise RuntimeError("select a local parent before tracking this branch")
        if not self.repo.graphite.configured:
            raise RuntimeError("Graphite is not initialized in this repository; run gt init first")
        print(f"Track {worktree.branch} on top of {node.direct_parent} with Graphite.")
        if not self._confirm("Continue?"):
            return
        node, _ = self._refresh_selected(node, exact=True)
        worktree = node.worktree
        if node.source == ParentSource.GRAPHITE:
            raise RuntimeError("branch became Graphite-tracked while the picker was open")
        self.repo.graphite.require_supported_version()
        result = self.runner.run(
            [
                "gt",
                "track",
                worktree.branch,
                "--parent",
                node.direct_parent,
                "--cwd",
                str(worktree.path),
                "--no-interactive",
            ],
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        self._pause(result.stdout.strip() or f"Tracked {worktree.branch}.")

    def _reparent(self, node: HierarchyNode, hierarchy: Hierarchy) -> None:
        worktree = node.worktree
        if worktree.is_root:
            raise RuntimeError("the main worktree cannot be reparented")
        if worktree.branch is None:
            raise RuntimeError("detached worktrees cannot be reparented")
        if node.source == ParentSource.GRAPHITE:
            raise RuntimeError(
                f"run `gt move --source {worktree.branch} --onto <parent>` manually; "
                "the picker will refresh Graphite metadata afterward"
            )
        if node.source == ParentSource.UNRESOLVED:
            raise RuntimeError("Graphite state is unresolved; reparenting is disabled")

        descendants = self._descendants(hierarchy, worktree.id)
        candidates = [
            item
            for item in self.repo.managed_worktrees
            if item.branch
            and item.id != worktree.id
            and item.id not in descendants
            and item.path.exists()
            and item.prunable is None
        ]
        payload = "\n".join(f"{item.branch}\t{item.path}" for item in candidates)
        result = self.runner.run(
            [
                "fzf",
                "--reverse",
                "--delimiter=\t",
                "--with-nth=1",
                f"--header=New parent for {worktree.branch}",
            ],
            check=False,
            input_text=payload,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
        parent = result.stdout.rstrip("\n").split("\t", 1)[0]

        node, _ = self._refresh_selected(node, exact=True)
        if not any(item.branch == parent for item in self.repo.managed_worktrees):
            raise RuntimeError(f"selected parent is no longer available: {parent}")
        self.repo.set_local_parent(worktree.branch, parent)

    def _remove(
        self,
        node: HierarchyNode,
        hierarchy: Hierarchy,
        *,
        delete_branch: bool,
    ) -> bool:
        worktree = node.worktree
        if worktree.is_root:
            raise RuntimeError("the main worktree cannot be removed")
        if worktree.branch is None:
            raise RuntimeError(
                "detached worktrees cannot be deactivated safely; create a branch for this commit first"
            )
        if delete_branch and node.source == ParentSource.GRAPHITE:
            raise RuntimeError(
                "remove the checkout with ctrl-x, then run gt delete manually from another worktree"
            )
        if delete_branch and node.source == ParentSource.UNRESOLVED:
            raise RuntimeError("Graphite state is unresolved; refusing to choose a branch deletion strategy")
        if not self.repo.is_clean(worktree):
            raise RuntimeError("worktree is dirty; commit, stash, or clean it before removal")

        session = self.tmux.lookup_session(self.repo, worktree)
        confirmed_session_id = session.id if session else None
        ignored = self.repo.ignored_paths(worktree)
        ignored_snapshot = self.repo.ignored_snapshot(worktree, ignored)
        print(f"Worktree: {worktree.path}")
        print(f"Branch:   {worktree.branch or '(detached)'}")
        if session:
            commands = self.tmux.pane_commands(session.id)
            print(f"Session:  {session.name}")
            if session.attached:
                print("          attached client present")
            for command in commands:
                print(f"          {command}")
        if ignored:
            print(f"Ignored paths that will also be deleted ({len(ignored)}):")
            for path in ignored:
                print(f"          {path}")

        if delete_branch:
            print("The checkout will be deactivated first. Branch deletion remains non-forced.")
            confirmation = input(
                f"Type {worktree.branch} to deactivate the checkout and delete its branch: "
            ).strip()
            if confirmation != worktree.branch:
                return False
        elif not self._confirm("Deactivate this checkout and keep its branch?"):
            return False

        kill_session = bool(session) and self._confirm(
            f"Also kill tmux session {session.name}?"
        )

        node, hierarchy = self._refresh_selected(node, exact=True)
        worktree = node.worktree
        parent_worktree = self._nearest_active_parent(hierarchy, node)
        session = self.tmux.lookup_session(self.repo, worktree)
        if session and confirmed_session_id and session.id != confirmed_session_id:
            raise RuntimeError("tmux session changed while awaiting confirmation; action aborted")
        if bool(session) != bool(confirmed_session_id):
            kill_session = False
        current_path = self.tmux.current_pane_path()
        current_owner = (
            self.tmux.worktree_for_path(self.repo, current_path) if current_path is not None else None
        )
        parent_session = None
        if current_owner and current_owner.id == worktree.id:
            parent_session, _ = self.tmux.ensure_session(self.repo, parent_worktree)

        if delete_branch and worktree.branch:
            if not self.repo.branch_is_retained(worktree.branch, node.direct_parent):
                raise RuntimeError(
                    "branch tip is not retained by its logical parent; refusing non-interactive deletion"
                )
        self.repo.remove_worktree(
            worktree,
            confirmed_ignored=ignored_snapshot,
            preserve_parent=node.direct_parent or self.repo.root_worktree.branch,
        )

        branch_error: str | None = None
        if delete_branch and worktree.branch:
            try:
                self.repo.delete_local_branch(worktree.branch, node.direct_parent)
            except (RuntimeError, CommandError) as error:
                branch_error = str(error)

        switched = parent_session is not None
        if parent_session is not None:
            self.tmux.switch_worktree(self.repo, parent_worktree, parent_session)
        if session and kill_session:
            current_session = next(
                (item for item in self.tmux.sessions() if item.id == session.id), None
            )
            expected_identity = (
                session.repo,
                session.path,
                session.branch,
                session.generation,
            )
            current_identity = (
                (
                    current_session.repo,
                    current_session.path,
                    current_session.branch,
                    current_session.generation,
                )
                if current_session
                else None
            )
            if current_identity == expected_identity:
                self.tmux.kill_session(session.id)
            elif current_session is not None:
                self._pause("Checkout removed, but the tmux session changed identity and was left running.")
        if branch_error:
            self._pause(f"Checkout removed, but the branch was kept: {branch_error}")
        return switched

    def _refresh_selected(
        self,
        node: HierarchyNode,
        *,
        exact: bool,
    ) -> tuple[HierarchyNode, Hierarchy]:
        refreshed = Repository.discover(self.repo.root, self.runner)
        worktree = refreshed.managed_worktree(node.worktree.path)
        if worktree is None:
            raise RuntimeError("selected worktree no longer exists")
        if worktree.branch != node.worktree.branch:
            raise RuntimeError("selected worktree changed branches while the picker was open")
        hierarchy = refreshed.hierarchy()
        fresh_node = hierarchy.nodes[worktree.id]
        if exact and (
            worktree.head != node.worktree.head
            or worktree.locked != node.worktree.locked
            or fresh_node.source != node.source
            or fresh_node.direct_parent != node.direct_parent
        ):
            raise RuntimeError("selected worktree changed while awaiting confirmation; action aborted")
        self.repo = refreshed
        return fresh_node, hierarchy

    def _refresh_picker_node(
        self,
        node: PickerNode,
        *,
        exact: bool,
    ) -> tuple[PickerNode, Hierarchy]:
        if isinstance(node, HierarchyNode):
            return self._refresh_selected(node, exact=exact)
        refreshed = Repository.discover(self.repo.root, self.runner)
        hierarchy = refreshed.hierarchy()
        managed = refreshed.worktree_for_branch(node.branch)
        if managed is not None and managed.id in hierarchy.nodes:
            self.repo = refreshed
            return hierarchy.nodes[managed.id], hierarchy
        if not refreshed.branch_exists(node.branch):
            raise RuntimeError(f"branch no longer exists: {node.branch}")
        for _, candidate in render_hierarchy(
            refreshed, hierarchy, include_inactive=True
        ):
            if isinstance(candidate, VirtualBranchNode) and candidate.branch == node.branch:
                if exact and (
                    candidate.direct_parent != node.direct_parent
                    or candidate.source != node.source
                ):
                    raise RuntimeError("branch hierarchy changed while awaiting confirmation")
                self.repo = refreshed
                return candidate, hierarchy
        raise RuntimeError(f"branch is no longer part of the displayed worktree hierarchy: {node.branch}")

    def _descendants(self, hierarchy: Hierarchy, node_id: str) -> set[str]:
        descendants: set[str] = set()
        pending = list(hierarchy.children.get(node_id, []))
        while pending:
            child = pending.pop()
            if child in descendants:
                continue
            descendants.add(child)
            pending.extend(hierarchy.children.get(child, []))
        return descendants

    def _nearest_active_parent(
        self, hierarchy: Hierarchy, node: HierarchyNode
    ) -> Worktree:
        parent_id = node.parent_id
        while parent_id:
            parent = hierarchy.nodes[parent_id]
            worktree = parent.worktree
            if worktree.path.exists() and worktree.prunable is None:
                return worktree
            parent_id = parent.parent_id
        return self.repo.root_worktree

    def _confirm(self, prompt: str) -> bool:
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}

    def _pause(self, message: str) -> None:
        print(message)
        input("Press enter to continue...")


def preview(repo_root: Path, worktree_path: Path, runner: Runner | None = None) -> str:
    runner = runner or Runner()
    repo = Repository.discover(repo_root, runner)
    worktree = repo.managed_worktree(worktree_path)
    if worktree is None:
        return f"Unregistered worktree\n{worktree_path}"
    hierarchy = repo.hierarchy()
    node = hierarchy.nodes[worktree.id]
    lines = [
        worktree.label,
        str(worktree.path),
        "",
        f"Relationship: {node.source.value}",
        f"Direct parent: {node.direct_parent or '-'}",
    ]
    if node.skipped_parents:
        lines.append("Projected via: " + " -> ".join(node.skipped_parents))
    if worktree.locked is not None:
        lines.append(f"Locked: {worktree.locked or 'yes'}")
    if worktree.prunable is not None:
        lines.append(f"Prunable: {worktree.prunable or 'yes'}")
    if node.warnings:
        lines.extend(f"Warning: {warning}" for warning in node.warnings)
    lines.extend(["", "Git status", repo.status(worktree)])
    log = repo.git(["log", "-1", "--format=%h %s (%ar)"], check=False, cwd=worktree.path)
    if log.returncode == 0 and log.stdout.strip():
        lines.extend(["", "Latest commit", log.stdout.strip()])

    tmux = TmuxManager(runner)
    session = tmux.lookup_session(repo, worktree) if tmux.is_running() else None
    if session:
        lines.extend(["", f"Tmux session: {session.name}"])
        lines.extend(tmux.pane_commands(session.id))
    else:
        lines.extend(["", "Tmux session: not started"])
    return "\n".join(lines)


def preview_branch(repo_root: Path, branch: str, runner: Runner | None = None) -> str:
    runner = runner or Runner()
    repo = Repository.discover(repo_root, runner)
    if not repo.branch_exists(branch):
        return f"Branch no longer exists\n{branch}"
    info = repo.parent_for_virtual_branch(branch)
    existing = repo.worktree_for_branch(branch)
    external_path = (
        existing.path
        if existing is not None and repo.managed_worktree(existing.path) is None
        else None
    )
    lines = [
        branch,
        (
            f"Stale worktree registration: {external_path}"
            if external_path
            and existing
            and (existing.prunable is not None or not existing.path.exists())
            else f"External worktree: {external_path}"
            if external_path
            else "Branch only; no managed worktree"
        ),
        "",
        f"Relationship: {info.source.value}",
        f"Direct parent: {info.parent or '-'}",
        "",
        (
            "Inspect stale registrations with doctor before pruning them manually."
            if external_path
            and existing
            and (existing.prunable is not None or not existing.path.exists())
            else "External worktrees are display-only in this navigator."
            if external_path
            else "Press Enter to create its worktree and open a tmux session."
        ),
    ]
    if info.warning:
        lines.append(f"Warning: {info.warning}")
    log = repo.git(["log", "-1", "--format=%h %s (%ar)", branch], check=False)
    if log.returncode == 0 and log.stdout.strip():
        lines.extend(["", "Latest commit", log.stdout.strip()])
    return "\n".join(lines)


def doctor(repo: Repository, tmux: TmuxManager | None = None) -> list[str]:
    issues: list[str] = []
    hierarchy = repo.hierarchy()
    if repo.graphite.trunks() and repo.root_worktree.branch not in repo.graphite.trunks():
        issues.append(
            f"main checkout is {repo.root_worktree.branch}; Graphite trunk is {', '.join(repo.graphite.trunks())}"
        )
    if repo.managed_directory_is_internal:
        ignored = repo.git(["check-ignore", "--quiet", str(repo.worktrees_dir)], check=False)
        if ignored.returncode != 0:
            issues.append(f"managed directory is not ignored: {repo.worktrees_dir}")
    for node in hierarchy.nodes.values():
        worktree = node.worktree
        if worktree.detached:
            issues.append(f"detached worktree: {worktree.path}")
        if worktree.locked is not None:
            issues.append(f"locked worktree: {worktree.path}")
        if worktree.prunable is not None:
            issues.append(f"prunable worktree: {worktree.path}")
        elif not worktree.path.exists():
            issues.append(f"missing worktree path: {worktree.path}")
        if node.source == ParentSource.INFERRED and not worktree.is_root:
            issues.append(f"inferred parent for {worktree.label}: {node.direct_parent or 'none'}")
        issues.extend(f"{worktree.label}: {warning}" for warning in node.warnings)
    managed_ids = {item.id for item in repo.managed_worktrees}
    for worktree in repo.external_worktrees:
        if worktree.id not in managed_ids and (
            worktree.prunable is not None or not worktree.path.exists()
        ):
            identity = (
                f"branch {worktree.branch}"
                if worktree.branch
                else f"detached at {(worktree.head or 'unknown')[:12]}"
            )
            issues.append(f"missing external worktree: {worktree.path} ({identity})")
    by_branch: dict[str, list[Worktree]] = {}
    for worktree in repo.all_worktrees:
        if worktree.branch:
            by_branch.setdefault(worktree.branch, []).append(worktree)
    for branch, worktrees in by_branch.items():
        if len(worktrees) > 1:
            paths = ", ".join(str(item.path) for item in worktrees)
            issues.append(f"branch has multiple worktree registrations: {branch} ({paths})")
    tmux = tmux or TmuxManager(repo.runner)
    if tmux.is_running():
        sessions = tmux.sessions()
        by_path: dict[str, list[str]] = {}
        for session in sessions:
            if session.repo == str(repo.common_dir) and session.path:
                by_path.setdefault(session.path, []).append(session.name)
        for path, names in by_path.items():
            if len(names) > 1:
                issues.append(f"duplicate sessions for {path}: {', '.join(names)}")
        for session in tmux.orphaned_sessions(repo):
            issues.append(f"orphaned tmux session {session.name}: {session.path}")
    return issues
