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

