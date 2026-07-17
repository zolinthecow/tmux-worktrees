# tmux-worktrees

A personal tmux navigator that presents Git worktrees as a logical branch tree. Each managed worktree gets its own tmux session, while open GitHub PR base branches define registered stacks.

## Requirements

- Python 3.11+
- Git
- tmux
- fzf
- GitHub CLI (`gh`), authenticated for PR registration and stack discovery

## Usage

From inside a Git repository or managed worktree:

```sh
./tmux-worktrees pick
./tmux-worktrees list
./tmux-worktrees list --all
./tmux-worktrees list --json
./tmux-worktrees doctor
./tmux-worktrees register --branch feature --parent staging
./tmux-worktrees register --local --branch empty-child --parent feature
./tmux-worktrees unregister --branch feature
```

The configured tmux binding is prefix + `w`.

The main checkout reuses the project session created by `tmux-sessionizer` when that session has the repository basename and at least one pane in the root checkout. Child worktrees continue to use dedicated sessions.

`tmux-sessionizer` resumes the last worktree selected through this picker for each repository. If that worktree has been removed, it falls back to the main checkout session.

### Picker keys

| Key | Action |
| --- | --- |
| `enter` | Find or create the selected worktree's tmux session and switch to it |
| `ctrl-a` | Create a child branch and worktree |
| `ctrl-g` | Register a branch by importing its open PR or creating a draft PR |
| `ctrl-p` | Confirm or change a branch's logical parent |
| `ctrl-b` | Toggle inactive branches and external worktrees |
| `ctrl-x` | Deactivate a clean checkout while keeping its branch |
| `ctrl-d` | Deactivate a clean checkout and safely delete its ordinary local branch |
| `ctrl-r` | Refresh Git and tmux state |
| `esc` | Close the popup |

Relationship badges are `P` for an open GitHub PR, `L` for explicit local metadata, `U` for unregistered, and `D` for detached.

The default picker contains every active managed worktree plus active PR stacks. An active folder remains visible as `[U]` if an agent switches it to an unregistered branch, so its folder-anchored tmux session is never hidden. Open PRs matching GitHub's `involves:@me` search, PR branches checked out in managed worktrees, and explicitly registered branches act as seeds; the navigator follows base branches in both directions to include each complete stack. Activated relationships are cached locally and remain visible as `[L]` after their PRs merge or close. They are removed only with `unregister`; that command keeps the Git branch, worktree, and tmux session. Press `ctrl-b` to show inactive unregistered branches and external worktrees. Remote branches in an active stack are fetched only when opened.

Branches already checked out by an IDE or agent outside the managed directory are marked as external in the recovery view and remain display-only.

### Branch switches

When the picker detects that a managed worktree changed branches outside the navigator, it reconciles the session automatically. Session ownership is anchored to the worktree folder and generation, not to a permanent branch:

- Switching branches in place keeps the same folder, panes, processes, and session ID. The session is retagged and renamed for the newly checked-out branch.
- Previous branches become branch-only; no replacement worktree or session is created automatically.
- Selecting one of those branches later creates a new managed worktree and session for it.
- Every uniquely anchored session is checked on refresh, including inactive sessions whose branch was changed by a background agent.
- Ambiguous duplicate sessions are left untouched and reported by `doctor`.

Session reconciliation does not create or change branch-parent metadata.

## Storage

The main checkout and registered worktrees below `<repo>/.worktrees/` appear in the picker. Other registered worktrees, including IDE and agent-created worktrees, are hidden.

All managed worktrees are physical siblings:

```text
repo/
├── .git/
├── .worktrees/
│   ├── feature-a/
│   ├── feature-b/
│   └── feature-c/
└── ...
```

Configure another location per repository if needed:

```sh
git config tmux-worktrees.directory ../my-worktrees
```

For in-repository directories, the tool adds the path to `.git/info/exclude` if the repository does not already ignore it.

## Hierarchy

Parent resolution uses this precedence:

1. The base branch of an open GitHub PR in a registered stack.
2. `branch.<name>.tmux-worktrees-parent` from repository-local Git config.
3. The configured trunk for unregistered branches.

Configure the trunk with `git config tmux-worktrees.trunk <branch>`. A newly created branch can be registered before it has commits with `register --local`; it appears as `[L]` and no remote operation occurs. Branches created with `ctrl-a` are registered locally automatically. Later, `ctrl-g` or `register` promotes the relationship by importing an existing open PR or pushing the committed branch tip and creating a draft PR. Uncommitted worktree changes are not included in that push. Reparenting a registered PR updates its GitHub base branch.

Inactive leaf branches are omitted from the default selectable list. Branch-only nodes that connect active worktrees remain visible so stacks are not flattened. The `ctrl-b` recovery view includes every remaining inactive branch in its logical position.

## Safety

- The main checkout cannot be removed.
- Dirty or locked worktrees cannot be removed.
- Detached worktrees cannot be deactivated until their commit is placed on a branch.
- Ignored roots are shown before removal and their recursive contents are snapshotted because Git otherwise deletes them silently.
- Normal removal keeps the branch.
- Missing external registrations are reported by `doctor` and are never pruned automatically.
- Branch deletion never uses force and is blocked unless the branch tip is already reachable from its logical parent.
- Parent branches may remain checked out in other worktrees while descendants are rebased with ordinary Git commands.
- Tmux sessions are identified with canonical user options instead of names.
- Destructive session cleanup requires an exact repository, path, and branch tag match.
- Preview rendering never adopts or modifies tmux sessions.

## Migration

Inspect existing tmux windows that can be moved into one-session-per-worktree layout:

```sh
./tmux-worktrees migrate-sessions --cwd ~/src/wags/content-products
```

Apply the displayed plan:

```sh
./tmux-worktrees migrate-sessions --cwd ~/src/wags/content-products --apply
```

Migration leaves tagged sessions and windows containing panes from multiple repositories untouched.
Normal picker navigation never claims untagged legacy sessions; adoption only happens through this explicit migration flow.

## Development

```sh
python3 -m unittest -v
```

Tests create temporary Git repositories and use isolated `tmux -L` servers. They do not touch the default tmux server.
