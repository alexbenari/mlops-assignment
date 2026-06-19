# REPORT

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
- `--gpu-memory-utilization 0.9`: Uses ~90% of total GPU memory for the model runtime, mainly when sizing KV cache and related allocations. This is the recommended initial value for this setting in the docs. I kept it fixed across the reported runs because the submitted results did not indicate it was the first bottleneck to investigate.
- `--max-model-len 12288`: gives enough headroom for the agent's schema-heavy prompts without paying the extra KV-cache cost of a much larger context window. Based on the data provided about expected query lengths, plus headroom. Could likely have been reduced somewhat, but results below show that was not a priority.
- `--max-num-seqs 50`: limits the number of concurrent sequences vLLM processes simultaneously. This was added during optimization phases to improve throughput.

## Baseline eval
Baseline eval summary from `results/eval_baseline.json`:

- Questions: `30`
- Final accuracy: `0.4000`
- Per-iteration accuracy:
  - `iter_0`: `0.3667`
  - `iter_1`: `0.4000`
  - `iter_2`: `0.4000`
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
- This is directly relevant to the assignment's quality-vs-latency tradeoff: it reduces end-to-end latency by removing useless loop work while preserving answer quality, so it is exactly the kind of agent-side optimization that helps move the system toward the Phase 6 SLO without paying a quality penalty.


## Runtime optimization phases

Submission note: instead of a single `screenshots/grafana_after.png`, this submission includes three Phase 6 "after" snapshots: `screenshots/phase6-iter-1-grafana-after.png`, `screenshots/phase6-iter-4-grafana-after.png`, and `screenshots/phase6-iter-5-grafana-after.png`. Each is cited above at the iteration where it was used as evidence.

### Baseline
Saw: The initial Phase 6 load test missed the SLO badly: `achieved_rps = 8.333240031785369 < 10` and `latency_p95 = 106.59788288000004s >> 5s` (`results/load_test_phase6_baseline_rps10.json`).
Hypothesis: The stack is overloaded, but Grafana is needed to determine whether the first bottleneck is vLLM concurrency or the agent.
Change: None. This run establishes the baseline.
Result: Only `255/3000` requests completed successfully (`8.5%`), confirming the baseline is far from acceptable.

### 1
Saw: Grafana shows that from a certain point, KV cache memory usage reaches 100% and from that point on preemptions start happening and requests start getting queued (see `screenshots/grafana_before.png`).
Hypothesis: vLLM is running too many requests in parallel. Inspecting the Running Requests graph shows the KV cache got to 100% at 35 concurrent requests.
Change: Reduced vLLM concurrency to `--max-num-seqs 35`. Grafana showed KV-cache saturation and the onset of preemptions at roughly 35 concurrent running requests, so I used that as the cap.
Result: Preemptions dropped to zero and KV cache stopped saturating (see `screenshots/phase6-iter-1-grafana-after.png`), but end-to-end performance did not improve: `achieved_rps = 8.333245542892458` and `latency_p95 = 116.82494848199894s`. This ruled out KV-cache pressure as the main remaining SLO blocker.

### 2
Saw: Langfuse log analysis surfaced two issues: 1. a crash introduced in a previous commit 2. requests failing because of "too many open file descriptors" due to the fact the agent implementation is initializing a new connection per LLM request instead of pooling connections.
Hypothesis: these issues are causing many requests to crash/fail before they even reach the LLM, so it completely changes the latency/throughput landscape. After it is fixed we need to remeasure to see what the real baseline is.
Change: Fixed the crash and reused/pool-managed LLM client connections.
Result: Those specific agent failures disappeared, but the SLO was still missed, so deeper latency issues remained in the agent path.

### 3
Saw: A large number of very long-running agent queries.
Hypothesis: Analyzing the Langfuse logs shows two issues: 1. a lot of time is sometimes spent in the verify and revise LLM queries 2. SQL execution sometimes takes a very long time, occasionally even more than a minute for a single running query.
Change: Optimized prompts + added a hard timeout of 5s on SQL execution. After the timeout the executing SQL query is aborted.
Result: Latency improved meaningfully, but the system still missed `p95 <= 5s`, even when running with `--rps 1`. This showed the long tail was reduced, not eliminated.

