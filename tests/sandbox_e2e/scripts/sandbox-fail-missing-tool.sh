#!/usr/bin/env bash
# Uses a tool that the runner image does not have.
set -euo pipefail
pipeline_file=${1:?missing pipeline file}
kubectl version --client >/dev/null
pmt modify -f "$pipeline_file" task hello add-param kubectl yes
