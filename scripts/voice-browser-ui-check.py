"""Exercise Chromium's real microphone AudioWorklet using a generated WAV, without hardware.

Requires the optional test tool Playwright and its Chromium installation. Login JSON stays
local and is never printed. The WAV should contain one question; startup silence is added.
"""

import argparse
import asyncio
import json
from pathlib import Path
import wave

from playwright.async_api import async_playwright


async def main(args):
    """Drive the deployed page, await a spoken answer, and keep a screenshot and transcript."""
    credentials = json.loads(args.login_file.read_text())
    output = Path("voice-test")
    output.mkdir(exist_ok=True)
    microphone = output / "ui-microphone.wav"
    with wave.open(str(args.audio), "rb") as source, wave.open(str(microphone), "wb") as target:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("Use a mono signed 16-bit WAV")
        target.setparams(source.getparams())
        target.writeframes(bytes(source.getframerate() * 2 * 12))
        target.writeframes(source.readframes(source.getnframes()))
        target.writeframes(bytes(source.getframerate() * 2 * 30))
    errors = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            "--use-file-for-fake-audio-capture=" + str(microphone.resolve()) + "%noloop",
            "--autoplay-policy=no-user-gesture-required"])
        page = await browser.new_page(permissions=["microphone"], viewport={"width": 1100, "height": 850})
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(args.url)
            await page.locator("#name").fill(credentials["name"])
            await page.locator("#password").fill(credentials["password"])
            if credentials.get("code"):
                await page.locator("#code").fill(credentials["code"])
            await page.locator("#mode").select_option(credentials.get("conversation_mode", "direct"))
            await page.locator("#connect").click()
            await page.wait_for_function("document.querySelector('#transcript').textContent.includes('Assistant')", timeout=90000)
            await page.wait_for_function("document.querySelector('#status').textContent === 'Listening'", timeout=90000)
            await page.wait_for_timeout(1000)
            transcript = await page.locator("#transcript").inner_text()
            assert "You" in transcript and "Assistant" in transcript
            assert not errors, errors
            await page.screenshot(path=str(output / "browser-final.png"), full_page=True)
            (output / "browser-final.json").write_text(json.dumps({"transcript": transcript, "errors": errors}, indent=2))
            print(transcript)
            await page.locator("#hangup").click()
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--login-file", type=Path, required=True)
    parser.add_argument("--url", default="http://localhost:18446")
    asyncio.run(main(parser.parse_args()))
