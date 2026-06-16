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
- WSL required standard native build tooling for the compile path, so local setup included the usual Linux build prerequisites.

This local configuration is only intended to unblock development of the agent, Prometheus metrics, and tracing flow. It is not representative of the final serving configuration, and none of its latency or throughput characteristics should be used for the final SLO or quality claims.

## Future
- more instances of vllm if memory allows
- KV cache hits optimization (size of KV cache)
- vllm config: optimize max-model-len param based on eval + 30%
- quantized model
- https://docs.vllm.ai/en/latest/configuration/optimization/


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
  --max-model-len 4096
```

Flag rationale:

- `--model Qwen/Qwen3-30B-A3B-Instruct-2507`: this is the assignment target model, so all final latency and quality measurements should use it.
- `--host 0.0.0.0`: exposes the API server on the VM so local SSH port forwarding can reach it.
- `--port 8000`: keeps the repo's default endpoint wiring unchanged for Prometheus scraping and the agent client.
- `--reasoning-parser qwen3`: enables the correct parser for Qwen3 reasoning-format responses on the OpenAI-compatible server.
- `--generation-config vllm`: avoids inheriting model-side generation defaults from Hugging Face config files and keeps serving behavior explicit.
- `--gpu-memory-utilization 0.9`: reserves substantially more GPU memory for KV cache than the local-dev setting; `0.7` failed on the H100 because the model weights and compile overhead left no cache space.
- `--max-model-len 4096`: gives enough headroom for the agent's schema-heavy prompts without paying the extra KV-cache cost of a much larger context window. Based on the data provided about expected query lengts, plus headroom. Might be optimized further later. 
