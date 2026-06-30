# AGENTS.md — SQLite Task Board · Opencode Operating Instructions

> You are a single execution agent. This document is your complete operating manual: schema, lifecycle, security constraints, logging format, and error handling. Read it in full before touching the task board.

**Schema Version:** 1  
**Protocol Version:** 3.0

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│ Startup                                                 │
│  • Verify database & schema version                     │
│  • Recover any interrupted tasks → requeue or dead-letter│
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│ Execution Loop                                          │
│  • Claim next pending task (priority → age order)       │
│  • Parse & validate JSON payload against action schema  │
│  • Enforce security policies                            │
│  • Execute approved action                              │
│  • Report result & emit structured log                  │
└──────────────────────────┬──────────────────────────────┘
                           │
           ┌───────────────┼──────────────┬──────────────┐
           ▼               ▼              ▼              ▼
      completed         pending      dead-lettered  [queue empty]
      (success)    (retriable err)  (max attempts)  → replenish
```

**State machine:**

```
pending → running → completed
                 ↘ pending       (retriable failure, attempt_count < max_attempts)
                 ↘ dead-lettered (final failure, attempt_count ≥ max_attempts)
```

---

## Quick Start

```bash
# 1. Configure
cp config.example.yaml config.yaml   # edit paths and security settings

# 2. Initialise the database
sqlite3 tasks.db < migrations/0001_initial.sql

# 3. Run
python agent.py

# 4. Dry-run (no state mutations)
AGENT_DRY_RUN=true python agent.py
```

---

## 1. Database Schema

### 1.1 Tasks Table

```sql
-- migrations/0001_initial.sql
CREATE TABLE IF NOT EXISTS tasks (

  -- Identity
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  instruction      TEXT NOT NULL,
  action_type      TEXT NOT NULL,
  idempotency_key  TEXT UNIQUE,
  checksum         TEXT,

  -- State
  status           TEXT NOT NULL DEFAULT 'pending',
  priority         TEXT          DEFAULT 'medium',

  -- Timing
  created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at       TIMESTAMP,
  completed_at     TIMESTAMP,
  failed_at        TIMESTAMP,
  updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  -- Retry tracking
  attempt_count    INTEGER DEFAULT 0,
  max_attempts     INTEGER DEFAULT 3,

  -- Structured error capture
  error_code       TEXT,
  error_message    TEXT,
  last_error       TEXT,

  CONSTRAINT status_valid CHECK (status IN (
    'pending', 'running', 'completed', 'failed', 'dead-lettered'
  )),
  CONSTRAINT priority_valid CHECK (priority IN (
    'critical', 'high', 'medium', 'low'
  ))
);

CREATE INDEX IF NOT EXISTS idx_tasks_status
  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority_status
  ON tasks(priority, status);
CREATE INDEX IF NOT EXISTS idx_tasks_idempotency
  ON tasks(idempotency_key);
```

### 1.2 Schema Migrations Table

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     INTEGER PRIMARY KEY,
  applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  description TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_migrations (version, description) VALUES
(1, 'Initial schema: single-agent task board with retry tracking and audit fields');
```

### 1.3 Field Reference

| Field | Type | Purpose |
|---|---|---|
| `id` | INTEGER | Unique task identifier |
| `instruction` | TEXT | Full JSON payload — validated before execution |
| `action_type` | TEXT | Extracted action name for fast filtering |
| `idempotency_key` | TEXT | Prevents duplicate execution of the same logical work unit |
| `checksum` | TEXT | SHA-256 of instruction for integrity verification |
| `status` | TEXT | `pending` · `running` · `completed` · `failed` · `dead-lettered` |
| `priority` | TEXT | `critical` → `high` → `medium` → `low` |
| `started_at` | TIMESTAMP | When the current execution attempt began |
| `completed_at` | TIMESTAMP | When the task successfully finished |
| `failed_at` | TIMESTAMP | When the task was dead-lettered |
| `updated_at` | TIMESTAMP | Last state mutation |
| `attempt_count` | INTEGER | Incremented on each claim |
| `max_attempts` | INTEGER | Tasks exceeding this threshold are dead-lettered |
| `error_code` | TEXT | Machine-readable error category (see §7) |
| `error_message` | TEXT | Human-readable error summary |
| `last_error` | TEXT | Full stack trace or raw system output |

