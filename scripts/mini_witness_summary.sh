#!/usr/bin/env bash
# Summarize a Mini NAV-PVT chrony witness feed from refclocks.log.
#
# Thin wrapper over mini_witness_summary.py (stdlib only) -- see
# docs/MINI_TIMING_WITNESS.md for what this measures, how to enable
# the feed, and how to read the output.
#
# Usage:
#   mini_witness_summary.sh [LOGFILE...] [--json] [--mini-refid ID]
#                            [--fuse-refid ID] [--max-pair-gap-sec N]
#                            [--gap-threshold-sec N]
#
# LOGFILE defaults to /var/log/chrony/refclocks.log, which is usually
# root-only -- run this with sudo on a station. More than one LOGFILE
# merges by sample timestamp (e.g. a rotated refclocks.log.1 plus the
# current refclocks.log), in either order.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/mini_witness_summary.py" "$@"
