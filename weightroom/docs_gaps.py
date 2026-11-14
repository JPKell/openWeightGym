"""A bounded control-node list of unanswered question text, with no caller or answer fields."""

import hashlib
import time
import unicodedata

from db import database, migrate
from db.schemas import DOCS_GAPS


class Gaps:
    """Keep only normalized question identity, question text, count and last-asked time."""

    def __init__(self, source):
        """Use the existing control-node accounts store and its normal backup/transfer lifecycle."""
        source = database(source)
        self.db = source.connect(check_same_thread=False)
        migrate(source, self.db, 'weightroom.docs_gaps', DOCS_GAPS)

    def record(self, question):
        """Deduplicate bounded normalized text atomically and retain the newest 200 questions."""
        question = ' '.join(unicodedata.normalize('NFKC', question).split())[:500]
        if not question:
            return
        identifier = hashlib.sha256(question.casefold().encode()).hexdigest()
        with self.db:
            self.db.execute('INSERT INTO docs_gaps (id,question,count,last_asked) VALUES (?,?,1,?) '
                            'ON CONFLICT(id) DO UPDATE SET count=docs_gaps.count+1,last_asked=excluded.last_asked',
                            (identifier, question, time.time()))
            self.db.execute('DELETE FROM docs_gaps WHERE id NOT IN '
                            '(SELECT id FROM docs_gaps ORDER BY last_asked DESC,id LIMIT 200)')

    def listing(self):
        """Read the bounded administrator list without joining accounts or request records."""
        return [dict(zip(('id', 'question', 'count', 'last_asked'), row)) for row in
                self.db.execute('SELECT id,question,count,last_asked FROM docs_gaps ORDER BY last_asked DESC,id').fetchall()]

    def dismiss(self, identifier):
        """Remove one question by its opaque normalized-text digest."""
        with self.db:
            self.db.execute('DELETE FROM docs_gaps WHERE id=?', (identifier,))
