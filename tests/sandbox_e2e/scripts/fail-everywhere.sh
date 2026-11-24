#!/usr/bin/env bash
# A broken migration fails both on the host and in the sandbox.
set -euo pipefail
pipeline_file=${1:?missing pipeline file}
echo "migration is broken" >&2
exit 3
