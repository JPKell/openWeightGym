"""Compact location rows, rendered from the real templates with made-up folders."""

import re

from starlette.templating import Jinja2Templates

from weightroom import display
from weightroom.app import HERE, size

GB, TB = 2**30, 2**40


def render(found: dict) -> str:
    """The location cards for one listing, as the page's #files-places card body holds them."""
    pages = Jinja2Templates(directory=[HERE / "pages"])
    pages.env.filters.update(size=size, timestamp=display.timestamp)
    return pages.env.get_template("files_parts.html").module.places_block(found)


def place(name: str, files: int, total: int, free: int, drive: str, tier: str = "cache") -> dict:
    """A folder as the files listing reports it, with its relative name already worked out."""
    return {"name": name, "relative": name.removeprefix("storage_"), "folder": f"/models/{name}", "tier": tier,
            "files": files, "total": total, "free": free, "filesystem": drive}


def test_each_location_is_a_row_with_free_room_and_folder_details():
    found = {"artifact_machine": "front", "store_available": True, "nodes": [
        {"name": "front", "state": "ready", "answering": True, "problems": [], "place_groups": [
            {"tier": "cache", "root": "/srv/cache", "places": [place("llm", 300 * GB, 2 * TB, TB, "a"),
                                                                place("adapters", 2 * GB, 2 * TB, TB, "a")]},
            {"tier": "storage", "root": "/srv/storage", "places": [place("storage_llm", 900 * GB, 500 * GB, 50 * GB, "b", "storage"),
                                                                    place("storage_projectors", GB, 500 * GB, 50 * GB, "c", "storage")]}]},
        {"name": "gpu-box", "state": "ready", "answering": True, "problems": [], "place_groups": [
            {"tier": "cache", "root": "/var/models", "places": [place("llm", 40 * GB, 900 * GB, 600 * GB, "d")]},
            {"tier": "storage", "root": "/var/storage", "places": [place("storage_llm", GB, 900 * GB, 600 * GB, "d", "storage")]}]},
        {"name": "laptop", "state": "not answering", "answering": False, "problems": [], "place_groups": []},
    ]}
    html = render(found)
    assert "/<wbr>srv/<wbr>cache" in html  # long paths break after a slash, never inside a folder's name
    html = html.replace("<wbr>", "")
    cards = re.findall(r'<tr aria-label="([^"]+)">(.*?)(?=<tr aria-label=|\Z)', html, re.S)
    # Front cache and storage, the node's cache (its storage is not shown), and a note for the node that is not answering.
    assert [title for title, _ in cards] == ["Front cache", "Storage", "gpu-box"]
    front, storage, node = (body for _, body in cards)

    # Folders on one drive count it once: 2 TB, of which 1 TB is free; models take 302 GB of it.
    assert "/srv/cache" in front and "<b>302.0 GB</b>" in front and "/ 2.0 TB" in front and "<b>1.0 TB</b> free" in front
    assert '<td title="llm">llm</td><td class="text-end">300.0 GB</td>' in front and '<td title="adapters">adapters</td>' in front
    assert "drives" not in front

    # Folders on two drives add both up and say so; a share larger than the drives never draws past the end of the bar.
    assert "/ 1.0 TB" in storage and "<b>100.0 GB</b> free" in storage and "on 2 drives" in storage
    widths = [float(width) for width in re.findall(r'class="progress-bar[^"]*" style="width: ([\d.]+)%"', storage)]
    assert widths == [90.0]
    assert '<td title="llm">llm</td>' in storage and '<td title="projectors">projectors</td>' in storage

    # A compute node's card says its state; one that is not answering says why it shows nothing.
    assert "/var/models" in node and "bg-green-lt" in node and ">ready<" in node and "/var/storage" not in html
    assert "laptop" in html and "This node is offline" in html
    assert html.count('role="progressbar"') == 3


def test_problems_and_an_empty_listing_are_said_plainly():
    problem = {"artifact_machine": "front", "store_available": False, "nodes": [
        {"name": "front", "state": "ready", "answering": True, "problems": ["/mnt/models is not available."], "place_groups": []}]}
    html = render(problem)
    assert "No model-file locations are available yet." in html and "/mnt/models is not available." in html
    assert "Front model storage is optional." in html
    assert "No model-file locations are available." in render({"nodes": []})
