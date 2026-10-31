"""Concurrency and input-boundary regressions from the application sweep."""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from loadcoach import files, hub, statement
from db import Database
from db.connection import Connection
from loadcoach.groups import Group
from loadcoach.users import SignInRefused, Users
from toollocker.servers import LONGEST_LINE, Remote, Server, ToolProblem, catalogue
from toollocker import tools as tool_host
from toollocker.tools import files as file_tools
from turnstile.accounts import read_json
from turnstile.tasks import FrontTasks
from test_weightroom import desk, sign_in  # noqa: F401


@pytest.mark.parametrize('change', ['disable', 'password', 'replace'])
def test_sign_in_rechecks_account_after_password_hashing(monkeypatch, change):
    """Revocation or account replacement during hashing cannot issue an old login."""
    users = Users(':memory:', {'plain': Group()})
    users.add('pat', 'plain')
    users.change('pat', password='correct horse')

    async def scenario():
        """Mutate the account exactly where the password worker yields."""
        async def hash_in_flight(*args):
            """Simulate a valid hash finishing after an administrative edit."""
            if change == 'disable':
                users.change('pat', enabled=False)
            elif change == 'password':
                users.change('pat', password='replacement password')
            else:
                users.remove('pat')
                users.add('pat', 'plain')
                users.change('pat', password='correct horse')
            return True

        monkeypatch.setattr('loadcoach.users.asyncio.to_thread', hash_in_flight)
        with pytest.raises(SignInRefused):
            await users.sign_in('pat', 'correct horse')
        assert users.db.execute('SELECT COUNT(*) FROM keys').fetchone()[0] == 0

    try:
        asyncio.run(scenario())
    finally:
        users.db.close()


def test_overlapping_bad_password_checks_preserve_lockout(monkeypatch):
    """Pending password workers cannot erase a lock created by another attempt."""
    users = Users(':memory:', {'plain': Group()})
    users.add('pat', 'plain')
    users.change('pat', password='correct horse')

    async def scenario():
        """Release eight already admitted password checks together."""
        ready = asyncio.Event()
        admitted = 0

        async def delayed_hash(*args):
            """Hold password workers until every concurrent attempt has entered."""
            nonlocal admitted
            admitted += 1
            if admitted == 8:
                ready.set()
            await ready.wait()
            return False

        monkeypatch.setattr('loadcoach.users.asyncio.to_thread', delayed_hash)
        results = await asyncio.gather(*(users.sign_in('pat', 'wrong') for _ in range(8)), return_exceptions=True)
        assert [result.status for result in results].count(429) == 3
        with pytest.raises(SignInRefused) as refused:
            await users.sign_in('pat', 'correct horse')
        assert refused.value.status == 429

    try:
        asyncio.run(scenario())
    finally:
        users.db.close()


def test_cross_drive_copy_never_overwrites_a_competing_writer(tmp_path, monkeypatch):
    """Concurrent copies publish one whole file and preserve both original sources."""
    monkeypatch.setattr(files, 'same_drive', lambda *args: False)
    sources = [tmp_path / 'first.gguf', tmp_path / 'second.gguf']
    for source, content in zip(sources, (b'a' * 100_000, b'b' * 100_000)):
        source.write_bytes(content)
    target = tmp_path / 'result.gguf'
    with ThreadPoolExecutor(2) as workers:
        futures = [workers.submit(files.carry, source, target, {'done': 0}) for source in sources]
        failures = [future.exception() for future in futures]
    assert sum(isinstance(error, FileExistsError) for error in failures) == 1
    assert target.read_bytes() in [source.read_bytes() for source in sources]
    assert not list(tmp_path.glob('*.part'))


def test_key_replacement_ignores_predictable_temporary_symlinks(tmp_path):
    """Rotating a key cannot overwrite a file behind the old predictable temp name."""
    victim = tmp_path / 'untouched'
    victim.write_text('keep')
    (tmp_path / 'machine.key.new').symlink_to(victim)
    statement.replace_key(str(tmp_path / 'machine.key'), 'a' * 43)
    assert victim.read_text() == 'keep'
    assert (tmp_path / 'machine.key').stat().st_mode & 0o777 == 0o600


