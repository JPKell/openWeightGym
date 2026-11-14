"""Portable docs: current content, separate code references, and usable search/navigation."""

import runpy
import re
from pathlib import Path

import pytest

from weightroom import system_docs

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def preview_file(tmp_path_factory):
    """Build once from the same templates and Markdown as the live documentation."""
    output = tmp_path_factory.mktemp('docs-designs') / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-docs-designs.py'))['render']())
    return output


@pytest.fixture
def preview(preview_file):
    """Open an isolated preview, detecting JavaScript errors and unexpected network requests."""
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url) if request.url.startswith('http') else None)
        page.goto(preview_file.as_uri())
        yield page
        assert not errors
        assert not requests
        assert page.evaluate('localStorage.length') == 0
        browser.close()


def open_article(page, url):
    """Navigate through the preview's bookmarkable local routes."""
    page.evaluate('(url) => location.hash = "page=" + encodeURIComponent(url)', url)
    expect(page.locator('body')).to_have_attribute('data-view', 'article')
    expect(page.locator('#article')).to_be_visible()


def test_current_content_complete_and_code_references_separate(preview):
    page = preview
    records = page.evaluate('JSON.parse(document.getElementById("docs-data").textContent).pages')
    by_url = {record['url']: record for record in records}
    sources = {topic['source'] for section in system_docs.SECTIONS for topic in section['topics'] if topic['source']}
    assert {record['url'].removeprefix('/docs/code/') for record in records if record['section'] == 'code' and not record['overview']} == sources
    for section in system_docs.SECTIONS:
        assert section['url'] in by_url
        for topic in section['topics']:
            if topic['slug'] == 'source':
                assert topic['url'] not in by_url
            else:
                assert topic['url'] in by_url
                assert ('id="source"' in by_url[topic['url']]['html']) == (section['slug'] == 'code')
    open_article(page, '/docs/web-app/chat')
    expect(page.locator('#article')).to_contain_text('Shift+Enter adds a line')
    assert page.locator('#article #source').count() == 0
    open_article(page, '/docs/code/loadcoach')
    expect(page.locator('#article #source')).to_be_visible()
    expect(page.locator('#article nav[aria-label="Source modules"] a')).not_to_have_count(0)


def test_full_text_search_scoping_empty_and_escaping(preview):
    page = preview
    page.locator('#query').fill('recipients.txt')
    expect(page.locator('#results-list')).to_contain_text('Backups and restore')
    expect(page.locator('.result-excerpt mark').first).to_have_text('recipients.txt')
    page.locator('#search-scope').select_option('web-app')
    expect(page.locator('#search-empty')).to_be_visible()
    page.locator('#reset-search').click()
    page.locator('#query').fill('shift+enter')
    page.locator('#results-list a').first.click()
    expect(page.locator('#article')).to_contain_text('Shift+Enter adds a line')
    page.locator('#query').fill('<img src=x onerror=alert(1)>')
    expect(page.locator('#search-empty')).to_be_visible()
    expect(page.locator('#results-list img')).to_have_count(0)
    page.keyboard.press('Escape')
    expect(page.locator('#article')).to_be_visible()
    page.locator('#query').fill('LoadCoach')
    page.locator('#search-scope').select_option('code')
    assert page.locator('.result-category').count() > 0
    assert all(text.startswith('Code references') for text in page.locator('.result-category').all_text_contents())


def test_reader_contents_history_keyboard_and_code_alias(preview):
    page = preview
    page.locator('.home-shortcuts a[href="/docs/web-app/api-keys"]').click()
    expect(page.locator('#article .page-title')).to_have_text('API keys')
    page.locator('#outline-links a').last.click()
    expect(page).to_have_url(re.compile('anchor=api-connect'))
    expect(page.locator('#next')).to_be_hidden()
    page.go_back()
    expect(page).not_to_have_url(re.compile('anchor='))
    open_article(page, '/docs/api/source')
    expect(page.locator('#reader-category')).to_have_text('Code references')
    expect(page.locator('#article #source')).to_be_visible()
    page.locator('#content').focus()
    page.keyboard.press('/')
    expect(page.locator('#query')).to_be_focused()


def test_three_designs_light_dark_mobile_and_all_article_widths(preview, tmp_path):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            before = page.evaluate('getComputedStyle(document.body).color')
            page.locator('#theme').click()
            assert page.evaluate('getComputedStyle(document.body).color') != before
        for design in ('library', 'guided', 'reference'):
            page.locator(f'button[data-design={design}]').click()
            expect(page.locator('button[data-design][aria-pressed=true]')).to_have_count(1)
            for width in (1440, 1024, 768, 390, 320):
                page.set_viewport_size({'width': width, 'height': 1000})
                page.evaluate('location.hash = "home"')
                expect(page.locator('#home-content')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (design, theme, width, 'home')
                for url in ('/docs/web-app/chat', '/docs/api/openai', '/docs/data/backups', '/docs/code/loadcoach', '/docs/system'):
                    open_article(page, url)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (design, theme, width, url)
                if width == 320:
                    page.locator('#menu').click()
                    expect(page.locator('#library-rail')).to_be_visible()
                    if page.locator('#library-rail [data-section=code]').get_attribute('open') is None:
                        page.locator('#library-rail [data-section=code] summary').click()
                    page.locator('#library-rail a[href="/docs/code/voice"]').click()
                    expect(page.locator('#article .page-title')).to_have_text('Voice')
                    expect(page.locator('#library-rail')).to_be_hidden()
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.evaluate('location.hash = "home"')
            expect(page.locator('#home-content')).to_be_visible()
            page.wait_for_timeout(200)  # Let the shared pill transition finish before review screenshots.
            page.screenshot(path=str(tmp_path / f'{design}-{theme}.png'), full_page=True)
    ids = page.locator('[id]').evaluate_all('(nodes) => nodes.map(node => node.id)')
    assert len(ids) == len(set(ids))
