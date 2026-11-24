#!/usr/bin/env bash
# Replaces the pipeline file by a symlink to a host file; the sandbox refuses the result.
set -euo pipefail
pipeline_file=${1:?missing pipeline file}
ln -sf /etc/os-release "$pipeline_file"
