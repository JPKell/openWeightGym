"""Views worth bookmarking come back from their address: a reload or a new tab opens the same filters, sort and page."""

from urllib.parse import parse_qs, urlsplit

from test_weightroom import desk, sign_in  # noqa: F401
from test_fingerprints import archive  # noqa: F401
from test_schedule_studio import studio  # noqa: F401
from test_console_browser import browser_context, playwright
from weightroom.fingerprint_views import directory_url

expect = playwright.expect


def asked(page):
    """The address's query, one value per name."""
    return {name: values[0] for name, values in parse_qs(urlsplit(page.url).query).items()}


def fresh(page):
    """The same address in a new tab, as a bookmark opens it."""
    other = page.context.new_page()
    other.goto(page.url)
    return other


def test_the_fingerprint_archive_keeps_its_rows_page_and_sort_and_a_record_returns_to_them(archive):
    server, _, records = archive
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/fingerprints')
        page.locator('#page-size').select_option('12')
        page.locator('#fingerprints-table th', has_text='Context').click()
        page.locator('#fingerprints-table th', has_text='Context').click()
        page.locator('[data-fingerprint-page-step="1"]').click()
        expect(page.locator('#page-summary')).to_contain_text('Showing 13–')
        assert asked(page) == {'sort': '5', 'page': '2'}  # Rows at 12 is how the page came, so it stays out
        page.locator('#page-size').select_option('all')
        assert asked(page)['rows'] == 'all' and 'page' not in asked(page)
        page.locator('#page-size').select_option('12')
        page.locator('[data-fingerprint-page-step="1"]').click()
        shown = page.locator('#fingerprint-rows [data-record]:not([hidden])').evaluate_all('rows => rows.map(row => row.dataset.fingerprint)')
        other = fresh(page)
        expect(other.locator('#page-summary')).to_contain_text('Showing 13–')
        expect(other.locator('#fingerprints-table th', has_text='Context')).to_have_attribute('data-mark', '▲')
        assert other.locator('#fingerprint-rows [data-record]:not([hidden])').evaluate_all('rows => rows.map(row => row.dataset.fingerprint)') == shown
        # A record opened from page two returns to page two, in the same order.
        with other.expect_navigation():
            other.locator('#record-peek a', has_text='Open complete record').click()
        back = other.locator('a', has_text='All fingerprints')
        assert {'page': '2', 'sort': '5'}.items() <= parse_qs_one(back.get_attribute('href')).items()
        with other.expect_navigation():
            back.click()
        expect(other.locator('#page-summary')).to_contain_text('Showing 13–')
        assert not errors
        browser.close()


def parse_qs_one(href):
    return {name: values[0] for name, values in parse_qs(urlsplit(href).query).items()}


def test_a_return_link_keeps_the_archive_view_and_nothing_else():
    target = directory_url('/fingerprints?start=ab&rows=all&page=3&sort=-2,5&return=/elsewhere&peek=x')
    assert parse_qs_one(target) == {'start': 'ab', 'rows': 'all', 'page': '3', 'sort': '-2,5', 'view': 'directory'}


def test_users_groups_and_cloud_accounts_keep_their_directory_filters(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    assert server.post('/cloud-apis', data={'do': 'add', 'name': 'qa-browser', 'url': 'https://provider.example/v1', 'ceiling': '25'}).status_code == 200
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        for path, kind in (('/users/jordan', 'user'), ('/groups/admin', 'group')):
            page.goto('http://console.test' + path)
            page.locator(f'#{kind}-search').fill('jor' if kind == 'user' else 'adm')
            choice = page.locator(f'#{kind}-filter option').nth(1).get_attribute('value')
            page.locator(f'#{kind}-filter').select_option(choice)
            assert asked(page) == {'find': 'jor' if kind == 'user' else 'adm', 'show': choice}
            other = fresh(page)
            expect(other.locator(f'#{kind}-search')).to_have_value('jor' if kind == 'user' else 'adm')
            expect(other.locator(f'#{kind}-filter')).to_have_value(choice)
            other.close()
        page.goto('http://console.test/cloud-apis')
        page.locator('#search').fill('qa-browser')
        page.locator('#status-filter').select_option('key')
        assert asked(page)['find'] == 'qa-browser' and asked(page)['state'] == 'key'
        other = fresh(page)
        expect(other.locator('#search')).to_have_value('qa-browser')
        expect(other.locator('#status-filter')).to_have_value('key')
        assert not errors
        browser.close()


def test_schedules_keep_their_directory_filters(studio):
    server, _, _ = studio
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/schedules')
        page.locator('#schedule-search').fill('Alpha')
        page.locator('#schedule-status-filter').select_option('active')
        assert asked(page)['find'] == 'Alpha' and asked(page)['status'] == 'active'
        other = fresh(page)
        expect(other.locator('#schedule-search')).to_have_value('Alpha')
        expect(other.locator('#schedule-count')).to_have_text('1 of 2 schedules')
        other.goto('http://console.test/schedules?status=no-such-status')
        expect(other.locator('#schedule-status-filter')).to_have_value('all')
        expect(other.locator('#schedule-count')).to_have_text('2 of 2 schedules')
        browser.close()