---

## 2. Configuration

```yaml
# config.yaml
agent:
  workspace: "/var/agent/workspace"   # writable working directory
  database:  "/var/agent/tasks.db"    # SQLite database path

task_board:
  max_attempts_default:     3
  replenishment_batch_size: 10
  max_generated_per_day:    50

security:
  workspace_boundary: "/var/agent/workspace"
  readonly_paths:
    - "/var/agent/templates"
  network_allowlist:
    - "127.0.0.1"
    - "192.168.0.0/16"
    - "10.0.0.0/8"
  package_managers: ["pip", "npm", "uv"]
  internal_registries:
    - "https://your-internal-registry/pypi"

logging:
  level:  "INFO"    # INFO | DEBUG | WARNING
  format: "jsonl"
  output: "stdout"
```

```python
import yaml, os

def load_config() -> dict:
    path = os.getenv('AGENT_CONFIG', '/etc/agent/config.yaml')
    with open(path) as f:
        config = yaml.safe_load(f)
    assert config['agent']['workspace'], 'agent.workspace is required'
    assert config['agent']['database'],  'agent.database is required'
    return config
```

---

## 3. Startup

Run every check below before entering the execution loop. Stop and exit on any failure.

- [ ] Load and validate configuration
- [ ] Open SQLite connection at `agent.database`
- [ ] **Schema version check** — if version ≠ expected, exit with a critical error; do not auto-migrate
- [ ] Confirm `agent.workspace` exists and is writable
- [ ] Confirm all `security.readonly_paths` are accessible
- [ ] Confirm process is running as non-root
- [ ] Initialise JSONL logging to stdout
- [ ] **Run crash recovery** (§3.1)
- [ ] Emit `STARTUP` log event, then enter the execution loop

**Schema version check:**

```sql
SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1;
```

### 3.1 Crash Recovery

On startup, any task left in `running` status means the previous session was interrupted. Recover it before claiming new work.

```sql
BEGIN;

-- Requeue interrupted tasks that still have attempts remaining
UPDATE tasks
SET
  status        = 'pending',
  started_at    = NULL,
  updated_at    = CURRENT_TIMESTAMP
WHERE status        = 'running'
  AND attempt_count < max_attempts;

-- Dead-letter interrupted tasks with no attempts remaining
UPDATE tasks
SET
  status     = 'dead-lettered',
  error_code = 'INTERRUPTED_MAX_ATTEMPTS_EXCEEDED',
  failed_at  = CURRENT_TIMESTAMP,
  updated_at = CURRENT_TIMESTAMP
WHERE status        = 'running'
  AND attempt_count >= max_attempts;

COMMIT;
```

Emit a `RECOVERY_COMPLETE` log event with `requeued` and `dead_lettered` counts.

---

## 4. Execution Loop

### Step 1 — Claim a Task

```sql
BEGIN;

SELECT id, instruction, action_type, attempt_count, max_attempts
FROM tasks
WHERE status        = 'pending'
  AND attempt_count < max_attempts
ORDER BY
  CASE priority
    WHEN 'critical' THEN 0
    WHEN 'high'     THEN 1
    WHEN 'medium'   THEN 2
    WHEN 'low'      THEN 3
  END,
  created_at ASC,
  id ASC
LIMIT 1;

-- Claim it
UPDATE tasks
SET
  status        = 'running',
  started_at    = CURRENT_TIMESTAMP,
  updated_at    = CURRENT_TIMESTAMP,
  attempt_count = attempt_count + 1
WHERE id = :task_id;

COMMIT;
```

If the `SELECT` returns no rows, the queue is empty — trigger replenishment (§5), then loop back.

