#!/usr/bin/env bash
# Process substitution works: the runner bash uses named pipes, not /dev/fd.
set -euo pipefail
pipeline_file=${1:?missing pipeline file}
while read -r name; do
    echo "task: $name"
done < <(yq ".spec.pipelineSpec.tasks[].name" "$pipeline_file")
pmt modify -f "$pipeline_file" task hello add-param from-procsub yes
