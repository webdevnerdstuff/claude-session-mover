# AGENTS.md

Guidance for AI agents and contributors working on this repository.

## What this is

`session-mover` is a Claude Code **mod**: a plugin of function hooks (`hooks/hooks.json` names one hooks module, `hooks/register.tsx`) plus a Python helper (`bin/session_mover.py`) that does the file work. It moves Claude desktop Code sessions between sidebar workspaces. The repository is also its own marketplace (`.claude-plugin/marketplace.json`, source `./`).

The mod API is early access. Its declarations are the authority: Claude Code lays them in `.claude-plugin/types/claude-code/index.d.ts` each time it loads the plugin from this folder (git-ignored). Grep that file for an event or noun before using it.

## Layout

| Path | What |
| --- | --- |
| `.claude-plugin/plugin.json` | Manifest; `version` is the release version. The name may not start with `claude-` (reserved), so the plugin is `session-mover` and the repo and marketplace keep `claude-session-mover` |
| `.claude-plugin/marketplace.json` | Makes the repo installable with `/plugin install session-mover --marketplace webdevnerdstuff/claude-session-mover` |
| `hooks/register.tsx` | The hooks module: `session.start` (registers `/move-sessions`, reports the last apply), `command.run`, and `ui.render` on the `Pane` |
| `bin/session_mover.py` | `list`, `apply -` (moves on stdin) and `last`; reads and appends to the app's LevelDB with no dependencies |
| `types/index.d.ts` | The plugin's contract: its `$.state` keys |
| `tests/pane.test.tsx` | `claude plugin test` suite for the pane |
| `tests/test_session_mover.py` | `unittest` suite for the helper, on temporary files |

## Commands

```bash
pnpm validate     # claude plugin validate .
pnpm test         # claude plugin test .
pnpm test:helper  # the helper's unittest suite
pnpm check        # all three
```

Run `pnpm check` before every commit. A type-check needs the types Claude Code lays when it loads the plugin: `claude --plugin-dir .` once, then `npx tsc -p .`.

## How a move works

The desktop app keeps a session's workspace in two places, and the sidebar reads the second:

1. The session's file, `~/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/local_<id>.json`: `cwd`, `originCwd`, `gitAnchors`, `envScopeId` and `startedFromEnvironmentId`.
2. The renderer's Local Storage (LevelDB, `Local Storage/leveldb`), key `persisted.ccd-started-environments`, `state.bySessionId[<session id>]` → environment preset id. **This is what the sidebar groups by.** A session missing from it appears to fall back to its file's `envScopeId`, then to a preset with the same folder (observed, not documented).

Workspaces are the presets in `ccd-session-presets.json` with a `local` target and a `cwd`.

Both are only safe to write while the app is quit (it holds LevelDB open and writes the session files back on quit). So `apply` detaches (double fork and `setsid`, so quitting Claude does not end it), quits the app by its bundle id, waits for `/Applications/Claude.app/Contents/MacOS/Claude` to exit, backs up, writes, and reopens the app. It writes `~/claude-session-backups/last-apply.json`, which the next `session.start` reports in a toast.

## Rules to keep

- **Never write while Claude runs.** `apply` refuses to change anything until the app has exited, and changes nothing if it does not quit within 2 minutes.
- **Back up before every write**: each session file, and the whole `leveldb/` folder, into the run's `mover-<time>/` folder.
- **The LevelDB write is one appended record** in the newest `.log`, a full write batch with a sequence number above every one in the database, checksummed (masked CRC32C) and fragmented across 32 KiB blocks as LevelDB's log format requires; it is read back after writing. Never rewrite or delete the app's files.
- **Only change entries the map already has.** A new key would change the value's size, which the app keeps beside it in the origin's `META` entry. A session not in the map is moved by its file alone.
- **Copy the conversation, never move it**: the session's `<cliSessionId>.jsonl` and folder from the old folder's project under `~/.claude/projects/` to the new one's (`/` and every other non-alphanumeric character become `-`), leaving the original.
- **The pane has no `Select` on mobile.** It draws a note there instead.

## Rules the mod loader enforces

These are refused at load, so `validate` catches them:

- **No `import()`**, no DOM and no Node in the module. Files and processes are reached through `$` (`$.process.run`, `$.fs.read`).
- **`$` is spelled `$.noun.method(...)` at the call site**: never store a noun in a variable.
- **An `atom` takes a literal reference** (`{ plugin: 'session-mover', key: 'k' }` written inline).
- **The type contract is self-contained**: `types/index.d.ts` has no `import`.
- **A render hook never writes state.** Write from a handler closure, `command.run` or `session.start`.

## Tests

- The pane goes through the engine: `$.ui.mount({ plugin: 'session-mover', surface, component: 'Pane', requestId: 'session-mover', props })`, with `process.run` answered by a hook standing for the helper (`engine()` in `tests/pane.test.tsx`).
- The helper is tested on temporary directories with a LevelDB log the tests write themselves. Never point a test at the real `~/Library/Application Support/Claude`.

## Style

- Comments say what the code holds or why, in plain sentences; no restating the code.
- No `else` after a `return`.
- Named exports; `register` is the hooks module's entry.
- Python: standard library only, tabs, runs on the `python3` macOS ships.

## Git

- **Never commit** without an explicit "commit this".
- Commit messages: short and imperative.

## Releases

- Bump `version` in `.claude-plugin/plugin.json` and `package.json` in the PR that changes behaviour: `claude plugin update` compares it.
- After merging: tag `vX.Y.Z` on `main`, push the tag, and create a GitHub Release with generated notes (`gh release create vX.Y.Z --generate-notes`). There is no CHANGELOG file; the Releases page is the history.