### Step 2 — Validate the Payload

```python
import json, jsonschema

# 1. Parse JSON
try:
    payload = json.loads(instruction_string)
except json.JSONDecodeError as e:
    fail_task(task_id, 'INVALID_JSON', str(e)[:256])
    return

# 2. Idempotency — skip if an identical task already completed
if key := payload.get('idempotency_key'):
    row = db.execute("""
        SELECT id FROM tasks
        WHERE idempotency_key = ? AND status = 'completed'
        LIMIT 1
    """, (key,)).fetchone()
    if row:
        mark_completed(task_id)
        log_event({'event': 'EXEC_IDEMPOTENT', 'task_id': task_id, 'key': key})
        return

# 3. Validate against action schema
action = payload.get('action')
try:
    jsonschema.validate(payload, get_schema(action))
except jsonschema.ValidationError as e:
    fail_task(task_id, 'SCHEMA_VALIDATION_FAILED', str(e)[:256])
    return
```

### Step 3 — Execute

Dispatch to the action handler. All security checks happen inside the handler before any side effects.

```python
def execute_task(task_id: int, payload: dict) -> None:
    action = payload['action']
    try:
        result = ACTION_REGISTRY[action](payload)
    except KeyError:
        result = {'success': False, 'error_code': 'UNKNOWN_ACTION',
                  'error_message': f'No handler registered for: {action}'}
    except Exception as e:
        result = {'success': False, 'error_code': 'UNKNOWN_EXCEPTION',
                  'error_message': str(e)[:256]}

    if result['success']:
        mark_completed(task_id)
    else:
        handle_failure(task_id, result['error_code'], result['error_message'])
```

### Step 4 — Report Result

**Success:**

```sql
UPDATE tasks
SET
  status       = 'completed',
  completed_at = CURRENT_TIMESTAMP,
  updated_at   = CURRENT_TIMESTAMP
WHERE id = :task_id AND status = 'running';
```

**Retriable failure** (`attempt_count < max_attempts`):

```sql
UPDATE tasks
SET
  status        = 'pending',
  started_at    = NULL,
  error_code    = :error_code,
  error_message = :error_message,
  updated_at    = CURRENT_TIMESTAMP
WHERE id = :task_id AND status = 'running';
```

**Final failure** (`attempt_count >= max_attempts`):

```sql
UPDATE tasks
SET
  status        = 'dead-lettered',
  failed_at     = CURRENT_TIMESTAMP,
  error_code    = :error_code,
  error_message = :error_message,
  updated_at    = CURRENT_TIMESTAMP
WHERE id = :task_id AND status = 'running';
```

**Failure helper:**

```python
def handle_failure(task_id: int, error_code: str, error_message: str) -> None:
    row = db.execute(
        "SELECT attempt_count, max_attempts FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    retriable  = row['attempt_count'] < row['max_attempts']
    new_status = 'pending' if retriable else 'dead-lettered'

    db.execute("""
        UPDATE tasks
        SET status        = ?,
            started_at    = CASE WHEN ? = 'pending' THEN NULL ELSE started_at END,
            failed_at     = CASE WHEN ? = 'dead-lettered' THEN CURRENT_TIMESTAMP ELSE failed_at END,
            error_code    = ?,
            error_message = ?,
            updated_at    = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'running'
    """, (new_status, new_status, new_status, error_code, error_message[:256], task_id))
    db.commit()

    log_event({'event': 'EXEC_ERROR', 'task_id': task_id, 'error_code': error_code,
               'retriable': retriable})
```

---

## 5. Queue Replenishment

When the queue is empty, inject new tasks before looping back.

```sql
BEGIN;

-- Confirm the queue is still empty before inserting
SELECT COUNT(*) FROM tasks WHERE status = 'pending';

-- If 0, insert the replenishment batch
INSERT INTO tasks (instruction, action_type, idempotency_key, status, priority, max_attempts)
VALUES
  ('{"action": "run_health_check", ...}', 'run_health_check', 'hc-001', 'pending', 'medium', 3);
  -- add rows up to replenishment_batch_size

COMMIT;
```

