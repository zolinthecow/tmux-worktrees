# tmux-worktrees

A personal tmux navigator that presents Git worktrees as a logical branch tree. Each managed worktree gets its own tmux session, while Graphite or local Git metadata defines the displayed hierarchy.

## Requirements

- Python 3.11+
- Git
- tmux
- fzf
- Graphite CLI 1.8.4+ (`gt`) when using Graphite-managed stacks

## Usage

From inside a Git repository or managed worktree:

```sh
./tmux-worktrees pick
./tmux-worktrees list
./tmux-worktrees list --json
./tmux-worktrees doctor
```

The configured tmux binding is prefix + `w`.

The main checkout reuses the project session created by `tmux-sessionizer` when that session has the repository basename and at least one pane in the root checkout. Child worktrees continue to use dedicated sessions.

`tmux-sessionizer` resumes the last worktree selected through this picker for each repository. If that worktree has been removed, it falls back to the main checkout session.

### Picker keys

| Key | Action |
| --- | --- |
| `enter` | Find or create the selected worktree's tmux session and switch to it |
| `ctrl-a` | Create a child branch and worktree |
| `ctrl-t` | Track an ordinary branch with Graphite using its current logical parent |
| `ctrl-p` | Reparent an ordinary local branch |
| `ctrl-x` | Remove a clean checkout while keeping its branch |
| `alt-x` | Remove a clean checkout and safely delete its ordinary local branch |
| `ctrl-r` | Refresh Git, Graphite, and tmux state |
| `esc` | Close the popup |

Relationship badges are `G` for Graphite, `L` for explicit local metadata, `?` for inferred Git ancestry, `D` for detached, and `!` for unresolved provider state.

Branches in Graphite or the local hierarchy remain visible even when they have no managed checkout. These rows are marked `[G branch]` or `[L branch]`; pressing `Enter` creates the worktree and opens its tmux session.

Branches already checked out by an IDE or agent outside the managed directory are marked `[G external]` or `[L external]` and remain display-only.

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

1. Graphite metadata for tracked branches.
2. `branch.<name>.tmux-worktrees-parent` from local Git config.
3. The nearest managed branch found in Git ancestry.
4. The main checkout.

Graphite metadata is read from its local SQLite database in read-only mode for fast popup startup. The schema is checked first, and the public `gt` CLI is used as a compatibility fallback.

Branches without worktrees are omitted from the selectable list. Their relationships are preserved by connecting a worktree to its nearest ancestor that also has a managed worktree. The preview shows skipped branch names.

## Safety

- The main checkout cannot be removed.
- Dirty or locked worktrees cannot be removed.
- Ignored roots are shown before removal and their recursive contents are snapshotted because Git otherwise deletes them silently.
- Normal removal keeps the branch.
- Branch deletion never uses force and is blocked unless the branch tip is already reachable from its logical parent.
- Graphite reparenting and branch deletion remain explicit `gt move`/`gt delete` operations because Graphite cannot make their multi-worktree restacks atomic.
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
