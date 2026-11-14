"""Permission-scoped documentation reading and full-text search over current rendered articles."""

import asyncio
from html import escape
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
        self.blocks, self.section_stack = {}, []

    def handle_starttag(self, tag, attrs):
        """Capture each shared docs-section heading and its stable anchor."""
        attrs = dict(attrs)
        if tag == 'section':
            self.section_stack.append(self.section_id)
        if 'docs-section' in attrs.get('class', '').split():
            self.section_id = attrs.get('id', '')
            self.blocks.setdefault(self.section_id, [])
        if tag == 'h2' and 'card-heading' in attrs.get('class', '').split():
            self.heading, self.heading_id = [], self.section_id

    def handle_data(self, value):
        """Keep code and prose searchable, with heading labels collected separately."""
        self.text.append(value)
        if self.section_id:
            self.blocks[self.section_id].append(value)
        if self.heading is not None:
            self.heading.append(value)

    def handle_endtag(self, tag):
        """Finish a heading using the same anchor as the live documentation card."""
        if tag == 'h2' and self.heading is not None:
            if self.heading_id:
                self.contents.append((self.heading_id, ''.join(self.heading).strip()))
            self.heading = None
        if tag == 'section' and self.section_stack:
            self.section_id = self.section_stack.pop()


def article(environment, section, topic, person, api_base, source_reader, api_reader, selected=''):
    """Render one authorized page through the shared body and existing allowlisted source readers."""
    reference = source_reader(topic['source'], selected) if topic and topic['source'] else None
    api = api_reader(system_docs.source_files('api')) if topic and (section['slug'], topic['slug']) == ('api', 'custom') else None
    from weightroom import docs_reference
    generated = (docs_reference.settings() if topic and topic.get('generated') == 'settings' else
                 docs_reference.abilities() if topic and topic.get('generated') == 'abilities' else None)
    html = environment.get_template('docs_article.html').render(
        section=section, topic=topic, sections=system_docs.visible(person.can), person=person,
        document=system_docs.written(topic or section, admin='manage-settings' in person.can), reference=reference, api=api, api_base=api_base, generated=generated,
    )
    # These two published address placeholders retain the former guide templates'
    # request-host convenience; guide prose itself is never executed as Jinja.
    if section['slug'] == 'web-app':
        html = html.replace('http://CONTROL_NODE:8442/v1', escape(api_base)).replace(
            'http://CONTROL_NODE:8443/mcp', escape(api_base.replace(':8442/v1', ':8443/mcp')))
    parsed = ArticleText()
    parsed.feed(html)
    return Markup(html), ' '.join(' '.join(parsed.text).split()), parsed.contents


def revision(sections):
    """Invalidate the search cache when a guide, template or allowlisted source revision changes."""
    paths = set((system_docs.ROOT / 'weightroom/pages').glob('docs*.html'))
    paths.update((system_docs.ROOT / 'weightroom/pages/docs').rglob('*.html'))
    paths.update((system_docs.ROOT / 'docs/guide').glob('*/*.md'))
    paths.update((system_docs.ROOT / 'settings/defaults').glob('*.toml'))
    paths.update(system_docs.ROOT / name for name in ('loadcoach/groups.py', 'loadcoach/setting_catalog.py', 'weightroom/setting_labels.py', 'weightroom/app.py'))
    paths.update(system_docs.ROOT / topic['doc'] for section in sections for topic in section['topics'] if topic.get('doc'))
    if any(section['slug'] == 'code' for section in sections):
        paths.update(system_docs.ROOT / relative for source in system_docs.SOURCES for relative in system_docs.source_files(source))
    return tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in sorted(paths))


