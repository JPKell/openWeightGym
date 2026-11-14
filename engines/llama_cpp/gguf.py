"""Reading what a model file says about itself.

A .gguf file begins with a list of named facts: what the model is called, how it is built, how
much context it was trained for, its chat template. This reads that list and nothing more. The
weights that follow, which are nearly all of the file, are never scanned or decoded. Metadata
uses small read buffers, so a 20 GB file is inspected as quickly as a small one.

The layout is fixed by llama.cpp: four letters, three counts, then the facts one after another.
Each fact is a name, a number saying what kind of value follows, and the value.
"""

from __future__ import annotations

import re
import struct

# How each kind of single value is laid out, by the number the format gives that kind.
SINGLE = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
TEXT, LIST = 8, 9
SHORT = 16  # a list longer than this is counted, not kept: a vocabulary has a hundred thousand entries
SANE = 2**28  # no name or text in a real file is longer; a longer one means the file is not what it claims


def read_metadata(path: str, count_parameters: bool = False, shapes: bool = False) -> dict:
    """Read GGUF facts, optionally counting tensor elements or listing tensor shapes without reading weights.

    A single shard cannot establish the whole model's parameter count. For split files only
    an explicitly declared count is returned. With `shapes`, `tensors` maps each tensor name to
    its dimensions in GGUF order (innermost first). Raises ValueError for a malformed header.
    """
    with open(path, "rb") as file:

        def take(layout: str):
            """Read exactly the requested binary value, rejecting truncated GGUF input."""
            data = file.read(struct.calcsize(layout))
            if len(data) != struct.calcsize(layout):
                raise ValueError("the file ends in the middle of its own description")
            return struct.unpack(layout, data)[0]

        def text(keep: bool = True) -> str | None:
            """Read a length-prefixed UTF-8 GGUF metadata string."""
            length = take("<Q")
            if length > SANE:
                raise ValueError("it does not read as a .gguf file")
            if keep:
                return file.read(length).decode("utf-8", "replace")
            file.seek(length, 1)  # stepped over, not read
            return None

        def value(kind: int):
            """Decode or skip one typed GGUF metadata value."""
            if kind in SINGLE:
                return take(SINGLE[kind])
            if kind == TEXT:
                return text()
            if kind == LIST:
                inner, count = take("<I"), take("<Q")
                if count <= SHORT:
                    return [value(inner) for _ in range(count)]
                if inner in SINGLE:
                    file.seek(struct.calcsize(SINGLE[inner]) * count, 1)
                elif inner == TEXT:
                    # Scan lengths in bounded blocks, avoiding a read/seek pair for
                    # each of the millions of vocabulary strings in a model library.
                    start, offset, block = file.tell(), 0, b""
                    length_of = struct.Struct("<Q").unpack_from
                    for _ in range(count):
                        if offset + 8 > len(block):
                            file.seek(start + offset)
                            start += offset
                            block, offset = file.read(64 * 1024), 0
                            if len(block) < 8:
                                raise ValueError("the file ends in the middle of its own description")
                        length = length_of(block, offset)[0]
                        if length > SANE:
                            raise ValueError("it does not read as a .gguf file")
                        offset += 8 + length
                    file.seek(start + offset)
                else:
                    raise ValueError("a long list of lists, which is not expected here")
                return f"{count} values"
            raise ValueError(f"a kind of value this does not know, number {kind}")

        if file.read(4) != b"GGUF":
            raise ValueError("it is not a .gguf file")
        take("<I")  # the format's version
        tensors = take("<Q")
        count = take("<Q")
        if count > 100_000:
            raise ValueError("it does not read as a .gguf file")
        metadata = {text(): value(take("<I")) for _ in range(count)}
        if shapes:
            if tensors > 1_000_000:
                raise ValueError('too many tensor descriptions')
            found = {}
            for _ in range(tensors):
                name = text()
                dimensions = take('<I')
                if not 1 <= dimensions <= 4:
                    raise ValueError('invalid tensor dimensions')
                found[name] = tuple(take('<Q') for _ in range(dimensions))
                take('<I')  # tensor storage type
                take('<Q')  # offset of its data
            metadata['tensors'] = found
            return metadata
        if count_parameters and 'general.parameter_count' not in metadata and metadata.get('split.count', 1) == 1 and tensors:
            if tensors > 1_000_000:
                raise ValueError('too many tensor descriptions')
            parameters = 0
            for _ in range(tensors):
                text(keep=False)
                dimensions = take('<I')
                if not 1 <= dimensions <= 4:
                    raise ValueError('invalid tensor dimensions')
                elements = 1
                for _ in range(dimensions):
                    elements *= take('<Q')
                parameters += elements
                take('<I')  # tensor storage type
                take('<Q')  # offset of its data
            metadata['general.parameter_count'] = parameters
        return metadata


