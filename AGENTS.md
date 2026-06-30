# AGENTS.md - SQLite Task Board Opencode Autonomous Bootstrap Protocol

> You are a single Opencode execution agent. This file is the only user-facing instruction required to configure the project. When directed to this file, read it completely, create the full project scaffold, initialize the SQLite task board, seed the first task queue, and begin operating from the queue without asking the user for additional setup instructions.

**Schema Version:** 1  
**Protocol Version:** 3.1  
**Bootstrap Mode:** autonomous  
**Runtime Target:** local Python 3.11+ with SQLite

---

## 0. Prime Directive

The user should only need to point Opencode at this `AGENTS.md` file. From that point forward, you must autonomously configure the entire project scaffold.

You must:

1. Read this file in full before making changes.
2. Treat this file as the source of truth.
3. Create every required project file listed in the scaffold manifest.
4. Create `opencode.json` first so future Opencode sessions know to load this file.
5. Create configuration, database migration, runtime agent code, seed task data, tests, and documentation.
6. Initialize the SQLite database from the migration.
7. Insert the bootstrap task queue.
8. Run validation checks.
9. Continue execution from the SQLite task board.

You must not:

- Ask the user where files should go unless the filesystem is unavailable.
- Require manual editing before first run.
- Store secrets in files or logs.
- Write outside the repository root or configured workspace.
- Use network access unless an explicit queued task passes allowlist checks.
- Run commands with `shell=True`.
- Run as root unless the execution environment gives no alternative and the command is read-only or local scaffold creation only.

---

## 1. Repository Goal

This repository implements a local SQLite task board for one autonomous execution agent. The task board stores JSON instructions in a `tasks` table. The agent claims pending tasks in priority order, validates every payload against strict schemas, applies security checks, executes approved actions, logs structured JSONL events, retries failures, and dead-letters exhausted tasks.

The project is intentionally small. SQLite is the source of task state. `AGENTS.md` is the source of operating instructions. `agent.py` is the runtime entry point.

---

## 2. Required Final Project Structure

Create this structure exactly unless the user has already provided equivalent files. Existing matching files may be preserved if they satisfy this protocol.

```text
sqlite-task-board/
├── AGENTS.md
├── README.md
├── opencode.json
├── agent.py
├── config.example.yaml
├── config.yaml
├── requirements.txt
├── .gitignore
├── migrations/
│   └── 0001_initial.sql
├── seeds/
│   └── bootstrap_tasks.sql
├── tests/
│   └── test_agent_contract.py
└── workspace/
    └── .gitkeep
```

`workspace/` is the only default writable task workspace. It must be safe to delete and recreate.

---

## 3. Autonomous Bootstrap Sequence

Perform this sequence when Opencode is directed to this file.

### Step 1: Resolve repository root

Use the current working directory as the repository root. If `AGENTS.md` is in a subdirectory, use the directory containing `AGENTS.md` as the root.

Set these internal variables:

```text
REPO_ROOT=<directory containing AGENTS.md>
WORKSPACE=<REPO_ROOT>/workspace
DATABASE=<REPO_ROOT>/tasks.db
CONFIG=<REPO_ROOT>/config.yaml
```

### Step 2: Create `opencode.json` first

