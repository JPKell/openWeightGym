"""Bounded result controls and bookmarkable pagination shared by console pages."""

from urllib.parse import urlencode

SIZES = (10, 25, 50, 100, 250, 500, 1000)


def controls(params, default=25):
    """Parse bounded page and page-size controls from request parameters."""
    def number(key, fallback, ceiling):
        """Parse and clamp an integer pagination parameter with a safe fallback."""
        try:
            return min(max(int(params.get(key, fallback)), 1), ceiling)
        except (TypeError, ValueError):
            return fallback
    return number('limit', default, 1000), number('page', 1, 1_000_000)


def pagination(path, params, total, limit, current):
    """Build pagination metadata and URLs while preserving active filters."""
    pages = max(1, (total + limit - 1) // limit)
    query = {key: value for key, value in params.items() if key not in ('part', 'page', 'said', 'failed')}
    query['limit'] = limit
    def link(number):
        """Build a filtered listing URL for the selected page number."""
        return path + '?' + urlencode({**query, 'page': number})
    return {'limit': limit, 'page': current, 'pages': pages, 'total': total,
            'sizes': sorted(set((*SIZES, limit))),
            'previous': link(current - 1) if current > 1 else '',
            'next': link(current + 1) if current < pages else ''}
