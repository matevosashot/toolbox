#!/usr/bin/env bash
# Launch a tasker worker against ./tasks/.
#
# Drop new bash scripts into ./tasks/pending/ and the worker will pick
# them up. See toolbox/tasker/worker.py for the file-name conventions
# (priority "!", task arrays "[N]", non-propagating arrays "*[N]").
#
# Any extra CLI arguments are forwarded to `toolbox worker`, so e.g.
#   ./run_worker.sh --no-random
# will pick tasks in directory order instead of randomly.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p $HERE/tasks/pending
cp -r $HERE/tasks/draft/* $HERE/tasks/pending/

toolbox worker \
    --task-base-path "$HERE/tasks" \
    --loop \
    --idle-sleep 3 \
    "$@"