**Constraints:**
- Insert exactly `replenishment_batch_size` tasks (default: 10) per cycle.
- Do not exceed `max_generated_per_day` across all cycles.
- Vary action types — homogeneous batches risk exhausting a single resource class.

---

## 6. Action Schemas

Every payload must match its action's JSON Schema exactly. `additionalProperties: false` is required on all schemas. `idempotency_key` is required on all actions.

To add an action: define its schema below, implement a handler, register it in `ACTION_REGISTRY`.

### `verify_runtime`

```json
{
  "type": "object",
  "required": ["action", "language", "min_version", "idempotency_key"],
  "additionalProperties": false,
  "properties": {
    "action":          { "type": "string", "const": "verify_runtime" },
    "language":        { "type": "string", "enum": ["python", "node", "go", "rust", "java"], "maxLength": 32 },
    "min_version":     { "type": "string", "pattern": "^\\d+(\\.\\d+)?(\\.\\d+)?$", "maxLength": 16 },
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
    "action":          { "type": "string", "const": "create_directories" },
    "paths":           { "type": "array", "minItems": 1, "maxItems": 20,
                         "items": { "type": "string", "maxLength": 256 } },
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
    "action":            { "type": "string", "const": "setup_env_file" },
    "source_template":   { "type": "string", "maxLength": 256 },
    "target_file":       { "type": "string", "maxLength": 256 },
    "require_overwrite": { "type": "boolean", "default": false },
    "idempotency_key":   { "type": "string", "maxLength": 128, "pattern": "^[a-z0-9\\-_]+$" }
  }
}
```

### `install_dependencies`

```json
{
  "type": "object",
  "required": ["action", "manager", "requirements_file", "registry",
               "require_hashes", "timeout_seconds", "idempotency_key"],
  "additionalProperties": false,
  "properties": {
    "action":            { "type": "string", "const": "install_dependencies" },
    "manager":           { "type": "string", "enum": ["pip", "npm", "uv"], "maxLength": 32 },
    "requirements_file": { "type": "string", "maxLength": 256 },
    "registry":          { "type": "string", "maxLength": 512, "format": "uri" },
    "require_hashes":    { "type": "boolean" },
    "timeout_seconds":   { "type": "integer", "minimum": 30, "maximum": 600 },
    "idempotency_key":   { "type": "string", "maxLength": 128, "pattern": "^[a-z0-9\\-_]+$" }
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
    "action":          { "type": "string", "const": "run_health_check" },
    "endpoint":        { "type": "string", "maxLength": 512, "format": "uri" },
    "timeout_seconds": { "type": "integer", "minimum": 1, "maximum": 60 },
    "idempotency_key": { "type": "string", "maxLength": 128, "pattern": "^[a-z0-9\\-_]+$" }
  }
}
```

---

## 7. Security Model

All operations run under a zero-trust policy. A security violation fails the task immediately and emits a `SECURITY_ALERT` log event. There are no exceptions.

### 7.1 Workspace Boundary

All filesystem writes must stay inside `security.workspace_boundary`. Reads from `security.readonly_paths` are permitted.

```python
from pathlib import Path

WORKSPACE = Path(config['security']['workspace_boundary']).resolve()
READONLY  = [Path(p).resolve() for p in config['security']['readonly_paths']]

def is_path_safe(path_str: str) -> tuple[bool, Path | None]:
    try:
        target = Path(path_str).resolve()
        if target.is_relative_to(WORKSPACE):
            return True, target
        if any(target.is_relative_to(ro) for ro in READONLY):
            return True, target
        return False, target
    except (ValueError, RuntimeError):
        return False, None
```

### 7.2 Command Injection Prevention

Never use `shell=True`. Always pass arguments as a list.

