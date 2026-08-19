# Conversation Compression Summary

Prompt: `prompts/conversation_prompt.json` (12-message LiteLLM-format conversation —
system prompt + 11 user/assistant turns, a multi-turn incident-debugging dialogue).
Compressed per-message with LLMLingua-2 at `--rate 0.5` via
`scripts/litellm_compress.py`. System message is excluded from compression by
default (kept intact for instruction fidelity); pass `--compress-system` to
include it.

## Per-message breakdown

| # | Role      | Tokens Before | Tokens After | Saved   |
|---|-----------|---------------:|--------------:|--------:|
| 0 | system    | 151            | 151            | 0.0% (not compressed) |
| 1 | user      | 303            | 141            | 53.5%   |
| 2 | assistant | 451            | 211            | 53.2%   |
| 3 | user      | 405            | 195            | 51.9%   |
| 4 | assistant | 529            | 244            | 53.9%   |
| 5 | user      | 344            | 162            | 52.9%   |
| 6 | assistant | 569            | 271            | 52.4%   |
| 7 | user      | 331            | 157            | 52.6%   |
| 8 | assistant | 556            | 265            | 52.3%   |
| 9 | user      | 229            | 110            | 52.0%   |
| 10 | assistant | 316           | 153            | 51.6%   |
| 11 | user      | 141           | 68             | 51.8%   |

## Totals

| Metric                    | Before Compression | After Compression | Change            |
|----------------------------|--------------------:|--------------------:|-------------------:|
| Total tokens (all 12 msgs) | 4,325                | 2,128                | -2,197 (-50.8%)    |
| Compressible-only tokens (11 user/assistant msgs) | 4,174 | 1,977 | -2,197 (-52.6%)    |
| Messages compressed        | 11 / 12               | —                    | system left intact |
| Cost (USD, Claude Opus 5, input side only) * | $0.02163 | $0.01064 | -$0.01099 (-50.8%) |

\* Estimated using Opus 5 input pricing ($5.00/1M tokens) on total input tokens
alone — this run hasn't been sent to a live model yet (`--send` not used), so
there's no output-token or response-quality comparison here, unlike the earlier
document-compression eval.

## Takeaways

- **Consistent ~52-54% per-message compression** across every user/assistant
  turn, regardless of turn length (shortest turn at 141 tokens, longest at 569
  — the savings rate barely moves), suggesting LLMLingua-2 is trimming
  proportionally rather than hitting a fixed floor.
- Compression held up well qualitatively on a conversational register too, not
  just the dense technical-document style tested earlier — spot-checking the
  output shows filler words, hedges ("I think", "just to be clear"), and
  restated context get stripped while technical nouns/verbs survive intact.
- The **system prompt is deliberately excluded** from compression by default,
  since it's short (151 tokens) and instruction-following fidelity there
  matters more than the token savings would justify.
- Not yet evaluated for downstream response quality — run with `--send --model
  ollama/llama3.2:3b` (free) or `--send --model claude-opus-5` (needs API
  credits) to see whether the compressed conversation still produces a
  coherent, on-topic reply to the final open-ended question in turn 11.

## Source files

- Original conversation: `prompts/conversation_prompt.json`
- Compressed conversation: `results/compressed_conversation.json`
- Compression script: `scripts/litellm_compress.py`
