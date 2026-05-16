#!/bin/bash
echo "========================================="
echo "  Video Encoder Bot - Starting..."
echo "========================================="

# Change to workspace directory
WORKSPACE="/workspaces/video-encoder-bot"
if [ ! -d "$WORKSPACE" ]; then
    WORKSPACE="/workspaces/$(ls /workspaces/ 2>/dev/null | head -1)"
fi
cd "$WORKSPACE" || exit 1

# Check if .env file exists
if [ ! -f .env ]; then
    echo ""
    echo "ERROR: .env file not found!"
    echo "Please create a .env file with your bot credentials."
    echo "Copy .env.sample to .env and fill in your values:"
    echo "  cp .env.sample .env"
    echo ""
    exit 1
fi

# Kill any existing bot process
pkill -f "python3 bot.py" 2>/dev/null
sleep 1

# Remove broken venv if it exists
if [ -d "venv" ] && [ ! -x "venv/bin/python3" ]; then
    echo "Removing broken virtual environment..."
    rm -rf venv
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Install dependencies if pyrogram not installed
if ! ./venv/bin/python3 -c "import pyrogram" 2>/dev/null; then
    echo "Installing dependencies..."
    ./venv/bin/python3 -m pip install --no-cache-dir -r requirements.txt -q
fi

# Install FFmpeg if not present
if ! command -v ffmpeg &> /dev/null; then
    echo "Installing FFmpeg..."
    # Try multiple methods
    if command -v sudo &> /dev/null && command -v apt-get &> /dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg 2>/dev/null
    fi

    # If still not found, download static binary
    if ! command -v ffmpeg &> /dev/null; then
        echo "Downloading FFmpeg static binary..."
        mkdir -p /tmp/ffmpeg-dl
        curl -sL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o /tmp/ffmpeg-dl/ffmpeg.tar.xz
        cd /tmp/ffmpeg-dl && tar xf ffmpeg.tar.xz 2>/dev/null
        FFMPEG_BIN=$(find /tmp/ffmpeg-dl -name "ffmpeg" -type f | head -1)
        FFPROBE_BIN=$(find /tmp/ffmpeg-dl -name "ffprobe" -type f | head -1)
        if [ -n "$FFMPEG_BIN" ]; then
            cp "$FFMPEG_BIN" /usr/local/bin/ffmpeg 2>/dev/null || cp "$FFMPEG_BIN" "$WORKSPACE/venv/bin/ffmpeg"
            chmod +x /usr/local/bin/ffmpeg 2>/dev/null || chmod +x "$WORKSPACE/venv/bin/ffmpeg"
        fi
        if [ -n "$FFPROBE_BIN" ]; then
            cp "$FFPROBE_BIN" /usr/local/bin/ffprobe 2>/dev/null || cp "$FFPROBE_BIN" "$WORKSPACE/venv/bin/ffprobe"
            chmod +x /usr/local/bin/ffprobe 2>/dev/null || chmod +x "$WORKSPACE/venv/bin/ffprobe"
        fi
        rm -rf /tmp/ffmpeg-dl
        cd "$WORKSPACE"
        export PATH="$WORKSPACE/venv/bin:$PATH"
    fi
fi

echo ""

# Export all variables from .env file
echo "Loading environment variables from .env..."
set -a
source .env
set +a
echo "Environment loaded successfully!"

# Add venv/bin to PATH for ffmpeg
export PATH="$WORKSPACE/venv/bin:$PATH"

if command -v ffmpeg &> /dev/null; then
    echo "FFmpeg: $(ffmpeg -version 2>&1 | head -1)"
else
    echo "WARNING: FFmpeg not found! Encoding will fail."
fi

echo ""
echo "Starting bot..."
echo "========================================="

# Run the bot
exec ./venv/bin/python3 bot.py
