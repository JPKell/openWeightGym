"""Explicit lossless storage for raw text that PostgreSQL TEXT cannot represent."""

import base64


def pack_text(value):
    """Encode retained text in the portable compressed storage format."""
    if "\0" in value:
        return base64.b64encode(value.encode("utf-8")).decode("ascii"), "base64-utf8"
    return value, ""


def unpack_text(value, codec):
    """Decode stored text while preserving compatibility with older records."""
    if not codec:
        return value
    if codec != "base64-utf8":
        raise ValueError("Unrecognized stored text encoding")
    return base64.b64decode(value, validate=True).decode("utf-8")
