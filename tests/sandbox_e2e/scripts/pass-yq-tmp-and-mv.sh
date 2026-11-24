#!/usr/bin/env bash
# Rewrite through a temporary file and rename it over the pipeline file.
set -euo pipefail
pipeline_file=${1:?missing pipeline file}
tmp=$(mktemp)
yq '(.spec.pipelineSpec.tasks[] | select(.name == "hello") | .params[0].value) = "hello"' "$pipeline_file" >"$tmp"
mv "$tmp" "$pipeline_file"
