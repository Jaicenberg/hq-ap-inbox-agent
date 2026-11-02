#!/usr/bin/env bash
# Runs the AI classification step, spreading the work over several Workers AI models
# (each model has its own capacity) through AI Gateway. Safe to re-run: finished items are skipped.
# Usage: pipeline/run_batch.sh <raw.sqlite> <work.sqlite> <attachments_dir>
set -u
RAW=$1; WORK=$2; ATT=$3; DIR=$(dirname "$0")
run() { BATCH_MODEL=$1 SHARD=$2 BATCH_CONCURRENCY=$3 python -I "$DIR/classify.py" "$RAW" "$WORK" "$ATT" "$4"; }
run @cf/deepseek-ai/deepseek-v4.1-flash-1017 0/2 12 emails &
run @cf/openai/gpt-oss-120b                  1/2 12 emails &
run @cf/google/gemma-4-26b-a4b-it            0/2 12 attachments &
run @cf/zai-org/glm-4.7-flash                1/2 12 attachments &
run @cf/google/gemma-4-26b-a4b-it            0/2 10 scans &
run @cf/meta/llama-4-scout-17b-16e-instruct  1/2 10 scans &
wait
