#!/bin/bash
# Dremes local test — drop ref images in test_refs/{brand}/{pool}/ and run
# Usage:
#   ./test.sh                              # run all test refs
#   ./test.sh cinco-h-ranch cream         # run one brand+pool
#   ./test.sh cinco-h-ranch cream ref.jpg  # run a single ref

set -e
cd "$(dirname "$0")"

# Load .env
[ -f .env ] && set -a && source .env && set +a

if [ -z "$GEMINI_API_KEY" ]; then
    echo "❌ GEMINI_API_KEY not set. Put it in .env: GEMINI_API_KEY=your_key"
    exit 1
fi

export DATA_DIR="${DATA_DIR:-/tmp/dremes-data}"
export REFS_VOLUME="${DATA_DIR}"

mkdir -p "$DATA_DIR/output/ad-approval" "$DATA_DIR/output/ads-bad" "$DATA_DIR/output/posts"
mkdir -p "$DATA_DIR/public/images/refs" "$DATA_DIR/public/data/refs"

BRAND="${1:-}"
POOL="${2:-}"
REF_FILE="${3:-}"

if [ -n "$BRAND" ] && [ -n "$POOL" ] && [ -n "$REF_FILE" ]; then
    # Single ref mode
    REF_PATH="test_refs/${BRAND}/${POOL}/${REF_FILE}"
    if [ ! -f "$REF_PATH" ]; then
        echo "❌ Reference not found: $REF_PATH"
        exit 1
    fi
    echo "Testing: $BRAND / $POOL / $REF_FILE"
    python3 dremes_agent.py --brand "$BRAND" --category "$POOL" "$REF_PATH"
elif [ -n "$BRAND" ] && [ -n "$POOL" ]; then
    # All refs in pool mode
    REFS=(test_refs/${BRAND}/${POOL}/*.{jpg,jpeg,png,webp} 2>/dev/null)
    if [ ${#REFS[@]} -eq 0 ] || [ ! -f "${REFS[0]}" ]; then
        echo "❌ No refs in test_refs/${BRAND}/${POOL}/"
        echo "   Drop reference images there first."
        exit 1
    fi
    echo "Testing: $BRAND / $POOL — ${#REFS[@]} refs"
    for ref in "${REFS[@]}"; do
        [ -f "$ref" ] || continue
        echo ""
        echo "━━━ $(basename "$ref") ━━━"
        python3 dremes_agent.py --brand "$BRAND" --category "$POOL" "$ref"
    done
else
    echo "Usage:"
    echo "  ./test.sh                              # run all test refs in test_refs/"
    echo "  ./test.sh cinco-h-ranch cream         # run all cream refs"
    echo "  ./test.sh cinco-h-ranch cream ref.jpg  # run one ref"
    echo ""
    echo "Drop reference images in test_refs/{brand}/{pool}/"
    echo "  test_refs/cinco-h-ranch/cream/   ← cream ref images"
    echo "  test_refs/cinco-h-ranch/soap/    ← soap ref images"
    echo ""
    echo "Products for Cinco H Ranch pools:"
    echo "  cream:  Rejuvenating, Unscented Luxury, Purifying, Serenity, Ultra Healing"
    echo "  soap:   Honey Vanilla, Chamomile, Texas Air, Texas Campfire, Detox, La Blanca"
    echo "  sunscreen-stick: Sunscreen Stick"
fi
