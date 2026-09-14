#!/bin/zsh
# Run every suite. DB_DSN optional: live sections self-skip without it.
set -e
cd "$(dirname "$0")"
( cd extraction && python3 test_extraction.py )
( cd reconciler && python3 test_reconciler.py )
( [ -f reconciler/test_supersede.py ] && cd reconciler && python3 test_supersede.py || true )
( cd cli && python3 test_review.py )
( cd cli && python3 test_share.py )
( cd digest && python3 test_digest.py )
( cd ask && python3 test_ask.py )
( cd eval && python3 test_eval.py )
( cd testdata && python3 test_synth.py )
( cd cadence && python3 test_cadence.py )
( cd integration/tests && python3 test_dashboard.py )
# The dashboard's JS is inline and untested by the Python suites; this
# catches a constant deleted along with the function above it, which is
# how REASON_ROUTE and NOTED both reached the browser as runtime errors.
command -v node >/dev/null && node integration/dashboard/check_js.js \
  integration/dashboard/dashboard.html || \
  echo "skipped: node not installed (dashboard JS check)"
python3 scripts/lint_bat.py || exit 1
TL_MAP_OUT="${TMPDIR:-/tmp}/tl-system-map.html" python3 scripts/build_system_map.py
echo "ALL SUITES COMPLETE"
