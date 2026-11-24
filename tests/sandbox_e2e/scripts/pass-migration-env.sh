#!/usr/bin/env bash
# Relies on the environment pmt migrate sets for migration scripts, in the sandbox too.
set -euo pipefail
pipeline_file=${1:?missing pipeline file}
[ "${PMT_IN_MIGRATION:-}" = 1 ] || { echo "PMT_IN_MIGRATION is not set" >&2; exit 1; }
pmt modify -f "$pipeline_file" task hello add-param api-version "${PMT_API_VERSION:?PMT_API_VERSION is not set}"
