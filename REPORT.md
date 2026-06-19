# REPORT

## Local development setup note

Local development was done under WSL2 and opened in VS Code via Remote - WSL. The assignment repo runs locally first, with the hosted API kept only as a fallback and the final benchmark/tuning work deferred to a hosted GPU VM.

The local stand-in inference server runs from the repo environment with vLLM on the laptop GPU, not on CPU. The machine exposes one NVIDIA GeForce RTX 3050-class GPU to WSL with 4 GB VRAM, which is far too small for the target assignment model, so local testing uses `Qwen/Qwen3-0.6B` as a functional stand-in for graph wiring, metrics, and general request flow.

Working local vLLM command:

```bash
uv run python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-0.6B \
  --host 0.0.0.0 \
  --port 8000 \
  --gpu-memory-utilization 0.7 \
  --max-model-len 2560
```

Local constraints and adjustments:

- The repo initially resolved `transformers` to a 5.x release, which was incompatible with `vllm 0.10.2` for this Qwen3 path. Pinning `transformers` to `<5` fixed tokenizer compatibility.
- The default vLLM memory target was too aggressive for a 4 GB GPU, so `gpu_memory_utilization` had to be reduced.
- The model's default context window was unnecessarily large for local iteration and increased KV-cache pressure, so `max_model_len` was reduced to `2560`.

This local configuration is only intended to unblock development of the agent, Prometheus metrics, and tracing flow. It is not representative of the final serving configuration, and none of its latency or throughput characteristics should be used for the final SLO or quality claims.


## Phase 1 serving configuration on the H100 VM

Hosted vLLM launch command:

```bash
/home/ubuntu/.local/bin/uv run python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-30B-A3B-Instruct-2507 \
  --host 0.0.0.0 \
  --port 8000 \
  --reasoning-parser qwen3 \
  --generation-config vllm \
  --gpu-memory-utilization 0.9 \
  --max-model-len 12288 \
  --max-num-seqs 50
```

Flag rationale:

- `--model Qwen/Qwen3-30B-A3B-Instruct-2507`: the assignment's defined target model
- `--host 0.0.0.0`: exposes vLLM both locally on the VM as well as reachable externally, to allow SSH port forwarding
- `--port 8000`: per the repo's default endpoint wiring for Prometheus scraping and the agent client.
- `--reasoning-parser qwen3`: enables the Qwen3 reasoning-format parser, to allow clean separation between text and reasoning in the API response
- `--generation-config vllm`: use vLLM's defaults for model serving, not the ones from HF, in order to make the setup explicit
- `--gpu-memory-utilization 0.9`: Uses ~90% of total GPU memory for the model runtime, mainly when sizing KV cache and related allocations. This is the recommended initial value for this setting in the docs, and there was no need to further tune it per the results below.
- `--max-model-len 12288`: gives enough headroom for the agent's schema-heavy prompts without paying the extra KV-cache cost of a much larger context window. Based on the data provided about expected query lengths, plus headroom. Could likely have been reduced somewhat, but results below show that was not a priority.
- `--max-num-seqs 50`: limits the number of concurrent sequences vLLM processes simultaneously. This was added during optimization phases to improve throughput.

## Baseline eval
Final eval summary from `results/eval_after_tuning.json`:

- Questions: `30`
- Final accuracy: `0.4333`
- Per-iteration accuracy:
  - `iter_0`: `0.3667`
  - `iter_1`: `0.4000`
  - `iter_2`: `0.4333`
- Average iterations: `1.6`
- Revised questions: `12`

These values suggest that the loop has some effect, but not enough to justify
the overhead. Every iteration of the loop adds little in terms of quality at
the cost of doubling and sometimes tripling the number of LLM calls.

## Agent loop optimization: early exit on stalled revise

One graph optimization was added to the agent loop: if the `revise` step
returns the same SQL it received, the graph now exits immediately instead of
running another `execute -> verify` cycle on an unchanged query. This does not
change answer quality, it only avoids wasted work once the loop has
stalled.

To measure the effect, I compared two full eval runs in Langfuse using trace
latency aggregated over the 30 question traces in each run:

- Baseline run tag: `propmt-tightening-6`
- Optimized run tag: `graph-exit-after-no-revise`

Langfuse comparison:

- `propmt-tightening-6`: total trace latency `50.173s`, average per-question
  trace latency `1.672s`, median `1.145s`, trace-span `51.746s`
- `graph-exit-after-no-revise`: total trace latency `34.498s`, average
  per-question trace latency `1.150s`, median `0.777s`, trace-span `36.060s`

Observed improvement from the optimization:

- Total trace latency improved by `15.675s` (`31.2%` lower)
- Average per-question trace latency improved by `0.523s` (`31.2%` lower)
- End-to-end trace span improved by `15.686s` (`30.3%` lower)

Interpretation:

- The optimization behaves as intended: it cuts wasted loop work when the reviser is stuck and produces no substantive SQL change.
- This latency win does not change the quality of results at all. 


## Runtime optimization phases

### Baseline
The load test on the initial serving configuration was very far from meeting the SLO.
Full results are in `results/load_test_phase6_baseline_rps10.json`.
"achieved_rps": 8.333240031785369 < 10
"latency_p95": 106.59788288000004 >> 5
Only 255/3000 = 8.5% of queries completed successfully (the rest were subject to timeout/load related errors)

