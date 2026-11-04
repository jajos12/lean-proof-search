"""Pinned full-precision reference inference, loaded lazily and cached locally."""

import gc
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import time

from ..artifacts import _runtime

REFERENCE = "deepseek-ai/DeepSeek-Prover-V2-7B"
REVISION = "a8d9e14432b2e8dd9df2a4d4e70f1ba9bc8d9b7b"
PLAN = (
    "Before producing the Lean 4 code to formally prove the given theorem, provide a detailed proof plan "
    "outlining the main proof steps and strategies.\nThe plan should highlight key ideas, intermediate "
    "lemmas, and proof structures that will guide the construction of the final formal proof."
)


def _preflight(args):
    result = {"runtime": _runtime()}
    missing = [name for name in ("torch", "transformers", "accelerate") if result["runtime"][name] is None]
    if missing:
        raise RuntimeError("Missing Python dependencies: " + ", ".join(missing)
                           + "; install the inference extra in a writable Python environment")
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA-enabled PyTorch required; inspect the user environment before installing")
    device = torch.device(args.device)
    if device.type != "cuda":
        raise ValueError("Reference control requires an explicit CUDA device, without CPU offload")
    free, total = torch.cuda.mem_get_info(device)
    result.update(gpu=torch.cuda.get_device_name(device), free_vram_gib=free / 2**30,
                  total_vram_gib=total / 2**30, dtype=args.dtype)
    if free < 18 * 2**30:
        raise RuntimeError(f"Reference control needs ~18 GiB free VRAM; available {free / 2**30:.2f} GiB")
    with torch.cuda.device(device):
        if args.dtype == "bfloat16" and not torch.cuda.is_bf16_supported():
            raise RuntimeError("BF16 unsupported on selected setup; use --dtype float16")
    cache = args.hf_cache or Path(os.environ.get("HF_HOME", Path.home() / ".cache/huggingface")) / "hub"
    while not cache.exists():
        cache = cache.parent
    disk = shutil.disk_usage(cache).free
    result["free_cache_disk_gib"] = disk / 2**30
    if disk < args.min_free_disk_gib * 2**30:
        raise RuntimeError("Insufficient cache disk for reference weights; inspect space before downloading")
    return result


class ReferenceGenerator:
    """One frozen reference load, reusable across independent prompts/tasks."""

    def __init__(self, args, tokenizer):
        self.args, self.tokenizer, self.model = args, tokenizer, None

    def __enter__(self):
        import torch
        from packaging.version import Version
        from transformers import AutoModelForCausalLM
        started = time.monotonic()
        precision_key = ("dtype" if Version(importlib.metadata.version("transformers")) >= Version("4.56")
                         else "torch_dtype")
        self.model = AutoModelForCausalLM.from_pretrained(
            REFERENCE, revision=REVISION, cache_dir=self.args.hf_cache,
            **{precision_key: getattr(torch, self.args.dtype)}, device_map={"": self.args.device},
            attn_implementation="sdpa", trust_remote_code=False,
            local_files_only=getattr(self.args, "local_files_only", False),
        ).eval()
        try:
            if getattr(self.model, "is_loaded_in_4bit", False) or getattr(self.model, "is_loaded_in_8bit", False):
                raise RuntimeError("Reference model must not be quantized")
            if any(p.device.type != "cuda" for p in self.model.parameters()):
                raise RuntimeError("Reference model unexpectedly offloaded")
        except Exception:
            self.close()
            raise
        print(json.dumps({"reference_load_seconds": round(time.monotonic() - started, 3),
                          "parameter_dtypes": sorted({str(p.dtype) for p in self.model.parameters()})}), flush=True)
        return self

    def generate(self, input_ids, seed):
        import torch
        from transformers import GenerationConfig, set_seed
        args, tokenizer = self.args, self.tokenizer
        inputs = torch.tensor([input_ids], device=args.device)
        generation = GenerationConfig(
            do_sample=True, num_beams=1, max_new_tokens=args.max_new_tokens,
            temperature=0.6, top_p=0.95, top_k=50, min_p=0.0,
            repetition_penalty=1.0, no_repeat_ngram_size=0,
            eos_token_id=tokenizer.eos_token_id, pad_token_id=tokenizer.eos_token_id,
            bos_token_id=tokenizer.bos_token_id, use_cache=True, max_time=args.request_timeout,
        )
        set_seed(seed)
        torch.cuda.synchronize(args.device)
        started = time.monotonic()
        with torch.inference_mode():
            output = self.model.generate(input_ids=inputs, attention_mask=torch.ones_like(inputs),
                                         generation_config=generation)
        torch.cuda.synchronize(args.device)
        tokens = output[0, len(input_ids):].tolist()
        ended = bool(tokens) and tokens[-1] == tokenizer.eos_token_id
        return {"seed": seed, "status": "generated",
                "raw_text": tokenizer.decode(tokens, skip_special_tokens=True),
                "wall_seconds": round(time.monotonic() - started, 3),
                "input_tokens": len(input_ids), "input_token_count_matches": True,
                "output_tokens": len(tokens),
                "done_reason": "stop" if ended else "length" if len(tokens) >= args.max_new_tokens else "time_limit"}

    def close(self):
        import torch
        self.model = None
        gc.collect()
        torch.cuda.empty_cache()

    def __exit__(self, *args):
        self.close()