def test_evidence_retry_visits_the_durable_queue():
    """The retry wrapper calls the bounded queue rather than an undefined task variable."""
    calls = []

    async def publish():
        """Record the durable queue retry without a live model or database."""
        calls.append(True)

    asyncio.run(FrontTasks.retry_publication(SimpleNamespace(publish_pending=publish)))
    assert calls == [True]


def test_account_json_limit_applies_while_streaming():
    """Reject oversized account input before asking the transport for another chunk."""
    async def scenario():
        """Feed a chunked request with no Content-Length header."""
        count = 0

        async def receive():
            """Fail if the bounded reader continues past its first oversized chunk."""
            nonlocal count
            count += 1
            assert count == 1
            return {'type': 'http.request', 'body': b'x' * (64 * 1024 + 1), 'more_body': True}

        request = Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': []}, receive)
        assert (await read_json(request, {'name'})).status_code == 413

    asyncio.run(scenario())


@pytest.mark.parametrize('body', [b'\xff', b'a=%FF', b'a=1&' * 4097, b'a=' + b'x' * (256 * 1024)])
def test_sign_in_form_rejects_invalid_or_excessive_input(desk, body):
    """Public sign-in forms have finite field and body limits and valid UTF-8."""
    browser, _, _ = desk
    assert browser.post('/sign-in', content=body).status_code in (400, 413)


def test_private_pages_are_not_cacheable(desk):
    """Pages holding account data must opt out of browser and shared-cache storage."""
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    assert browser.get('/account').headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('reply', ['wrong-id', 'oversized', 'non-object'])
def test_remote_tool_replies_are_bounded_and_matched(reply):
    """Remote tool responses cannot cross request IDs or grow without a bound."""
    async def scenario():
        """Exercise actual httpx streaming against an in-memory remote tool server."""
        remote = Remote('test', 'http://tools.test', None, 'normal')
        await remote.client.aclose()

        def respond(request):
            """Return the selected malformed tool-server response."""
            number = json.loads(request.content)['id']
            if reply == 'oversized':
                return httpx.Response(200, content=b'x' * (LONGEST_LINE + 1))
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': number + (reply == 'wrong-id'),
                                             'result': ['bad'] if reply == 'non-object' else {}})

        remote.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(ToolProblem):
                await remote.ask('tools/list', {})
        finally:
            await remote.client.aclose()

    asyncio.run(scenario())


def test_stdio_write_failure_does_not_leak_a_pending_request(monkeypatch):
    """A broken stdin fails the call without leaving an unreachable future behind."""
    async def scenario():
        """Fail at the send boundary before any response can be awaited."""
        server = Server('test', [], 'normal')
        monkeypatch.setattr(server, 'dead', lambda: False)

        def fail(*args):
            """Simulate a child exiting immediately before its request is written."""
            raise BrokenPipeError()

        monkeypatch.setattr(server, 'tell', fail)
        with pytest.raises(ToolProblem):
            await server.ask('tools/list', {})
        assert server.waiting == {}

    asyncio.run(scenario())


def test_file_tool_bounds_requested_lines_and_newline_free_content(tmp_path, monkeypatch):
    """Tool output cannot allocate an arbitrarily large excerpt or one enormous line."""
    monkeypatch.setattr(file_tools, 'ROOTS', [tmp_path.resolve()])
    source = tmp_path / 'large.txt'
    source.write_text('x' * (file_tools.MAX_READ + 1))
    source.chmod(0o644)
    with pytest.raises(ValueError, match='line exceeds'):
        file_tools.read_file(str(source))
    with pytest.raises(ValueError, match='1–2000'):
        file_tools.read_file(str(source), lines=2_000_000)


