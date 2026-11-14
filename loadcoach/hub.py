"""Hugging Face, as far as LoadCoach uses it: finding repositories that hold model files, listing those files, and bringing one down.

Three of its addresses are asked, with the HTTP client LoadCoach already has. No program of
Hugging Face's own is needed.

    /api/models?search=...             the repositories that match some words
    /api/models/OWNER/NAME/tree/main   the files of one repository, each with its size and its SHA-256
    /OWNER/NAME/resolve/main/FILE      one file

Nothing here knows of users, places or the log. That is in app.py.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import httpx

HUB = "https://huggingface.co"
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")  # OWNER/NAME, as Hugging Face writes a repository
PART_OF = re.compile(r"-\d{5}-of-(\d{5})\.gguf$")  # how llama.cpp names the parts of a model that is split over several files
PIECE = 2**20  # how much is written at a time, and so how often the progress moves on
CLASSIFIER_PAGES = {}


async def classifier_search_page(http, words, token, cursor, sort, direction):
    """Merge a paginated OR of pipeline and tags, retaining private bounded cursor snapshots."""
    identity = (words, sort, direction, hashlib.sha256(token.encode()).hexdigest())
    now = time.monotonic()
    for identifier, saved in list(CLASSIFIER_PAGES.items()):
        if saved['expires'] < now:
            CLASSIFIER_PAGES.pop(identifier, None)
    if cursor:
        saved = CLASSIFIER_PAGES.get(cursor)
        if not saved or saved['identity'] != identity:
            raise Refused(400, 'This Classifier search page expired. Search again to refresh the results.')
        state = copy.deepcopy(saved['state'])
    else:
        state = {'seen': set(), 'branches': [{'params': params, 'rows': [], 'cursor': '', 'done': False} for params in
            [{'pipeline_tag': 'text-classification'}, *[{'filter': tag} for tag in ('decision-model', 'system-one', 'typed-decisions', 'jev')]]]}

    async def refill(branch):
        """Read one branch only after its retained candidates have been consumed."""
        params = {'search': words, 'sort': sort, 'direction': direction, 'limit': '20', **branch['params']}
        if branch['cursor']:
            params['cursor'] = branch['cursor']
        try:
            answer = await http.get(HUB + '/api/models', params=params, headers=signed(token))
        except httpx.HTTPError as problem:
            raise Refused(502, f'Hugging Face did not answer: {type(problem).__name__}.') from None
        refused(answer, token)
        branch['rows'] = [{key: row.get(key, '' if key in ('id', 'createdAt') else 0)
                           for key in ('id', 'createdAt', 'downloads', 'likes')} for row in answer.json()]
        branch['cursor'] = parse_qs(urlsplit(answer.links.get('next', {}).get('url', '')).query).get('cursor', [''])[0]
        branch['done'] = not branch['cursor']
        if not branch['rows']:
            branch['done'] = True

    found = []
    while len(found) < 20:
        await asyncio.gather(*(refill(branch) for branch in state['branches'] if not branch['rows'] and not branch['done']))
        available = [branch for branch in state['branches'] if branch['rows']]
        if not available:
            break
        chosen = (min if direction == '1' else max)(available, key=lambda branch: branch['rows'][0][sort])
        row = chosen['rows'].pop(0)
        if row['id'] in state['seen']:
            continue
        state['seen'].add(row['id'])
        found.append({'repository': row['id'], 'downloads': row['downloads'], 'likes': row['likes'], 'made': row['createdAt'][:10]})
    next_cursor = ''
    if len(state['seen']) < 10000 and any(branch['rows'] or not branch['done'] for branch in state['branches']):
        if len(CLASSIFIER_PAGES) >= 512:
            CLASSIFIER_PAGES.pop(next(iter(CLASSIFIER_PAGES)))
        next_cursor = secrets.token_urlsafe(24)
        CLASSIFIER_PAGES[next_cursor] = {'identity': identity, 'expires': now + 900, 'state': state}
    return {'found': found, 'next_cursor': next_cursor, 'paging': {'limit': 20, 'sort': sort, 'direction': direction}}


class Refused(Exception):
    """Hugging Face said no, or did not answer. `status` is what LoadCoach answers its own caller with."""

    def __init__(self, status: int, message: str) -> None:
        """Initialize the failure message and its status metadata."""
        super().__init__(message)
        self.status = status


def signed(token: str) -> dict:
    """The header that says whose account is asking. Nothing, with no token."""
    return {"authorization": f"Bearer {token}"} if token else {}


def refused(answer: httpx.Response, token: str) -> None:
    """Raise Refused, in plain words, if Hugging Face did not give what was asked for."""
    if answer.status_code < 400:
        return
    if answer.status_code in (401, 403):  # it answers 401 for a repository that is not there as well, so as not to say which exist
        how = "The token LoadCoach has is not allowed it." if token else "LoadCoach has no token of an account there."
        raise Refused(403, f"Hugging Face says there is no such repository or file, or that it is only for accounts it was opened to. {how}")
    if answer.status_code == 404:
        raise Refused(404, "Hugging Face has no such repository or file.")
    raise Refused(502, f"Hugging Face answered {answer.status_code}.")


async def asked(http: httpx.AsyncClient, path: str, token: str, **params) -> list:
    """What one of Hugging Face's listing addresses answers: a list."""
    try:
        answer = await http.get(HUB + path, params=params, headers=signed(token))
    except httpx.HTTPError as problem:
        raise Refused(502, f"Hugging Face did not answer: {type(problem).__name__}.") from None
    refused(answer, token)
    return answer.json()


