#!/usr/bin/env bash
# Reads /proc, which does not exist in the sandbox.
set -euo pipefail
pipeline_file=${1:?missing pipeline file}
grep -q "^Name:" /proc/self/status
pmt modify -f "$pipeline_file" task hello add-param proc ok
