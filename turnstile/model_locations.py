"""Front-owned eligibility for a logical model's per-node copies."""

import time

from db import database, migrate
from db.schemas import MODEL_LOCATIONS


class ModelLocations:
    """Persisted per-machine enablement of model placements."""
    def __init__(self, source):
        """Bind model-location enablement to the front-owned configuration store."""
        self.source = database(source)
        self.db = self.source.connect(check_same_thread=False)
        migrate(self.source, self.db, "weightroom.model_locations", MODEL_LOCATIONS)

    def enabled(self, machine: str, model_id: str | None) -> bool:
        """Return whether a model is enabled on the selected compute node."""
        if not model_id:
            return True
        row = self.db.execute(
            "SELECT enabled FROM model_locations WHERE machine=? AND model_id=?",
            (machine, model_id),
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