Create this file before all other scaffold files.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "instructions": ["AGENTS.md"],
  "permission": {
    "edit": "allow",
    "bash": "ask",
    "webfetch": "ask"
  },
  "formatter": {
    "python": "python -m py_compile"
  }
}
```

Purpose: this makes future Opencode sessions load `AGENTS.md` as the operating manual.

### Step 3: Create required directories

Create:

```text
migrations/
seeds/
tests/
workspace/
```

Add `workspace/.gitkeep` so the workspace directory exists in version control while remaining empty.

### Step 4: Create config files

Create `config.example.yaml` and `config.yaml` from the configuration template in Section 4. `config.yaml` must use repository-relative defaults and must not contain secrets.

### Step 5: Create migration and seed files

Create:

- `migrations/0001_initial.sql` from Section 5.
- `seeds/bootstrap_tasks.sql` from Section 6.

### Step 6: Create runtime files

Create:

- `agent.py` from Section 7.
- `requirements.txt` from Section 8.
- `tests/test_agent_contract.py` from Section 9.
- `.gitignore` from Section 10.
- `README.md` from Section 11 if missing or clearly outdated.

### Step 7: Initialize the database

Run these local commands from `REPO_ROOT` if SQLite is available:

```bash
sqlite3 tasks.db < migrations/0001_initial.sql
sqlite3 tasks.db < seeds/bootstrap_tasks.sql
```

If the `sqlite3` CLI is unavailable, initialize the database with Python's built-in `sqlite3` module using the same SQL files.

### Step 8: Validate scaffold

Run:

```bash
python -m py_compile agent.py
python agent.py --check
```

If `pytest` is available, also run:

```bash
python -m pytest tests
```

If `pytest` is not available, skip the pytest command and log `PYTEST_NOT_AVAILABLE`.

### Step 9: Start queued execution

After validation, run:

```bash
python agent.py --once
```

Then continue with normal task board operation. Do not bypass the task queue for ordinary work after bootstrap is complete.

---

## 4. Configuration Files

### `config.example.yaml`

```yaml
agent:
  workspace: "./workspace"
  database: "./tasks.db"
  schema_version: 1
  protocol_version: "3.1"

task_board:
  max_attempts_default: 3
  replenishment_batch_size: 10
  max_generated_per_day: 50
  claim_limit: 1

security:
  workspace_boundary: "./workspace"
  readonly_paths:
    - "."
  network_allowlist:
    - "127.0.0.1"
    - "localhost"
  package_managers:
    - "pip"
    - "uv"
    - "npm"
  internal_registries: []
  allow_public_registries: false

logging:
  level: "INFO"
  format: "jsonl"
  output: "stdout"
```

### `config.yaml`

Create `config.yaml` with the same values as `config.example.yaml`. These defaults are safe for local operation and require no secrets.

---

## 5. Database Migration

Create `migrations/0001_initial.sql` with this content.

```sql
BEGIN;

