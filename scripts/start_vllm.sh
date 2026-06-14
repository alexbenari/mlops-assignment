#!/usr/bin/env bash
#
# Start vLLM with the current local development configuration.
#
# This script is for local WSL2 development on a small laptop GPU. It serves a
# stand-in model so the agent, Prometheus, and Langfuse flows can be developed
# end-to-end without the final hosted GPU VM.
#
# This is NOT the final benchmarking / tuning configuration. The assignment's
# real target model and serving settings will need a larger hosted GPU.
#
# Reference: https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html

set -euo pipefail

# Local GPU stand-in model that fits the current WSL laptop setup.
#MODEL="Qwen/Qwen3-0.6B"
MODEL="Qwen/Qwen3-30B-A3B-Instruct-2507"

exec uv run python -m vllm.entrypoints.openai.api_server \
    --model "$MODEL" \
    --host 0.0.0.0 \
    --port 8000 \
    --reasoning-parser qwen3 \
    --gpu-memory-utilization 0.7 \
    --max-model-len 2560
