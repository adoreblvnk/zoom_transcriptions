#!/bin/bash
cd "$(dirname "$0")"

while true; do
    echo "========================================"
    echo "  Zoom Transcriptions"
    echo "========================================"
    echo ""
    echo "  1. All recordings (skip existing)"
    echo "  2. Latest recording only"
    echo "  3. Specific module"
    echo "  4. Re-download all (overwrite existing)"
    echo ""
    echo "========================================"

    read -p "Select option (1-4): " choice

    case "$choice" in
        1) uv run zoom-transcriptions ;;
        2) uv run zoom-transcriptions --latest ;;
        3)
            read -p "Enter module code (e.g. INF2004): " mod
            uv run zoom-transcriptions --module "$mod"
            ;;
        4) uv run zoom-transcriptions --no-skip-existing ;;
        *) echo "Invalid option" ;;
    esac

    echo ""
    read -n 1 -p "Press 'r' to download something else, or any other key to exit: " replay
    echo ""
    if [ "$replay" != "r" ]; then
        break
    fi
    echo ""
done
