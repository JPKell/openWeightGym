#!/usr/bin/env python3
"""Check after a deployment that video generation works end to end.

For each video model the key's account may use (or the ones named with --model), it:

1. reads the model from GET /v1/models and fails if it has no video profile, or if its
   qualification failed on the current runtime build (a pending re-qualification is reported,
   not failed: the earlier build's evidence is still in use);
2. makes one short text-only clip and one clip from a start image (--no-start-image skips it)
   through the public POST /v1/videos route, polls each to completion and downloads the WebM;
3. deletes each finished clip and its job, so nothing is left in the account's library.

It changes no settings and wakes no node: a model on a sleeping node fails the check. The API
key is read from --key-file and never printed. Exit status 0 means every clip completed.

    scripts/smoke-video.py --url http://10.77.10.42:8440 --key-file ~/.config/owg/qa-video.key
"""
import argparse
import base64
import io
import json
import sys
import time
from pathlib import Path

import httpx
from PIL import Image

PROMPT = "A red sailboat moving slowly across a calm lake, natural daylight."


def start_image(width, height):
    """A plain sky-over-water picture as a data URL, sized to the clip."""
    picture = Image.new("RGB", (width, height), (90, 140, 200))
    picture.paste((40, 80, 120), (0, height // 2, width, height))
    picture.paste((200, 40, 40), (width // 2 - width // 16, height // 2 - height // 8, width // 2 + width // 16, height // 2))
    output = io.BytesIO()
    picture.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def video_models(http, names):
    """(name, profile) of each video model to check."""
    answer = http.get("/v1/models")
    answer.raise_for_status()
    found = {one["id"]: one for one in answer.json()["data"] if (one.get("capabilities") or {}).get("video_generation")}
    if names:
        missing = [name for name in names if name not in found]
        if missing:
            raise SystemExit(f"Not offered to this key as a video model: {', '.join(missing)}")
        return [(name, found[name].get("video_workflow") or {}) for name in names]
    if not found:
        raise SystemExit("This key may use no video model.")
    return [(name, one.get("video_workflow") or {}) for name, one in found.items()]


def clip(http, body, timeout):
    """Queue one clip, wait for it, download it, delete it; (ok, detail)."""
    answer = http.post("/v1/videos", json=body)
    if answer.status_code >= 400:
        return False, f"refused: HTTP {answer.status_code} {answer.text[:300]}"
    job = answer.json()
    started = time.monotonic()
    try:
        while job.get("status") in ("queued", "in_progress"):
            if time.monotonic() - started > timeout:
                http.post(f"/v1/videos/{job['id']}/cancel")
                return False, f"still {job['status']} after {timeout} s; cancelled"
            time.sleep(5)
            job = http.get(f"/v1/videos/{job['id']}").json()
        if job.get("status") != "completed":
            return False, f"{job.get('status')}: {json.dumps(job.get('error'))[:300]}"
        content = http.get(f"/v1/videos/{job['id']}/content")
        if content.status_code != 200 or not content.content.startswith(b"\x1a\x45\xdf\xa3"):
            return False, f"content: HTTP {content.status_code}, {len(content.content)} bytes, not a WebM"
        return True, f"{len(content.content)} bytes in {time.monotonic() - started:.0f} s"
    finally:
        if job.get("status") not in ("queued", "in_progress"):
            http.delete(f"/v1/videos/{job['id']}")


def main():
    """Check each chosen video model, print one line per clip and exit non-zero on any failure."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:8440", help="console address of the control node")
    parser.add_argument("--key-file", type=Path, required=True, help="file holding an API key of an account that may make videos")
    parser.add_argument("--model", action="append", default=[], help="video model to check (repeatable; default: all offered)")
    parser.add_argument("--no-start-image", action="store_true", help="skip the clip made from a start image")
    parser.add_argument("--timeout", type=int, default=1800, help="seconds to wait for each clip")
    args = parser.parse_args()
    key = args.key_file.expanduser().read_text().strip()
    failed = False
    with httpx.Client(base_url=args.url.rstrip("/"), headers={"authorization": "Bearer " + key}, timeout=120) as http:
        for name, profile in video_models(http, args.model):
            state = profile.get("qualification") or {}
            if not profile:
                print(f"FAIL {name}: no video profile evidence")
                failed = True
                continue
            if state.get("failed"):
                print(f"FAIL {name}: {state['failed']}")
                failed = True
                continue
            if state.get("stale"):
                print(f"note {name}: runtime changed; {'re-qualification pending' if state.get('pending') else 'automatic re-qualification is off'}")
            normal = profile.get("defaults") or {}
            width, height = int(normal.get("width") or 832), int(normal.get("height") or 480)
            body = {"model": name, "prompt": PROMPT, "seconds": min(2.1, float((profile.get("limits") or {}).get("max_seconds") or 2.1)),
                    "width": width, "height": height, "seed": 42}
            runs = [("text", body)] + ([] if args.no_start_image else [("start image", {**body, "input_reference": start_image(width, height)})])
            for label, request in runs:
                ok, detail = clip(http, request, args.timeout)
                print(f"{'ok  ' if ok else 'FAIL'} {name} {label}: {detail}", flush=True)
                failed |= not ok
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
