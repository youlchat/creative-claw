from __future__ import annotations

from typing import Any

from .db import Database
from .ledger import Ledger
from .tools import ToolRegistry
from .util import json_dumps, json_loads, new_id, utc_now


class AgentRuntime:
    def __init__(self, database: Database, registry: ToolRegistry):
        self.database = database
        self.registry = registry
        self.ledger = Ledger(database)

    def create_task(self, project_id: str, goal: str, plan: list[dict[str, Any]]) -> dict[str, Any]:
        for index, step in enumerate(plan):
            if not isinstance(step, dict) or not step.get("tool"):
                raise ValueError(f"Plan step {index} must contain a tool name")
            step.setdefault("args", {})
            self.registry.validate_call(step["tool"], step["args"])
        task_id = new_id("task")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks(id, project_id, goal, status, plan_json, cursor, checkpoint_json, result_json, created_at, updated_at)
                VALUES (?, ?, ?, 'ready', ?, 0, '{}', '{}', ?, ?)
                """,
                (task_id, project_id, goal, json_dumps(plan), now, now),
            )
        self.ledger.append(project_id, "agent.task.created", {"task_id": task_id, "goal": goal, "plan": plan}, "agent")
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            runs = connection.execute("SELECT * FROM tool_runs WHERE task_id=? ORDER BY step_index", (task_id,)).fetchall()
        if not row:
            raise KeyError(f"Unknown task: {task_id}")
        return {
            **dict(row),
            "plan": json_loads(row["plan_json"], []),
            "checkpoint": json_loads(row["checkpoint_json"]),
            "result": json_loads(row["result_json"]),
            "tool_runs": [
                {
                    **dict(run),
                    "input": json_loads(run["input_json"]),
                    "output": json_loads(run["output_json"]) if run["output_json"] else None,
                }
                for run in runs
            ],
        }

    def step(self, task_id: str, *, approve: bool = False) -> dict[str, Any]:
        task = self.get_task(task_id)
        plan = task["plan"]
        cursor = int(task["cursor"])
        if task["status"] in {"completed", "failed", "rejected"}:
            return task
        if cursor >= len(plan):
            return self._complete(task)

        step = plan[cursor]
        tool = self.registry.get(step["tool"])
        args = dict(step.get("args") or {})
        now = utc_now()
        pending = next((run for run in task["tool_runs"] if run["step_index"] == cursor), None)

        if tool.risk != "read" and not approve:
            run_id = pending["id"] if pending else new_id("run")
            with self.database.connect() as connection:
                if not pending:
                    connection.execute(
                        """
                        INSERT INTO tool_runs(id, task_id, project_id, step_index, tool_name, risk, approval_status,
                                              input_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                        """,
                        (run_id, task_id, task["project_id"], cursor, tool.name, tool.risk, json_dumps(args), now),
                    )
                connection.execute(
                    "UPDATE tasks SET status='awaiting_approval', checkpoint_json=?, updated_at=? WHERE id=?",
                    (json_dumps({"step_index": cursor, "tool": tool.name, "risk": tool.risk, "args": args}), now, task_id),
                )
            return self.get_task(task_id)

        run_id = pending["id"] if pending else new_id("run")
        approval_status = "approved" if approve else "auto"
        with self.database.connect() as connection:
            if pending:
                connection.execute(
                    "UPDATE tool_runs SET approval_status=? WHERE id=?",
                    (approval_status, run_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO tool_runs(id, task_id, project_id, step_index, tool_name, risk, approval_status,
                                          input_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, task_id, task["project_id"], cursor, tool.name, tool.risk, approval_status, json_dumps(args), now),
                )
            connection.execute(
                "UPDATE tasks SET status='running', checkpoint_json='{}', updated_at=? WHERE id=?",
                (now, task_id),
            )

        try:
            output = tool.handler(task["project_id"], args)
        except Exception as exc:
            completed_at = utc_now()
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE tool_runs SET error_text=?, completed_at=? WHERE id=?",
                    (str(exc), completed_at, run_id),
                )
                connection.execute(
                    "UPDATE tasks SET status='failed', checkpoint_json=?, updated_at=? WHERE id=?",
                    (json_dumps({"step_index": cursor, "tool": tool.name, "error": str(exc)}), completed_at, task_id),
                )
            self.ledger.append(task["project_id"], "agent.tool.failed", {"task_id": task_id, "step_index": cursor, "tool": tool.name, "error": str(exc)}, "agent")
            return self.get_task(task_id)

        completed_at = utc_now()
        next_cursor = cursor + 1
        next_status = "completed" if next_cursor >= len(plan) else "ready"
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE tool_runs SET output_json=?, completed_at=? WHERE id=?",
                (json_dumps(output), completed_at, run_id),
            )
            connection.execute(
                "UPDATE tasks SET cursor=?, status=?, result_json=?, checkpoint_json='{}', updated_at=? WHERE id=?",
                (next_cursor, next_status, json_dumps(output), completed_at, task_id),
            )
        self.ledger.append(
            task["project_id"],
            "agent.tool.executed",
            {"task_id": task_id, "step_index": cursor, "tool": tool.name, "risk": tool.risk, "approval": approval_status},
            "agent",
        )
        return self.get_task(task_id)

    def run_until_blocked(self, task_id: str) -> dict[str, Any]:
        while True:
            task = self.get_task(task_id)
            if task["status"] in {"awaiting_approval", "completed", "failed", "rejected"}:
                return task
            previous_cursor = task["cursor"]
            task = self.step(task_id)
            if task["status"] in {"awaiting_approval", "completed", "failed", "rejected"} or task["cursor"] == previous_cursor:
                return task

    def reject(self, task_id: str, *, reason: str = "Rejected by user") -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != "awaiting_approval":
            raise ValueError("Only a task awaiting approval can be rejected")
        cursor = int(task["cursor"])
        run = next((item for item in task["tool_runs"] if item["step_index"] == cursor), None)
        if not run:
            raise RuntimeError("Approval checkpoint has no pending tool run")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE tool_runs SET approval_status='rejected', error_text=?, completed_at=? WHERE id=?",
                (reason, now, run["id"]),
            )
            connection.execute(
                "UPDATE tasks SET status='rejected', checkpoint_json=?, updated_at=? WHERE id=?",
                (json_dumps({"step_index": cursor, "tool": run["tool_name"], "reason": reason}), now, task_id),
            )
        self.ledger.append(
            task["project_id"],
            "agent.tool.rejected",
            {"task_id": task_id, "step_index": cursor, "tool": run["tool_name"], "reason": reason},
            "user",
        )
        return self.get_task(task_id)

    def _complete(self, task: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("UPDATE tasks SET status='completed', updated_at=? WHERE id=?", (now, task["id"]))
        return self.get_task(task["id"])
