#!/usr/bin/env bash
# /dev/stdin needs /proc.
set -euo pipefail
pipeline_file=${1:?missing pipeline file}
value=$(echo from-stdin | cat /dev/stdin)
pmt modify -f "$pipeline_file" task hello add-param stdin "$value"
