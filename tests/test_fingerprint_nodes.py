"""A fingerprint kept on several nodes is several records; each link opens its own (QA-027)."""

import re
from urllib.parse import parse_qs, urlsplit

from test_fingerprints import archive  # noqa: F401
from test_weightroom import desk  # noqa: F401
from weightroom.fingerprint_views import fingerprint_context

SNAPSHOT = 'b67e95743a114a9ad970d5e12455a3fefced73df265ab5352966f3e0be1322c4'
ALIASED = '4e70cbd7af243d0252f2d05df1643c63c4de80796fe31b57cc981033b5fb8c3b'


def record(fingerprint, node, model, first):
    return {'fingerprint': fingerprint, 'node': node, 'model': model, 'first': first, 'files': {}, 'settings': {'flags': {'ctx-size': 4096}}}


RECORDS = [record(SNAPSHOT, node, 'qwen3-embedding-0.6b', 10 + index) for index, node in enumerate(('jpk1', 'jpk2', 'jpk3'))] + [
    record(ALIASED, 'jpk2', 'qa-browser-multinode', 5), record(ALIASED, 'jpk1', 'qa-browser-small', 4)]


def opened(url):
    return {key: values[-1] for key, values in parse_qs(urlsplit(url).query).items()}


def test_each_copys_link_opens_that_copy_and_no_other():
    listed = fingerprint_context(RECORDS, {'start': SNAPSHOT})
    assert listed['one'] is None and len(listed['found']) == 3  # without a node, the three copies are listed side by side
    for copy in listed['found']:
        params = opened(listed['record_url'](copy))
        assert params['node'] == copy['node']
        assert fingerprint_context(RECORDS, params)['one'] is copy


def test_a_preview_shows_the_copy_that_was_chosen_not_the_first_with_that_fingerprint():
    small = fingerprint_context(RECORDS, {'peek': ALIASED, 'peek_node': 'jpk1'})['selected']
    assert small['model'] == 'qa-browser-small'
    assert fingerprint_context(RECORDS, {'peek': ALIASED, 'peek_node': 'jpk2'})['selected']['model'] == 'qa-browser-multinode'
    assert fingerprint_context(RECORDS, {'peek': ALIASED, 'peek_node': 'nowhere'})['selected'] is None
    assert fingerprint_context(RECORDS, {'peek': ALIASED})['selected']['model'] == 'qa-browser-multinode'  # older links still open one


def test_a_unique_fingerprint_still_opens_from_a_link_without_a_node():
    alone = [record('a' * 64, 'jpk1', 'one', 1)]
    assert fingerprint_context(alone, {'start': 'a' * 12})['one'] is alone[0]


def test_rows_previews_and_records_carry_the_node(archive):
    server, _, _ = archive
    page = server.get('/fingerprints').text
    rows = re.findall(r'<tr data-record data-fingerprint="([0-9a-f]+)" data-node="([^"]*)"', page)
    assert rows and all(node for _, node in rows)
    fingerprint, node = rows[0]
    assert f'peek={fingerprint}&amp;peek_node={node}' in page and f'On {node}' in page
    peek = server.get('/fingerprints', params={'peek': fingerprint, 'peek_node': node}).text
    assert f'data-selected-node="{node}"' in peek and '>Node<' in peek
    detail = server.get('/fingerprints', params={'start': fingerprint, 'node': node}).text
    assert f'data-record-link="/fingerprints?start={fingerprint}&amp;node={node}"' in detail
