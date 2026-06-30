# sqlite-task-board

A single-agent SQLite task board with schema-validated payloads, structured crash recovery, and a zero-trust security model. Designed to be read by [Opencode](https://opencode.ai) as its operating manual via `AGENTS.md`.

[AGENTS.md](https://github.com/Jewelzufo/taskboard-agent/blob/main/AGENTS.md)

---

## What it is

A lightweight task queue where one agent works through a prioritised list of JSON instructions stored in a local SQLite database. Each task is validated against a strict JSON Schema, executed inside a security sandbox, and logged to stdout as JSONL. Failed tasks retry up to a configurable limit before being dead-lettered.

**Five task states:** `pending → running → completed / failed / dead-lettered`

**Five built-in actions:** `verify_runtime` · `create_directories` · `setup_env_file` · `install_dependencies` · `run_health_check`

---

## Quick start

```bash
cp config.example.yaml config.yaml      # set workspace, database, and security paths
sqlite3 tasks.db < migrations/0001_initial.sql
python agent.py

# Dry-run (reads normally, all writes rolled back)
AGENT_DRY_RUN=true python agent.py
```

---

## Repository layout

```
AGENTS.md                  # full operating manual — read by Opencode at session start
config.example.yaml        # annotated configuration template
agent.py                   # agent entry point
migrations/
  0001_initial.sql         # baseline schema
```

---

## Key behaviours

**Crash recovery.** On startup the agent scans for any `running` tasks left by a previous interrupted session. Tasks with attempts remaining are requeued; exhausted tasks are dead-lettered.

**Idempotency.** Every task carries an `idempotency_key`. If a completed task shares a key with an incoming one, the new task is marked complete immediately without re-executing.

**Security sandbox.** All filesystem writes are confined to `security.workspace_boundary`. Network endpoints are checked against an explicit allowlist. Subprocesses never run with `shell=True`. The agent refuses to start as root.

**Queue replenishment.** When the pending queue empties the agent injects a new batch (default: 10 tasks) before looping, up to a configurable daily cap.

**Structured logging.** Every lifecycle event emits a JSONL line to stdout with `timestamp` and `event` as minimum fields.

---

## Adding an action

1. Define a JSON Schema in `AGENTS.md §6` with `additionalProperties: false` and a required `idempotency_key`.
2. Implement a handler returning `{'success': bool, 'error_code'?: str, 'error_message'?: str}`.
3. Apply security checks (path, network, subprocess) before any side effects.
4. Register the handler in `ACTION_REGISTRY` in `agent.py`.

---

## Configuration reference

| Key | Default | Purpose |
|---|---|---|
| `agent.workspace` | — | Writable working directory |
| `agent.database` | — | SQLite database path |
| `task_board.max_attempts_default` | `3` | Retries before dead-lettering |
| `task_board.replenishment_batch_size` | `10` | Tasks injected per replenishment cycle |
| `task_board.max_generated_per_day` | `50` | Daily replenishment cap |
| `security.workspace_boundary` | — | Filesystem write boundary |
| `security.network_allowlist` | — | Permitted hosts/CIDRs |

See `config.example.yaml` and `AGENTS.md §2` for the full schema.

---

## Schema migrations

Add a file under `migrations/`, include a version row in `schema_migrations`, apply it manually, then update the expected version constant in `agent.py`. The agent hard-exits on a version mismatch — it never auto-migrates.

---

**Schema Version:** 1 · **Protocol Version:** 3.0 · **License:** MIT
