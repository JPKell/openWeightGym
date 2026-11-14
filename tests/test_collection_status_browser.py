"""Collection status transitions preserve active inputs and retained answers."""

from pathlib import Path

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]
URL = 'http://collection.test/collections/review'


def serve_asset(route):
    from urllib.parse import urlsplit
    path = ROOT / 'weightroom' / urlsplit(route.request.url).path.lstrip('/')
    route.fulfill(content_type='text/css' if path.suffix == '.css' else 'text/javascript', path=path)


def collection_page(state, result=None):
    environment = Environment(loader=ChoiceLoader([
        DictLoader({'frame.html': '<!doctype html>{% block scripts %}{% endblock %}{% block main %}{% endblock %}'}),
        FileSystemLoader(ROOT / 'weightroom/pages'),
    ]), autoescape=True)
    collection = dict(id='review', title='Review sources', level='normal', model_label='embed',
                      model_id='embed-id', fingerprint='fp', build='build', dimensions=1024 if state == 'ready' else None,
                      documents=[dict(id='document', name='notes.txt', mime='text/plain', state=state,
                                      status='Indexed chunks: 1.' if state == 'ready' else 'Extracting and embedding.',
                                      chunks=1 if state == 'ready' else 0,
                                      diagnostics={'request':'123'} if state == 'failed' else {})])
    return environment.get_template('collections.html').render(
        collection=collection, collections=[], embedders=[], answer_models=[dict(id='chat')],
        levels=['normal'], form={}, said='', unavailable='', result=result)


def test_index_completion_preserves_selection_question_and_answer():
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page()
        visits = []

        def serve(route):
            if '/static/' in route.request.url:
                return serve_asset(route)
            visits.append(route.request.url)
            result = dict(generated=True, answer='Preserved prior answer.', sources=[]) if len(visits) == 1 else None
            route.fulfill(content_type='text/html', body=collection_page('indexing' if len(visits) == 1 else 'ready', result))

        page.route('http://collection.test/**', serve)
        page.goto(URL)
        assert page.locator('[data-document-index]').get_attribute('data-confirm-two') is not None
        page.get_by_label('Question', exact=True).fill('Keep this question')
        page.get_by_label('Answer model', exact=True).select_option('chat')
        page.get_by_label('Minimum cosine similarity').fill('.40')
        page.evaluate("""() => {
            document.activeElement.blur();
            getSelection().removeAllRanges();
            const range = document.createRange();
            range.selectNodeContents(document.querySelector('[data-document-id] a'));
            getSelection().addRange(range);
        }""")
        assert page.evaluate('getSelection().toString()') == 'notes.txt'
        with page.expect_response(URL):
            pass  # The indexing timer makes the next read without any owner refresh.
        playwright.expect(page.locator('[data-document-id]')).to_have_attribute('data-document-state', 'indexing')
        assert page.evaluate('getSelection().toString()') == 'notes.txt'
        page.evaluate('getSelection().removeAllRanges()')
        playwright.expect(page.locator('[data-document-id]')).to_have_attribute('data-document-state', 'ready', timeout=6000)
        playwright.expect(page.locator('[data-index-status]')).to_have_text('1 ready.')
        playwright.expect(page.locator('[data-document-chunks]')).to_have_text('1')
        playwright.expect(page.locator('[data-collection-dimensions]')).to_have_text('1024')
        playwright.expect(page.get_by_role('button', name='Reindex', exact=True)).to_be_visible()
        assert page.locator('[data-document-index]').get_attribute('data-confirm-two') is None
        playwright.expect(page.get_by_label('Question', exact=True)).to_have_value('Keep this question')
        playwright.expect(page.get_by_label('Answer model', exact=True)).to_have_value('chat')
        playwright.expect(page.get_by_label('Minimum cosine similarity')).to_have_value('.40')
        playwright.expect(page.locator('#collection-result')).to_contain_text('Preserved prior answer.')
        assert page.url == URL
        browser.close()


def test_failed_index_poll_retains_the_request_link_and_question():
    with playwright.sync_playwright() as engine:
        browser=engine.chromium.launch(headless=True,args=['--disable-gpu'])
        page=browser.new_page();visits=[]
        def serve(route):
            if '/static/' in route.request.url:
                return serve_asset(route)
            visits.append(route.request.url)
            route.fulfill(content_type='text/html',body=collection_page('indexing' if len(visits)==1 else 'failed'))
        page.route('http://collection.test/**',serve)
        page.goto(URL)
        page.get_by_label('Question',exact=True).fill('Keep this question')
        playwright.expect(page.locator('[data-document-status] a')).to_have_attribute('href','/requests/123',timeout=6000)
        playwright.expect(page.locator('[data-index-status]')).to_contain_text('1 failed')
        playwright.expect(page.get_by_label('Question',exact=True)).to_have_value('Keep this question')
        browser.close()


@pytest.mark.parametrize('failure', ['unavailable', 'sign-in'])
def test_status_failure_is_visible_and_recovers_without_navigating(failure):
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page()
        visits = []

        def serve(route):
            if '/static/' in route.request.url:
                return serve_asset(route)
            visits.append(route.request.url)
            if len(visits) == 2:
                return route.fulfill(status=503 if failure == 'unavailable' else 200,
                                     content_type='text/html', body='<form>Sign in</form>')
            route.fulfill(content_type='text/html', body=collection_page('indexing' if len(visits) == 1 else 'ready'))

        page.route('http://collection.test/**', serve)
        page.goto(URL)
        page.get_by_label('Question', exact=True).fill('Keep typing')
        playwright.expect(page.locator('[data-index-status]')).to_contain_text('temporarily unavailable', timeout=6000)
        playwright.expect(page.locator('[data-index-status]')).to_have_text('1 ready.', timeout=8000)
        playwright.expect(page.get_by_label('Question', exact=True)).to_have_value('Keep typing')
        assert page.url == URL
        browser.close()
