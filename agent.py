#!/usr/bin/env python3
"""
SQLite Task Board Agent


Local-first autonomous execution agent driven by AGENTS.md.

Features:
- Reads tasks from SQLite tasks.db
- Lifecycle: pending -> running -> completed / pending (retry) / dead-lettered
- Priority order: critical > high > medium > low
- Security: writes confined to workspace/, shell=False, no path traversal
- Modes: --check, --once, continuous

Point Opencode at AGENTS.md to bootstrap this project.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
import yaml

# --- Constants ---
ROOT = Path(__file__).parent.resolve()
DB_PATH = ROOT / "tasks.db"
WORKSPACE = ROOT / "workspace"
CONFIG_PATH = ROOT / "config.yaml"

PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
ALLOWED_SHELL_CMDS = {"python", "python3", sys.executable}

DRY_RUN = os.getenv("AGENT_DRY_RUN", "").lower() in ("1", "true", "yes")

# --- DB Schema (matches migrations/0001_initial.sql) ---
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    priority TEXT NOT NULL DEFAULT 'medium',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, priority, created_at);
"""

class Agent:
    def __init__(self, db_path=DB_PATH):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def check(self):
        errors = []
        # 1. Workspace
        try:
            WORKSPACE.mkdir(exist_ok=True)
            test_file = WORKSPACE / ".write_test"
            test_file.write_text("ok")
            test_file.unlink()
        except Exception as e:
            errors.append(f"workspace not writable: {e}")

        # 2. DB
        try:
            cur = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
            if not cur.fetchone():
                errors.append("tasks table missing")
        except Exception as e:
            errors.append(f"db error: {e}")

        # 3. Config
        config = {}
        if CONFIG_PATH.exists():
            try:
                config = yaml.safe_load(CONFIG_PATH.read_text()) or {}
            except Exception as e:
                errors.append(f"config.yaml invalid: {e}")

        # 4. Pending tasks
        pending = self.conn.execute("SELECT COUNT(*) as c FROM tasks WHERE status='pending'").fetchone()["c"]

        if errors:
            print("CHECK FAILED")
            for e in errors:
                print(f" - {e}")
            return False
        else:
            print("CHECK PASSED")
            print(f" - workspace: {WORKSPACE}")
            print(f" - db: {self.db_path}")
            print(f" - pending tasks: {pending}")
            print(f" - dry_run: {DRY_RUN}")
            return True

    def fetch_next_task(self):
        sql = """
        SELECT * FROM tasks
        WHERE status='pending' AND attempts < max_attempts
        ORDER BY
          CASE priority
            WHEN 'critical' THEN 0
            WHEN 'high' THEN 1
            WHEN 'medium' THEN 2
            WHEN 'low' THEN 3
            ELSE 4
          END,
          created_at ASC
        LIMIT 1
        """
        return self.conn.execute(sql).fetchone()

    def update_task(self, task_id, **fields):
        if DRY_RUN:
            return
        fields["updated_at"] = datetime.utcnow().isoformat()
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [task_id]
        self.conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", vals)
        self.conn.commit()

    def run_once(self):
        task = self.fetch_next_task()
        if not task:
            print("No pending tasks")
            return False

        task_id = task["id"]
        print(f"[{task_id}] {task['type']} ({task['priority']}) attempt {task['attempts']+1}/{task['max_attempts']}")

        # mark running
        self.update_task(task_id, status="running", attempts=task["attempts"]+1)

        try:
            payload = json.loads(task["payload"])
            result = self.execute_task(task["type"], payload)
            self.update_task(task_id, status="completed", last_error=None)
            print(f"[{task_id}] completed: {result}")
            return True
        except Exception as e:
            err = str(e)
            attempts = task["attempts"] + 1
            if attempts >= task["max_attempts"]:
                self.update_task(task_id, status="dead-lettered", last_error=err)
                print(f"[{task_id}] dead-lettered: {err}")
            else:
                self.update_task(task_id, status="pending", last_error=err)
                print(f"[{task_id}] retryable failure: {err}")
            return False

    def execute_task(self, task_type, payload):
        if DRY_RUN:
            return f"DRY_RUN {task_type}"

        if task_type == "health_check":
            return "ok"

        if task_type == "ensure_workspace":
            WORKSPACE.mkdir(exist_ok=True)
            return str(WORKSPACE)

        if task_type == "file_write":
            rel_path = payload.get("path", "")
            content = payload.get("content", "")
            # Security: prevent traversal
            target = (WORKSPACE / rel_path).resolve()
            if not str(target).startswith(str(WORKSPACE.resolve())):
                raise ValueError("path traversal rejected")
            if ".." in Path(rel_path).parts:
                raise ValueError("unsafe path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"wrote {target.relative_to(ROOT)}"

        if task_type == "shell":
            cmd = payload.get("cmd", [])
            if not isinstance(cmd, list) or not cmd:
                raise ValueError("cmd must be non-empty list")
            exe = Path(cmd[0]).name
            if exe not in {Path(c).name for c in ALLOWED_SHELL_CMDS}:
                raise ValueError(f"command not allowed: {exe}")
            # Enforce shell=False
            proc = subprocess.run(
                cmd,
                cwd=WORKSPACE,
                capture_output=True,
                text=True,
                shell=False,
                timeout=payload.get("timeout", 30)
            )
            if proc.returncode!= 0:
                raise RuntimeError(proc.stderr[:500])
            return proc.stdout[:500]

        raise ValueError(f"unknown task type: {task_type}")

    def run_loop(self):
        print(f"Agent started. DRY_RUN={DRY_RUN}. Ctrl-C to stop.")
        while True:
            ran = self.run_once()
            if not ran:
                time.sleep(2)

def main():
    parser = argparse.ArgumentParser(description="SQLite Task Board Agent")
    parser.add_argument("--check", action="store_true", help="Validate configuration")
    parser.add_argument("--once", action="store_true", help="Run one task")
    parser.add_argument("--db", default=str(DB_PATH), help="Path to tasks.db")
    args = parser.parse_args()

    agent = Agent(args.db)

    if args.check:
        ok = agent.check()
        sys.exit(0 if ok else 1)
    elif args.once:
        agent.run_once()
    else:
        agent.run_loop()

if __name__ == "__main__":
    main()
