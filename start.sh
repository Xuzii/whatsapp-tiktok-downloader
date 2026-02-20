#!/bin/bash
set -e
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load .env file if it exists
if [ -f "$PROJECT_DIR/.env" ]; then
    export $(grep -v '^#' "$PROJECT_DIR/.env" | xargs)
    echo "Loaded environment from .env"
fi

# Ensure /tmp download directory exists
mkdir -p /tmp/tiktok_videos

cleanup() {
    echo ""
    echo "Stopping services..."
    if [ -n "$PYTHON_PID" ]; then
        kill "$PYTHON_PID" 2>/dev/null
        echo "Stopped Python server (PID $PYTHON_PID)"
    fi
    exit 0
}
trap cleanup INT TERM

echo "=== WhatsApp TikTok Restaurant Analyzer ==="
echo "GCS_BUCKET: ${GCS_BUCKET:-whatsapp-tiktok-restaurants}"
echo ""

# Activate Python venv if it exists (used on VM)
if [ -f "$PROJECT_DIR/python/venv/bin/activate" ]; then
    source "$PROJECT_DIR/python/venv/bin/activate"
    echo "Activated Python venv"
fi

# Start Python download server in background
echo "Starting Python download server..."
cd "$PROJECT_DIR/python"
python3 downloader.py &
PYTHON_PID=$!

# Wait for Python server to be ready (health check)
echo "Waiting for Python server..."
for i in $(seq 1 15); do
    sleep 1
    if curl -s http://localhost:5001/health > /dev/null 2>&1; then
        echo "Python server is ready."
        break
    fi
    if [ $i -eq 15 ]; then
        echo "ERROR: Python server failed to start."
        kill "$PYTHON_PID" 2>/dev/null
        exit 1
    fi
done
echo ""

# Start Node.js WhatsApp listener in foreground (so QR code is visible)
echo "Starting WhatsApp listener..."
cd "$PROJECT_DIR/node"
node index.js

# If Node.js exits, clean up Python too
cleanup