def test_postgresql_literal_percent_survives_parameter_translation(tmp_path):
    """Wildcard key revocation and literal percentages remain valid psycopg SQL."""
    queries = pytest.importorskip('psycopg._queries')
    connection = Connection(Database.postgresql(tmp_path / 'unused-dsn'), None)
    sql = "SELECT hash FROM keys WHERE user = ? AND hash LIKE ? || '%' -- 100%"
    bound = connection.sql(sql, parameters=True)
    converted, *_ = queries._query2pg(bound.encode(), 'utf-8')
    assert converted == b'SELECT hash FROM keys WHERE "user" = $1 AND hash LIKE $2 || \'%\' -- 100%'
    assert connection.sql("SELECT '100%'") == "SELECT '100%'"
    native = connection.database.upsert_sql('items', ['id', 'value'], ['id'], ['value'])
    converted, formats, order, parts = queries._query2pg(connection.sql(native, parameters=True).encode(), 'utf-8')
    assert b'VALUES ($1, $2)' in converted
    assert connection.sql("SELECT %s, '%s', ? /* %t */", parameters=True) == "SELECT %s, '%%s', %s /* %%t */"


@pytest.mark.parametrize('page', [{'tools': [], 'nextCursor': 'again'}, {'tools': [42]},
                                 {'tools': [{'name': 'tool', 'inputSchema': {}}, {'name': 'tool', 'inputSchema': {}}]}])
def test_mcp_catalogue_rejects_infinite_or_malformed_pages(page):
    """Bad discovery data cannot loop forever or take down the other tool servers."""
    calls = []

    async def ask(*args):
        """Return one controlled malformed or repeating catalogue page."""
        calls.append(True)
        assert len(calls) <= 2
        return page

    with pytest.raises(ToolProblem):
        asyncio.run(catalogue(SimpleNamespace(name='test', ask=ask)))


@pytest.mark.parametrize('length', ['-1', str(16 * 2**20 + 1)])
def test_http_tool_host_rejects_unbounded_lengths_before_reading(tmp_path, monkeypatch, length):
    """A signed HTTP tool caller cannot request an unbounded socket body read."""
    key = tmp_path / 'tool.key'
    key.write_text('secret')
    statuses = []

    def server(address, handler):
        """Exercise the real nested HTTP handler without opening a network listener."""
        request = handler.__new__(handler)
        request.headers = {'authorization': 'Bearer secret', 'content-length': length}
        request.reply = lambda status, body=None: statuses.append(status)
        return SimpleNamespace(serve_forever=request.do_POST)

    monkeypatch.setattr(tool_host, 'HTTPServer', server)
    tool_host.serve_http('test', (), 0, str(key))
    assert statuses == [413]


def test_http_tool_host_rejects_empty_keys(tmp_path):
    """An empty credential file must fail startup rather than accept an empty bearer."""
    key = tmp_path / 'empty.key'
    key.write_text('')
    with pytest.raises(ValueError, match='nonempty key'):
        tool_host.serve_http('test', (), 0, str(key))


def test_failed_commit_rolls_back_the_connection():
    """A deferred constraint failure cannot leave a shared store in a broken transaction."""
    connection = Database.sqlite(':memory:').connect()
    try:
        connection.executescript('PRAGMA foreign_keys=ON; CREATE TABLE parents(id INTEGER PRIMARY KEY); '
            'CREATE TABLE children(parent INTEGER REFERENCES parents(id) DEFERRABLE INITIALLY DEFERRED);')
        with pytest.raises(sqlite3.IntegrityError), connection:
            connection.execute('INSERT INTO children VALUES (1)')
        assert not connection.in_transaction
        assert connection.execute('SELECT * FROM children').fetchall() == []
        with connection:
            connection.execute('INSERT INTO parents VALUES (1)')
            connection.execute('INSERT INTO children VALUES (1)')
    finally:
        connection.close()


def test_download_does_not_write_beyond_advertised_size(tmp_path):
    """An inconsistent remote response cannot keep writing until the drive fills."""
    target = tmp_path / 'model.gguf'

    async def scenario():
        """Return more bytes than the selected Hub entry allows."""
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'oversized'))) as client:
            with pytest.raises(OSError, match='exceeds the file size'):
                await hub.fetch(client, 'owner/model', {'path': 'model.gguf', 'size': 3, 'sha256': None}, target, '', {})
        assert not target.exists()
        assert target.with_suffix('.gguf.part').stat().st_size <= 3

    asyncio.run(scenario())
