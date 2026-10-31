"""Chat follows growing answers only while the reader is near the bottom."""

from pathlib import Path

import pytest


playwright = pytest.importorskip("playwright.sync_api")


def test_stream_follows_after_short_downward_scroll_but_respects_reading_position():
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 900, "height": 500})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content("""
            <style>body { margin: 0; line-height: 24px; }
            #messages { height: 1400px; }
            .composer { height: 100px; }
            </style>
            <section id="chat-main" data-chat-ref="example">
              <div id="messages"><div id="live" data-stream="/chat/example/stream">Waiting</div></div>
              <div class="composer">Stop</div>
            </section>
        """)
        page.evaluate("""() => {
            window.streams = [];
            window.EventSource = class {
                constructor(url) { this.url = url; streams.push(this); }
                addEventListener() {}
            };
        }""")
        page.add_script_tag(path=str(Path(__file__).resolve().parents[1] / "weightroom/static/chat.js"))
        page.wait_for_function("scrollY > 500 && streams.length === 1")
        page.wait_for_function("lastScroll === scrollY")

        # Seven lines upward marks the composer hidden. During a streamed turn
        # the Stop control remains visible, but the hidden state used to disable
        # following even after a short return to the bottom.
        page.evaluate("document.scrollingElement.scrollTop -= 200")
        page.wait_for_function("composerHidden")
        page.evaluate("document.scrollingElement.scrollTop += 100")
        page.wait_for_function("lastScroll === scrollY && composerHidden")
        page.evaluate("streams[0].onmessage({data: '<div style=\"height:900px\">Growing answer</div>'})")
        page.wait_for_function("document.scrollingElement.scrollHeight - innerHeight - scrollY < 2")

        page.evaluate("document.scrollingElement.scrollTop -= 400")
        before = page.evaluate("scrollY")
        page.evaluate("streams[0].onmessage({data: '<div style=\"height:1400px\">More answer</div>'})")
        assert abs(page.evaluate("scrollY") - before) < 2
        assert not errors
        browser.close()
