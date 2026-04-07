#!/usr/bin/env bash
# Launch Phase 5 worktrees by wave number
# Usage: ./scripts/launch-wave.sh <wave>
# Each worktree runs a Claude Code instance with the corresponding prompt

set -euo pipefail

PROMPTS_DIR=".planning/phases/phase-5/prompts"
WAVE="${1:-}"

if [[ -z "$WAVE" ]]; then
    echo "Usage: $0 <wave-number>"
    echo ""
    echo "Waves:"
    echo "  1  — P5.1.1 (db-dedup) + P5.3.1 (ensemble-cache) + P5.5.1 (telegram)"
    echo "       NOTE: P5.1.2 (dedup-logic) runs AFTER P5.1.1 merges"
    echo "  2  — P5.2.1 (resolution-indexer) then P5.2.2 (resolution-trader)"
    echo "  3  — P5.4.1 (backtest)"
    echo ""
    echo "Manual launch (interactive):"
    echo "  claude -w p5-dedup       # then follow P5.1.1 prompt"
    echo ""
    echo "Headless launch (autonomous):"
    echo "  claude -w p5-dedup -p \"\$(cat $PROMPTS_DIR/p5.1.1-db-dedup.md)\""
    exit 1
fi

launch_headless() {
    local name="$1"
    local prompt_file="$2"
    echo "Launching worktree: $name (from $prompt_file)"
    # Run in background — each gets its own terminal via &
    claude -w "$name" -p "$(cat "$prompt_file")" &
}

case "$WAVE" in
    1)
        echo "=== WAVE 1: Dedup DB + Ensemble Cache + Telegram ==="
        echo "NOTE: These touch different files and can run in parallel."
        echo "P5.1.2 (dedup logic) must wait for P5.1.1 to merge."
        echo ""
        launch_headless "p5-dedup-db"   "$PROMPTS_DIR/p5.1.1-db-dedup.md"
        launch_headless "p5-cache"      "$PROMPTS_DIR/p5.3.1-ensemble-cache.md"
        launch_headless "p5-telegram"   "$PROMPTS_DIR/p5.5.1-telegram.md"
        echo ""
        echo "3 agents launched. Monitor with: git worktree list"
        echo "After all complete + merge, run P5.1.2 separately:"
        echo "  claude -w p5-dedup-logic -p \"\$(cat $PROMPTS_DIR/p5.1.2-dedup-logic.md)\""
        ;;
    2)
        echo "=== WAVE 2: Real Resolution ==="
        echo "P5.2.1 first, then P5.2.2 (sequential — P5.2.2 depends on P5.2.1)"
        echo ""
        launch_headless "p5-resolution" "$PROMPTS_DIR/p5.2.1-resolution-indexer.md"
        echo "Launched P5.2.1. After it completes + merges, run P5.2.2:"
        echo "  claude -w p5-resolution-trader -p \"\$(cat $PROMPTS_DIR/p5.2.2-resolution-trader.md)\""
        ;;
    3)
        echo "=== WAVE 3: Backtest ==="
        launch_headless "p5-backtest" "$PROMPTS_DIR/p5.4.1-backtest.md"
        echo "Launched P5.4.1. After complete, run full regression:"
        echo "  python -m pytest tests/ -v"
        ;;
    *)
        echo "Unknown wave: $WAVE (use 1, 2, or 3)"
        exit 1
        ;;
esac

wait
echo "All background processes finished."
