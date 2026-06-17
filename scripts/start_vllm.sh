#!/usr/bin/env bash
#
# Start vLLM with the current cloud-VM baseline configuration.
#
# This script is intended as the first hosted-GPU baseline for the assignment's
# target model on a single H100. It is a starting point for tuning, not a final
# benchmark claim.
#
# Reference: https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html

set -euo pipefail

MODEL="Qwen/Qwen3-30B-A3B-Instruct-2507"
UV_BIN="${HOME}/.local/bin/uv"

exec "$UV_BIN" run python -m vllm.entrypoints.openai.api_server \
    --model "$MODEL" \
    --host 0.0.0.0 \
    --port 8000 \
    --reasoning-parser qwen3 \
    --generation-config vllm \
    --gpu-memory-utilization 0.9 \
    --max-model-len 12288 \
    --max-num-seqs 35 