async def search(http: httpx.AsyncClient, words: str, token: str = "") -> list[dict]:
    """The repositories that hold .gguf files and match these words, the most downloaded first."""
    found = await asked(http, "/api/models", token, search=words, filter="gguf", sort="downloads", direction="-1", limit="40")
    return [{"repository": one["id"], "downloads": one.get("downloads", 0), "likes": one.get("likes", 0), "made": one.get("createdAt", "")[:10]} for one in found]


def search_order(params) -> tuple[str, str]:
    """Keep model discovery ordering within the supported provider fields."""
    sort = params.get('sort', 'downloads')
    sort = sort if sort in ('downloads', 'likes', 'createdAt') else 'downloads'
    # The Hub rejects ascending downloads/likes; creation time supports either order.
    return sort, '1' if sort == 'createdAt' and params.get('direction') == '1' else '-1'


async def search_page(http: httpx.AsyncClient, words: str, token: str = "", cursor: str = "",
                      sort: str = 'downloads', direction: str = '-1', kind: str = '') -> dict:
    """Read one bounded result page; only forward the provider's opaque cursor."""
    sort, direction = search_order({'sort': sort, 'direction': direction})
    if kind == 'classifier':
        # Hub tag filters are conjunctive. Search each alternative separately so a model
        # tagged only as a decision model is not lost behind the pipeline filter.
        return await classifier_search_page(http, words, token, cursor, sort, direction)
    params = {'search': words, 'filter': 'gguf', 'sort': sort, 'direction': direction, 'limit': '20'}
    if cursor:
        params['cursor'] = cursor[:4096]
    try:
        answer = await http.get(HUB + '/api/models', params=params, headers=signed(token))
    except httpx.HTTPError as problem:
        raise Refused(502, f"Hugging Face did not answer: {type(problem).__name__}.") from None
    refused(answer, token)
    next_url = answer.links.get('next', {}).get('url', '')
    next_cursor = parse_qs(urlsplit(next_url).query).get('cursor', [''])[0]
    return {'found': [{"repository": one['id'], "downloads": one.get('downloads', 0),
                       "likes": one.get('likes', 0), "made": one.get('createdAt', '')[:10]}
                      for one in answer.json()], 'next_cursor': next_cursor,
            'paging': {'limit': 20, 'sort': sort, 'direction': direction}}


async def decision_header(http, repository, filename, token=''):
    """Inspect a bounded GGUF range; absence or truncated metadata never earns a badge."""
    import tempfile
    from engines.llama_cpp.gguf import facts, read_metadata
    maximum = 8 * 2**20
    try:
        async with http.stream('GET', HUB + f'/{repository}/resolve/main/' + quote(filename, safe='/'),
                headers={**signed(token), 'range': f'bytes=0-{maximum-1}'}, follow_redirects=True) as response:
            if response.status_code not in (200, 206):
                return False
            with tempfile.NamedTemporaryFile(suffix='.gguf') as file:
                size = 0
                async for chunk in response.aiter_bytes():
                    piece = chunk[:maximum-size]
                    file.write(piece)
                    size += len(piece)
                    if size >= maximum:
                        break
                file.flush()
                return facts(read_metadata(file.name)).get('what') == 'classifier'
    except (httpx.HTTPError, OSError, ValueError):
        return False


