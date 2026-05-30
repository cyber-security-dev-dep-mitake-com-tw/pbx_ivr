#!/bin/bash
# Create a local venv and install script dependencies.
# Run once from any directory:
#   bash scripts/test_protocol_simulation/setup_venv.sh
#
# Then activate before running scripts:
#   source scripts/test_protocol_simulation/.venv/bin/activate

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install httpx websockets

echo ""
echo "✓ venv ready at $VENV"
echo ""
echo "Activate with:"
echo "  source $VENV/bin/activate"
echo ""
echo "Then run any script normally, e.g.:"
echo "  python scripts/test_protocol_simulation/fxs_monitor.py"
echo "  python scripts/test_protocol_simulation/watch_events.py"
echo "  python scripts/test_protocol_simulation/simulate_call_flow.py --scenario support"
