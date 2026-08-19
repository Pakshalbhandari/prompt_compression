#!/usr/bin/env python3
"""
Evaluate an LLMLingua-compressed prompt against its original by sending both
to an LLM and comparing response quality, token cost, and latency.

Backends:
    claude  - Anthropic API (needs ANTHROPIC_API_KEY, costs money)
    ollama  - local model via a running `ollama serve` (free, no key)

Usage (run from the project root):
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 scripts/eval_compression.py --backend claude --model claude-opus-5 --judge

    ollama serve &
    python3 scripts/eval_compression.py --backend ollama --model llama3.2:3b --judge
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

# $ per 1M tokens: (input, output) — Claude only; local models are always $0.
PRICING = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

DEFAULT_MODEL = {"claude": "claude-opus-5", "ollama": "llama3.2:3b"}


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def cost_usd(backend: str, model: str, input_tokens: int, output_tokens: int) -> float:
    if backend != "claude":
        return 0.0
    in_rate, out_rate = PRICING.get(model, PRICING["claude-opus-5"])
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


def call_claude(client, model: str, prompt: str) -> dict:
    import anthropic

    start = time.perf_counter()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.RateLimitError as e:
        retry_after = int(e.response.headers.get("retry-after", "60"))
        print(f"Rate limited. Retry after {retry_after}s.", file=sys.stderr)
        raise
    except anthropic.AuthenticationError:
        print("Invalid or missing ANTHROPIC_API_KEY.", file=sys.stderr)
        raise
    except anthropic.APIStatusError as e:
        print(f"API error ({e.status_code}): {e.message}", file=sys.stderr)
        raise
    except anthropic.APIConnectionError:
        print("Network error reaching the Anthropic API.", file=sys.stderr)
        raise
    latency = time.perf_counter() - start

    text = "".join(b.text for b in response.content if b.type == "text")
    return {
        "text": text,
        "latency": latency,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "stop_reason": response.stop_reason,
    }


def call_ollama(client, model: str, prompt: str) -> dict:
    import ollama

    # Ollama defaults num_ctx to 2048-4096 regardless of the model's max context,
    # which silently truncates long prompts. Size it to the prompt plus headroom
    # for the response, capped at llama3.2's 131072-token max.
    num_ctx = min(131072, max(4096, int(len(prompt.split()) * 1.6) + 4096))

    start = time.perf_counter()
    try:
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            options={"num_ctx": num_ctx},
        )
    except ollama.ResponseError as e:
        print(f"Ollama error ({e.status_code}): {e.error}", file=sys.stderr)
        if e.status_code == 404:
            print(f"Model not pulled locally. Run: ollama pull {model}", file=sys.stderr)
        raise
    except ConnectionError:
        print("Could not reach Ollama. Start it with: ollama serve", file=sys.stderr)
        raise
    latency = time.perf_counter() - start

    return {
        "text": response["message"]["content"],
        "latency": latency,
        "input_tokens": response.get("prompt_eval_count", 0),
        "output_tokens": response.get("eval_count", 0),
        "stop_reason": response.get("done_reason", "stop"),
    }


def call_model(backend: str, client, model: str, prompt: str) -> dict:
    return call_claude(client, model, prompt) if backend == "claude" else call_ollama(client, model, prompt)


def run_n(backend: str, client, model: str, prompt: str, runs: int) -> dict:
    calls = [call_model(backend, client, model, prompt) for _ in range(runs)]
    return {
        "text": calls[-1]["text"],  # keep the last response as representative
        "stop_reason": calls[-1]["stop_reason"],
        "input_tokens": calls[-1]["input_tokens"],
        "output_tokens": statistics.mean(c["output_tokens"] for c in calls),
        "latency_mean": statistics.mean(c["latency"] for c in calls),
        "latency_min": min(c["latency"] for c in calls),
        "latency_max": max(c["latency"] for c in calls),
    }


def judge(backend: str, client, model: str, original_response: str, compressed_response: str) -> str:
    judge_prompt = (
        "You will compare two AI responses that were generated from the same underlying "
        "task, but the second was produced from a token-compressed version of the prompt.\n\n"
        f"RESPONSE A (from original prompt):\n{original_response}\n\n"
        f"RESPONSE B (from compressed prompt):\n{compressed_response}\n\n"
        "Rate how well Response B preserves the substance, correctness, and usefulness of "
        "Response A on a 1-5 scale (5 = equivalent quality, 1 = compression broke the task). "
        "Give the score first as 'Score: N/5', then a one-sentence justification."
    )
    result = call_model(backend, client, model, judge_prompt)
    return result["text"]


def build_client(backend: str):
    if backend == "claude":
        import anthropic

        return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    import ollama

    return ollama.Client()


def main():
    parser = argparse.ArgumentParser(description="Evaluate LLMLingua compression via an LLM.")
    parser.add_argument("--original", default="prompts/input_prompt.md", help="Path to the original prompt.")
    parser.add_argument("--compressed", default="prompts/output_compressed_prompt.md", help="Path to the compressed prompt.")
    parser.add_argument("--backend", choices=["claude", "ollama"], default="claude", help="Which LLM to evaluate against.")
    parser.add_argument("--model", default=None, help="Model name for the chosen backend (defaults: claude-opus-5 / llama3.2:3b).")
    parser.add_argument("--runs", type=int, default=1, help="Repeat each call N times and average latency/output tokens.")
    parser.add_argument("--judge", action="store_true", help="Use the same backend/model to score response-quality preservation.")
    parser.add_argument("--save-original-response", default="results/sample_output.md", help="Where to save the original-prompt response (the reference/baseline answer). Pass '' to skip.")
    parser.add_argument("--save-compressed-response", default="results/compressed_output.md", help="Where to save the compressed-prompt response. Pass '' to skip.")
    args = parser.parse_args()

    model = args.model or DEFAULT_MODEL[args.backend]
    original_prompt = read_text(args.original)
    compressed_prompt = read_text(args.compressed)

    client = build_client(args.backend)

    print(f"Running {args.runs} call(s) per prompt against {args.backend}:{model}...", file=sys.stderr)
    original = run_n(args.backend, client, model, original_prompt, args.runs)
    compressed = run_n(args.backend, client, model, compressed_prompt, args.runs)

    original_cost = cost_usd(args.backend, model, original["input_tokens"], original["output_tokens"])
    compressed_cost = cost_usd(args.backend, model, compressed["input_tokens"], compressed["output_tokens"])

    print("\n=== Prompt Compression Evaluation ===")
    print(f"Backend: {args.backend}:{model}  (runs per prompt: {args.runs})\n")

    print(f"{'':20}{'Original':>15}{'Compressed':>15}{'Delta':>15}")
    print(f"{'Input tokens':20}{original['input_tokens']:>15}{compressed['input_tokens']:>15}"
          f"{compressed['input_tokens'] - original['input_tokens']:>15}")
    print(f"{'Output tokens':20}{original['output_tokens']:>15.1f}{compressed['output_tokens']:>15.1f}"
          f"{compressed['output_tokens'] - original['output_tokens']:>15.1f}")
    print(f"{'Latency mean (s)':20}{original['latency_mean']:>15.2f}{compressed['latency_mean']:>15.2f}"
          f"{compressed['latency_mean'] - original['latency_mean']:>15.2f}")
    print(f"{'Cost (USD)':20}{original_cost:>15.5f}{compressed_cost:>15.5f}"
          f"{compressed_cost - original_cost:>15.5f}")

    input_savings = (1 - compressed["input_tokens"] / original["input_tokens"]) * 100
    print(f"\nInput token savings: {input_savings:.1f}%")
    if args.backend == "claude":
        cost_savings = (1 - compressed_cost / original_cost) * 100 if original_cost else 0
        print(f"Cost savings:        {cost_savings:.1f}%")
    else:
        print("Cost savings:        n/a (local model, $0 either way)")

    print("\n--- Original-prompt response ---")
    print(original["text"])
    print("\n--- Compressed-prompt response ---")
    print(compressed["text"])

    if args.save_original_response:
        Path(args.save_original_response).write_text(original["text"], encoding="utf-8")
        print(f"\nSaved original-prompt response to {args.save_original_response}", file=sys.stderr)
    if args.save_compressed_response:
        Path(args.save_compressed_response).write_text(compressed["text"], encoding="utf-8")
        print(f"Saved compressed-prompt response to {args.save_compressed_response}", file=sys.stderr)

    if args.judge:
        print("\n--- LLM-as-judge: does the compressed response preserve quality? ---")
        verdict = judge(args.backend, client, model, original["text"], compressed["text"])
        print(verdict)


if __name__ == "__main__":
    main()
