from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .model import Repository
from .process import CommandError, Runner
from .tmux import TmuxManager
from .ui import Picker, doctor, hierarchy_as_json, preview, preview_branch, render_hierarchy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tmux-worktrees",
        description="Navigate Git worktrees as a hierarchy of tmux sessions.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command")

    pick = subcommands.add_parser("pick", help="open the interactive worktree picker")
    pick.add_argument("--cwd", type=Path, default=Path.cwd())

    list_command = subcommands.add_parser("list", help="print the managed worktree hierarchy")
    list_command.add_argument("--cwd", type=Path, default=Path.cwd())
    list_command.add_argument("--json", action="store_true")
    list_command.add_argument(
        "--all", action="store_true", help="include inactive branches and external worktrees"
    )

    preview_command = subcommands.add_parser("preview", help="render details for the fzf preview pane")
    preview_command.add_argument("--repo", type=Path, required=True)
    preview_command.add_argument("--path", type=Path)
    preview_command.add_argument("--kind", choices=("worktree", "branch"))
    preview_command.add_argument("--target")

    doctor_command = subcommands.add_parser("doctor", help="report worktree and session problems")
    doctor_command.add_argument("--cwd", type=Path, default=Path.cwd())

    register = subcommands.add_parser(
        "register", help="import an open PR or create a draft PR for a branch"
    )
    register.add_argument("--cwd", type=Path, default=Path.cwd())
    register.add_argument("--branch")
    register.add_argument("--parent")
    register.add_argument(
        "--local",
        action="store_true",
        help="record the relationship without pushing or creating a PR",
    )

    unregister = subcommands.add_parser(
        "unregister", help="remove a branch from the persistent local registry"
    )
    unregister.add_argument("--cwd", type=Path, default=Path.cwd())
    unregister.add_argument("--branch")

    resume = subcommands.add_parser(
        "resume", help="switch to the last active worktree session for a repository"
    )
    resume.add_argument("--cwd", type=Path, default=Path.cwd())
    resume.add_argument("--root-session")

    migration = subcommands.add_parser(
        "migrate-sessions", help="move legacy worktree windows into dedicated sessions"
    )
    migration.add_argument("--cwd", type=Path, default=Path.cwd())
    migration.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "pick"
    runner = Runner()
    try:
        if command == "preview":
            if args.kind == "branch" and args.target:
                print(preview_branch(args.repo, args.target, runner))
            elif args.kind == "worktree" and args.target:
                print(preview(args.repo, Path(args.target), runner))
            elif args.path is not None:
                print(preview(args.repo, args.path, runner))
            else:
                raise RuntimeError("preview requires --path or --kind with --target")
            return 0

        repo = Repository.discover(getattr(args, "cwd", Path.cwd()), runner)
        if command == "pick":
            Picker(repo, runner=runner, executable=str(Path(sys.argv[0]).resolve())).run()
            return 0
        if command == "list":
            hierarchy = repo.hierarchy()
            if args.json:
                print(hierarchy_as_json(repo, hierarchy, include_inactive=args.all))
            else:
                for display, _ in render_hierarchy(
                    repo, hierarchy, include_inactive=args.all
                ):
                    print(display)
                if repo.external_worktrees and not args.all:
                    print(f"\n({len(repo.external_worktrees)} external worktree(s) hidden)")
                elif args.all:
                    detached_external = sum(
                        item.branch is None for item in repo.external_worktrees
                    )
                    if detached_external:
                        print(
                            f"\n({detached_external} detached external worktree(s) omitted)"
                        )
            return 0
        if command == "doctor":
            issues = doctor(repo)
            if issues:
                print("\n".join(f"- {issue}" for issue in issues))
                return 1
            print("No problems found.")
            return 0
        if command == "register":
            branch = args.branch or repo.git(["branch", "--show-current"]).stdout.strip()
            parent = args.parent or repo.trunk_branch
            if not branch or not parent:
                raise RuntimeError("register requires a branch and parent")
            if branch == repo.trunk_branch:
                raise RuntimeError("the trunk branch is already registered")
            if args.local:
                repo.register_local(branch, parent)
                print(f"Registered {branch} locally on top of {parent}")
                return 0
            pull_request = repo.github.register(branch, parent)
            print(f"Registered {branch} as PR #{pull_request.number}: {pull_request.url}")
            return 0
        if command == "unregister":
            branch = args.branch or repo.git(["branch", "--show-current"]).stdout.strip()
            if not branch:
                raise RuntimeError("unregister requires a branch")
            repo.unregister(branch)
            print(f"Unregistered {branch}; its branch, worktree, and session were kept")
            return 0
        if command == "resume":
            TmuxManager(runner).resume_project(repo, args.root_session)
            return 0
        if command == "migrate-sessions":
            return migrate_sessions(repo, apply=args.apply, runner=runner)
    except (RuntimeError, CommandError) as error:
        print(f"tmux-worktrees: {error}", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {command}")
    return 2


def migrate_sessions(repo: Repository, *, apply: bool, runner: Runner) -> int:
    tmux = TmuxManager(runner)
    if not tmux.is_running():
        print("No tmux server is running.")
        return 1
    plan, ambiguous = tmux.migration_plan(repo)
    if not plan:
        print("No worktree windows need migration.")
    else:
        print("Windows to move:")
        for item in plan:
            print(
                f"- {item.window.session_name}:{item.window.name} -> "
                f"{item.worktree.label} ({item.worktree.path})"
            )
    if ambiguous:
        print("\nWindows left untouched because their panes span multiple worktrees:")
        for window in ambiguous:
            print(f"- {window.session_name}:{window.name}")
    if not apply or not plan:
        if plan:
            print("\nDry run only. Re-run with --apply to move these windows.")
        return 0
    confirmation = input("Type migrate to move these windows: ").strip()
    if confirmation != "migrate":
        print("Migration cancelled.")
        return 1
    refreshed = Repository.discover(repo.root, runner)
    current_plan, current_ambiguous = tmux.migration_plan(refreshed)
    if current_ambiguous != ambiguous or current_plan != plan:
        print("Git or tmux state changed after the dry run. Migration aborted.")
        return 1
    tmux.apply_migration(refreshed, current_plan)
    print("Migration complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
