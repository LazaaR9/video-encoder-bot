# 🧠 AI Video Encoder Bot

A powerful Telegram bot for **AI-enhanced video encoding** with T4 GPU acceleration.

## Features

### AI Processing Pipeline (NCNN + Vulkan on T4)

| Model | Purpose | GPU Accel |
|-------|---------|-----------|
| **Real-ESRGAN** | Neural network super-resolution (general purpose) | ✅ NCNN/Vulkan |
| **waifu2x** | AI upscaling optimized for anime/cartoon content | ✅ NCNN/Vulkan |
| **RIFE** | Real-time AI frame interpolation (2x/4x FPS boost) | ✅ NCNN/Vulkan |
| **VMAF** | Netflix perceptual quality scoring (post-encode) | CPU |

### Encoding

| Codec | Method | Speed | Compression |
|-------|--------|-------|-------------|
| **H.264** | NVENC (GPU) | ⚡ Fast | Good |
| **H.265/HEVC** | NVENC (GPU) | ⚡ Fast | Better (~50% smaller) |
| **AV1** | SVT-AV1 (CPU) | 🐌 Slower | Best (~30% smaller than H.265) |

### Settings

- **CRF**: 0 (lossless) → 51 (worst), default 23
- **Presets**: ultrafast → veryslow (9 options)
- **Resolution**: 4K / 2K / 1080p / 720p / 540p / 480p / 360p / Original
- **AI Upscaler**: Real-ESRGAN (general) or waifu2x (anime)
- **RIFE**: 2x or 4x frame interpolation with model selection
- **VMAF**: Automatic quality scoring after encode

## AI Pipeline Flow

```
Video In
  │
  ├─ [1] Extract Frames (FFmpeg)
  │
  ├─ [2] AI Upscale? ──→ Real-ESRGAN or waifu2x (NCNN/Vulkan on T4)
  │
  ├─ [3] AI Interpolate? ──→ RIFE 2x/4x (NCNN/Vulkan on T4)
  │
  ├─ [4] Encode (NVENC GPU / SVT-AV1 CPU)
  │
  ├─ [5] VMAF Quality Score
  │
  └─ Video Out + Quality Report
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Start the bot |
| `/help` | Help & guide |
| `/encode` | Show encoding settings |
| `/settings` | Current settings |
| `/status` | Bot status & GPU info |
| `/models` | Check AI model installation status |
| `/cancel` | Cancel active encoding |
| `/about` | About this bot |

## Setup

### Requirements

- Python 3.10+
- FFmpeg (with NVENC support)
- NVIDIA GPU (T4 recommended)
- Vulkan drivers (for AI models)

### Installation

```bash
# Clone the repo
git clone https://github.com/VEncod/video-encoder-bot.git
cd video-encoder-bot

# Install Python dependencies
pip install -r requirements.txt

# Copy and configure .env
cp .env.sample .env
# Edit .env with your credentials

# Run
python bot.py
```

### AI Models (Auto-Installed)

The AI models are automatically downloaded and installed on first use:

- **Real-ESRGAN**: `realesrgan-ncnn-vulkan` from [xinntao/Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN)
- **waifu2x**: `waifu2x-ncnn-vulkan` from [nihui/waifu2x-ncnn-vulkan](https://github.com/nihui/waifu2x-ncnn-vulkan)
- **RIFE**: `rife-ncnn-vulkan` from [nihui/rife-ncnn-vulkan](https://github.com/nihui/rife-ncnn-vulkan)

Check model status with `/models` command.

### GPU Notes

- **T4 GPU**: 16GB VRAM, Tensor Cores, NVENC hardware encoder
- AI models use **NCNN + Vulkan** for inference (no PyTorch/CUDA needed)
- NVENC supports H.264 and H.265 hardware encoding
- AV1 encoding is CPU-only (SVT-AV1) — slower but best compression
- Dual GPU load balancing is supported (round-robin)

## .env Configuration

```env
BOT_TOKEN=your_bot_token_here        # From @BotFather
API_ID=your_api_id_here              # From my.telegram.org
API_HASH=your_api_hash_here          # From my.telegram.org
ADMINS=your_user_id_here             # Comma-separated Telegram user IDs
LOG_CHANNEL=your_log_channel_id_here # Channel for startup messages
GDRIVE_SA_FILE=service_account.json  # Google Drive service account
GDRIVE_FOLDER_ID=your_folder_id_here # Google Drive destination folder
```

## Architecture

```
bot.py                  # Main bot (all-in-one)
├── AI Model Manager    # Auto-install & manage NCNN/Vulkan binaries
├── AI Pipelines        # Real-ESRGAN, waifu2x, RIFE processing
├── FFmpeg Encoder      # NVENC (GPU) + SVT-AV1/x264/x265 (CPU)
├── VMAF Scorer         # Perceptual quality measurement
├── Google Drive        # Large file upload (>2GB)
└── Telegram Bot        # Pyrogram-based interactive UI
```

## License

MIT