def search(environment, sections, person, api_base, query, scope, source_reader, api_reader, cache):
    """Search only permitted rendered pages; cache plain text by permission scope and file revisions."""
    key = (tuple(section['slug'] for section in sections), 'manage-settings' in person.can, api_base, revision(sections))
    records = cache.get(key)
    if records is None:
        records = []
        for section in sections:
            for topic in (None, *section['topics']):
                item = topic or section
                try:
                    html, text, contents = article(environment, section, topic, person, api_base, source_reader, api_reader)
                except (OSError, SyntaxError, UnicodeError):
                    raise OSError('Documentation search is temporarily unavailable.') from None
                parsed = ArticleText()
                parsed.feed(str(html))
                records.append({'url': item['url'], 'title': item['title'], 'summary': item['summary'],
                                'blocks': [(ident, dict(contents).get(ident, item['title']), ' '.join(' '.join(values).split())) for ident, values in parsed.blocks.items()],
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
        block = next((block for block in record['blocks'] if all(term in block[2].casefold() for term in terms)), None)
        results.append(record | {'score': score, 'excerpt': excerpt, 'anchor': block[0] if block else '',
                                 'heading': block[1] if block else record['title']})
    return sorted(results, key=lambda item: (-item['score'], item['title'].casefold()))


async def documentation_page(request, person, page, environment, source_reader, api_reader, cache):
    """Serve native docs routes and search fragments with the same authorization and no-store policy."""
    name, part = request.path_params.get('section'), request.path_params.get('topic')
    path = request.url.path
    sections = system_docs.visible(person.can)
    admin = 'manage-settings' in person.can
    alias = system_docs.ALIASES.get(path)
    if alias:
        destination = system_docs.BY_SLUG.get(alias.split('/')[2])
        if destination and destination['audience'] == 'admin' and not admin:
            return page(request, 'refused.html', person, status=403, why='Your account may not view system documentation.')
        return RedirectResponse(alias + ('?' + request.url.query if request.url.query else ''), status_code=308)
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
    if topic and topic['audience'] == 'admin' and not admin:
        return page(request, 'refused.html', person, status=403, why='Your account may not view system documentation.')
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
    if section is None:
        from weightroom.docs_reference import page_map
        context['page_guides'] = page_map(person.can)
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


class DocsAPI:
    """Read the same authorized library through account keys or caller-bound machine proof."""

    def __init__(self, environment, ask, source_reader, api_reader, cache, key_file=''):
        """Bind shared renderers and a replay cache; never create an independent identity store."""
        from loadcoach.statement import Nonces
        self.environment, self.ask = environment, ask
        self.source_reader, self.api_reader, self.cache = source_reader, api_reader, cache
        self.key_file, self.nonces = key_file, Nonces()

    async def identity(self, request):
        """Authenticate a signed tool caller or recheck the supplied account key at Turnstile."""
        import time
        from types import SimpleNamespace
        from loadcoach import statement
        proof = {}
        signed = request.headers.get(statement.HEADER)
        if signed:
            try:
                accepted = None
                for key in statement.keys(self.key_file):
                    try:
                        accepted = statement.check(key, signed, request.method,
                            statement.address((request.scope.get('raw_path') or request.url.path.encode()).decode(), request.scope['query_string']),
                            await request.body(), self.nonces)
                        break
                    except (ValueError, KeyError, TypeError):
                        continue
                if not accepted or accepted.get('purpose') != 'docs-tool' or not accepted.get('caller'):
                    raise ValueError('Missing documentation caller proof.')
                user = statement.user_of(accepted['caller'])
                if user.until is not None and user.until <= time.time():
                    raise ValueError('The caller session has expired.')
                proof[statement.PROOF] = statement.proof(key, accepted['nonce'])
                person = SimpleNamespace(name=user.name, can=user.group.can, key=None, me={'identity': user.identity})
            except (OSError, ValueError, KeyError, TypeError):
                raise PermissionError('Missing or invalid documentation caller proof.') from None
        else:
            authorization = request.headers.get('authorization', '')
            key = authorization[7:] if authorization.startswith('Bearer ') else request.cookies.get('weightroom', '')
            if not key:
                raise PermissionError('Use an account key or signed documentation caller proof.')
            answer = await self.ask('GET', '/loadcoach/me', key)
            if not answer.is_success:
                raise PermissionError('Missing or invalid account key.')
            current = answer.json()
            person = SimpleNamespace(name=current['name'], can=current['can'], key=key, me=current)
        return person, proof

    def article_text(self, person, url, anchor='', offset=0):
        """Read only catalogue URLs and page plain text without converting input to a file path."""
        import re
        from urllib.parse import urlsplit
        from toollocker.docs import READ_CHARS
        parsed = urlsplit(url)
        if (parsed.scheme or parsed.netloc or parsed.query or not re.fullmatch(r'/docs/[a-z0-9-]+(?:/[a-z0-9-]+)?', parsed.path)
                or anchor and parsed.fragment and anchor != parsed.fragment):
            raise ValueError('Use a canonical local documentation URL without a query.')
        path = system_docs.ALIASES.get(parsed.path, parsed.path)
        parts = path.strip('/').split('/')
        section = next((one for one in system_docs.visible(person.can) if one['slug'] == parts[1]), None)
        topic = section['by_slug'].get(parts[2]) if section and len(parts) == 3 else None
        if section is None or len(parts) == 3 and topic is None:
            raise LookupError('Documentation page not found or not visible to this account.')
        html, text, _ = article(self.environment, section, topic, person, 'http://CONTROL_NODE:8442/v1', self.source_reader, self.api_reader)
        anchor = anchor or parsed.fragment
        if anchor:
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,199}', anchor):
                raise ValueError('Invalid documentation anchor.')
            reader = SectionText(anchor)
            reader.feed(str(html))
            if not reader.found:
                raise LookupError('Documentation heading not found.')
            text = ' '.join(' '.join(reader.text).split())
        if offset > len(text):
            raise ValueError('Offset is beyond this article.')
        return {'title': (topic or section)['title'], 'url': path + ('#' + anchor if anchor else ''),
                'text': text[offset:offset + READ_CHARS], 'offset': offset,
                'next_offset': offset + READ_CHARS if len(text) > offset + READ_CHARS else None}

    def read_source(self, module, symbol='', offset=0):
        """Read documented source only through SOURCES, using the existing source-reference reader."""
        from toollocker.docs import READ_CHARS
        from urllib.parse import quote
        source = next((source for source in system_docs.SOURCES if module in system_docs.source_files(source)), None)
        if not source:
            raise LookupError('Documentation source not found.')
        found = self.source_reader(source, module)['module']
        definitions = found['definitions']
        selected = next((item for item in definitions if item['name'] == symbol), None) if symbol else None
        if symbol and selected is None:
            raise LookupError('Documented symbol not found.')
        url = '/docs/code/' + source + '?module=' + quote(module, safe='')
        if selected:
            text = selected['name'] + selected['signature'] + '\n' + selected['doc']
            url += '#symbol-' + str(selected['line'])
        else:
            text = found['doc'] + '\n' + '\n'.join(item['name'] + item['signature'] + ' — ' + item['doc'].split('\n')[0] for item in definitions)
        if offset > len(text):
            raise ValueError('Offset is beyond this source reference.')
        return {'module': module, 'symbol': symbol, 'url': url, 'text': text[offset:offset + READ_CHARS],
                'offset': offset, 'next_offset': offset + READ_CHARS if len(text) > offset + READ_CHARS else None}

    async def endpoint(self, request):
        """Return caller-scoped documentation records; admin references remain private and bounded."""
        from starlette.responses import JSONResponse
        from weightroom import docs_reference
        from toollocker.docs import SEARCH_LIMIT
        headers = {'cache-control': 'no-store', 'x-content-type-options': 'nosniff'}
        try:
            person, proof = await self.identity(request)
            headers.update(proof)
            if 'use-tools' not in person.can:
                return JSONResponse({'error': {'message': 'Documentation tools require use-tools.'}}, 403, headers=headers)
            operation = request.path_params.get('operation', request.url.path.rsplit('/', 1)[-1])
            allowed = {'catalogue': {'offset'}, 'search': {'q','scope','limit'}, 'article': {'url','anchor','offset'},
                       'endpoints': {'q'}, 'settings': {'q'}, 'abilities': {'q'}, 'source': {'module','symbol','offset'}}
            if operation not in allowed:
                raise LookupError('Unknown documentation operation.')
            params = request.query_params
            if params.keys() - allowed[operation] or any(len(params.getlist(key)) != 1 for key in params):
                raise ValueError('Unknown or repeated documentation arguments.')
            if any(len(value) > 500 for value in params.values()):
                raise ValueError('Documentation arguments are too long.')
            if operation in ('endpoints','settings','abilities','source') and 'manage-settings' not in person.can:
                return JSONResponse({'error': {'message': 'An administrator can help with this reference.'}}, 403, headers=headers)
            offset = int(params.get('offset', 0))
            if not 0 <= offset <= 2000000:
                raise ValueError('Invalid documentation offset.')
            sections = system_docs.visible(person.can)
            query = params.get('q', '').strip()[:300]
            if operation == 'catalogue':
                records = [(section, topic) for section in sections for topic in (None, *section['topics'])]
                if offset > len(records):
                    raise ValueError('Offset is beyond this catalogue.')
                selected = {}
                for section, topic in records[offset:offset + 20]:
                    row = selected.setdefault(section['url'], {'title': section['title'][:200], 'summary': section['summary'][:500],
                        'url': section['url'], 'covers': section.get('covers', []), 'topics': []})
                    if topic:
                        row['topics'].append({'title': topic['title'][:200], 'summary': topic['summary'][:500],
                            'url': topic['url'], 'covers': topic.get('covers', [])})
                result = {'sections': list(selected.values()), 'offset': offset,
                          'next_offset': offset + 20 if len(records) > offset + 20 else None}
            elif operation == 'search':
                scope = params.get('scope','')
                if scope and scope not in {one['slug'] for one in sections}:
                    return JSONResponse({'error': {'message': 'Documentation scope is not visible.'}}, 403, headers=headers)
                limit = int(params.get('limit', SEARCH_LIMIT))
                if not 1 <= limit <= SEARCH_LIMIT:
                    raise ValueError('Search limit must be from 1 to 10.')
                results = await asyncio.to_thread(search, self.environment, sections, person, 'http://CONTROL_NODE:8442/v1', query, scope, self.source_reader, self.api_reader, self.cache)
                result = {'hits': [{'title': hit['title'], 'url': hit['url'] + ('#' + hit['anchor'] if hit['anchor'] else ''),
                                   'heading': hit['heading'], 'snippet': hit['excerpt']} for hit in results[:limit]]}
            elif operation == 'article':
                result = await asyncio.to_thread(self.article_text, person, params.get('url',''), params.get('anchor',''), offset)
            elif operation == 'source':
                result = await asyncio.to_thread(self.read_source, params.get('module',''), params.get('symbol',''), offset)
            else:
                entries = (docs_reference.settings() if operation == 'settings' else docs_reference.abilities() if operation == 'abilities'
                           else [dict(row, source=group['path']) for group in self.api_reader(system_docs.source_files('api')) for row in group['rows']])
                terms = query.casefold().split()
                result = {'entries': [entry for entry in entries if all(term in str(entry).casefold() for term in terms)][:SEARCH_LIMIT]}
            return JSONResponse(result, headers=headers)
        except PermissionError as error:
            return JSONResponse({'error': {'message': str(error)}}, 401, headers=headers)
        except LookupError as error:
            return JSONResponse({'error': {'message': str(error)}}, 404, headers=headers)
        except (ValueError, TypeError):
            return JSONResponse({'error': {'message': 'Invalid documentation arguments.'}}, 400, headers=headers)
        except (OSError, SyntaxError, UnicodeError):
            return JSONResponse({'error': {'message': 'Documentation is temporarily unavailable.'}}, 503, headers=headers)

    def routes(self):
        """Register the public docs API under its explicit read-only operations."""
        from starlette.routing import Route
        return [Route(path, self.endpoint) for path in (
            '/owg/docs/catalogue', '/owg/docs/search', '/owg/docs/article', '/owg/docs/endpoints',
            '/owg/docs/settings', '/owg/docs/abilities', '/owg/docs/source')]


class SectionText(HTMLParser):
    """Extract one existing heading/row subtree as plain text for a bounded citation read."""
    def __init__(self, anchor):
        """Start a subtree reader for one exact rendered anchor."""
        super().__init__(convert_charrefs=True)
        self.anchor, self.found, self.depth, self.text = anchor, False, 0, []

    def handle_starttag(self, tag, attrs):
        """Begin at the matching ID and count only non-void elements in its subtree."""
        if dict(attrs).get('id') == self.anchor and not self.found:
            self.found, self.depth = True, 1
        elif self.depth and tag not in ('br','hr','img','input','meta','link'):
            self.depth += 1

    def handle_endtag(self, tag):
        """Stop collecting when the selected subtree closes."""
        if self.depth:
            self.depth -= 1

    def handle_data(self, value):
        """Keep only readable text inside the selected rendered subtree."""
        if self.depth:
            self.text.append(value)