def goes_with(part: dict, model: dict) -> bool:
    """True if a part's file says it was made for the model that a model's file says it is. Both are given as their `facts`.

    It goes by names, since a file holds nothing surer: letters and digits are compared, and
    for an adapter the way the model is built as well. So it says what the files claim, not
    that the part will load. llama-server finds that out.
    """
    plain = lambda text: re.sub(r"[^a-z0-9]", "", str(text or "").lower())  # noqa: E731
    named = bool(plain(part.get("made_for"))) and plain(part.get("made_for")) == plain(model.get("name"))
    return named and (part.get("what") != "adapter" or part.get("architecture") == model.get("architecture"))


def facts(metadata: dict) -> dict:
    """The few facts worth showing at a glance, picked out of everything a file says."""
    build = metadata.get("general.architecture", "")
    template = metadata.get("tokenizer.chat_template") or ""
    kind = metadata.get("general.type")
    # What the file is for LoadCoach: a model, or a part that goes onto one. A projector, which lets a model see, is called several things by those who make them.
    what = "adapter" if kind == "adapter" else "projector" if kind in ("mmproj", "clip-vision", "projector") or build == "clip" else "model"
    decision_type = metadata.get(f"{build}.decision.type")
    systemone = bool(metadata.get('tokenizer.chat_template.systemone'))
    if decision_type and systemone and what == 'model':
        what = 'classifier'
    # The model a part says it was made for. An adapter names the model it was trained on, sometimes with a path before the name. A projector carries its model's own name.
    made_for = str(metadata.get("general.base_model.0.name") or "").rsplit("/", 1)[-1] if what == "adapter" else metadata.get("general.name") if what == "projector" else None
    picked = {
        "name": metadata.get("general.name"),
        "kind": kind,
        "what": what,
        "made_for": made_for or None,
        "architecture": build or None,
        "size_label": metadata.get("general.size_label"),
        "trained_context": metadata.get(f"{build}.context_length"),
        "layers": metadata.get(f"{build}.block_count"),
        "license": metadata.get("general.license"),
        # What its chat template can do, read off the template's own text.
        "chat_template": bool(template),
        "takes_tools": "tools" in template,
        "takes_images": "image" in template,
        "thinking_switch": "enable_thinking" in template,
        "decision_type": decision_type,
        "systemone": systemone if decision_type else None,
        "decision_temperatures": {key.removeprefix(f'{build}.decision.temperature.'): value
                                  for key, value in metadata.items() if key.startswith(f'{build}.decision.temperature.')} if decision_type else None,
        "decision_tokenizer_pre": metadata.get('tokenizer.ggml.pre') if decision_type else None,
        "decision_metaspace_prepend": metadata.get('tokenizer.ggml.metaspace.prepend_scheme') if decision_type else None,
        "decision_max_head_tokens": metadata.get(f'{build}.decision.max_head_tokens'),
        'decision_max_choices': metadata.get(f'{build}.decision.max_choices'),
    }
    return {name: value for name, value in picked.items() if value is not None}