CREATE TABLE IF NOT EXISTS tasks (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  instruction      TEXT NOT NULL,
  action_type      TEXT NOT NULL,
  idempotency_key  TEXT UNIQUE,
  checksum         TEXT,
  status           TEXT NOT NULL DEFAULT 'pending',
  priority         TEXT DEFAULT 'medium',
  created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at       TIMESTAMP,
  completed_at     TIMESTAMP,
  failed_at        TIMESTAMP,
  updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  attempt_count    INTEGER DEFAULT 0,
  max_attempts     INTEGER DEFAULT 3,
  error_code       TEXT,
  error_message    TEXT,
  last_error       TEXT,
  CONSTRAINT status_valid CHECK (status IN ('pending', 'running', 'completed', 'failed', 'dead-lettered')),
  CONSTRAINT priority_valid CHECK (priority IN ('critical', 'high', 'medium', 'low'))
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version     INTEGER PRIMARY KEY,
  applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_generation_log (
  generation_date TEXT NOT NULL,
  generated_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (generation_date)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority_status ON tasks(priority, status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_idempotency ON tasks(idempotency_key);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES (1, 'Initial schema: autonomous SQLite task board with retry tracking and bootstrap support');

COMMIT;
```

---

## 6. Bootstrap Queue Seed

Create `seeds/bootstrap_tasks.sql` with this content.

```sql
BEGIN;

INSERT OR IGNORE INTO tasks (instruction, action_type, idempotency_key, status, priority, max_attempts)
VALUES
  (
    '{"action":"verify_runtime","language":"python","min_version":"3.11","idempotency_key":"bootstrap-verify-python-311"}',
    'verify_runtime',
    'bootstrap-verify-python-311',
    'pending',
    'critical',
    3
  ),
  (
    '{"action":"create_directories","paths":["./workspace/logs","./workspace/data","./workspace/tmp"],"idempotency_key":"bootstrap-create-workspace-dirs"}',
    'create_directories',
    'bootstrap-create-workspace-dirs',
    'pending',
    'high',
    3
  ),
  (
    '{"action":"run_health_check","endpoint":"http://127.0.0.1:0/health","timeout_seconds":1,"idempotency_key":"bootstrap-health-placeholder"}',
    'run_health_check',
    'bootstrap-health-placeholder',
    'pending',
    'low',
    1
  );

COMMIT;
```

The placeholder health check is allowed to fail cleanly because no local service is expected on port `0`. Its purpose is to verify failure handling and dead-letter behavior.

---

## 7. Runtime Agent Contract

Create `agent.py` as a self-contained Python runtime. It must implement the following behavior:

- Load YAML config from `AGENT_CONFIG` or `./config.yaml`.
- Refuse schema version mismatches.
- Recover interrupted `running` tasks at startup.
- Claim the next pending task by priority, age, and id.
- Validate JSON payloads with strict action schemas.
- Enforce workspace path boundaries.
- Enforce network allowlist checks.
- Execute only registered actions.
- Emit JSONL logs to stdout.
- Support `--check`, `--once`, and continuous default mode.
- Support dry-run via `AGENT_DRY_RUN=true`.

Minimum registered actions:

```text
verify_runtime
create_directories
setup_env_file
install_dependencies
run_health_check
```

Implementation requirements:

```text
- Use sqlite3 from the Python standard library.
- Use subprocess with shell=False only.
- Use urllib from the Python standard library for health checks.
- Prefer PyYAML for config parsing. If PyYAML is unavailable, fail with CONFIG_LOAD_FAILED and explain that pyyaml must be installed.
- Do not require network access during bootstrap.
- Do not write outside security.workspace_boundary except for project scaffold files created during Phase 0 bootstrap.
```

---

## 8. Python Requirements

Create `requirements.txt`.

```text
pyyaml>=6.0.0
jsonschema>=4.0.0
pytest>=8.0.0
```

Runtime should use `pyyaml` and `jsonschema` when available. If dependencies are missing, the agent must produce a clear error rather than silently proceeding with unsafe validation.

---

## 9. Test Contract

Create `tests/test_agent_contract.py` with tests that verify the static contract.

Required tests:

```text
- AGENTS.md exists.
- opencode.json exists and references AGENTS.md.
- config.example.yaml exists.
- config.yaml exists.
- migrations/0001_initial.sql exists and creates tasks and schema_migrations.
- seeds/bootstrap_tasks.sql exists and inserts bootstrap tasks.
- agent.py exists and contains ACTION_REGISTRY.
- requirements.txt exists.
```

The tests should not require external network access.

---

## 10. Git Ignore

Create `.gitignore`.

```gitignore
# Python
__pycache__/
*.py[cod]
.pytest_cache/
.venv/
venv/

# Local runtime
*.db
*.db-shm
*.db-wal
*.sqlite
*.sqlite3
logs/
workspace/*
!workspace/.gitkeep

# Local config and secrets
.env
.env.*
!.env.example

# OS/editor
.DS_Store
.idea/
.vscode/
```

`config.yaml` is intentionally not ignored because this project uses non-secret local defaults.

---

## 11. README Contract

Create or update `README.md` so it explains:

1. The project is a local SQLite task board for one Opencode execution agent.
2. The user only needs to direct Opencode to `AGENTS.md`.
3. The first autonomous action is creating `opencode.json`.
4. The agent creates all scaffold files and initializes `tasks.db`.
5. Normal execution happens through the SQLite queue.
6. Security controls restrict filesystem, subprocess, network, and secret handling.
7. Quick start commands are optional because the agent can bootstrap itself.

---

## 12. Database Schema Reference

### 12.1 Task states

```text
pending -> running -> completed
                 -> pending        retriable failure, attempt_count < max_attempts
                 -> dead-lettered  final failure, attempt_count >= max_attempts
```

### 12.2 Task priority order

```text
critical -> high -> medium -> low
```

### 12.3 Claim query

```sql
BEGIN;

SELECT id, instruction, action_type, attempt_count, max_attempts
FROM tasks
WHERE status = 'pending'
  AND attempt_count < max_attempts
ORDER BY
  CASE priority
    WHEN 'critical' THEN 0
    WHEN 'high' THEN 1
    WHEN 'medium' THEN 2
    WHEN 'low' THEN 3
  END,
  created_at ASC,
  id ASC
LIMIT 1;

UPDATE tasks
SET status = 'running',
    started_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP,
    attempt_count = attempt_count + 1
WHERE id = :task_id
  AND status = 'pending';

COMMIT;
```

---

## 13. Action Schemas

Every payload must match its action schema exactly. All schemas must set `additionalProperties: false`. Every action must require `idempotency_key`.

### `verify_runtime`

```json
{
  "type": "object",
  "required": ["action", "language", "min_version", "idempotency_key"],
  "additionalProperties": false,
  "properties": {
    "action": { "type": "string", "const": "verify_runtime" },
    "language": { "type": "string", "enum": ["python", "node", "go", "rust", "java"], "maxLength": 32 },
    "min_version": { "type": "string", "pattern": "^\\d+(\\.\\d+)?(\\.\\d+)?$", "maxLength": 16 },
    "idempotency_key": { "type": "string", "maxLength": 128, "pattern": "^[a-z0-9\\-_]+$" }
  }
}
```

### `create_directories`

```json
{
  "type": "object",
  "required": ["action", "paths", "idempotency_key"],
  "additionalProperties": false,
  "properties": {
    "action": { "type": "string", "const": "create_directories" },
    "paths": {
      "type": "array",
      "minItems": 1,
      "maxItems": 20,
      "items": { "type": "string", "maxLength": 256 }
    },
    "idempotency_key": { "type": "string", "maxLength": 128, "pattern": "^[a-z0-9\\-_]+$" }
  }
}
```

### `setup_env_file`

```json
{
  "type": "object",
  "required": ["action", "source_template", "target_file", "idempotency_key"],
  "additionalProperties": false,
  "properties": {
    "action": { "type": "string", "const": "setup_env_file" },
    "source_template": { "type": "string", "maxLength": 256 },
    "target_file": { "type": "string", "maxLength": 256 },
    "require_overwrite": { "type": "boolean", "default": false },
    "idempotency_key": { "type": "string", "maxLength": 128, "pattern": "^[a-z0-9\\-_]+$" }
  }
}
```

### `install_dependencies`

```json
{
  "type": "object",
  "required": ["action", "manager", "requirements_file", "registry", "require_hashes", "timeout_seconds", "idempotency_key"],
  "additionalProperties": false,
  "properties": {
    "action": { "type": "string", "const": "install_dependencies" },
    "manager": { "type": "string", "enum": ["pip", "npm", "uv"], "maxLength": 32 },
    "requirements_file": { "type": "string", "maxLength": 256 },
    "registry": { "type": "string", "maxLength": 512, "format": "uri" },
    "require_hashes": { "type": "boolean" },
    "timeout_seconds": { "type": "integer", "minimum": 30, "maximum": 600 },
    "idempotency_key": { "type": "string", "maxLength": 128, "pattern": "^[a-z0-9\\-_]+$" }
  }
}
```

### `run_health_check`

```json
{
  "type": "object",
  "required": ["action", "endpoint", "timeout_seconds", "idempotency_key"],
  "additionalProperties": false,
  "properties": {
    "action": { "type": "string", "const": "run_health_check" },
    "endpoint": { "type": "string", "maxLength": 512, "format": "uri" },
    "timeout_seconds": { "type": "integer", "minimum": 1, "maximum": 60 },
    "idempotency_key": { "type": "string", "maxLength": 128, "pattern": "^[a-z0-9\\-_]+$" }
  }
}
```

---

## 14. Security Model

All ordinary task execution uses zero-trust defaults.

### 14.1 Filesystem

- During Phase 0 bootstrap, scaffold writes may occur under `REPO_ROOT` only.
- After bootstrap, task writes must stay inside `security.workspace_boundary`.
- Path traversal such as `../` must be rejected.
- Absolute paths outside the workspace must be rejected.

### 14.2 Subprocesses

- Never use `shell=True`.
- Pass command arguments as lists.
- Use a minimal environment.
- Set timeouts for all subprocess calls.
- Capture and truncate stdout and stderr to 10 KB.

### 14.3 Network

- Only `http` and `https` are allowed.
- Hosts must match `security.network_allowlist`.
- Bootstrap must not require network access.

### 14.4 Secrets

- Never log secrets.
- Never create `.env` files containing real secrets.
- Redact keys containing `password`, `secret`, `token`, `credential`, `api_key`, or `key` before logging.

---

## 15. Structured Logging

Emit JSONL to stdout for all lifecycle events. Every event must include:

```text
timestamp
event
```

Task-scoped events must also include `task_id`.

Required events:

```text
BOOTSTRAP_START
BOOTSTRAP_FILE_WRITTEN
BOOTSTRAP_DATABASE_INITIALIZED
STARTUP
SCHEMA_CHECK_OK
RECOVERY_COMPLETE
EXEC_START
EXEC_END
EXEC_ERROR
SECURITY_ALERT
QUEUE_REPLENISH
SHUTDOWN_REQUESTED
TASK_RELEASED
SHUTDOWN_COMPLETE
```

Example:

```jsonl
{"timestamp":"2026-01-01T10:00:00Z","event":"BOOTSTRAP_START","protocol_version":"3.1"}
{"timestamp":"2026-01-01T10:00:01Z","event":"BOOTSTRAP_FILE_WRITTEN","path":"opencode.json"}
{"timestamp":"2026-01-01T10:00:02Z","event":"BOOTSTRAP_DATABASE_INITIALIZED","database":"./tasks.db"}
{"timestamp":"2026-01-01T10:00:03Z","event":"EXEC_START","task_id":1,"action":"verify_runtime","attempt":1}
```

---

## 16. Error Reference

```text
CONFIG_LOAD_FAILED                 configuration could not be loaded
SCHEMA_VERSION_MISMATCH            database schema does not match expected version
INVALID_JSON                       instruction is not valid JSON
SCHEMA_VALIDATION_FAILED           payload fails action schema validation
UNKNOWN_ACTION                     action not in ACTION_REGISTRY
SECURITY_POLICY_VIOLATION          generic security boundary breach
PATH_OUTSIDE_WORKSPACE             write target outside workspace boundary
PATH_TRAVERSAL_ATTEMPT             detected path traversal
UNAPPROVED_HOST                    network host not in allowlist
UNAPPROVED_PROTOCOL                protocol other than http or https
DEPENDENCY_INSTALL_BLOCKED         dependency install constraint violated
PROCESS_TIMEOUT                    subprocess exceeded timeout
PROCESS_EXIT_NONZERO               subprocess returned non-zero exit code
INTERRUPTED_MAX_ATTEMPTS_EXCEEDED  crash-recovered task with no retries left
MAX_ATTEMPTS_EXCEEDED              attempt_count reached max_attempts
FILE_NOT_FOUND                     referenced file does not exist
PERMISSION_DENIED                  insufficient filesystem permissions
UNKNOWN_EXCEPTION                  unhandled exception
PYTEST_NOT_AVAILABLE               pytest is not installed, skipped optional tests
```

---

## 17. Retry and Recovery

On startup, recover interrupted work before claiming new tasks.

```sql
BEGIN;

UPDATE tasks
SET status = 'pending',
    started_at = NULL,
    updated_at = CURRENT_TIMESTAMP
WHERE status = 'running'
  AND attempt_count < max_attempts;

UPDATE tasks
SET status = 'dead-lettered',
    error_code = 'INTERRUPTED_MAX_ATTEMPTS_EXCEEDED',
    failed_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE status = 'running'
  AND attempt_count >= max_attempts;

COMMIT;
```

Failure handling:

```text
attempt_count < max_attempts   -> pending
attempt_count >= max_attempts  -> dead-lettered
```

There is no mandatory inter-retry delay in v3.1.

---

## 18. Queue Replenishment

When no pending tasks remain, generate up to `task_board.replenishment_batch_size` new tasks without exceeding `task_board.max_generated_per_day`.

Replenishment tasks should be safe and local by default:

```text
verify_runtime
create_directories
run_health_check against allowlisted local endpoints only
```

Do not generate dependency installation tasks unless required lockfiles and approved registries are present.

---

## 19. Dry-Run Mode

If `AGENT_DRY_RUN=true`, execute the lifecycle inside a database transaction and roll back before exit. Append `"dry_run": true` to every log event.

Dry-run must still perform validation, security checks, and command planning. It must not persist task status changes.

---

## 20. Acceptance Criteria

The bootstrap is complete only when all of the following are true:

- `opencode.json` exists and references `AGENTS.md`.
- `config.example.yaml` exists.
- `config.yaml` exists with safe local defaults.
- `migrations/0001_initial.sql` exists.
- `seeds/bootstrap_tasks.sql` exists.
- `agent.py` exists.
- `requirements.txt` exists.