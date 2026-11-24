#!/usr/bin/env bash
# The recommended way: pmt modify.
set -euo pipefail
pipeline_file=${1:?missing pipeline file}
pmt modify -f "$pipeline_file" task hello add-param pipelinerun-name '$(context.pipelineRun.name)'
