"""Permission-scoped documentation reading and full-text search over current rendered articles."""

import asyncio
from html.parser import HTMLParser

from markupsafe import Markup
from starlette.responses import RedirectResponse, Response

from weightroom import system_docs


class ArticleText(HTMLParser):
    """Read article text and section headings without treating documentation as executable markup."""

    def __init__(self):
        """Start a fresh readable-text and contents collection for one article."""
        super().__init__(convert_charrefs=True)
        self.text, self.contents = [], []
        self.section_id, self.heading, self.heading_id = '', None, ''

    def handle_starttag(self, tag, attrs):
        """Capture each shared docs-section heading and its stable anchor."""
        attrs = dict(attrs)
        if 'docs-section' in attrs.get('class', '').split():
            self.section_id = attrs.get('id', '')
        if tag == 'h2' and 'card-heading' in attrs.get('class', '').split():
            self.heading, self.heading_id = [], self.section_id

    def handle_data(self, value):
        """Keep code and prose searchable, with heading labels collected separately."""
        self.text.append(value)
        if self.heading is not None:
            self.heading.append(value)

    def handle_endtag(self, tag):
        """Finish a heading using the same anchor as the live documentation card."""
        if tag == 'h2' and self.heading is not None:
            if self.heading_id:
                self.contents.append((self.heading_id, ''.join(self.heading).strip()))
            self.heading = None


def article(environment, section, topic, person, api_base, source_reader, api_reader, selected=''):
    """Render one authorized page through the shared body and existing allowlisted source readers."""
    reference = source_reader(topic['source'], selected) if topic and topic['source'] else None
    api = api_reader(system_docs.source_files('api')) if topic and (section['slug'], topic['slug']) == ('api', 'custom') else None
    html = environment.get_template('docs_article.html').render(
        section=section, topic=topic, sections=system_docs.visible(person.can), person=person,
        document=system_docs.written(topic) if topic else None, reference=reference, api=api, api_base=api_base,
    )
    parsed = ArticleText()
    parsed.feed(html)
    return Markup(html), ' '.join(' '.join(parsed.text).split()), parsed.contents


def revision(sections):
    """Invalidate the search cache when a guide, template or allowlisted source revision changes."""
    paths = set((system_docs.ROOT / 'weightroom/pages').glob('docs*.html'))
    paths.update((system_docs.ROOT / 'weightroom/pages/docs').rglob('*.html'))
    paths.update(system_docs.ROOT / topic['doc'] for section in sections for topic in section['topics'] if topic.get('doc'))
    if any(section['slug'] == 'code' for section in sections):
        paths.update(system_docs.ROOT / relative for source in system_docs.SOURCES for relative in system_docs.source_files(source))
    return tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in sorted(paths))


def search(environment, sections, person, api_base, query, scope, source_reader, api_reader, cache):
    """Search only permitted rendered pages; cache plain text by permission scope and file revisions."""
    key = (tuple(section['slug'] for section in sections), api_base, revision(sections))
    records = cache.get(key)
    if records is None:
        records = []
        for section in sections:
            for topic in (None, *section['topics']):
                item = topic or section
                try:
                    _, text, _ = article(environment, section, topic, person, api_base, source_reader, api_reader)
                except (OSError, SyntaxError, UnicodeError):
                    raise OSError('Documentation search is temporarily unavailable.') from None
                records.append({'url': item['url'], 'title': item['title'], 'summary': item['summary'],
                                'section': section['slug'], 'category': section['title'], 'text': text})
        cache[key] = records
        while len(cache) > 4:
            cache.pop(next(iter(cache)))
    terms = query.casefold().split()
    results = []
    for record in records:
        text = (record['title'] + ' ' + record['summary'] + ' ' + record['text']).casefold()
        if (scope and record['section'] != scope) or not all(term in text for term in terms):
            continue
        score = sum(8 * (term in record['title'].casefold()) + 3 * (term in record['summary'].casefold()) for term in terms)
        start = max(0, record['text'].casefold().find(terms[0]) - 65) if terms else 0
        excerpt = ('…' if start else '') + record['text'][start:start + 225] + ('…' if len(record['text']) > start + 225 else '')
        results.append(record | {'score': score, 'excerpt': excerpt})
    return sorted(results, key=lambda item: (-item['score'], item['title'].casefold()))


async def documentation_page(request, person, page, environment, source_reader, api_reader, cache):
    """Serve native docs routes and search fragments with the same authorization and no-store policy."""
    name, part = request.path_params.get('section'), request.path_params.get('topic')
    path = request.url.path
    sections = system_docs.visible(person.can)
    admin = 'manage-settings' in person.can
    if part is None and name in system_docs.MOVED:
        if not admin and name != 'collections':
            return page(request, 'refused.html', person, status=403, why='Your account may not view system documentation.')
        target = system_docs.MOVED[name]
        return RedirectResponse(target + ('?' + request.url.query if request.url.query else ''), status_code=308)
    section = system_docs.BY_SLUG.get(name) if name else None
    if name and section is None:
        return Response('Documentation page not found.', status_code=404)
    if section and section not in sections:
        return page(request, 'refused.html', person, status=403, why='Your account may not view system documentation.')
    alias = system_docs.CODE_ALIASES.get(path)
    if alias or request.query_params.get('module') and path in system_docs.SOURCE_PAGES:
        if not admin:
            return page(request, 'refused.html', person, status=403, why='Your account may not view system documentation.')
        target = alias or system_docs.SOURCE_PAGES[path]
        return RedirectResponse(target + ('?' + request.url.query if request.url.query else ''), status_code=308)
    topic = section['by_slug'].get(part) if section and part else None
    if part and topic is None:
        return Response('Documentation page not found.', status_code=404)
    host = request.url.hostname or 'FRONT'
    host = '[' + host + ']' if ':' in host else host
    api_base = f'http://{host}:8442/v1'
    query = request.query_params.get('q', '').strip()[:300]
    scope = request.query_params.get('scope', '')
    if scope not in {item['slug'] for item in sections}:
        scope = ''
    searching = bool(query or scope)
    context = dict(section=section, topic=topic, sections=sections, q=query, scope=scope, searching=searching,
                   home=section is None, results=[], search_error='', article_html='', contents=[], previous=None, following=None,
                   page_count=sum(1 + len(item['topics']) for item in sections))
    try:
        if searching:
            context['results'] = await asyncio.to_thread(search, environment, sections, person, api_base, query, scope, source_reader, api_reader, cache)
        elif section:
            html, _, contents = await asyncio.to_thread(article, environment, section, topic, person, api_base, source_reader, api_reader, request.query_params.get('module', ''))
            siblings = [section, *section['topics']]
            index = siblings.index(topic or section)
            context.update(article_html=html, contents=contents, previous=siblings[index - 1] if index else None,
                           following=siblings[index + 1] if index + 1 < len(siblings) else None)
    except ValueError:
        return Response('Documentation source not found.', status_code=404)
    except (OSError, SyntaxError, UnicodeError):
        if not searching:
            return Response('Source documentation is temporarily unavailable.', status_code=503)
        context['search_error'] = 'Documentation search is temporarily unavailable. Try again, or browse the sections below.'
    template = 'docs_search_results.html' if request.query_params.get('part') == 'search' else 'docs_page.html'
    return page(request, template, person, status=503 if context['search_error'] else 200, **context)
