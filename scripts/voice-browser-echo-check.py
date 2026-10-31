"""Check real Chromium AEC settings and local WebRTC playback without devices or accounts.

Serves the production browser files on localhost, with only the remote conversation mocked.
This checks routing and full-duplex capture, not physical room echo on a phone or desktop.
"""

import asyncio
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from playwright.async_api import async_playwright


async def main():
    """Play generated speech while receiving microphone frames, then check cleanup and reconnect."""
    root = Path(__file__).resolve().parents[1]
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(root / "voice"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=[
                "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required"])
            page = await browser.new_page(permissions=["microphone"], viewport={"width": 390, "height": 844})
            errors, microphones = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("**/browser/config", lambda route: route.fulfill(json={"modes": ["direct"]}))
            await page.route("**/browser/sessions", lambda route: route.fulfill(json={"session_id": "aec-check"}))
            pcm = (root / "voice/prompts/connecting.pcm").read_bytes()

            def connected(socket):
                """Exercise a connected browser voice session using the selected audio fixture."""
                def receive(message):
                    """Collect browser voice events and PCM from the active verification session."""
                    if isinstance(message, bytes):
                        microphones.append(message)
                    else:
                        event = json.loads(message)
                        if event.get("type") == "session.cancel":
                            asyncio.create_task(socket.close())
                socket.on_message(receive)
                socket.send(json.dumps(dict(type="session.ready")))
                socket.send(json.dumps(dict(type="turn.started", turn=1)))
                socket.send(json.dumps(dict(type="playback.segment", turn=1, segment=1,
                                            samples=len(pcm) // 2, text="Connecting.")))

                async def speak():
                    """Send the selected speech fixture and its explicit endpoint to the session."""
                    for i in range(0, len(pcm), 960):
                        socket.send(pcm[i:i + 960])
                        await asyncio.sleep(.02)
                    socket.send(json.dumps(dict(type="turn.completed", turn=1)))
                asyncio.create_task(speak())

            await page.route_web_socket("**/browser/sessions/*/stream", connected)
            await page.goto(f"http://127.0.0.1:{server.server_port}/static/index.html")
            await page.locator("#name").fill("test")
            await page.locator("#password").fill("test")
            for enabled in (True, False):
                await page.locator("#echo").set_checked(enabled)
                await page.locator("#connect").click()
                await page.wait_for_function("ready && playback.incoming.connectionState === 'connected'", timeout=15000)
                await page.wait_for_function("document.querySelector('#status').textContent === 'Listening'", timeout=10000)
                assert await page.evaluate("microphone.getAudioTracks()[0].getSettings().echoCancellation") == enabled
                stats = await page.evaluate("""async () => {
                    const stats = [...(await playback.incoming.getStats()).values()];
                    const audio = stats.find(s => s.type === 'inbound-rtp' && s.kind === 'audio');
                    return {samples: audio?.totalSamplesReceived, energy: audio?.totalAudioEnergy,
                            players: document.querySelectorAll('audio').length};
                }""")
                assert stats["samples"] > 0 and stats["energy"] > 0 and stats["players"] == 1, stats
                assert microphones, "Microphone capture stopped during playback"
                print(json.dumps({"echo_cancellation": enabled, "microphone_frames": len(microphones), **stats}))
                await page.locator("#hangup").click()
                await page.wait_for_function("context === null && playback === null")
                assert await page.locator("audio").count() == 0
                await page.locator("#password").fill("test")
            assert not errors, errors
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
