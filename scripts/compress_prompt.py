#!/usr/bin/env python3
"""
Compress prompts with LLMLingua to cut token count before sending them
to any downstream LLM (OpenAI, Anthropic Claude, local models, etc.).

Usage:
    python3 scripts/compress_prompt.py -f prompts/input_prompt.md -o prompts/output_compressed_prompt.md
    python3 scripts/compress_prompt.py -f prompt.txt --instruction instr.txt --question q.txt
    echo "long prompt text..." | python3 scripts/compress_prompt.py --rate 0.4
    python3 scripts/compress_prompt.py -f prompt.txt --long-llmlingua --target-tokens 500
"""

import argparse
import sys
from pathlib import Path

import torch
from llmlingua import PromptCompressor

DEFAULT_MODEL = "microsoft/llmlingua-2-xlm-roberta-large-meetingbank"


def default_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def read_text(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return sys.stdin.read().strip()


def build_compressor(use_llmlingua1: bool, device: str) -> PromptCompressor:
    device_map = device if device == "cuda" else device  # llmlingua accepts "cuda"/"cpu"/"mps"
    if use_llmlingua1:
        return PromptCompressor(device_map=device_map)  # NousResearch/Llama-2-7b-hf (~13GB, slower, perplexity-based)
    return PromptCompressor(
        model_name=DEFAULT_MODEL,
        use_llmlingua2=True,
        device_map=device_map,
    )  # ~560MB token-classification model, fast, recommended default


def compress(
    compressor: PromptCompressor,
    prompt: str,
    instruction: str,
    question: str,
    rate: float,
    target_tokens: int | None,
    use_context_chunking: bool,
    use_long_llmlingua: bool,
) -> dict:
    kwargs = dict(
        context=[prompt],
        instruction=instruction,
        question=question,
        rate=rate,
        force_tokens=["\n", "?"],
    )
    if target_tokens is not None:
        kwargs["target_token"] = target_tokens
    if use_long_llmlingua:
        kwargs["condition_in_question"] = "after_condition"
        kwargs["reorder_context"] = "sort"
        kwargs["dynamic_context_compression_ratio"] = 0.3
        kwargs["condition_compare"] = True
        kwargs["context_budget"] = "+100"
        kwargs["rank_method"] = "longllmlingua"
    if use_context_chunking:
        kwargs["chunk_end_tokens"] = [".", "\n"]

    return compressor.compress_prompt(**kwargs)


def main():
    parser = argparse.ArgumentParser(description="Compress a prompt with LLMLingua.")
    parser.add_argument("-f", "--file", help="Path to the prompt file (defaults to stdin).")
    parser.add_argument("--instruction", help="Path to a file with system/task instructions to keep uncompressed.")
    parser.add_argument("--question", help="Path to a file with the trailing question/task to keep uncompressed.")
    parser.add_argument("--rate", type=float, default=0.5, help="Target fraction of tokens to keep (0-1). Default 0.5.")
    parser.add_argument("--target-tokens", type=int, default=None, help="Absolute token budget; overrides --rate if set.")
    parser.add_argument("--llmlingua1", action="store_true", help="Use the original LLMLingua-1 (Llama-2-7b perplexity model) instead of the fast LLMLingua-2 default. Much larger download, slower.")
    parser.add_argument("--long-llmlingua", action="store_true", help="Enable LongLLMLingua settings for long, multi-document contexts.")
    parser.add_argument("--chunk", action="store_true", help="Chunk context on sentence boundaries before compression.")
    parser.add_argument("-o", "--output", help="Write compressed prompt to this file instead of stdout.")
    parser.add_argument("--device", choices=["cuda", "mps", "cpu"], default=None, help="Device to run the compression model on. Defaults to auto-detect (cuda > mps > cpu).")
    args = parser.parse_args()

    prompt = read_text(args.file)
    if not prompt:
        parser.error("No prompt text provided (empty file/stdin).")

    instruction = read_text(args.instruction) if args.instruction else ""
    question = read_text(args.question) if args.question else ""

    device = args.device or default_device()
    compressor = build_compressor(use_llmlingua1=args.llmlingua1, device=device)

    result = compress(
        compressor,
        prompt,
        instruction,
        question,
        rate=args.rate,
        target_tokens=args.target_tokens,
        use_context_chunking=args.chunk,
        use_long_llmlingua=args.long_llmlingua,
    )

    compressed_prompt = result["compressed_prompt"]

    if args.output:
        Path(args.output).write_text(compressed_prompt, encoding="utf-8")
    else:
        print(compressed_prompt)

    print(
        f"\n--- stats ---\n"
        f"original tokens:   {result['origin_tokens']}\n"
        f"compressed tokens: {result['compressed_tokens']}\n"
        f"compression ratio: {result['ratio']}\n"
        f"saved:             {result.get('rate', 'n/a')}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
