"""
test_agent_contract.py

Contract tests for SQLite Task Board agent.py

Validates:
- Schema creation
- Task lifecycle (pending -> running -> completed)
- Priority ordering
- Security: workspace confinement and path traversal rejection
- Retry and dead-letter behavior
"""

import json
import sys
import tempfile
from pathlib import Path

# Ensure project root is importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from agent import Agent

def setup_agent(tmpdir):
    db_path = Path(tmpdir) / "test_tasks.db"
    agent = Agent(db_path)
    # Override workspace to temp location for isolation
    import agent as agent_module
    agent_module.WORKSPACE = Path(tmpdir) / "workspace"
    agent_module.WORKSPACE.mkdir(exist_ok=True)
    return agent

def test_schema_creates_tasks_table():
    with tempfile.TemporaryDirectory() as tmp:
        agent = setup_agent(tmp)
        cur = agent.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
        assert cur.fetchone() is not None, "tasks table should exist"

def test_priority_ordering():
    with tempfile.TemporaryDirectory() as tmp:
        agent = setup_agent(tmp)
        # Insert tasks in reverse priority
        for prio in ["low", "medium", "high", "critical"]:
            agent.conn.execute(
                "INSERT INTO tasks (type, payload, priority) VALUES (?,?,?)",
                ("health_check", json.dumps({}), prio)
            )
        agent.conn.commit()
        task = agent.fetch_next_task()
        assert task["priority"] == "critical", "critical should be fetched first"

def test_lifecycle_pending_to_completed():
    with tempfile.TemporaryDirectory() as tmp:
        agent = setup_agent(tmp)
        agent.conn.execute(
            "INSERT INTO tasks (type, payload, priority) VALUES (?,?,?)",
            ("health_check", json.dumps({}), "high")
        )
        agent.conn.commit()
        assert agent.run_once() is True
        status = agent.conn.execute("SELECT status FROM tasks").fetchone()[0]
        assert status == "completed"

def test_file_write_security():
    with tempfile.TemporaryDirectory() as tmp:
        agent = setup_agent(tmp)
        # Valid write
        agent.conn.execute(
            "INSERT INTO tasks (type, payload) VALUES (?,?)",
            ("file_write", json.dumps({"path": "safe.txt", "content": "ok"}))
        )
        # Path traversal attempt
        agent.conn.execute(
            "INSERT INTO tasks (type, payload) VALUES (?,?)",
            ("file_write", json.dumps({"path": "../escape.txt", "content": "bad"}))
        )
        agent.conn.commit()

        agent.run_once() # should succeed
        agent.run_once() # should fail and retry

        # Check results
        rows = list(agent.conn.execute("SELECT type, status, last_error FROM tasks ORDER BY id"))
        assert rows[0]["status"] == "completed"
        assert rows[1]["status"] in ("pending", "dead-lettered")
        assert "traversal" in (rows[1]["last_error"] or "").lower()

def test_retry_then_dead_letter():
    with tempfile.TemporaryDirectory() as tmp:
        agent = setup_agent(tmp)
        # Task that will always fail (unknown type)
        agent.conn.execute(
            "INSERT INTO tasks (type, payload, max_attempts) VALUES (?,?,?)",
            ("unknown_task", json.dumps({}), 2)
        )
        agent.conn.commit()

        agent.run_once() # attempt 1 -> pending
        agent.run_once() # attempt 2 -> dead-lettered

        status = agent.conn.execute("SELECT status, attempts FROM tasks").fetchone()
        assert status["status"] == "dead-lettered"
        assert status["attempts"] == 2

def test_check_passes():
    with tempfile.TemporaryDirectory() as tmp:
        agent = setup_agent(tmp)
        assert agent.check() is True

if __name__ == "__main__":
    # Simple runner without pytest
    tests = [
        test_schema_creates_tasks_table,
        test_priority_ordering,
        test_lifecycle_pending_to_completed,
        test_file_write_security,
        test_retry_then_dead_letter,
        test_check_passes,
    ]
    for test in tests:
        try:
            test()
            print(f"✓ {test.__name__}")
        except AssertionError as e:
            print(f"✗ {test.__name__}: {e}")
            sys.exit(1)
    print("All contract tests passed")