```python
# ❌ Forbidden
subprocess.run(f"install {package}", shell=True)

# ✓ Required
subprocess.run(
    ["pip", "install", "--require-hashes", "-r", str(req_path)],
    cwd=str(WORKSPACE),
    env=minimal_env(),
    timeout=timeout_seconds,
    capture_output=True,
    shell=False,
    check=False,
)
```

Rules: list arguments only; executables from allowlist; no relative executable paths; no shell metacharacters in any argument.

### 7.3 Network Allowlist

Endpoints must be on the allowlist and use `http` or `https` only.

```python
from urllib.parse import urlparse
import ipaddress

ALLOWLIST = config['security']['network_allowlist']

def is_endpoint_safe(url: str) -> tuple[bool, str | None]:
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return False, f'Unsupported protocol: {parsed.scheme}'
    host = parsed.hostname
    for entry in ALLOWLIST:
        try:
            if '/' in entry:
                if ipaddress.ip_address(host) in ipaddress.ip_network(entry, strict=False):
                    return True, None
            elif host == entry:
                return True, None
        except (ValueError, TypeError):
            pass
    return False, f'Host not in allowlist: {host}'
```

### 7.4 Dependency Installation Controls

| Control | Requirement |
|---|---|
| Package managers | Allowlist only: `pip`, `npm`, `uv` |
| Registry | Internal only — no public PyPI or npm |
| Lockfiles | Required; no loose version specs |
| Hash verification | Enforced (`pip --require-hashes`) |
| Timeout | 30–600 s, set per task in schema |
| Process identity | Never run as root |
| Output capture | stdout/stderr truncated to 10 KB |

### 7.5 Least Privilege

```bash
useradd -r -s /bin/false -d /var/agent/workspace agent-runner
chown agent-runner:agent-runner /var/agent/workspace /var/agent/tasks.db
chmod 750 /var/agent/workspace
chmod 600 /var/agent/tasks.db
sudo -u agent-runner python agent.py
```

### 7.6 Secret Handling

Never log, transmit, or store secrets in plain text. Redact before any log emission.

```python
SENSITIVE = {'password', 'secret', 'token', 'key', 'credential', 'api_key'}

def redact(payload: dict) -> dict:
    return {
        k: '***REDACTED***' if any(s in k.lower() for s in SENSITIVE) else v
        for k, v in payload.items()
    }
```

### 7.7 Subprocess Hardening

```python
def safe_run(cmd: list[str], timeout: int = 30) -> dict:
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
           'HOME': str(WORKSPACE)}
    try:
        r = subprocess.run(
            cmd, cwd=str(WORKSPACE), env=env,
            timeout=timeout, capture_output=True,
            text=True, shell=False, check=False,
        )
        return {
            'stdout': r.stdout[:10_000],
            'stderr': r.stderr[:10_000],
            'returncode': r.returncode,
        }
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'Command timed out after {timeout}s')
```

---

## 8. Error Reference

```
INVALID_JSON                       instruction is not valid JSON
SCHEMA_VALIDATION_FAILED           payload fails action schema validation
UNKNOWN_ACTION                     action not in ACTION_REGISTRY
SECURITY_POLICY_VIOLATION          generic security boundary breach
PATH_OUTSIDE_WORKSPACE             write target outside workspace boundary
PATH_TRAVERSAL_ATTEMPT             detected ../ or similar traversal
UNAPPROVED_HOST                    network host not in allowlist
UNAPPROVED_PROTOCOL                protocol other than http/https
DEPENDENCY_INSTALL_BLOCKED         install_dependencies constraint violated
PROCESS_TIMEOUT                    subprocess exceeded timeout
PROCESS_EXIT_NONZERO               subprocess returned non-zero exit code
INTERRUPTED_MAX_ATTEMPTS_EXCEEDED  crash-recovered task with no retries left
MAX_ATTEMPTS_EXCEEDED              attempt_count reached max_attempts
FILE_NOT_FOUND                     referenced file does not exist
PERMISSION_DENIED                  insufficient filesystem permissions
UNKNOWN_EXCEPTION                  unhandled exception — check last_error
```