### 4
Saw: A long tail of long-running agent LLM queries in the verify and revise nodes. Despite the prompt which asks for brief answers, these sometimes generate a lot of tokens.
Hypothesis: Capping the max output tokens of the LLM will prevent this long tail from happening without hurting quality (long answers are likely not that useful anyway).
Change: `max_completion_tokens = 512` for all agent-initiated LLM calls. Another change made was to improve the mechanism for killing long SQL queries to avoid overrun above the 5s cap which occasionally happened. Now "max 5s" is really 5s, not "usually 5s but sometimes 10s".
Result: Latency improved again, but the system still missed `p95 <= 5s`, even when running with `--rps 1`, and `rps=10` still failed. Grafana also showed vLLM still had headroom, with KV cache only reaching about `60%` (see `screenshots/phase6-iter-4-grafana-after.png`). Quality survived these changes and improved slightly: the required post-tuning eval artifact, `results/eval_after_tuning.json`, reached `0.4333` final accuracy versus `0.4000` in `results/eval_baseline.json`, with the gain coming from `iter_2` improving from `0.4000` to `0.4333`.

### 5
Saw: Grafana dashboard shows vLLM is underutilized. KV cache is only ~45% used at the peak, zero preemptions but some queries are waiting in queue.
Hypothesis: --max-num-seqs is too low
Change: Increased --max-num-seqs from 35 to 50
Result: Utilization increased only modestly (see `screenshots/phase6-iter-5-grafana-after.png`): KV cache usage went from about `45%` to `55%`, and concurrent requests from about `35` to `40`. The SLO was still missed, which strengthened the conclusion that vLLM was not the dominant bottleneck.

### 6
Saw: the bottleneck revolves around the agent loop: too many loops, too long inference time for each turn. The ideal solution is to further optimize the agent itself (prompts and loop logic).
Hypothesis: That said, switching to a quantized model can cut LLM inference time, moving us closer to the SLA. The risk is that reduced quality may generate more loops, which is bad for the SLO. Quality degradation of answers in general is also a risk. But we have the eval to measure.
Change: --quantization bitsandbytes and --dtype bfloat16
Result: Memory usage decreased as expected, but latency did not improve and even regressed somewhat, so the change was reverted.

## Summary 
Bottom line: SLO was not achieved. Using the final non-quantized configuration described above, achieved throughput was `8.33 RPS` versus the `10.0 RPS` target, a shortfall of `1.67 RPS` (`16.7%` below target). P95 end-to-end latency was `117.37s` versus the `5.0s` target, missing by `112.37s` (`23.5x` over target).
Main reason: long tail of long-running agent requests (verify, execute SQL, revise). The correct optimization path is to improve the agent itself. Based on the submitted runs, the strongest remaining bottleneck appears to be on the agent side rather than in vLLM serving, since the Grafana snapshots still show headroom in KV-cache usage while end-to-end latency remains poor. Quality did survive the tuning pass and improved slightly: `results/eval_after_tuning.json` reached `0.4333` final accuracy versus `0.4000` in the baseline eval.

## Agent value
The agent loop provides real but still limited value. In the baseline eval, `results/eval_baseline.json` shows per-iteration accuracy rising from `0.3667` at `iter_0` to `0.4000` at `iter_1`, then staying flat at `0.4000` at `iter_2`, so the first revision sometimes helps but the second one does not. In the final quality measurement, `results/eval_after_tuning.json` improves `iter_2` to `0.4333`, which means the second stage of the loop did start adding measurable value after prompt and agent changes. Even so, the overall gain is still modest relative to the latency and LLM-call overhead, so the loop is useful but not yet efficient enough to justify its current cost under the Phase 6 SLO.


## Future
Given more time, I would focus on the core problem - long agent loops. I would work on the prompts to make the agent converge in fewer iterations, and fail fast when it is not converging. Examples:
- Improve first-try SQL quality
- Make verify and revise much stricter and more targeted
- Optimize loop: stop if the loop is not productive, e.g. revise is not materially changing the query or verify repeats the same complaint
- Once the above are resolved, if vLLM becomes the bottleneck, things that would make sense to try are:
  - optimize `max_num_seqs`, model quantization, KV-cache quantization
