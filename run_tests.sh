#!/bin/zsh
# Run every suite. DB_DSN optional: live sections self-skip without it.
set -e
cd "$(dirname "$0")"
( cd extraction && python3 test_extraction.py )
( cd reconciler && python3 test_reconciler.py )
( [ -f reconciler/test_supersede.py ] && cd reconciler && python3 test_supersede.py || true )
( cd cli && python3 test_review.py )
( cd cli && python3 test_share.py )
( cd integration/tests && python3 test_dashboard.py )
python3 scripts/build_system_map.py
echo "ALL SUITES COMPLETE"
