#!/usr/bin/env python3
"""Losslessly restore BFL tensor layout for a native comparison of igen weights.

Use igen's Python. Original shards are read-only. Every block is round-tripped
through Diffusers' existing forward converter and compared bit-for-bit before
writing. Output is a new sharded safetensors model, not a new quantization.
"""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import re
import sys


GLOBAL = {
    "x_embedder": "img_in", "context_embedder": "txt_in",
    "time_guidance_embed.timestep_embedder.linear_1": "time_in.in_layer",
    "time_guidance_embed.timestep_embedder.linear_2": "time_in.out_layer",
    "time_guidance_embed.guidance_embedder.linear_1": "guidance_in.in_layer",
    "time_guidance_embed.guidance_embedder.linear_2": "guidance_in.out_layer",
    "double_stream_modulation_img.linear": "double_stream_modulation_img.lin",
    "double_stream_modulation_txt.linear": "double_stream_modulation_txt.lin",
    "single_stream_modulation.linear": "single_stream_modulation.lin",
    "proj_out": "final_layer.linear",
}
DOUBLE = {
    "attn.norm_q": "img_attn.norm.query_norm", "attn.norm_k": "img_attn.norm.key_norm",
    "attn.to_out.0": "img_attn.proj", "ff.linear_in": "img_mlp.0", "ff.linear_out": "img_mlp.2",
    "attn.norm_added_q": "txt_attn.norm.query_norm", "attn.norm_added_k": "txt_attn.norm.key_norm",
    "attn.to_add_out": "txt_attn.proj", "ff_context.linear_in": "txt_mlp.0", "ff_context.linear_out": "txt_mlp.2",
}
SINGLE = {"attn.to_qkv_mlp_proj": "linear1", "attn.to_out": "linear2",
          "attn.norm_q": "norm.query_norm", "attn.norm_k": "norm.key_norm"}


def restore(state):
    """Reconstruct native Flux tensor names and fused QKV weights without changing values."""
    import torch
    result, fused = {}, {}
    for name, value in state.items():
        match = re.fullmatch(r"(single_transformer_blocks|transformer_blocks)\.(\d+)\.(.+)\.(weight|bias)", name)
        if match:
            family, index, module, kind = match.groups()
            prefix = ("single_blocks." if family == "single_transformer_blocks" else "double_blocks.") + index + "."
            if family == "transformer_blocks" and module in ("attn.to_q", "attn.to_k", "attn.to_v", "attn.add_q_proj", "attn.add_k_proj", "attn.add_v_proj"):
                text = module.startswith("attn.add_")
                letter = module.split("_")[1] if text else module[-1]
                key = prefix + ("txt_attn" if text else "img_attn") + ".qkv." + kind
                fused.setdefault(key, {})[letter] = value
                continue
            mapped = (SINGLE if family == "single_transformer_blocks" else DOUBLE)[module]
            if ".norm." in "." + mapped:
                kind = "scale"
            result[prefix + mapped + "." + kind] = value
        else:
            module, kind = name.rsplit(".", 1)
            if module == "norm_out.linear":
                if kind != "weight":
                    raise ValueError("Unexpected final modulation bias")
                first, second = torch.chunk(value, 2, dim=0)
                result["final_layer.adaLN_modulation.1.weight"] = torch.cat((second, first), dim=0)
            else:
                result[GLOBAL[module] + "." + kind] = value
    for name, pieces in fused.items():
        if set(pieces) != {"q", "k", "v"}:
            raise ValueError("Incomplete QKV: " + name)
        result[name] = torch.cat([pieces[letter] for letter in "qkv"], dim=0)
    return result


def main():
    """Parse explicit command options and run the requested operation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--igen-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.igen_root))
    import torch
    from safetensors import safe_open
    from diffusers.loaders.single_file_utils import convert_flux2_transformer_checkpoint_to_diffusers
    from igen.convert.flux2 import Shards
    torch.set_num_threads(4)
    index = json.loads((args.source / "diffusion_pytorch_model.safetensors.index.json").read_text())["weight_map"]
    groups = {}
    for key in index:
        match = re.match(r"(single_transformer_blocks|transformer_blocks)\.(\d+)\.", key)
        group = (match[1], int(match[2])) if match else ("global", 0)
        groups.setdefault(group, []).append(key)
    args.output.mkdir(parents=True, exist_ok=False)
    writer = Shards(args.output, "model", limit=2**30)
    for group, keys in sorted(groups.items()):
        with ExitStack() as stack:
            readers = {file: stack.enter_context(safe_open(args.source / file, framework="pt", device="cpu"))
                       for file in set(index[key] for key in keys)}
            original = {key: readers[index[key]].get_tensor(key) for key in keys}
            restored = restore(original)
            checked = convert_flux2_transformer_checkpoint_to_diffusers(dict(restored))
            if set(checked) != set(original) or any(not torch.equal(checked[key], original[key]) for key in original):
                raise RuntimeError("Lossless conversion verification failed: " + str(group))
            writer.add({"model.diffusion_model." + name: tensor for name, tensor in restored.items()})
            print("VERIFIED", group, len(original), "input tensors", flush=True)
    count = writer.close()
    # Verify the bytes written, too; conversion is outside inference timings.
    written_index = json.loads((args.output / "model.safetensors.index.json").read_text())["weight_map"]
    for group, keys in sorted(groups.items()):
        with ExitStack() as stack:
            readers = {file: stack.enter_context(safe_open(args.source / file, framework="pt", device="cpu"))
                       for file in set(index[key] for key in keys)}
            original = {key: readers[index[key]].get_tensor(key) for key in keys}
            expected = restore(original)
            outputs = {written_index["model.diffusion_model." + name] for name in expected}
            saved = {file: stack.enter_context(safe_open(args.output / file, framework="pt", device="cpu")) for file in outputs}
            for name, value in expected.items():
                target = "model.diffusion_model." + name
                if not torch.equal(saved[written_index[target]].get_tensor(target), value):
                    raise RuntimeError("Written tensor differs: " + target)
    (args.output / "conversion.json").write_text(json.dumps({"source": str(args.source), "tensors": count,
        "verified": "Every input tensor round-tripped bit-for-bit; every output tensor read back and compared",
        "quantization_changed": False}, indent=2) + "\n")
    print("COMPLETE", count, "native-layout tensors; original weights unchanged", flush=True)


if __name__ == "__main__":
    main()
