"""Which of a logical model's per-node copies may be used, kept on the front."""

import time

from db import database, migrate
from db.schemas import MODEL_LOCATIONS


class ModelLocations:
    """Persisted per-machine enablement of model placements."""
    def __init__(self, source):
        """Bind model-location enablement to the configuration store the front keeps."""
        self.source = database(source)
        self.db = self.source.connect(check_same_thread=False)
        self.former: dict[str, str] = {}  # old node name → its current name, until stored rows are renamed
        migrate(self.source, self.db, "weightroom.model_locations", MODEL_LOCATIONS)

    def enabled(self, machine: str, model_id: str | None) -> bool:
        """Return whether a model is enabled on the selected compute node."""
        if not model_id:
            return True
        names = (machine, *(old for old, new in self.former.items() if new == machine))
        row = self.db.execute(
            f"SELECT enabled FROM model_locations WHERE machine IN ({','.join('?' * len(names))}) AND model_id=? "
            "ORDER BY machine = ? DESC LIMIT 1",
            (*names, model_id, machine),
        ).fetchone()
        return row is None or bool(row[0])

    def set(self, machine: str, model_id: str, enabled: bool, user: str) -> None:
        """Persist the selected model location's enabled state."""
        with self.db:
            self.db.execute(
                "INSERT INTO model_locations(machine,model_id,enabled,changed,changed_by) VALUES(?,?,?,?,?) "
                "ON CONFLICT(machine,model_id) DO UPDATE SET enabled=excluded.enabled,changed=excluded.changed,changed_by=excluded.changed_by",
                (machine, model_id, int(enabled), time.time(), user),
            )

