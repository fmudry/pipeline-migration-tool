#!/usr/bin/env bash
# The network is shared with the host, so registries are reachable.
set -euo pipefail
pipeline_file=${1:?missing pipeline file}
curl -fsS -o /dev/null https://quay.io/v2/ || [ "$(curl -s -o /dev/null -w "%{http_code}" https://quay.io/v2/)" = 401 ]
pmt modify -f "$pipeline_file" task hello add-param network ok
