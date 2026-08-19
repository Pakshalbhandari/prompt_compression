# Prompt Compression Summary

Prompt: `prompts/input_prompt.md` (design review packet — background doc + 40+ code
review comments + meeting transcript + task). Compressed with LLMLingua-2 at
`--rate 0.4` into `prompts/output_compressed_prompt.md`. Both evaluated against
`llama3.2:3b` via Ollama.

| Metric              | Before Compression | After Compression | Change            |
|---------------------|--------------------:|-------------------:|-------------------:|
| Input tokens         | 14,808              | 6,151               | -8,657 (-58.5%)    |
| Output tokens        | 687.0                | 603.0               | -84.0 (-12.2%)     |
| Latency (s)          | 89.26                | 35.73               | -53.53 (-60.0%)    |
| Cost (USD)           | $0.00000 *           | $0.00000 *          | n/a (local model)  |
| Response quality (LLM-as-judge, 1-5, vs. original) | 5 (reference) | 3 | -2 |

\* Cost is $0 because this run used a local Ollama model. On Claude Opus 5
pricing ($5.00/$25.00 per 1M tokens), the same token counts would cost
approximately **$0.0913** before compression vs **$0.0458** after — a 49.8%
cost reduction.

## Takeaways

- **Token savings: 58.5%** input tokens removed, roughly matching the
  compression ratio LLMLingua reported (2.4x) at compress time.
- **Latency dropped 60%**, mostly from the shorter prefill.
- **Quality dropped to 3/5** ("mostly preserved, loses some nuance") — the
  compressed response kept the same 5 risks and overall structure, but
  genericized the per-engineer ownership detail that the original correctly
  pulled from the meeting transcript.
- At this rate (0.4) on a dense, attribution-heavy document, compression trades
  a meaningful chunk of specific detail for cost/latency. For docs like this,
  a milder rate (e.g. `--rate 0.6`) is worth testing if fidelity matters more
  than the savings.

## Source files

- Original prompt: `prompts/input_prompt.md`
- Compressed prompt: `prompts/output_compressed_prompt.md`
- Original-prompt response (reference): `results/sample_output.md`
- Compressed-prompt response: `results/compressed_output.md`
