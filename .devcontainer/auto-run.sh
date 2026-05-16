#!/bin/bash
# Auto-run: starts the bot if not already running

WORKSPACE="/workspaces/video-encoder-bot"
[ ! -d "$WORKSPACE" ] && WORKSPACE="/workspaces/$(ls /workspaces/ 2>/dev/null | head -1)"
cd "$WORKSPACE" 2>/dev/null || exit 0

# Don't start if .env doesn't exist
if [ ! -f .env ]; then
    echo "[Auto-Run] No .env file found. Skipping bot start."
    echo "[Auto-Run] Create .env file and run: bash .devcontainer/start.sh"
    exit 0
fi

# Don't start if bot is already running
if pgrep -f "python3 bot.py" > /dev/null 2>&1; then
    echo "[Auto-Run] Bot is already running."
    exit 0
fi

# Ensure venv exists
if [ ! -d "venv" ] || [ ! -x "venv/bin/python3" ]; then
    echo "[Auto-Run] Setting up virtual environment..."
    rm -rf venv
    python3 -m venv venv
    ./venv/bin/python3 -m pip install --no-cache-dir -r requirements.txt -q
fi

# Ensure deps installed
if ! ./venv/bin/python3 -c "import pyrogram" 2>/dev/null; then
    echo "[Auto-Run] Installing dependencies..."
    ./venv/bin/python3 -m pip install --no-cache-dir -r requirements.txt -q
fi

# Install FFmpeg if missing
if ! command -v ffmpeg &> /dev/null; then
    echo "[Auto-Run] FFmpeg not found, downloading static binary..."
    mkdir -p /tmp/ffmpeg-dl
    curl -sL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o /tmp/ffmpeg-dl/ffmpeg.tar.xz
    cd /tmp/ffmpeg-dl && tar xf ffmpeg.tar.xz 2>/dev/null
    FFMPEG_BIN=$(find /tmp/ffmpeg-dl -name "ffmpeg" -type f | head -1)
    FFPROBE_BIN=$(find /tmp/ffmpeg-dl -name "ffprobe" -type f | head -1)
    [ -n "$FFMPEG_BIN" ] && cp "$FFMPEG_BIN" "$WORKSPACE/venv/bin/ffmpeg" && chmod +x "$WORKSPACE/venv/bin/ffmpeg"
    [ -n "$FFPROBE_BIN" ] && cp "$FFPROBE_BIN" "$WORKSPACE/venv/bin/ffprobe" && chmod +x "$WORKSPACE/venv/bin/ffprobe"
    rm -rf /tmp/ffmpeg-dl
    cd "$WORKSPACE"
fi

# Load env and start bot in background
echo "[Auto-Run] Starting bot..."
set -a
source .env
set +a
export PATH="$WORKSPACE/venv/bin:$PATH"

nohup "$WORKSPACE/venv/bin/python3" bot.py > /tmp/bot.log 2>&1 &
BOT_PID=$!
echo "[Auto-Run] Bot started (PID: $BOT_PID)"
echo "[Auto-Run] View logs: tail -f /tmp/bot.log"

# Wait a moment and check if it's still running
sleep 3
if kill -0 $BOT_PID 2>/dev/null; then
    echo "[Auto-Run] ✅ Bot is running successfully!"
else
    echo "[Auto-Run] ❌ Bot crashed! Check logs: cat /tmp/bot.log"
fi