---

## 9. Structured Logging

Emit JSONL to stdout for every lifecycle event. Required fields on every line: `timestamp` (ISO-8601 UTC) and `event`. Include `task_id` on all task-scoped events.

```jsonl
{"timestamp":"2026-01-01T10:00:00Z","event":"STARTUP","version":"3.0"}
{"timestamp":"2026-01-01T10:00:01Z","event":"SCHEMA_CHECK_OK","schema_version":1}
{"timestamp":"2026-01-01T10:00:02Z","event":"RECOVERY_COMPLETE","requeued":1,"dead_lettered":0}
{"timestamp":"2026-01-01T10:00:03Z","event":"EXEC_START","task_id":1,"action":"verify_runtime","attempt":1}
{"timestamp":"2026-01-01T10:00:06Z","event":"EXEC_END","task_id":1,"status":"completed","duration_seconds":3}
{"timestamp":"2026-01-01T10:00:07Z","event":"EXEC_ERROR","task_id":2,"error_code":"SCHEMA_VALIDATION_FAILED","retriable":true}
{"timestamp":"2026-01-01T10:00:08Z","event":"SECURITY_ALERT","task_id":3,"violation":"PATH_OUTSIDE_WORKSPACE","path":"/etc/shadow"}
{"timestamp":"2026-01-01T10:00:09Z","event":"QUEUE_REPLENISH","injected":10}
{"timestamp":"2026-01-01T10:00:10Z","event":"SHUTDOWN_COMPLETE"}
```

```python
import json
from datetime import datetime, timezone

def log_event(fields: dict) -> None:
    print(json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(), **fields}),
          flush=True)
```

---

## 10. Graceful Shutdown

On `SIGTERM` or `SIGINT`, release any in-progress task back to `pending` so it can be retried cleanly on the next startup.

```python
import signal, sys

current_task_id: int | None = None

def shutdown_handler(sig, frame) -> None:
    log_event({'event': 'SHUTDOWN_REQUESTED', 'signal': sig})
    if current_task_id is not None:
        db.execute("""
            UPDATE tasks
            SET status     = 'pending',
                started_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'running'
        """, (current_task_id,))
        db.commit()
        log_event({'event': 'TASK_RELEASED', 'task_id': current_task_id})
    db.close()
    log_event({'event': 'SHUTDOWN_COMPLETE'})
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT,  shutdown_handler)
```

---

## 11. Retry Policy

- If `attempt_count < max_attempts`: requeue the task as `pending`. The next loop iteration will re-claim it in priority order.
- If `attempt_count >= max_attempts`: dead-letter the task. It will not be retried.
- There is no inter-retry backoff delay — the queue itself provides sequencing.
- `max_attempts` defaults to 3. Override per task in the `instruction` payload or at insert time.

---

## 12. Dry-Run Mode

```python
import os

DRY_RUN = os.getenv('AGENT_DRY_RUN', '').lower() == 'true'

if DRY_RUN:
    db.execute('BEGIN')
    run_lifecycle()        # reads and writes execute normally
    db.execute('ROLLBACK') # nothing persists
```

In dry-run mode, append `"dry_run": true` to every log event.

---

## 13. Observability (Optional)

Emit Prometheus-style metrics to a file or HTTP endpoint.

```
# TYPE agent_tasks_claimed_total counter
agent_tasks_claimed_total 42

# TYPE agent_tasks_completed_total counter
agent_tasks_completed_total 38

# TYPE agent_tasks_failed_total counter
agent_tasks_failed_total 3

# TYPE agent_task_duration_seconds histogram
agent_task_duration_seconds_bucket{le="1.0"} 15
agent_task_duration_seconds_bucket{le="5.0"} 38
agent_task_duration_seconds_bucket{le="+Inf"} 42
```

---

## 14. Schema Migrations

When the schema evolves:

