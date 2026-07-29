"""
Persistent Todo List Tool for EDUAgent.

Tasks are stored in `<workspace>/.eduagent/todo_list.json`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class TodoListTools:
    """Persistent todo list persisted in `work_dir / .eduagent / todo_list.json`."""

    def __init__(self, work_dir: str | Path):
        self.work_dir = Path(work_dir).expanduser().resolve()
        if not self.work_dir.exists():
            self.work_dir.mkdir(parents=True, exist_ok=True)
        # Store todo list in a .eduagent subdirectory inside the workspace
        self._data_dir = self.work_dir / ".eduagent"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._file_path = self._data_dir / "todo_list.json"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        """Load and parse .eduagent/todo_list.json; return empty dict if missing/corrupt."""
        if not self._file_path.exists():
            return {"tasks": []}
        try:
            data = json.loads(self._file_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "tasks" not in data:
                logger.warning("todo_list.json is malformed; reinitializing.")
                return {"tasks": []}
            return data
        except Exception:
            logger.warning("Failed to parse todo_list.json; reinitializing.")
            return {"tasks": []}

    def _save(self, data: Dict[str, Any]) -> None:
        """Persist data to .eduagent/todo_list.json."""
        self._file_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _next_id(data: Dict[str, Any]) -> int:
        """Return the next auto-incremented task id."""
        existing = [t.get("id", 0) for t in data.get("tasks", [])]
        return max(existing) + 1 if existing else 1

    @staticmethod
    def _now_iso() -> str:
        """Return current UTC timestamp in ISO format."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    # ------------------------------------------------------------------
    # Tool methods (each returns a string)
    # ------------------------------------------------------------------

    def add_task(
        self,
        title: str,
        description: str = "",
        priority: str = "medium",
        due_date: str = "",
    ) -> str:
        """Create a new task."""
        if not title.strip():
            return "Error: 'title' is required."

        priority = priority.lower().strip()
        if priority not in ("low", "medium", "high", "urgent"):
            priority = "medium"

        data = self._load()
        task = {
            "id": self._next_id(data),
            "title": title.strip(),
            "description": description,
            "priority": priority,
            "status": "pending",
            "created_at": self._now_iso(),
            "due_date": due_date.strip() if due_date else "",
        }
        data["tasks"].append(task)
        self._save(data)
        return f"✅ Task #{task['id']} created: '{task['title']}' [{task['priority']}]"

    def list_tasks(self, filter_status: str = "", filter_priority: str = "") -> str:
        """List tasks, optionally filtering by status and/or priority."""
        data = self._load()
        tasks: List[Dict[str, Any]] = data.get("tasks", [])

        # Apply filters
        fs = filter_status.lower().strip() if filter_status else ""
        fp = filter_priority.lower().strip() if filter_priority else ""
        if fs:
            tasks = [t for t in tasks if t.get("status", "").lower() == fs]
        if fp:
            tasks = [t for t in tasks if t.get("priority", "").lower() == fp]

        if not tasks:
            msg = "📋 Todo list is empty."
            if fs or fp:
                msg = f"📋 No tasks match filter(s): status='{fs}', priority='{fp}'."
            return msg

        # Build table
        header = f"{'ID':>4} | {'Status':<12} | {'Priority':<8} | {'Title':<30} | {'Due Date':<12}"
        sep = "-" * len(header)
        rows = [header, sep]
        for t in tasks:
            tid = t.get("id", "?")
            status = t.get("status", "?")
            priority = t.get("priority", "?")
            title = t.get("title", "")[:28]
            due = t.get("due_date", "") or "-"
            rows.append(f"{tid:>4} | {status:<12} | {priority:<8} | {title:<30} | {due:<12}")

        # Summary counts
        all_tasks = data.get("tasks", [])
        total = len(all_tasks)
        pending = sum(1 for t in all_tasks if t.get("status") == "pending")
        in_progress = sum(1 for t in all_tasks if t.get("status") == "in_progress")
        done = sum(1 for t in all_tasks if t.get("status") == "done")

        rows.append(sep)
        rows.append(f"Total: {total}  |  Pending: {pending}  |  In Progress: {in_progress}  |  Done: {done}")
        return "\
".join(rows)

    def _find_task(self, task_id: int):
        """Return (task_dict, data) or (None, data) if not found."""
        data = self._load()
        for t in data.get("tasks", []):
            if t.get("id") == task_id:
                return t, data
        return None, data

    def mark_done(self, task_id: int) -> str:
        """Set task status to 'done'."""
        task, data = self._find_task(task_id)
        if task is None:
            return f"Error: Task #{task_id} not found."
        task["status"] = "done"
        self._save(data)
        return f"✅ Task #{task_id} marked as done: '{task['title']}'"

    def mark_in_progress(self, task_id: int) -> str:
        """Set task status to 'in_progress'."""
        task, data = self._find_task(task_id)
        if task is None:
            return f"Error: Task #{task_id} not found."
        task["status"] = "in_progress"
        self._save(data)
        return f"🔄 Task #{task_id} marked as in progress: '{task['title']}'"

    def update_task(
        self,
        task_id: int,
        title: str = "",
        description: str = "",
        priority: str = "",
        due_date: str = "",
    ) -> str:
        """Update fields on an existing task. Empty fields are left unchanged."""
        task, data = self._find_task(task_id)
        if task is None:
            return f"Error: Task #{task_id} not found."

        updated_fields = []
        if title.strip():
            task["title"] = title.strip()
            updated_fields.append("title")
        if description:
            task["description"] = description
            updated_fields.append("description")
        if priority.strip():
            p = priority.lower().strip()
            if p in ("low", "medium", "high", "urgent"):
                task["priority"] = p
                updated_fields.append("priority")
        if due_date.strip():
            task["due_date"] = due_date.strip()
            updated_fields.append("due_date")

        if not updated_fields:
            return f"⚠️ No fields updated for task #{task_id}."

        self._save(data)
        return f"✅ Task #{task_id} updated ({', '.join(updated_fields)}): '{task['title']}'"

    def remove_task(self, task_id: int) -> str:
        """Delete a task by ID."""
        task, data = self._find_task(task_id)
        if task is None:
            return f"Error: Task #{task_id} not found."
        data["tasks"] = [t for t in data["tasks"] if t.get("id") != task_id]
        self._save(data)
        return f"🗑️ Task #{task_id} removed: '{task['title']}'"

    # ------------------------------------------------------------------
    # Tool map & definitions (same pattern as FileTools / ShellTool)
    # ------------------------------------------------------------------

    def get_tool_map(self) -> Dict[str, Callable[..., str]]:
        return {
            "add_task": self.add_task,
            "list_tasks": self.list_tasks,
            "mark_done": self.mark_done,
            "mark_in_progress": self.mark_in_progress,
            "update_task": self.update_task,
            "remove_task": self.remove_task,
        }

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "add_task",
                "description": "Add a new task to the persistent todo list.",
                "parameters": {
                    "title": "str (task title, required)",
                    "description": "str (optional longer description)",
                    "priority": "str (low | medium | high | urgent, default 'medium')",
                    "due_date": "str (optional YYYY-MM-DD due date)",
                },
            },
            {
                "name": "list_tasks",
                "description": "List all tasks in the todo list with optional status/priority filters.",
                "parameters": {
                    "filter_status": "str (optional: pending | in_progress | done)",
                    "filter_priority": "str (optional: low | medium | high | urgent)",
                },
            },
            {
                "name": "mark_done",
                "description": "Mark a task as done by its ID.",
                "parameters": {"task_id": "int (the ID of the task to mark done)"},
            },
            {
                "name": "mark_in_progress",
                "description": "Mark a task as in progress by its ID.",
                "parameters": {"task_id": "int (the ID of the task to mark in progress)"},
            },
            {
                "name": "update_task",
                "description": "Update fields of an existing task. Empty fields are left unchanged.",
                "parameters": {
                    "task_id": "int (the ID of the task to update)",
                    "title": "str (optional new title)",
                    "description": "str (optional new description)",
                    "priority": "str (optional: low | medium | high | urgent)",
                    "due_date": "str (optional YYYY-MM-DD)",
                },
            },
            {
                "name": "remove_task",
                "description": "Delete a task from the todo list by its ID.",
                "parameters": {"task_id": "int (the ID of the task to remove)"},
            },
        ]
