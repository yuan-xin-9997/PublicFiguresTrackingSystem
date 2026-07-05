import logging
import threading
from typing import Any, Dict

from .database import Database
from .security import utc_now
from .services import run_collection_task


LOGGER = logging.getLogger("pfts.scheduler")


class Scheduler:
    def __init__(self, db: Database, config: Dict[str, Any]):
        self.db = db
        self.config = config
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="pfts-scheduler", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def _loop(self) -> None:
        poll = max(5, int(self.config["tasks"].get("poll_seconds", 30)))
        while not self.stop_event.wait(poll):
            due = self.db.fetch_all(
                "SELECT id FROM collection_tasks WHERE enabled=1 AND (next_run_at IS NULL OR next_run_at<=?) ORDER BY id LIMIT 1",
                (utc_now(),),
            )
            for task in due:
                try:
                    run_collection_task(self.db, int(task["id"]), self.config)
                except Exception:
                    LOGGER.exception("scheduled task failed")
