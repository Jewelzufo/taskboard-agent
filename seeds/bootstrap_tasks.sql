-- bootstrap_tasks.sql
-- SQLite Task Board - Initial Bootstrap Queue
-- These tasks verify the runtime after first migration

-- Clear any existing bootstrap tasks (idempotent re-seed)
DELETE FROM tasks WHERE type IN ('health_check', 'ensure_workspace', 'file_write') AND json_extract(payload, '$.bootstrap') = 1;

-- 1. Critical health check (startup)
INSERT INTO tasks (type, payload, priority, status, max_attempts)
VALUES ('health_check', '{"check":"startup","bootstrap":1}', 'critical', 'pending', 1);

-- 2. Ensure workspace directory exists
INSERT INTO tasks (type, payload, priority, status, max_attempts)
VALUES ('ensure_workspace', '{"bootstrap":1}', 'critical', 'pending', 3);

-- 3. Write bootstrap confirmation file (tests file_write security)
INSERT INTO tasks (type, payload, priority, status, max_attempts)
VALUES ('file_write', '{"path":"bootstrap/hello.txt","content":"SQLite Task Board bootstrapped successfully.\nWorkspace writes are confined and validated.\n","bootstrap":1}', 'high', 'pending', 2);

-- 4. Run a safe shell command (tests subprocess allowlist)
INSERT INTO tasks (type, payload, priority, status, max_attempts)
VALUES ('shell', '{"cmd":["python","-c","print(\"agent ok\")"],"bootstrap":1}', 'high', 'pending', 2);

-- 5. Final health check (post-bootstrap validation)
INSERT INTO tasks (type, payload, priority, status, max_attempts)
VALUES ('health_check', '{"check":"post_bootstrap","bootstrap":1}', 'medium', 'pending', 1);