async def files(http: httpx.AsyncClient, repository: str, token: str = "", *, classifier=False) -> list[dict]:
    """Every .gguf file of a repository: its path there, its size, and its SHA-256 where Hugging Face gives one."""
    if repository.startswith('civitai/'):
        from mirrorwall.civitai import files as image_files
        return await image_files(http,repository,token)
    # ponytail: the first 1000 entries of a repository. One with more lists the rest on further pages, which are not asked for.
    listed = await asked(http, f"/api/models/{repository}/tree/main", token, recursive="true")
    return [
        {"path": one["path"], "size": one["size"], "sha256": one.get("lfs", {}).get("oid")}
        for one in listed
        if one.get("type") == "file" and (one["path"].lower().endswith((".gguf", ".safetensors", ".pth"))
            or classifier and (one['path'].lower().endswith(('.json', '.model', '.txt', '.jinja')) or one['path'] == 'Modelfile'))
        and not one['path'].lower().endswith('.py')
    ]


def rows(listed: list[dict]) -> list[dict]:
    """The files as they are chosen: one row for each, and one row for all the parts of a split model. Smallest first."""
    together: dict[str, list[dict]] = {}
    for one in listed:
        split = PART_OF.search(one["path"])
        together.setdefault(one["path"][: split.start()] + split[1] if split else one["path"], []).append(one)
    made = [{"file": parts[0]["path"], "parts": parts, "size": sum(part["size"] for part in parts)} for parts in together.values()]
    return sorted(made, key=lambda row: row["size"])


def chosen(listed: list[dict], names: list[str]) -> list[dict]:
    """The files to bring down for these names. Naming one part of a split model brings all of its parts.

    A name is a file's path in the repository, or its name alone, which is how a download left
    half done is known. Raises Refused if the repository has no such file.
    """
    wanted: list[dict] = []
    for name in names:
        row = next((row for row in rows(listed) if any(name in (part["path"], Path(part["path"]).name) for part in row["parts"])), None)
        if row is None:
            raise Refused(404, f"That repository has no model file named {name}.")
        wanted += [part for part in row["parts"] if part not in wanted]
    return wanted


def folder_of(repository: str) -> str:
    """The folder a repository's files are put in, inside a place. Hugging Face allows no `--` in a name, so the way back is sure."""
    return repository.replace("/", "--")


def sha256_of(path: Path) -> str:
    """Return the SHA-256 checksum of the file contents."""
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


async def fetch(http: httpx.AsyncClient, repository: str, entry: dict, target: Path, token: str, progress: dict) -> None:
    """Bring one file down to `target`. `progress` is kept up to date for whoever is watching.

    It is written beside the target as `NAME.part`, which nothing takes for a model file, and
    given its real name only when all of it is there and its SHA-256 is the one Hugging Face
    names. A `.part` file already there is gone on with, not begun again.

    Raises Refused or OSError if it cannot be done. The `.part` file stays, to be gone on with,
    unless what came down turned out not to be the file.
    """
    if repository.startswith('civitai/'):
        from mirrorwall.civitai import fetch as image_fetch
        return await image_fetch(http,repository,entry,target,token,progress)
    part = target.with_name(target.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > entry["size"]:
        have = 0  # not this file. It is written over
    progress.update(done=have, start=have, since=time.monotonic())
    if have < entry["size"]:
        # Hugging Face answers with the address of the file on another machine, and the client goes there.
        # It leaves the token behind when it does: httpx sends `authorization` to the host it was meant for only.
        headers = {**signed(token), **({"range": f"bytes={have}-"} if have else {})}
        address = f"{HUB}/{repository}/resolve/main/{quote(entry['path'])}"
        async with http.stream("GET", address, headers=headers, follow_redirects=True) as answer:
            refused(answer, token)
            if answer.status_code != 206:  # the whole file is being sent, whatever was asked for
                progress["done"] = progress["start"] = have = 0
            with open(part, "ab" if have else "wb") as file:
                async for piece in answer.aiter_bytes(PIECE):
                    if progress["done"] + len(piece) > entry["size"]:
                        raise OSError("the download exceeds the file size advertised by Hugging Face")
                    # On another thread: a drive that is busy can make a write wait, and requests being answered must not wait with it.
                    await asyncio.to_thread(file.write, piece)
                    progress["done"] += len(piece)
    if part.stat().st_size != entry["size"]:
        raise OSError("the connection ended before the whole file had come. Continue it to go on from here")
    progress["state"] = "checking"
    if entry["sha256"] and await asyncio.to_thread(sha256_of, part) != entry["sha256"]:
        part.unlink()
        raise OSError("what came down is not the file Hugging Face describes: its SHA-256 differs. It was thrown away")
    part.replace(target)
