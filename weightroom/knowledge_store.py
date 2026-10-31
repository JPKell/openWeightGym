"""Owner-private source documents and embedding vectors in the existing chat database."""

import json
import secrets
import time

from db import DatabaseSource, database, migrate
from db.schemas import CHAT

MAX_COLLECTIONS = 5
MAX_DOCUMENTS = 20
MAX_CHUNKS = 500


def identifier():
    """Create a random opaque identifier for a private record."""
    return secrets.token_hex(16)


class Collections:
    """Owner-scoped collection metadata, source documents and embedding vectors."""
    def __init__(self, path: DatabaseSource):
        """Open the private collection store and apply chat schema migrations."""
        source = database(path)
        self.db = source.connect(check_same_thread=False)
        migrate(source, self.db, 'weightroom.chat', CHAT)

    def one(self, owner, number):
        """Read collection metadata only when its owner matches the caller."""
        columns = ('id', 'title', 'level', 'model_id', 'fingerprint', 'build', 'model_label', 'dimensions', 'revision', 'created')
        row = self.db.execute('SELECT id,title,level,model_id,fingerprint,build,model_label,dimensions,revision,created '
                              'FROM collections WHERE id=? AND owner=?', (number, owner)).fetchone()
        return dict(zip(columns, row)) if row else None

    def listing(self, owner):
        """List the owner's collections in creation order."""
        return [self.one(owner, row[0]) for row in self.db.execute(
            'SELECT id FROM collections WHERE owner=? ORDER BY created DESC,id', (owner,)).fetchall()]

    def create(self, owner, title, level, model):
        """Create an owner-private collection within its count limit."""
        number = identifier()
        with self.db:
            if self.db.execute('SELECT COUNT(*) FROM collections WHERE owner=?', (owner,)).fetchone()[0] >= MAX_COLLECTIONS:
                raise ValueError('Keep at most five collections; delete one before creating another.')
            self.db.execute('INSERT INTO collections '
                '(id,owner,title,level,model_id,fingerprint,build,model_label,created) VALUES (?,?,?,?,?,?,?,?,?)',
                (number, owner, title, level, model['model_id'], model['fingerprint'], model['build'], model['id'], time.time()))
        return number

    def lock(self, owner, number):
        """Serialize source/index changes for a collection on either supported database engine."""
        return self.db.execute('UPDATE collections SET revision=revision WHERE id=? AND owner=?', (number, owner)).rowcount == 1

    def documents(self, owner, number):
        """List a collection's source and chunk metadata after checking ownership."""
        if not self.one(owner, number):
            return []
        columns = ('id', 'name', 'mime', 'digest', 'state', 'status', 'generation', 'created', 'chunks')
        return [dict(zip(columns, row)) for row in self.db.execute(
            'SELECT d.id,d.name,d.mime,d.digest,d.state,d.status,d.generation,d.created,'
            '(SELECT COUNT(*) FROM document_chunks c WHERE c.document=d.id) '
            'FROM documents d WHERE d.collection=? ORDER BY d.created,d.id', (number,)).fetchall()]

    def document(self, owner, number, doc):
        """Read an owner's source document including its original encoded bytes."""
        found = next((d for d in self.documents(owner, number) if d['id'] == doc), None)
        if found:
            found['original'] = self.db.execute('SELECT original FROM documents WHERE id=? AND collection=?', (doc, number)).fetchone()[0]
        return found

    def add(self, owner, number, name, mime, original, digest):
        """Add a pending source under the collection's write lock and document limit."""
        doc = identifier()
        with self.db:
            if not self.lock(owner, number):
                return None
            if self.db.execute('SELECT COUNT(*) FROM documents WHERE collection=?', (number,)).fetchone()[0] >= MAX_DOCUMENTS:
                raise ValueError('Keep at most twenty documents per collection.')
            self.db.execute('INSERT INTO documents (id,collection,name,mime,original,digest,state,status,created) '
                            'VALUES (?,?,?,?,?,?,?,?,?)',
                            (doc, number, name, mime, original, digest, 'pending', 'Ready to index.', time.time()))
        return doc

    def begin(self, owner, number, doc):
        """Claim a new indexing generation and discard the source's older chunks."""
        with self.db:
            if not self.lock(owner, number):
                return None
            row = self.db.execute('SELECT generation FROM documents WHERE id=? AND collection=?', (doc, number)).fetchone()
            if not row:
                return None
            token = row[0] + 1
            self.db.execute('DELETE FROM document_chunks WHERE document=?', (doc,))
            self.db.execute("UPDATE documents SET generation=?,state='indexing',status='Extracting and embedding.' WHERE id=?",
                            (token, doc))
        return token

    def finish(self, owner, number, doc, token, revision, chunks):
        """A deleted/replaced/cancelled source cannot be resurrected by an old async result."""
        dimensions = len(chunks[0]['vector'])
        with self.db:
            if not self.lock(owner, number):
                return False
            collection = self.one(owner, number)
            if collection['revision'] != revision or collection['dimensions'] not in (0, dimensions):
                return False
            changed = self.db.execute("UPDATE documents SET state='ready',status=? WHERE id=? AND collection=? AND generation=? AND state='indexing'",
                                      (f'Indexed chunks: {len(chunks)}.', doc, number, token)).rowcount
            if not changed:
                return False
            self.db.execute('UPDATE collections SET dimensions=? WHERE id=?', (dimensions, number))
            count = self.db.execute('SELECT COUNT(*) FROM document_chunks WHERE collection=?', (number,)).fetchone()[0]
            if count + len(chunks) > MAX_CHUNKS:
                raise ValueError('A collection may contain at most 500 indexed chunks. Split or shorten its sources.')
            for ordinal, chunk in enumerate(chunks):
                self.db.execute('INSERT INTO document_chunks (id,document,collection,ordinal,page,line,text,vector) VALUES (?,?,?,?,?,?,?,?)',
                                (identifier(), doc, number, ordinal, chunk['page'], chunk['line'], chunk['text'], json.dumps(chunk['vector'], allow_nan=False)))
        return True

    def stop(self, owner, number, doc, status, state='failed', token=None):
        """Invalidate indexing output and remove the selected source generation's chunks."""
        with self.db:
            if not self.lock(owner, number):
                return
            condition = ' AND generation=?' if token is not None else ''
            args = (state, status, doc, number, *((token,) if token is not None else ()))
            changed = self.db.execute('UPDATE documents SET generation=generation+1,state=?,status=? WHERE id=? AND collection=?'+condition, args).rowcount
            if changed:
                self.db.execute('DELETE FROM document_chunks WHERE document=?', (doc,))

    def reset(self, owner, number, model):
        """Replace the embedding identity and invalidate all previously indexed sources."""
        with self.db:
            changed = self.db.execute('UPDATE collections SET model_id=?,fingerprint=?,build=?,model_label=?,dimensions=0,revision=revision+1 '
                'WHERE id=? AND owner=?', (model['model_id'], model['fingerprint'], model['build'], model['id'], number, owner)).rowcount
            if changed:
                self.db.execute('DELETE FROM document_chunks WHERE collection=?', (number,))
                self.db.execute("UPDATE documents SET generation=generation+1,state='pending',status='Embedding space changed; index again.' WHERE collection=?", (number,))

    def remove(self, owner, number, doc=None):
        """Delete an owner's source or collection and its associated chunks together."""
        with self.db:
            if not self.lock(owner, number):
                return
            if doc:
                self.db.execute('DELETE FROM document_chunks WHERE document=? AND collection=?', (doc, number))
                self.db.execute('DELETE FROM documents WHERE id=? AND collection=?', (doc, number))
            else:
                self.db.execute('DELETE FROM document_chunks WHERE collection=?', (number,))
                self.db.execute('DELETE FROM documents WHERE collection=?', (number,))
                self.db.execute('DELETE FROM collections WHERE id=? AND owner=?', (number, owner))

    def chunks(self, owner, number):
        """Read normalized vectors and source locations for ready owner-private documents."""
        if not self.one(owner, number):
            return []
        columns = ('id', 'document', 'name', 'digest', 'generation', 'ordinal', 'page', 'line', 'text', 'vector')
        rows = self.db.execute("SELECT c.id,c.document,d.name,d.digest,d.generation,c.ordinal,c.page,c.line,c.text,c.vector "
                               "FROM document_chunks c JOIN documents d ON d.id=c.document WHERE c.collection=? AND d.state='ready' ORDER BY c.document,c.ordinal", (number,)).fetchall()
        return [{**dict(zip(columns, row[:-1])), 'vector': json.loads(row[-1])} for row in rows]

    def current(self, owner, number, revision, sources):
        """Check that a collection revision and every cited chunk still exist."""
        collection = self.one(owner, number)
        if not collection or collection['revision'] != revision:
            return False
        active = {chunk['id'] for chunk in self.chunks(owner, number)}
        return all(source['id'] in active for source in sources)

    def interrupted(self):
        """Invalidate unfinished indexing after restart without automatically replaying it."""
        with self.db:
            self.db.execute("UPDATE documents SET generation=generation+1,state='failed',status='WeightRoom stopped during indexing; retry explicitly.' WHERE state='indexing'")

    def forget(self, owner):
        """Remove every collection and source belonging to the deleted owner."""
        for collection in self.listing(owner):
            self.remove(owner, collection['id'])
