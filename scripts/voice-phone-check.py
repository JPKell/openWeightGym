"""Simulate a signed Twilio call; no telephone is dialled."""
import argparse
import asyncio
from pathlib import Path
from voice.telephone_check import main


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--to-number", help="Configured destination in E.164 format")
    parser.add_argument("--url", default="http://localhost:18482")
    parser.add_argument("--public-url", default="https://voice.example.com")
    parser.add_argument("--wait", type=int, default=9)
    parser.add_argument("--echo", action="store_true", help="Mix delayed speaker output back into inbound telephone audio")
    parser.add_argument("--interrupt-audio", type=Path, help="Speak this WAV over the first response to test recovery")
    parser.add_argument("--signature-form", choices=("wss", "wss-slash", "https", "https-slash"), default="wss-slash",
                        help="Public URL form used to sign the WebSocket upgrade")
    asyncio.run(main(parser.parse_args()))