1. Write `migrations/0002_description.sql`
2. Insert a version row inside the migration
3. Apply the migration to the database manually
4. Update the expected version constant in agent code
5. Restart the agent — it will refuse to start on a version mismatch

```sql
-- migrations/0002_add_tags.sql
BEGIN;

ALTER TABLE tasks ADD COLUMN tags TEXT DEFAULT NULL;

INSERT INTO schema_migrations (version, description)
VALUES (2, 'Add optional tags field for task categorisation');

COMMIT;
```

---

## 15. Adding a New Action

1. Define a JSON Schema in §6 with `additionalProperties: false` and a required `idempotency_key`.
2. Implement a handler returning `{'success': bool, 'error_code'?: str, 'error_message'?: str}`.
3. Add security checks (path, network, command) inside the handler before any side effects.
4. Register the handler in `ACTION_REGISTRY`.
5. Add test cases to §16.

```python
def my_action_impl(payload: dict) -> dict:
    # 1. Validate business-rule constraints
    # 2. Check security boundaries
    # 3. Execute
    return {'success': True}

ACTION_REGISTRY = {
    'verify_runtime':       verify_runtime_impl,
    'create_directories':   create_directories_impl,
    'setup_env_file':       setup_env_file_impl,
    'install_dependencies': install_dependencies_impl,
    'run_health_check':     run_health_check_impl,
    'my_action':            my_action_impl,       # ← register here
}
```

---

## 16. Testing Checklist

**Validation**
- [ ] Invalid JSON in `instruction` → `INVALID_JSON`, task `failed`
- [ ] Unknown action → `SCHEMA_VALIDATION_FAILED`, task `failed`
- [ ] Extra field in payload → `SCHEMA_VALIDATION_FAILED`
- [ ] Duplicate `idempotency_key` with `completed` status → task skipped as `completed`

**Security**
- [ ] Path traversal (`../../etc/passwd`) → `PATH_OUTSIDE_WORKSPACE`, `SECURITY_ALERT` emitted
- [ ] Absolute path outside workspace → `PATH_OUTSIDE_WORKSPACE`, task `failed`
- [ ] `ftp://` scheme → `UNAPPROVED_PROTOCOL`, task `failed`
- [ ] Host not in allowlist → `UNAPPROVED_HOST`, task `failed`

**Crash recovery**
- [ ] Task in `running` state on startup, attempts remaining → requeued to `pending`
- [ ] Task in `running` state on startup, no attempts remaining → `dead-lettered`
- [ ] `SIGTERM` mid-execution → task released to `pending`, clean shutdown

**Execution**
- [ ] Subprocess timeout → `PROCESS_TIMEOUT`, task `failed`
- [ ] Subprocess non-zero exit → `PROCESS_EXIT_NONZERO`, task `failed`
- [ ] Max attempts exhausted → task `dead-lettered`
- [ ] Dry-run mode → no state mutations, all logs include `"dry_run": true`

---

## Appendix: Example Task Payloads

```json
{ "action": "verify_runtime", "language": "python", "min_version": "3.11",
  "idempotency_key": "verify-python-311-v1" }
```

```json
{ "action": "create_directories",
  "paths": ["/var/agent/workspace/logs", "/var/agent/workspace/data"],
  "idempotency_key": "create-workspace-dirs-v1" }
```

```json
{ "action": "setup_env_file",
  "source_template": "/var/agent/templates/.env.example",
  "target_file": "/var/agent/workspace/.env",
  "require_overwrite": false,
  "idempotency_key": "setup-env-v1" }
```

```json
{ "action": "install_dependencies", "manager": "pip",
  "requirements_file": "/var/agent/workspace/requirements.lock",
  "registry": "https://your-internal-registry/pypi",
  "require_hashes": true, "timeout_seconds": 120,
  "idempotency_key": "install-deps-v1" }
```

```json
{ "action": "run_health_check",
  "endpoint": "http://127.0.0.1:8080/health",
  "timeout_seconds": 5,
  "idempotency_key": "health-check-v1" }
```