### 1
Saw: Grafana shows that from a certain point, KV cache memory usage reaches 100% and from that point on preemptions start happening and requests start getting queued (see `screenshots/grafana_before.png`).
Hypothesis: vLLM is running too many requests in parallel. Inspecting the Running Requests graph shows the KV cache got to 100% at 35 concurrent requests.
Change: vLLM limited to run 35 concurrent requests at most (`--max-num-seqs 35`)
Result: Grafana shows the expected results: preemptions dropped to zero and KV cache no longer reaches 100% usage (see `screenshots/phase6-iter-1-grafana-after.png`). Essentially, the previous overload pattern was resolved (we even went to the other extreme now, the server is underloaded as can be seen from KV cache usage not exceeding 65% at any time). However, results themselves are still not improved. `achieved_rps` remains almost the same (`8.333245542892458`) and latency is even worse now: `latency_p95 = 116.82494848199894`. More significantly, it is now clear that 500 errors point to a real correctness issue on the agent side. Inspecting Langfuse is required to understand the cause of failures.

### 2
Saw: Langfuse log analysis surfaced two issues: 1. a crash introduced in a previous commit 2. requests failing because of "too many open file descriptors" due to the fact the agent implementation is initializing a new connection per LLM request instead of pooling connections.
Hypothesis: these issues are causing many requests to crash/fail before they even reach the LLM, so it completely changes the latency/throughput landscape. After it is fixed we need to remeasure to see what the real baseline is.
Change: issues fixed
Result: agent errors disappeared, but SLO is still not met, neither in terms of throughput nor P95 latency

### 3
Saw: A large number of very long-running agent queries.
Hypothesis: Analyzing the Langfuse logs shows two issues: 1. a lot of time is sometimes spent in the verify and revise LLM queries 2. SQL execution sometimes takes a very long time, occasionally even more than a minute for a single running query.
Change: Optimized prompts + added a hard timeout of 5s on SQL execution. After the timeout the executing SQL query is aborted.
Result: A meaningful improvement, but it still misses a `p95 <= 5s` latency target, even when running with `--rps 1`.

### 4
Saw: A long tail of long-running agent LLM queries in the verify and revise nodes. Despite the prompt which asks for brief answers, these sometimes generate a lot of tokens.
Hypothesis: Capping the max output tokens of the LLM will prevent this long tail from happening without hurting quality (long answers are likely not that useful anyway).
Change: `max_completion_tokens = 512` for all agent-initiated LLM calls. Another change made was to improve the mechanism for killing long SQL queries to avoid overrun above the 5s cap which occasionally happened. Now "max 5s" is really 5s, not "usually 5s but sometimes 10s".
Result: A meaningful improvement, but it still misses a `p95 <= 5s` latency target, even when running with `--rps 1`. Naturally, running with `rps=10` also fails. However, the Grafana dashboard shows clearly that vLLM is not saturated and even has headroom for improvement, since KV cache is not saturated (only reaches ~60%; see `screenshots/phase6-iter-4-grafana-after.png`).
The quality of the results did not change significantly following these optimizations. Final accuracy is now `0.4` vs. `0.43` previously.

### 5
Saw: Grafana dashboard shows vLLM is underutilized. KV cache is only ~45% used at the peak, zero preemptions but some queries are waiting in queue.
Hypothesis: --max-num-seqs is too low
Change: Increased --max-num-seqs from 35 to 50
Result: vLLM usage increased only a little (see `screenshots/phase6-iter-5-grafana-after.png`). KV cache usage went up `45 -> 55`, concurrent requests went up `35 -> 40`. This shows that the vLLM serving is not the bottleneck.
The bottleneck is the agent service, which suffocates under the load. 

### 6
Saw: the bottleneck revolves around the agent loop: too many loops, too long inference time for each turn. The ideal solution is to further optimize the agent itself (prompts and loop logic).
Hypothesis: That said, switching to a quantized model can cut LLM inference time, moving us closer to the SLA. The risk is that reduced quality may generate more loops, which is bad for the SLO. Quality degradation of answers in general is also a risk. But we have the eval to measure.
Change: --quantization bitsandbytes and --dtype bfloat16
Result: Memory usage decreased significantly as expected, but inference speed did not improve and even regressed somewhat. Change reverted.

## Summary 
Bottom line: SLO was not achieved. Using the final non-quantized configuration described above, achieved throughput was `8.33 RPS` versus the `10.0 RPS` target, a shortfall of `1.67 RPS` (`16.7%` below target). P95 end-to-end latency was `117.37s` versus the `5.0s` target, missing by `112.37s` (`23.5x` over target).
Main reason: long tail of long-running agent requests (verify, execute SQL, revise). The correct optimization path is to improve the agent itself. The vLLM setup looks close to optimal at this point and cannot be further optimized or even effectively utilized (e.g. KV cache does not exceed 55%) because of agent-side issues.

## Future
Given more time, I would focus on the core problem - long agent loops. I would work on the prompts to make the agent converge in fewer iterations, and fail fast when it is not converging. Examples:
- Improve first-try SQL quality
- Make verify and revise much stricter and more targeted
- Optimize loop: stop if the loop is not productive, e.g. revise is not materially changing the query or verify repeats the same complaint
- Once the above are resolved, if vLLM becomes the bottleneck, things that would make sense to try are:
  - optimize `max_num_seqs`, model quantization, KV-cache quantization
