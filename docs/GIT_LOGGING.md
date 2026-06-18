# Git activity → ooptdd logs (not email)

Log git activity (commit / merge / checkout / rebase-rewrite / push) as **ooptdd structured
events** instead of relying on email notifications, so it lands in your log store and feeds
the agent loop.

## Install (per machine — covers every repo)

```bash
python -m ooptdd_loop.git_hooks install      # or: <venv>/bin/python ooptdd_loop/git_hooks.py install
python -m ooptdd_loop.git_hooks status
```

`install` writes hooks to `$XDG_CONFIG_HOME/git/ooptdd-hooks` (default `~/.config/git/ooptdd-hooks`)
and points `git config --global core.hooksPath` at them — so **all repos on the machine** log
git activity. Each hook runs this file by absolute path with the interpreter you installed with
(so it must be one that can `import ooptdd` for the optional OpenObserve ship). It **never blocks
git**: every failure is swallowed and the hooks always `exit 0`.

Uninstall: `python -m ooptdd_loop.git_hooks uninstall` (unsets `core.hooksPath`, keeps files).

## Where events go

- **Always**: a durable local JSONL capture at `$XDG_STATE_HOME/ooptdd/git-events.jsonl`
  (default `~/.local/state/ooptdd/git-events.jsonl`). Zero infra.
- **Also, when `OOPTDD_OO_URL` is set**: shipped to OpenObserve via ooptdd's backend
  (stream `OOPTDD_OO_STREAM`, default `git`).

Each event is an ooptdd envelope — `event: git.<op>`, `cid`/`correlation_id`/`cycle_id` = the
repo identity (normalized remote, else toplevel name), plus `branch`, `commit`, `author`,
`subject`, `repo`, `host`, `_timestamp`. Because `cid` is the repo identity, a repo's events
correlate and are queryable like any other ooptdd trace.

```json
{"event":"git.commit","cid":"github.com/owner/repo","service":"git","level":"INFO",
 "branch":"main","commit":"…","author":"…","subject":"…","repo":"repo","_timestamp":…}
```

## Config (all optional, env)

| var | meaning | default |
|---|---|---|
| `OOPTDD_GIT_LOG` | local capture path | `$XDG_STATE_HOME/ooptdd/git-events.jsonl` |
| `OOPTDD_OO_URL` / `OOPTDD_OO_PASSWORD` / … | OpenObserve store (enables remote ship) | unset → local only |
| `OOPTDD_OO_STREAM` | OpenObserve stream for git events | `git` |
| `OOPTDD_GIT_SERVICE` | the event `service` field | `git` |
| `OOPTDD_GIT_HOOKS_DIR` | where `install` writes hooks | `$XDG_CONFIG_HOME/git/ooptdd-hooks` |
