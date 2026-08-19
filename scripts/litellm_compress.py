#!/usr/bin/env python3
"""
Compress LiteLLM-format chat messages with LLMLingua before sending them to
any LLM LiteLLM supports (Claude, OpenAI, Ollama, Bedrock, etc.) through one
interface.

Usage (CLI — compress a messages JSON file and optionally send it):
    python3 scripts/litellm_compress.py -f prompts/conversation_prompt.json --rate 0.5
    python3 scripts/litellm_compress.py -f prompts/conversation_prompt.json --rate 0.5 \
        --model ollama/llama3.2:3b --send

Usage (as a library):
    from litellm_compress import compress_messages, complete
    messages = [{"role": "user", "content": long_text}]
    compressed, stats = compress_messages(messages, rate=0.5)
    response, stats = complete(model="claude-opus-5", messages=messages, rate=0.5)
"""

import argparse
import json
import sys
from pathlib import Path

from llmlingua import PromptCompressor

DEFAULT_COMPRESSOR_MODEL = "microsoft/llmlingua-2-xlm-roberta-large-meetingbank"
COMPRESSIBLE_ROLES = ("user", "assistant")  # system prompts are kept intact by default

_compressor = None


def get_compressor():
    global _compressor
    if _compressor is None:
        import torch

        if torch.cuda.is_available():
            device = "cuda"
        elif torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"
        _compressor = PromptCompressor(
            model_name=DEFAULT_COMPRESSOR_MODEL,
            use_llmlingua2=True,
            device_map=device,
        )
    return _compressor


def compress_messages(messages: list[dict], rate: float = 0.5, compress_roles=COMPRESSIBLE_ROLES) -> tuple[list[dict], dict]:
    """Compress the content of each message whose role is in compress_roles.

    Returns (new_messages, stats) where stats has per-role and total token counts.
    Messages are never merged or reordered — only their text content shrinks.
    """
    compressor = get_compressor()
    new_messages = []
    tokens_before = tokens_after = 0
    compressed_count = 0

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role in compress_roles and isinstance(content, str) and content.strip():
            result = compressor.compress_prompt([content], rate=rate, force_tokens=["\n", "?"])
            new_messages.append({**msg, "content": result["compressed_prompt"]})
            tokens_before += result["origin_tokens"]
            tokens_after += result["compressed_tokens"]
            compressed_count += 1
        else:
            new_messages.append(dict(msg))

    stats = {
        "messages_compressed": compressed_count,
        "messages_total": len(messages),
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "tokens_saved": tokens_before - tokens_after,
        "savings_pct": (1 - tokens_after / tokens_before) * 100 if tokens_before else 0.0,
    }
    return new_messages, stats


def complete(model: str, messages: list[dict], rate: float = 0.5, compress_roles=COMPRESSIBLE_ROLES, **kwargs):
    """Compress messages, then call litellm.completion(). Drop-in for litellm.completion()."""
    import litellm

    compressed_messages, stats = compress_messages(messages, rate=rate, compress_roles=compress_roles)
    response = litellm.completion(model=model, messages=compressed_messages, **kwargs)
    return response, stats


def main():
    parser = argparse.ArgumentParser(description="Compress LiteLLM messages with LLMLingua, optionally send via LiteLLM.")
    parser.add_argument("-f", "--file", required=True, help="Path to a JSON file containing a LiteLLM messages array.")
    parser.add_argument("--rate", type=float, default=0.5, help="Target fraction of tokens to keep per message (0-1). Default 0.5.")
    parser.add_argument("--compress-system", action="store_true", help="Also compress system messages (kept intact by default).")
    parser.add_argument("-o", "--output", help="Write the compressed messages JSON here instead of stdout.")
    parser.add_argument("--model", help="LiteLLM model string (e.g. claude-opus-5, ollama/llama3.2:3b). Required with --send.")
    parser.add_argument("--send", action="store_true", help="Actually send the compressed messages via litellm.completion().")
    parser.add_argument("--max-tokens", type=int, default=4096)
    args = parser.parse_args()

    messages = json.loads(Path(args.file).read_text(encoding="utf-8"))
    compress_roles = COMPRESSIBLE_ROLES + (("system",) if args.compress_system else ())

    print(f"Compressing {len(messages)} messages (roles: {compress_roles}) at rate={args.rate}...", file=sys.stderr)
    compressed_messages, stats = compress_messages(messages, rate=args.rate, compress_roles=compress_roles)

    print(
        f"\n=== Compression stats ===\n"
        f"Messages compressed: {stats['messages_compressed']}/{stats['messages_total']}\n"
        f"Tokens: {stats['tokens_before']} -> {stats['tokens_after']} "
        f"(saved {stats['tokens_saved']}, {stats['savings_pct']:.1f}%)\n",
        file=sys.stderr,
    )

    output_json = json.dumps(compressed_messages, indent=2)
    if args.output:
        Path(args.output).write_text(output_json, encoding="utf-8")
        print(f"Wrote compressed messages to {args.output}", file=sys.stderr)
    else:
        print(output_json)

    if args.send:
        if not args.model:
            parser.error("--send requires --model")
        import litellm

        print(f"\nSending to {args.model}...", file=sys.stderr)
        response = litellm.completion(model=args.model, messages=compressed_messages, max_tokens=args.max_tokens)
        print("\n=== Response ===")
        print(response.choices[0].message.content)


if __name__ == "__main__":
    main()
