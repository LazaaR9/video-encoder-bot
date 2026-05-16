# Anime Video Encoder Bot - Kaggle Setup
# T4 GPU | waifu2x + RIFE + NVENC

# Cell 1: Credentials
import os
os.environ["BOT_TOKEN"] = "YOUR_BOT_TOKEN_HERE"
os.environ["API_ID"] = "YOUR_API_ID_HERE"
os.environ["API_HASH"] = "YOUR_API_HASH_HERE"
os.environ["ADMINS"] = "YOUR_ADMIN_IDS_COMMA_SEPARATED"
os.environ["LOG_CHANNEL"] = "YOUR_LOG_CHANNEL_ID_HERE"
```

```python
# Cell 2: Install FFmpeg
!apt update && apt install -y ffmpeg
```

```python
# Cell 3: Install waifu2x (anime AI upscaler)
!cd /tmp && wget -q https://github.com/nihui/waifu2x-ncnn-vulkan/releases/download/20220728/waifu2x-ncnn-vulkan-20220728-ubuntu.zip -O waifu2x.zip && unzip -o waifu2x.zip -d waifu2x && chmod +x waifu2x/waifu2x-ncnn-vulkan && mv waifu2x/waifu2x-ncnn-vulkan /usr/local/bin/ && mkdir -p /usr/local/bin/models && cp -rf waifu2x/models/* /usr/local/bin/models/ && echo "✅ waifu2x installed"
```

```python
# Cell 4: Install RIFE (AI frame interpolation)
!cd /tmp && wget -q https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/rife-ncnn-vulkan-20221029-ubuntu.zip -O rife.zip && unzip -o rife.zip -d rife && chmod +x rife/rife-ncnn-vulkan && mv rife/rife-ncnn-vulkan /usr/local/bin/ && cp -rf rife/models/* /usr/local/bin/models/ && echo "✅ RIFE installed"
```

```python
# Cell 5: Python deps
!pip install pyrofork==2.3.45 TgCrypto python-dotenv google-api-python-client google-auth
```

```python
# Cell 6: Clone and run
!git clone https://github.com/VEncod/video-encoder-bot.git /tmp/video-encoder-bot
!cp /tmp/video-encoder-bot/bot.py .
!mkdir -p downloads encoded
!python bot.py
```
