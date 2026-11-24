#!/usr/bin/env bash
# Writes outside the work directory; the sandbox root file system is read-only.
set -euo pipefail
pipeline_file=${1:?missing pipeline file}
echo state >/var/tmp/canary-state-$$
rm -f /var/tmp/canary-state-$$
pmt modify -f "$pipeline_file" task hello add-param wrote-outside yes
