#!/usr/bin/env bash
# Relies on a host environment variable (stands for tokens and secrets).
set -euo pipefail
pipeline_file=${1:?missing pipeline file}
pmt modify -f "$pipeline_file" task hello add-param secret "${CANARY_SECRET:?CANARY_SECRET is not set}"
