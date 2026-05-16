from pyrogram.errors import MessageNotModified
import os
import asyncio
import time
import shutil
import random
import re
import json
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaUploadProgress

load_dotenv()

# Load environment variables
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
ADMINS = [int(x) for x in os.environ.get("ADMINS", "").split(",") if x.strip()]
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", "0"))
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "1qXcnEaSzpI8QVLXT3Tvx5lMQ-FzDd8qa")
GDRIVE_SA_FILE = os.environ.get("GDRIVE_SA_FILE", "service_account.json")

app = Client("video_encoder_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# Track bot start time
BOT_START_TIME = time.time()

# Dictionary to store user-specific encoding settings
user_settings = {}

# Active encoding tasks (for cancel)
active_tasks = {}

# GPU allocation tracker - round-robin between available GPUs
gpu_lock = asyncio.Lock()
gpu_current_tasks = {0: 0, 1: 0}  # GPU index -> number of active tasks

# ─── AI Model Paths (auto-installed) ───────────────────────────────────
AI_MODELS = {
    "realesrgan": None,   # path to realesrgan-ncnn-vulkan binary
    "rife": None,         # path to rife-ncnn-vulkan binary
    "waifu2x": None,      # path to waifu2x-ncnn-vulkan binary
}


async def get_best_gpu():
    """Get the GPU with fewest active tasks (round-robin load balancing)."""
    async with gpu_lock:
        if gpu_current_tasks[1] < gpu_current_tasks[0]:
            return 1
        return 0


async def acquire_gpu():
    """Acquire a GPU slot and return the GPU index."""
    gpu_id = await get_best_gpu()
    async with gpu_lock:
        gpu_current_tasks[gpu_id] += 1
    return gpu_id


async def release_gpu(gpu_id):
    """Release a GPU slot."""
    async with gpu_lock:
        gpu_current_tasks[gpu_id] = max(0, gpu_current_tasks[gpu_id] - 1)


async def safe_edit_text(message, text, reply_markup=None):
    """Edit message text, ignoring MessageNotModified errors."""
    try:
        if reply_markup:
            await message.edit_text(text, reply_markup=reply_markup)
        else:
            await message.edit_text(text)
    except MessageNotModified:
        pass


# Big variety of reactions - different every time
ALL_REACTIONS = [
    "🔥", "⚡", "🎬", "👀", "🎯", "💯", "🏆", "⭐", "🎉", "💪",
    "🚀", "❤️", "👍", "🤩", "😎", "🥰", "👏", "🙏", "💫", "✨",
    "🎊", "💥", "🌟", "💎", "🦋", "🍾", "🎁", "🏅", "👑", "💜",
    "❤️🔥", "🤯", "😍", "🫡", "🕊", "🐳", "🌚", "🌈", "🍓", "🍀"
]

# Thumbnail directory
THUMB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thumbnails")

RESOLUTION_MAP = {
    "4k": "3840:-2",
    "2k": "2560:-2",
    "1080p": "1920:-2",
    "720p": "1280:-2",
    "540p": "960:-2",
    "480p": "854:-2",
    "360p": "640:-2",
    "original": None
}

PRESET_LIST = [
    "ultrafast", "superfast", "veryfast", "faster",
    "fast", "medium", "slow", "slower", "veryslow"
]

# NVENC preset mapping (x264 preset names → NVENC preset names)
NVENC_PRESET_MAP = {
    "ultrafast": "p1", "superfast": "p2", "veryfast": "p3",
    "faster": "p4", "fast": "p5", "medium": "p4",
    "slow": "p6", "slower": "p7", "veryslow": "p7"
}

# Available codecs
CODEC_OPTIONS = {
    "h264": {"label": "H.264 (x264)", "nvenc": "h264_nvenc", "sw": "libx264"},
    "h265": {"label": "H.265 (HEVC)", "nvenc": "hevc_nvenc", "sw": "libx265"},
    "av1":  {"label": "AV1 (SVT-AV1)", "nvenc": None, "sw": "libsvtav1"},
}

# ─── Maxrate caps per resolution ───────────────────────────────────────
MAXRATE_MAP = {
    "4k": "20M", "2k": "12M", "1080p": "8M",
    "720p": "5M", "540p": "3M", "480p": "2M", "360p": "1.5M"
}

# Resolution width mapping
RESOLUTION_WIDTHS = {
    "4k": 3840, "2k": 2560, "1080p": 1920, "720p": 1280,
    "540p": 960, "480p": 854, "360p": 640
}


# ═══════════════════════════════════════════════════════════════════════
#  UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════

def get_uptime():
    """Get bot uptime as a formatted string."""
    elapsed = int(time.time() - BOT_START_TIME)
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


async def react_to_message(client, chat_id, message_id, emoji=None):
    """Send a random reaction to a message."""
    if emoji is None:
        emoji = random.choice(ALL_REACTIONS)
    try:
        await client.send_reaction(chat_id, message_id, emoji=emoji)
    except Exception:
        pass


def get_random_thumbnail():
    """Get a random anime thumbnail from the thumbnails folder."""
    if os.path.exists(THUMB_DIR):
        thumbs = [f for f in os.listdir(THUMB_DIR) if f.endswith(('.jpg', '.png', '.jpeg'))]
        if thumbs:
            return os.path.join(THUMB_DIR, random.choice(thumbs))
    return None


def make_progress_bar(percentage, length=10):
    """Create a visual progress bar."""
    filled = int(length * percentage / 100)
    bar = "█" * filled + "░" * (length - filled)
    return bar


def parse_ffmpeg_progress(line, duration_seconds):
    """Parse FFmpeg stderr output to get progress percentage."""
    time_match = re.search(r"time=(\d+):(\d+):(\d+)\.(\d+)", line)
    if time_match and duration_seconds > 0:
        h, m, s = int(time_match.group(1)), int(time_match.group(2)), int(time_match.group(3))
        current = h * 3600 + m * 60 + s
        percentage = min(int((current / duration_seconds) * 100), 99)
        return percentage
    return None


async def run_cmd(cmd, env=None, timeout=600):
    """Run a subprocess command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, b"", b"Timeout"
    return proc.returncode, stdout, stderr


async def get_video_duration(input_path):
    """Get video duration in seconds using ffprobe."""
    try:
        rc, stdout, _ = await run_cmd([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", input_path
        ])
        if rc == 0:
            return float(stdout.decode().strip())
    except Exception:
        pass
    return 0


async def get_video_fps(input_path):
    """Get video framerate using ffprobe."""
    try:
        rc, stdout, _ = await run_cmd([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1", input_path
        ])
        if rc == 0:
            fps_str = stdout.decode().strip()
            if "/" in fps_str:
                num, den = fps_str.split("/")
                return float(num) / float(den)
            return float(fps_str)
    except Exception:
        pass
    return 24.0


async def get_video_codec(input_path):
    """Detect the video codec of input file using ffprobe."""
    try:
        rc, stdout, _ = await run_cmd([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1", input_path
        ])
        if rc == 0:
            return stdout.decode().strip().lower()
    except Exception:
        pass
    return "unknown"


async def get_video_resolution(input_path):
    """Detect the video resolution (width, height) using ffprobe."""
    try:
        rc, stdout, _ = await run_cmd([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "default=noprint_wrappers=1:nokey=1", input_path
        ])
        if rc == 0:
            lines = stdout.decode().strip().split("\n")
            width = int(lines[0].strip())
            height = int(lines[1].strip())
            return width, height
    except Exception:
        pass
    return 0, 0


def get_resolution_label(width):
    """Map a pixel width to a human-readable resolution label."""
    if width >= 3840:
        return "4K"
    elif width >= 2560:
        return "2K"
    elif width >= 1920:
        return "1080p"
    elif width >= 1280:
        return "720p"
    elif width >= 960:
        return "540p"
    elif width >= 854:
        return "480p"
    elif width >= 640:
        return "360p"
    else:
        return f"{width}p"


def get_nvidia_vulkan_env():
    """Get environment dict with NVIDIA Vulkan ICD configured."""
    env = os.environ.copy()
    nvidia_icd_candidates = [
        "/usr/share/vulkan/icd.d/nvidia_icd.json",
        "/etc/vulkan/icd.d/nvidia_icd.json",
        "/usr/share/vulkan/icd.d/nvidia_layers.json",
        "/etc/vulkan/icd.d/nvidia_layers.json",
    ]
    for icd_dir in ["/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d"]:
        if os.path.isdir(icd_dir):
            for f in os.listdir(icd_dir):
                if "nvidia" in f.lower() and f.endswith(".json"):
                    nvidia_icd_candidates.append(os.path.join(icd_dir, f))
    for candidate in nvidia_icd_candidates:
        if os.path.exists(candidate):
            env["VK_ICD_FILENAMES"] = candidate
            break
    return env


# ═══════════════════════════════════════════════════════════════════════
#  AI MODEL AUTO-INSTALLER
# ═══════════════════════════════════════════════════════════════════════

def _find_binary(name):
    """Check if a binary exists in PATH."""
    return shutil.which(name)


def _install_binary(name, url, zip_name, binary_name=None, extra_files=None):
    """Download and install a binary from a GitHub release zip."""
    import subprocess as sp
    if binary_name is None:
        binary_name = name
    try:
        print(f"Installing {name}...")
        dl_dir = f"/tmp/{name}_install"
        os.makedirs(dl_dir, exist_ok=True)
        zip_path = os.path.join(dl_dir, zip_name)
        sp.run(["wget", "-q", url, "-O", zip_path], timeout=120, check=True)
        sp.run(["unzip", "-o", zip_path, "-d", dl_dir], timeout=60, check=True)
        # Find the binary
        for root, dirs, files in os.walk(dl_dir):
            for f in files:
                if f == binary_name:
                    src = os.path.join(root, f)
                    dst = f"/usr/local/bin/{f}"
                    shutil.move(src, dst)
                    os.chmod(dst, 0o755)
                    # Copy model files if they exist
                    models_src = os.path.join(root, "models")
                    models_dst = "/usr/local/bin/models"
                    if os.path.isdir(models_src):
                        os.makedirs(models_dst, exist_ok=True)
                        for mf in os.listdir(models_src):
                            shutil.copy2(os.path.join(models_src, mf), os.path.join(models_dst, mf))
                    # Copy any extra files
                    if extra_files:
                        for ef in extra_files:
                            ef_src = os.path.join(root, ef)
                            if os.path.exists(ef_src):
                                if os.path.isdir(ef_src):
                                    dst_dir = f"/usr/local/bin/{ef}"
                                    if os.path.exists(dst_dir):
                                        shutil.rmtree(dst_dir)
                                    shutil.copytree(ef_src, dst_dir)
                                else:
                                    shutil.copy2(ef_src, f"/usr/local/bin/{ef}")
                    print(f"✅ {name} installed to {dst}")
                    return True
        print(f"❌ {name}: binary not found in archive")
    except Exception as e:
        print(f"❌ Failed to install {name}: {e}")
    finally:
        shutil.rmtree(dl_dir, ignore_errors=True)
    return False


def check_realesrgan():
    """Check/install realesrgan-ncnn-vulkan."""
    if AI_MODELS["realesrgan"] and os.path.exists(AI_MODELS["realesrgan"]):
        return True
    binary = _find_binary("realesrgan-ncnn-vulkan")
    if binary:
        AI_MODELS["realesrgan"] = binary
        return True
    ok = _install_binary(
        "realesrgan-ncnn-vulkan",
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
        "realesrgan-ncnn-vulkan.zip"
    )
    if ok:
        AI_MODELS["realesrgan"] = _find_binary("realesrgan-ncnn-vulkan")
    return ok


def check_rife():
    """Check/install rife-ncnn-vulkan (frame interpolation)."""
    if AI_MODELS["rife"] and os.path.exists(AI_MODELS["rife"]):
        return True
    binary = _find_binary("rife-ncnn-vulkan")
    if binary:
        AI_MODELS["rife"] = binary
        return True
    ok = _install_binary(
        "rife-ncnn-vulkan",
        "https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/rife-ncnn-vulkan-20221029-ubuntu.zip",
        "rife-ncnn-vulkan.zip"
    )
    if ok:
        AI_MODELS["rife"] = _find_binary("rife-ncnn-vulkan")
    return ok


def check_waifu2x():
    """Check/install waifu2x-ncnn-vulkan (anime-optimized upscaler)."""
    if AI_MODELS["waifu2x"] and os.path.exists(AI_MODELS["waifu2x"]):
        return True
    binary = _find_binary("waifu2x-ncnn-vulkan")
    if binary:
        AI_MODELS["waifu2x"] = binary
        return True
    ok = _install_binary(
        "waifu2x-ncnn-vulkan",
        "https://github.com/nihui/waifu2x-ncnn-vulkan/releases/download/20220728/waifu2x-ncnn-vulkan-20220728-ubuntu.zip",
        "waifu2x-ncnn-vulkan.zip"
    )
    if ok:
        AI_MODELS["waifu2x"] = _find_binary("waifu2x-ncnn-vulkan")
    return ok


def check_vmaf():
    """Check if VMAF library is available for FFmpeg."""
    try:
        result = os.popen("ffmpeg -filters 2>/dev/null | grep libvmaf").read()
        return "libvmaf" in result
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════
#  AI PROCESSING PIPELINES
# ═══════════════════════════════════════════════════════════════════════

async def extract_frames(input_path, frames_dir, message=None):
    """Extract video frames as PNG for AI processing."""
    if message:
        await safe_edit_text(message, "🔬 **AI Pipeline: Extracting frames...**")
    os.makedirs(frames_dir, exist_ok=True)
    rc, _, stderr = await run_cmd([
        "ffmpeg", "-y", "-i", input_path,
        "-vsync", "0",
        os.path.join(frames_dir, "frame_%08d.png")
    ])
    if rc != 0:
        print(f"Frame extraction failed: {stderr.decode()[-300:]}")
        return 0
    count = len([f for f in os.listdir(frames_dir) if f.endswith(".png")])
    return count


async def upscale_realesrgan(input_dir, output_dir, gpu_id, scale=2, message=None):
    """Upscale frames using Real-ESRGAN ncnn-vulkan."""
    if not check_realesrgan():
        return False
    os.makedirs(output_dir, exist_ok=True)
    if message:
        await safe_edit_text(
            message,
            f"🔬 **AI Upscaling (Real-ESRGAN): Processing frames...**\n"
            f"🖥️ GPU: **T4 #{gpu_id}** | Scale: **{scale}x**"
        )
    env = get_nvidia_vulkan_env()
    rc, _, stderr = await run_cmd([
        AI_MODELS["realesrgan"],
        "-i", input_dir, "-o", output_dir,
        "-s", str(scale), "-g", str(gpu_id),
        "-n", "realesrgan-x4plus", "-f", "png"
    ], env=env, timeout=3600)
    if rc != 0:
        print(f"Real-ESRGAN failed: {stderr.decode()[-300:]}")
        return False
    return len([f for f in os.listdir(output_dir) if f.endswith(".png")]) > 0


async def upscale_waifu2x(input_dir, output_dir, gpu_id, scale=2, noise_level=1, message=None):
    """Upscale frames using waifu2x-ncnn-vulkan (better for anime)."""
    if not check_waifu2x():
        return False
    os.makedirs(output_dir, exist_ok=True)
    if message:
        await safe_edit_text(
            message,
            f"🎨 **AI Upscaling (waifu2x): Processing frames...**\n"
            f"🖥️ GPU: **T4 #{gpu_id}** | Scale: **{scale}x** | Denoise: **{noise_level}**"
        )
    env = get_nvidia_vulkan_env()
    rc, _, stderr = await run_cmd([
        AI_MODELS["waifu2x"],
        "-i", input_dir, "-o", output_dir,
        "-s", str(scale), "-g", str(gpu_id),
        "-n", str(noise_level), "-f", "png"
    ], env=env, timeout=3600)
    if rc != 0:
        print(f"waifu2x failed: {stderr.decode()[-300:]}")
        return False
    return len([f for f in os.listdir(output_dir) if f.endswith(".png")]) > 0


async def interpolate_rife(input_dir, output_dir, gpu_id, multiplier=2, model="rife-v4", message=None):
    """Interpolate frames using RIFE ncnn-vulkan (increase FPS)."""
    if not check_rife():
        return False
    os.makedirs(output_dir, exist_ok=True)
    if message:
        await safe_edit_text(
            message,
            f"🎞️ **AI Frame Interpolation (RIFE): Processing...**\n"
            f"🖥️ GPU: **T4 #{gpu_id}** | Multiplier: **{multiplier}x** | Model: **{model}**"
        )
    env = get_nvidia_vulkan_env()
    rc, _, stderr = await run_cmd([
        AI_MODELS["rife"],
        "-i", input_dir, "-o", output_dir,
        "-g", str(gpu_id), "-m", model,
        "-n", str(multiplier)
    ], env=env, timeout=3600)
    if rc != 0:
        print(f"RIFE failed: {stderr.decode()[-300:]}")
        return False
    return len([f for f in os.listdir(output_dir) if f.endswith(".png")]) > 0


async def compute_vmaf(reference_path, distorted_path, message=None):
    """Compute VMAF quality score between reference and encoded video."""
    if not check_vmaf():
        return None
    if message:
        await safe_edit_text(message, "📊 **Computing VMAF quality score...**")
    vmaf_log = "/tmp/vmaf_score.json"
    rc, _, stderr = await run_cmd([
        "ffmpeg", "-y",
        "-i", distorted_path,
        "-i", reference_path,
        "-lavfi", f"libvmaf=log_path={vmaf_log}:log_fmt=json",
        "-f", "null", "-"
    ], timeout=600)
    if rc != 0:
        print(f"VMAF failed: {stderr.decode()[-300:]}")
        return None
    try:
        with open(vmaf_log, "r") as f:
            data = json.load(f)
        score = data.get("pooled_metrics", {}).get("vmaf", {}).get("mean", None)
        if score is None:
            # Try alternative format
            frames = data.get("frames", [])
            if frames:
                scores = [f.get("metrics", {}).get("vmaf", 0) for f in frames]
                score = sum(scores) / len(scores) if scores else None
        return round(score, 2) if score else None
    except Exception as e:
        print(f"VMAF parse error: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
#  COMBINED AI ENCODE PIPELINE
# ═══════════════════════════════════════════════════════════════════════

async def ai_encode_pipeline(input_path, output_path, settings, gpu_id, message=None):
    """
    Full AI-enhanced encoding pipeline:
    1. Extract frames
    2. AI upscaling (Real-ESRGAN or waifu2x)
    3. AI frame interpolation (RIFE, optional)
    4. Encode with FFmpeg (NVENC/AV1)
    5. VMAF quality scoring

    Returns: (success, vmaf_score, ai_info_dict)
    """
    base_dir = os.path.dirname(input_path)
    frames_dir = os.path.join(base_dir, "frames")
    upscaled_dir = os.path.join(base_dir, "upscaled")
    interpolated_dir = os.path.join(base_dir, "interpolated")

    ai_info = {
        "upscaler": None,
        "upscale_scale": 1,
        "interpolated": False,
        "rife_model": None,
        "original_fps": 0,
        "output_fps": 0,
    }

    try:
        # Get source video info
        src_fps = await get_video_fps(input_path)
        ai_info["original_fps"] = round(src_fps, 2)
        src_width, src_height = await get_video_resolution(input_path)

        # ─── Step 1: Extract frames ────────────────────────────────────
        frame_count = await extract_frames(input_path, frames_dir, message)
        if frame_count == 0:
            await safe_edit_text(message, "❌ **Frame extraction failed.**")
            return False, None, ai_info

        current_frames = frames_dir

        # ─── Step 2: AI Upscaling ──────────────────────────────────────
        upscale_enabled = settings.get("ai_upscale", False)
        upscaler = settings.get("upscaler", "realesrgan")  # realesrgan or waifu2x

        if upscale_enabled:
            scale = 2
            chosen_res = settings["resolution"]
            if chosen_res != "original" and src_width > 0:
                target_w = RESOLUTION_WIDTHS.get(chosen_res, 0)
                if target_w > 0:
                    ratio = target_w / src_width
                    scale = 4 if ratio > 2.5 else 2

            success = False
            if upscaler == "waifu2x":
                noise = settings.get("denoise_level", 1)
                success = await upscale_waifu2x(
                    current_frames, upscaled_dir, gpu_id,
                    scale=scale, noise_level=noise, message=message
                )
            else:
                success = await upscale_realesrgan(
                    current_frames, upscaled_dir, gpu_id,
                    scale=scale, message=message
                )

            if success:
                current_frames = upscaled_dir
                ai_info["upscaler"] = upscaler
                ai_info["upscale_scale"] = scale
            else:
                if message:
                    await safe_edit_text(
                        message,
                        f"⚠️ **{upscaler} failed — falling back to Real-ESRGAN...**"
                    )
                # Fallback to realesrgan
                if upscaler != "realesrgan":
                    success = await upscale_realesrgan(
                        current_frames, upscaled_dir, gpu_id,
                        scale=scale, message=message
                    )
                    if success:
                        current_frames = upscaled_dir
                        ai_info["upscaler"] = "realesrgan (fallback)"
                        ai_info["upscale_scale"] = scale

        # ─── Step 3: AI Frame Interpolation (RIFE) ────────────────────
        rife_enabled = settings.get("ai_interpolate", False)
        rife_multiplier = settings.get("rife_multiplier", 2)
        rife_model = settings.get("rife_model", "rife-v4")

        if rife_enabled:
            success = await interpolate_rife(
                current_frames, interpolated_dir, gpu_id,
                multiplier=rife_multiplier, model=rife_model,
                message=message
            )
            if success:
                current_frames = interpolated_dir
                ai_info["interpolated"] = True
                ai_info["rife_model"] = rife_model
                ai_info["output_fps"] = round(src_fps * rife_multiplier, 2)
            else:
                if message:
                    await safe_edit_text(
                        message,
                        "⚠️ **RIFE interpolation failed — encoding at original FPS.**"
                    )
                ai_info["output_fps"] = round(src_fps, 2)
        else:
            ai_info["output_fps"] = round(src_fps, 2)

        # ─── Step 4: Encode with FFmpeg ────────────────────────────────
        if message:
            codec_label = CODEC_OPTIONS[settings["codec"]]["label"]
            await safe_edit_text(
                message,
                f"🎬 **Encoding with {codec_label}...**\n"
                f"📊 CRF: **{settings['crf']}** | Preset: **{settings['preset']}**\n"
                f"🖥️ GPU: **T4 #{gpu_id}**"
            )

        # Build FFmpeg command
        output_fps = ai_info["output_fps"]
        use_gpu_encode = settings.get("gpu_enabled", True)
        codec = settings["codec"]

        if current_frames != frames_dir:
            # Using AI-processed frames (image sequence input)
            cmd = ["ffmpeg", "-y",
                   "-framerate", str(output_fps),
                   "-i", os.path.join(current_frames, "frame_%08d.png"),
                   "-i", input_path,  # Original for audio
                   "-map", "0:v:0", "-map", "1:a?"]
        else:
            # Direct encode (no AI processing)
            if use_gpu_encode:
                cmd = ["ffmpeg", "-y",
                       "-hwaccel", "cuda",
                       "-hwaccel_device", str(gpu_id),
                       "-i", input_path]
            else:
                cmd = ["ffmpeg", "-y", "-i", input_path]

        # Codec selection
        if use_gpu_encode and codec in ("h264", "h265") and current_frames == frames_dir:
            # NVENC hardware encoding (only when not using image sequence)
            nvenc_codec = CODEC_OPTIONS[codec]["nvenc"]
            cmd.extend(["-c:v", nvenc_codec, "-gpu", str(gpu_id)])
            cmd.extend(["-rc", "vbr", "-cq", str(settings["crf"])])

            # Maxrate cap
            chosen_res = settings["resolution"]
            if chosen_res == "original":
                if src_width >= 3840:
                    maxrate = "20M"
                elif src_width >= 2560:
                    maxrate = "12M"
                elif src_width >= 1920:
                    maxrate = "8M"
                elif src_width >= 1280:
                    maxrate = "5M"
                else:
                    maxrate = "3M"
            else:
                maxrate = MAXRATE_MAP.get(chosen_res, "8M")
            cmd.extend(["-maxrate", maxrate, "-bufsize", maxrate])

            # NVENC preset
            nvenc_preset = NVENC_PRESET_MAP.get(settings["preset"], "p4")
            cmd.extend(["-preset", nvenc_preset])

            # Pixel format (T4 = 8-bit NVENC)
            vf_filters = []
            if settings["resolution"] != "original":
                scale = RESOLUTION_MAP.get(settings["resolution"])
                if scale:
                    vf_filters.append(f"scale={scale}")
            vf_filters.append("format=nv12")
            if vf_filters:
                cmd.extend(["-vf", ",".join(vf_filters)])

        else:
            # Software encoding (libx264/libx265/libsvtav1)
            # Used when: AV1 codec, or AI frames (image sequence)
            if codec == "av1":
                cmd.extend(["-c:v", "libsvtav1",
                           "-crf", str(settings["crf"]),
                           "-preset", str(max(0, min(13, _preset_to_svtav1(settings["preset"]))))])
            elif codec == "h265":
                cmd.extend(["-c:v", "libx265",
                           "-crf", str(settings["crf"]),
                           "-preset", settings["preset"],
                           "-x265-params", "log-level=error"])
            else:
                cmd.extend(["-c:v", "libx264",
                           "-crf", str(settings["crf"]),
                           "-preset", settings["preset"]])

            # Maxrate
            chosen_res = settings["resolution"]
            if chosen_res == "original":
                maxrate = "20M" if src_width >= 3840 else "8M"
            else:
                maxrate = MAXRATE_MAP.get(chosen_res, "8M")
            cmd.extend(["-maxrate", maxrate, "-bufsize", maxrate])

            # Scale filter (only if not AI-upscaled)
            vf_filters = []
            if not settings.get("ai_upscale") and settings["resolution"] != "original":
                scale = RESOLUTION_MAP.get(settings["resolution"])
                if scale:
                    vf_filters.append(f"scale={scale}")
            if vf_filters:
                cmd.extend(["-vf", ",".join(vf_filters)])

        # Audio + container
        cmd.extend(["-c:a", "copy", "-movflags", "+faststart"])
        cmd.append(output_path)

        # Execute encoding with progress tracking
        duration = await get_video_duration(input_path)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024
        )
        active_tasks[settings.get("_user_id", 0)] = process

        last_progress = -1
        last_update_time = 0
        start_time = time.time()
        codec_label = CODEC_OPTIONS[codec]["label"]

        while True:
            line = await process.stderr.readline()
            if not line:
                break
            line_text = line.decode("utf-8", errors="ignore")

            if duration > 0:
                progress = parse_ffmpeg_progress(line_text, duration)
                if progress is not None and progress != last_progress:
                    now = time.time()
                    if now - last_update_time >= 5:
                        last_progress = progress
                        last_update_time = now
                        elapsed = now - start_time
                        elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
                        bar = make_progress_bar(progress)
                        if progress > 0:
                            eta_seconds = int((elapsed / progress) * (100 - progress))
                            eta_str = f"{eta_seconds // 60}m {eta_seconds % 60}s"
                        else:
                            eta_str = "calculating..."
                        try:
                            ai_label = ""
                            if ai_info["upscaler"]:
                                ai_label += f"🔬 Upscale: **{ai_info['upscaler']} {ai_info['upscale_scale']}x**\n"
                            if ai_info["interpolated"]:
                                ai_label += f"🎞️ Interpolation: **RIFE {rife_multiplier}x** ({ai_info['original_fps']}→{ai_info['output_fps']} fps)\n"
                            await message.edit_text(
                                f"🔄 **Encoding in Progress...**\n\n"
                                f"🎬 Codec: **{codec_label}** | CRF: **{settings['crf']}**\n"
                                f"⚡ Preset: **{settings['preset']}** | Res: **{settings['resolution']}**\n"
                                f"🖥️ GPU: **T4 #{gpu_id}**\n"
                                f"{ai_label}\n"
                                f"[{bar}] **{progress}%**\n\n"
                                f"⏱️ Elapsed: **{elapsed_str}**\n"
                                f"⏳ ETA: **{eta_str}**"
                            )
                        except Exception:
                            pass

        await process.wait()
        if settings.get("_user_id", 0) in active_tasks:
            del active_tasks[settings["_user_id"]]

        if process.returncode != 0:
            return False, None, ai_info

        # ─── Step 5: VMAF Quality Score ────────────────────────────────
        vmaf_score = None
        if check_vmaf() and os.path.exists(output_path):
            vmaf_score = await compute_vmaf(input_path, output_path, message)
            ai_info["vmaf"] = vmaf_score

        return True, vmaf_score, ai_info

    except Exception as e:
        print(f"AI encode pipeline error: {e}")
        import traceback
        traceback.print_exc()
        return False, None, ai_info

    finally:
        # Cleanup temp dirs
        for d in [frames_dir, upscaled_dir, interpolated_dir]:
            if d and os.path.exists(d):
                shutil.rmtree(d, ignore_errors=True)


def _preset_to_svtav1(preset_name):
    """Map x264-style preset names to SVT-AV1 preset values (0-13)."""
    mapping = {
        "ultrafast": 13, "superfast": 12, "veryfast": 11,
        "faster": 10, "fast": 9, "medium": 8,
        "slow": 6, "slower": 4, "veryslow": 2
    }
    return mapping.get(preset_name, 8)


# ═══════════════════════════════════════════════════════════════════════
#  GOOGLE DRIVE UPLOAD
# ═══════════════════════════════════════════════════════════════════════

def get_gdrive_service():
    """Create and return a Google Drive API service instance."""
    sa_json = os.environ.get("GDRIVE_SA_JSON", "")
    if sa_json:
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"]
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            GDRIVE_SA_FILE, scopes=["https://www.googleapis.com/auth/drive"]
        )
    return build("drive", "v3", credentials=creds)


async def upload_to_gdrive(file_path, file_name, message):
    """Upload a file to Google Drive with progress and return the shareable link."""
    service = get_gdrive_service()
    file_metadata = {"name": file_name, "parents": [GDRIVE_FOLDER_ID]}
    file_size = os.path.getsize(file_path)
    total_mb = file_size / (1024 * 1024)
    media = MediaFileUpload(file_path, resumable=True, chunksize=50 * 1024 * 1024)
    request = service.files().create(body=file_metadata, media_body=media, fields="id, webViewLink")

    upload_start = time.time()
    last_update = 0
    response = None

    while response is None:
        status, response = await asyncio.to_thread(request.next_chunk)
        if status:
            now = time.time()
            if now - last_update >= 3:
                last_update = now
                pct = int(status.progress() * 100)
                uploaded_mb = status.resumable_progress / (1024 * 1024)
                bar = make_progress_bar(pct)
                elapsed = now - upload_start
                speed = uploaded_mb / elapsed if elapsed > 0 else 0
                remaining_mb = total_mb - uploaded_mb
                eta = int(remaining_mb / speed) if speed > 0 else 0
                eta_str = f"{eta // 60}m {eta % 60}s" if eta > 0 else "calculating..."
                try:
                    await message.edit_text(
                        f"☁️ **Uploading to Google Drive...**\n\n"
                        f"[{bar}] **{pct}%**\n\n"
                        f"📤 {uploaded_mb:.1f} MB / {total_mb:.1f} MB\n"
                        f"🚀 Speed: **{speed:.1f} MB/s**\n"
                        f"⏳ ETA: **{eta_str}**"
                    )
                except Exception:
                    pass

    file_id = response.get("id")
    service.permissions().create(
        fileId=file_id, body={"type": "anyone", "role": "reader"}
    ).execute()
    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"


# ═══════════════════════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════

def get_main_keyboard(settings):
    codec_label = CODEC_OPTIONS.get(settings["codec"], {}).get("label", "H.264")
    codec_short = codec_label.split(" ")[0]  # e.g. "H.264"
    gpu_label = "ON" if settings["gpu_enabled"] else "OFF"
    upscale_label = "ON" if settings.get("ai_upscale") else "OFF"
    rife_label = "ON" if settings.get("ai_interpolate") else "OFF"
    upscaler_name = settings.get("upscaler", "realesrgan").split("-")[0].title()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎬 Codec: {codec_short}", callback_data="codec"),
         InlineKeyboardButton(f"📊 CRF: {settings['crf']}", callback_data="crf")],
        [InlineKeyboardButton(f"⚡ Preset: {settings['preset']}", callback_data="preset"),
         InlineKeyboardButton(f"📐 Res: {settings['resolution']}", callback_data="resolution")],
        [InlineKeyboardButton(f"🔬 Upscale: {upscale_label} ({upscaler_name})", callback_data="toggle_upscale"),
         InlineKeyboardButton(f"🎞️ RIFE: {rife_label}", callback_data="toggle_rife")],
        [InlineKeyboardButton(f"🚀 GPU Encode: {gpu_label}", callback_data="toggle_gpu"),
         InlineKeyboardButton(f"📊 VMAF: {'ON' if settings.get('vmaf_check') else 'OFF'}", callback_data="toggle_vmaf")],
        [InlineKeyboardButton("✅ Start Encoding", callback_data="encode")],
    ])


def get_codec_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("H.264 (NVENC)", callback_data="set_codec_h264"),
         InlineKeyboardButton("H.265 (NVENC)", callback_data="set_codec_h265")],
        [InlineKeyboardButton("AV1 (SVT-AV1) 🆕", callback_data="set_codec_av1")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])


def get_resolution_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("4K (2160p)", callback_data="set_res_4k"),
         InlineKeyboardButton("2K (1440p)", callback_data="set_res_2k")],
        [InlineKeyboardButton("1080p", callback_data="set_res_1080p"),
         InlineKeyboardButton("720p", callback_data="set_res_720p")],
        [InlineKeyboardButton("540p", callback_data="set_res_540p"),
         InlineKeyboardButton("480p", callback_data="set_res_480p")],
        [InlineKeyboardButton("360p", callback_data="set_res_360p"),
         InlineKeyboardButton("Original", callback_data="set_res_original")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])


def get_preset_keyboard():
    rows = []
    for i in range(0, len(PRESET_LIST), 3):
        row = [InlineKeyboardButton(p.capitalize(), callback_data=f"set_preset_{p}") for p in PRESET_LIST[i:i+3]]
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def get_crf_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("18 (High)", callback_data="set_crf_18"),
         InlineKeyboardButton("20", callback_data="set_crf_20"),
         InlineKeyboardButton("23 (Default)", callback_data="set_crf_23")],
        [InlineKeyboardButton("26", callback_data="set_crf_26"),
         InlineKeyboardButton("28 (Low)", callback_data="set_crf_28"),
         InlineKeyboardButton("30", callback_data="set_crf_30")],
        [InlineKeyboardButton("💬 Enter Custom CRF (0-51)", callback_data="custom_crf")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])


def get_upscaler_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔬 Real-ESRGAN (General)", callback_data="set_upscaler_realesrgan"),
         InlineKeyboardButton("🎨 waifu2x (Anime)", callback_data="set_upscaler_waifu2x")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])


def get_rife_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("2x (24→48 fps)", callback_data="set_rife_2"),
         InlineKeyboardButton("4x (24→96 fps)", callback_data="set_rife_4")],
        [InlineKeyboardButton("Model: RIFE v4", callback_data="set_rife_model_rife-v4"),
         InlineKeyboardButton("Model: RIFE v4.6", callback_data="set_rife_model_rife-v4.6")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])


# ═══════════════════════════════════════════════════════════════════════
#  COMMANDS
# ═══════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("start"))
async def start_command(client, message):
    await react_to_message(client, message.chat.id, message.id)
    thumb = get_random_thumbnail()
    caption = (
        f"👋 Hello **{message.from_user.first_name}**!\n\n"
        "🎬 I am **AI Video Encoder Bot**\n"
        "_Professional AI-Enhanced Video Encoding._\n\n"
        "> 🧠 AI Upscaling: Real-ESRGAN / waifu2x\n"
        "> 🎞️ AI Frame Interpolation: RIFE\n"
        "> 📊 Quality Metrics: VMAF\n"
        "> 🎬 Codecs: H.264 / H.265 / AV1\n"
        "> 📐 Resolution: Up to 4K\n\n"
        "**Commands:**\n"
        "/start - Start the bot\n"
        "/help - Help & guide\n"
        "/encode - Show encoding settings\n"
        "/settings - Current settings\n"
        "/status - Bot status\n"
        "/models - Check AI model status\n"
        "/cancel - Cancel encoding\n"
        "/about - About this bot\n\n"
        "👇 **Send a video to get started!**"
    )
    if thumb:
        await message.reply_photo(photo=thumb, caption=caption)
    else:
        await message.reply_text(caption)


@app.on_message(filters.command("help"))
async def help_command(client, message):
    await react_to_message(client, message.chat.id, message.id)
    thumb = get_random_thumbnail()
    caption = (
        "📖 **Help & Guide**\n\n"
        "**How to use:**\n"
        "1️⃣ Send a video file to the bot\n"
        "2️⃣ Choose your AI encoding settings:\n\n"
        "   • **Codec** — H.264, H.265 (NVENC GPU), or AV1 (CPU)\n"
        "   • **CRF** — Quality (0=lossless, 23=default, 51=worst)\n"
        "   • **Preset** — Speed vs compression\n"
        "   • **Resolution** — 4K down to 360p\n"
        "   • **AI Upscale** — Real-ESRGAN or waifu2x adds real detail\n"
        "   • **RIFE** — AI frame interpolation (smooth motion)\n"
        "   • **VMAF** — Perceptual quality score after encode\n\n"
        "3️⃣ Press **Start Encoding**\n"
        "4️⃣ Wait for the AI pipeline to finish\n"
        "5️⃣ Download your AI-enhanced video!\n\n"
        "**AI Models (all use T4 GPU via Vulkan/NCNN):**\n"
        "• 🔬 **Real-ESRGAN** — General-purpose AI upscaler\n"
        "• 🎨 **waifu2x** — Best for anime/cartoon content\n"
        "• 🎞️ **RIFE** — Real-time frame interpolation\n"
        "• 📊 **VMAF** — Netflix perceptual quality metric\n\n"
        "**Tips:**\n"
        "• Lower CRF = better quality but bigger file\n"
        "• AV1 gives ~30% better compression than H.265\n"
        "• Use waifu2x for anime, Real-ESRGAN for live-action\n"
        "• RIFE 2x doubles smoothness, great for anime panning"
    )
    if thumb:
        await message.reply_photo(photo=thumb, caption=caption)
    else:
        await message.reply_text(caption)


@app.on_message(filters.command("models"))
async def models_command(client, message):
    """Check status of all AI models."""
    await react_to_message(client, message.chat.id, message.id)

    realesrgan_ok = check_realesrgan()
    rife_ok = check_rife()
    waifu2x_ok = check_waifu2x()
    vmaf_ok = check_vmaf()

    text = (
        "🧠 **AI Model Status**\n\n"
        f"{'✅' if realesrgan_ok else '❌'} **Real-ESRGAN** — General AI upscaler\n"
        f"{'✅' if waifu2x_ok else '❌'} **waifu2x** — Anime AI upscaler\n"
        f"{'✅' if rife_ok else '❌'} **RIFE** — AI frame interpolation\n"
        f"{'✅' if vmaf_ok else '❌'} **VMAF** — Quality metrics\n\n"
        "All AI models use **NCNN + Vulkan** for T4 GPU acceleration.\n"
        "_Models are auto-installed on first use._"
    )
    await message.reply_text(text)


@app.on_message(filters.command("encode"))
async def encode_command(client, message):
    await react_to_message(client, message.chat.id, message.id)
    user_id = message.from_user.id
    if user_id in user_settings and user_settings[user_id].get("file_id"):
        settings = user_settings[user_id]
        await message.reply_text(
            "⚙️ **Current Encoding Settings:**\n\nModify your settings below:",
            reply_markup=get_main_keyboard(settings)
        )
    else:
        await message.reply_text(
            "📹 **No video loaded!**\n\n"
            "Send me a video file first, then use /encode to see settings."
        )


@app.on_message(filters.command("settings"))
async def settings_command(client, message):
    await react_to_message(client, message.chat.id, message.id)
    user_id = message.from_user.id
    if user_id in user_settings:
        s = user_settings[user_id]
        codec_name = CODEC_OPTIONS.get(s["codec"], {}).get("label", "H.264")
        upscaler = s.get("upscaler", "realesrgan").title()
        text = (
            "⚙️ **Your Current Settings:**\n\n"
            f"🎬 Codec: **{codec_name}**\n"
            f"📊 CRF: **{s['crf']}**\n"
            f"⚡ Preset: **{s['preset']}**\n"
            f"📐 Resolution: **{s['resolution']}**\n"
            f"🔬 AI Upscale: **{'ON (' + upscaler + ')' if s.get('ai_upscale') else 'OFF'}**\n"
            f"🎞️ RIFE: **{'ON (' + str(s.get('rife_multiplier', 2)) + 'x)' if s.get('ai_interpolate') else 'OFF'}**\n"
            f"📊 VMAF: **{'ON' if s.get('vmaf_check') else 'OFF'}**\n"
            f"🚀 GPU Encode: **{'ON' if s['gpu_enabled'] else 'OFF'}**\n"
            f"📁 File: **{s.get('file_name', 'None')}**"
        )
    else:
        text = (
            "⚙️ **Default Settings:**\n\n"
            "🎬 Codec: **H.264 (NVENC)**\n"
            "📊 CRF: **23**\n"
            "⚡ Preset: **medium**\n"
            "📐 Resolution: **original**\n"
            "🔬 AI Upscale: **OFF**\n"
            "🎞️ RIFE: **OFF**\n"
            "📊 VMAF: **OFF**\n"
            "🚀 GPU Encode: **ON**\n\n"
            "_Send a video to customize settings._"
        )
    await message.reply_text(text)


@app.on_message(filters.command("status"))
async def status_command(client, message):
    await react_to_message(client, message.chat.id, message.id)

    try:
        rc, stdout, _ = await run_cmd(["ffmpeg", "-version"])
        ffmpeg_ver = stdout.decode().split('\n')[0] if stdout else "Not found"
    except Exception:
        ffmpeg_ver = "❌ Not installed"

    active_count = len(active_tasks)

    # Check GPU
    gpu_info = "Unknown"
    try:
        rc, stdout, _ = await run_cmd(["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader"])
        if rc == 0:
            gpu_info = stdout.decode().strip().split("\n")[0]
    except Exception:
        pass

    text = (
        "📊 **Bot Status**\n\n"
        f"🟢 Status: **Online**\n"
        f"⏱️ Uptime: **{get_uptime()}**\n"
        f"🎬 Active Encodes: **{active_count}**\n"
        f"🔧 FFmpeg: `{ffmpeg_ver}`\n"
        f"🖥️ GPU: `{gpu_info}`\n"
        f"📅 Time: **{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}**"
    )
    await message.reply_text(text)


@app.on_message(filters.command("cancel"))
async def cancel_command(client, message):
    await react_to_message(client, message.chat.id, message.id)
    user_id = message.from_user.id
    if user_id in active_tasks:
        process = active_tasks[user_id]
        try:
            process.kill()
        except Exception:
            pass
        del active_tasks[user_id]
        await message.reply_text("🛑 **Encoding cancelled!**\n\nSend another video to start fresh.")
    else:
        await message.reply_text("ℹ️ **No active encoding to cancel.**")


@app.on_message(filters.command("about"))
async def about_command(client, message):
    await react_to_message(client, message.chat.id, message.id)
    thumb = get_random_thumbnail()
    caption = (
        "ℹ️ **About AI Video Encoder Bot**\n\n"
        "A powerful Telegram bot for **AI-enhanced** video encoding.\n\n"
        "**AI Capabilities (T4 GPU via NCNN/Vulkan):**\n"
        "• 🔬 Real-ESRGAN — Neural network upscaling\n"
        "• 🎨 waifu2x — Anime-optimized AI upscaler\n"
        "• 🎞️ RIFE — AI frame interpolation (2x/4x)\n"
        "• 📊 VMAF — Perceptual quality scoring\n\n"
        "**Encoding:**\n"
        "• H.264 & H.265 via NVENC (GPU)\n"
        "• AV1 via SVT-AV1 (CPU, best compression)\n"
        "• Custom CRF quality (0-51)\n"
        "• 9 presets (ultrafast → veryslow)\n"
        "• Resolution: 4K / 2K / 1080p / 720p / 540p / 480p / 360p\n\n"
        "**Pipeline:** AI Preprocess → Encode → Quality Score\n\n"
        "**Powered by:** FFmpeg + Pyrogram + NCNN + Vulkan"
    )
    if thumb:
        await message.reply_photo(photo=thumb, caption=caption)
    else:
        await message.reply_text(caption)


# ═══════════════════════════════════════════════════════════════════════
#  VIDEO HANDLER
# ═══════════════════════════════════════════════════════════════════════

@app.on_message(filters.video | filters.document)
async def video_handler(client, message):
    if message.document:
        mime = message.document.mime_type or ""
        if not mime.startswith("video/"):
            return

    user_id = message.from_user.id
    file_id = message.video.file_id if message.video else message.document.file_id
    file_name = (message.video.file_name if message.video else message.document.file_name) or f"video_{user_id}.mp4"
    file_size = (message.video.file_size if message.video else message.document.file_size) or 0

    await react_to_message(client, message.chat.id, message.id)

    user_settings[user_id] = {
        "file_id": file_id,
        "file_name": file_name,
        "file_size": file_size,
        "codec": "h264",
        "crf": 23,
        "preset": "medium",
        "resolution": "original",
        "gpu_enabled": True,
        "ai_upscale": True,
        "upscaler": "realesrgan",
        "denoise_level": 1,
        "ai_interpolate": False,
        "rife_multiplier": 2,
        "rife_model": "rife-v4",
        "vmaf_check": True,
        "awaiting_crf": False,
        "message_id": None,
        "original_msg_id": message.id,
        "source_width": 0,
        "source_height": 0,
    }

    settings = user_settings[user_id]
    msg = await message.reply_text(
        "✅ **Video received!**\n\n"
        "🧠 **AI-Enhanced Encoding Pipeline:**\n"
        "Choose your settings below:",
        reply_markup=get_main_keyboard(settings)
    )
    user_settings[user_id]["message_id"] = msg.id


# ═══════════════════════════════════════════════════════════════════════
#  CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════════════════

@app.on_callback_query()
async def callback_handler(client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    data = callback_query.data
    message = callback_query.message

    if user_id not in user_settings:
        await callback_query.answer("Please send a video first.", show_alert=True)
        return

    settings = user_settings[user_id]

    # ─── Codec ─────────────────────────────────────────────────────────
    if data == "codec":
        await safe_edit_text(message, "🎬 **Choose Codec:**\n\n"
            "• H.264 — Fast, compatible (NVENC GPU)\n"
            "• H.265 — Better compression (NVENC GPU)\n"
            "• AV1 — Best compression (CPU, slower)",
            reply_markup=get_codec_keyboard())

    elif data == "set_codec_h264":
        settings["codec"] = "h264"
        await callback_query.answer("Codec: H.264 (NVENC)")
        await safe_edit_text(message, "Choose your encoding settings:", reply_markup=get_main_keyboard(settings))

    elif data == "set_codec_h265":
        settings["codec"] = "h265"
        await callback_query.answer("Codec: H.265 (NVENC)")
        await safe_edit_text(message, "Choose your encoding settings:", reply_markup=get_main_keyboard(settings))

    elif data == "set_codec_av1":
        settings["codec"] = "av1"
        settings["gpu_enabled"] = False  # AV1 is CPU-only
        await callback_query.answer("Codec: AV1 (SVT-AV1, CPU)")
        await safe_edit_text(
            message,
            "🎬 **AV1 selected** — Best compression, ~30% smaller than H.265.\n"
            "⚠️ AV1 uses CPU encoding (slower). GPU encode disabled.\n\n"
            "Choose your encoding settings:",
            reply_markup=get_main_keyboard(settings)
        )

    # ─── CRF ───────────────────────────────────────────────────────────
    elif data == "crf":
        await safe_edit_text(message, "📊 **Choose CRF (lower = better quality, bigger file):**", reply_markup=get_crf_keyboard())

    elif data.startswith("set_crf_"):
        crf_val = int(data.replace("set_crf_", ""))
        settings["crf"] = crf_val
        await callback_query.answer(f"CRF set to {crf_val}")
        await safe_edit_text(message, "Choose your encoding settings:", reply_markup=get_main_keyboard(settings))

    elif data == "custom_crf":
        settings["awaiting_crf"] = True
        await callback_query.answer()
        await safe_edit_text(message, "💬 **Enter a custom CRF value (0-51):**\n\n0 = Lossless, 23 = Default, 51 = Worst quality")

    # ─── Preset ────────────────────────────────────────────────────────
    elif data == "preset":
        await safe_edit_text(message, "⚡ **Choose Encoding Preset:**\n\n(Slower = better compression, longer time)", reply_markup=get_preset_keyboard())

    elif data.startswith("set_preset_"):
        preset = data.replace("set_preset_", "")
        settings["preset"] = preset
        await callback_query.answer(f"Preset: {preset}")
        await safe_edit_text(message, "Choose your encoding settings:", reply_markup=get_main_keyboard(settings))

    # ─── Resolution ────────────────────────────────────────────────────
    elif data == "resolution":
        await safe_edit_text(message, "📐 **Choose Output Resolution:**", reply_markup=get_resolution_keyboard())

    elif data.startswith("set_res_"):
        res = data.replace("set_res_", "")
        settings["resolution"] = res
        hint = ""
        src_w = settings.get("source_width", 0)
        if src_w > 0 and res != "original":
            target_w = RESOLUTION_WIDTHS.get(res, 0)
            src_label = get_resolution_label(src_w)
            if target_w > src_w:
                if settings.get("ai_upscale"):
                    hint = f"\n\n🔬 AI Upscale is ON — Real-ESRGAN will add real detail when upscaling to {res.upper()}."
                else:
                    hint = f"\n\n⚠️ Source is {src_label} ({src_w}p). Upscaling without AI won't improve quality. Enable **AI Upscale**."
            elif target_w < src_w:
                hint = f"\n\n✅ Downscaling from {src_label} to {res.upper()} will reduce file size."
        await callback_query.answer(f"Resolution: {res}" if not hint else f"Resolution: {res}", show_alert=bool(hint))
        await safe_edit_text(message, f"Choose your encoding settings:{hint}", reply_markup=get_main_keyboard(settings))

    # ─── GPU Toggle ────────────────────────────────────────────────────
    elif data == "toggle_gpu":
        if settings["codec"] == "av1":
            await callback_query.answer("AV1 uses CPU encoding — GPU toggle disabled.", show_alert=True)
        else:
            settings["gpu_enabled"] = not settings["gpu_enabled"]
            gpu_status = "ON" if settings["gpu_enabled"] else "OFF"
            await callback_query.answer(f"GPU encode: {gpu_status}")
        await safe_edit_text(message, "Choose your encoding settings:", reply_markup=get_main_keyboard(settings))

    # ─── AI Upscale Toggle ─────────────────────────────────────────────
    elif data == "toggle_upscale":
        if not settings.get("ai_upscale"):
            # Turning ON
            settings["ai_upscale"] = True
            await callback_query.answer("AI Upscale: ON")
            await safe_edit_text(
                message,
                "🔬 **AI Upscale: ON**\n\n"
                "Choose your AI upscaler:\n"
                "• **Real-ESRGAN** — Best for live-action, general purpose\n"
                "• **waifu2x** — Best for anime/cartoon, includes denoising\n\n"
                "Both use T4 GPU via NCNN/Vulkan for fast inference.",
                reply_markup=get_upscaler_keyboard()
            )
        else:
            # Turning OFF
            settings["ai_upscale"] = False
            await callback_query.answer("AI Upscale: OFF")
            await safe_edit_text(message, "Choose your encoding settings:", reply_markup=get_main_keyboard(settings))

    # ─── Upscaler Selection ────────────────────────────────────────────
    elif data == "set_upscaler_realesrgan":
        settings["upscaler"] = "realesrgan"
        if not check_realesrgan():
            await callback_query.answer("❌ Real-ESRGAN not available.", show_alert=True)
            return
        await callback_query.answer("Upscaler: Real-ESRGAN")
        await safe_edit_text(
            message,
            "🔬 **Real-ESRGAN selected** — General-purpose AI upscaler.\n"
            "Adds real detail using deep learning. Great for live-action video.\n\n"
            "Choose your encoding settings:",
            reply_markup=get_main_keyboard(settings)
        )

    elif data == "set_upscaler_waifu2x":
        settings["upscaler"] = "waifu2x"
        if not check_waifu2x():
            await callback_query.answer("❌ waifu2x not available.", show_alert=True)
            return
        await callback_query.answer("Upscaler: waifu2x")
        await safe_edit_text(
            message,
            "🎨 **waifu2x selected** — Anime-optimized AI upscaler.\n"
            "Includes built-in denoising. Best for anime, cartoons, and illustrations.\n\n"
            "Choose your encoding settings:",
            reply_markup=get_main_keyboard(settings)
        )

    # ─── RIFE Toggle ───────────────────────────────────────────────────
    elif data == "toggle_rife":
        if not settings.get("ai_interpolate"):
            settings["ai_interpolate"] = True
            if not check_rife():
                await callback_query.answer("❌ RIFE not available.", show_alert=True)
                settings["ai_interpolate"] = False
                return
            await callback_query.answer("RIFE: ON")
            await safe_edit_text(
                message,
                "🎞️ **RIFE Frame Interpolation: ON**\n\n"
                "Choose interpolation settings:\n"
                "• **2x** — Double FPS (24→48, 30→60)\n"
                "• **4x** — Quadruple FPS (24→96)\n\n"
                "RIFE uses AI to generate intermediate frames for smoother motion.",
                reply_markup=get_rife_keyboard()
            )
        else:
            settings["ai_interpolate"] = False
            await callback_query.answer("RIFE: OFF")
            await safe_edit_text(message, "Choose your encoding settings:", reply_markup=get_main_keyboard(settings))

    # ─── RIFE Settings ─────────────────────────────────────────────────
    elif data.startswith("set_rife_") and "model" not in data:
        multiplier = int(data.replace("set_rife_", ""))
        settings["rife_multiplier"] = multiplier
        await callback_query.answer(f"RIFE: {multiplier}x interpolation")
        await safe_edit_text(message, "Choose your encoding settings:", reply_markup=get_main_keyboard(settings))

    elif data.startswith("set_rife_model_"):
        model = data.replace("set_rife_model_", "")
        settings["rife_model"] = model
        await callback_query.answer(f"RIFE model: {model}")
        await safe_edit_text(message, "Choose your encoding settings:", reply_markup=get_main_keyboard(settings))

    # ─── VMAF Toggle ───────────────────────────────────────────────────
    elif data == "toggle_vmaf":
        settings["vmaf_check"] = not settings.get("vmaf_check", False)
        status = "ON" if settings["vmaf_check"] else "OFF"
        await callback_query.answer(f"VMAF: {status}")
        if settings["vmaf_check"] and not check_vmaf():
            await safe_edit_text(
                message,
                "📊 **VMAF: ON**\n\n"
                "⚠️ VMAF library not found in FFmpeg.\n"
                "Quality scoring will be skipped if unavailable.\n\n"
                "Choose your encoding settings:",
                reply_markup=get_main_keyboard(settings)
            )
        else:
            await safe_edit_text(message, "Choose your encoding settings:", reply_markup=get_main_keyboard(settings))

    # ─── Start Encoding ────────────────────────────────────────────────
    elif data == "encode":
        await callback_query.answer("Encoding started! 🚀")
        await safe_edit_text(message, "⏳ **Starting AI encoding pipeline...**")
        await process_video(client, message, user_id, settings)

    # ─── Back ──────────────────────────────────────────────────────────
    elif data == "back_main":
        settings["awaiting_crf"] = False
        await safe_edit_text(message, "Choose your encoding settings:", reply_markup=get_main_keyboard(settings))


@app.on_message(filters.text & filters.private)
async def text_handler(client, message):
    user_id = message.from_user.id
    if user_id in user_settings and user_settings[user_id].get("awaiting_crf"):
        try:
            crf_value = int(message.text.strip())
            if 0 <= crf_value <= 51:
                user_settings[user_id]["crf"] = crf_value
                user_settings[user_id]["awaiting_crf"] = False
                settings = user_settings[user_id]
                await message.reply_text(
                    f"✅ CRF set to **{crf_value}**\n\nChoose your encoding settings:",
                    reply_markup=get_main_keyboard(settings)
                )
            else:
                await message.reply_text("❌ CRF must be between 0 and 51. Try again.")
        except ValueError:
            await message.reply_text("❌ Please enter a valid number (0-51).")


# ═══════════════════════════════════════════════════════════════════════
#  MAIN PROCESSING
# ═══════════════════════════════════════════════════════════════════════

async def process_video(client, message, user_id, settings):
    """Download, AI-enhance, encode, and upload the video."""
    download_dir = f"downloads/{user_id}_{int(time.time())}"
    os.makedirs(download_dir, exist_ok=True)
    os.makedirs("encoded", exist_ok=True)

    input_path = os.path.join(download_dir, settings["file_name"])
    base_name = os.path.splitext(settings["file_name"])[0]
    codec_tag = settings["codec"].upper()
    output_name = f"{base_name}_ai_{codec_tag}_crf{settings['crf']}_{settings['resolution']}.mp4"
    output_path = os.path.join("encoded", output_name)

    start_time = time.time()
    gpu_id = None

    try:
        # ─── Download ──────────────────────────────────────────────────
        total_size = settings.get("file_size", 0)
        total_mb = total_size / (1024 * 1024) if total_size else 0
        dl_start_time = time.time()

        async def monitor_download_progress():
            while True:
                await asyncio.sleep(4)
                try:
                    current_size = 0
                    if os.path.exists(input_path):
                        current_size = os.path.getsize(input_path)
                    else:
                        for f in os.listdir(download_dir):
                            fp = os.path.join(download_dir, f)
                            if os.path.isfile(fp):
                                current_size += os.path.getsize(fp)
                    if total_size > 0 and current_size > 0:
                        pct = min(int(current_size * 100 / total_size), 99)
                        cur_mb = current_size / (1024 * 1024)
                        elapsed = time.time() - dl_start_time
                        speed = cur_mb / elapsed if elapsed > 0 else 0
                        remaining = (total_mb - cur_mb) / speed if speed > 0 else 0
                        eta_str = f"{int(remaining) // 60}m {int(remaining) % 60}s"
                        bar = make_progress_bar(pct)
                        await message.edit_text(
                            f"⬇️ **Downloading...**\n\n"
                            f"[{bar}] **{pct}%**\n\n"
                            f"📥 {cur_mb:.1f} MB / {total_mb:.1f} MB\n"
                            f"🚀 Speed: **{speed:.1f} MB/s**\n"
                            f"⏳ ETA: **{eta_str}**"
                        )
                except Exception:
                    pass

        await safe_edit_text(message, f"⬇️ **Downloading video ({total_mb:.1f} MB)...**")
        progress_task = asyncio.create_task(monitor_download_progress())
        try:
            await client.download_media(settings["file_id"], file_name=input_path)
        finally:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass

        dl_elapsed = time.time() - dl_start_time
        dl_speed = total_mb / dl_elapsed if dl_elapsed > 0 else 0
        await safe_edit_text(
            message,
            f"✅ **Download complete!** ({total_mb:.1f} MB in {int(dl_elapsed)}s, {dl_speed:.1f} MB/s)"
        )
        await react_to_message(client, message.chat.id, message.id)

        # ─── Detect source resolution ──────────────────────────────────
        src_width, src_height = await get_video_resolution(input_path)
        settings["source_width"] = src_width
        settings["source_height"] = src_height
        src_label = get_resolution_label(src_width) if src_width > 0 else "unknown"

        # Warn if upscaling without AI
        chosen_res = settings["resolution"]
        if chosen_res != "original" and src_width > 0:
            target_width = RESOLUTION_WIDTHS.get(chosen_res, 0)
            if target_width > src_width:
                if settings.get("ai_upscale"):
                    scale_factor = max(2, round(target_width / src_width))
                    await safe_edit_text(
                        message,
                        f"🔬 **AI Upscale: {src_label} → {chosen_res.upper()}**\n\n"
                        f"Source: **{src_label}** ({src_width}x{src_height})\n"
                        f"Target: **{chosen_res.upper()}** ({target_width}p)\n"
                        f"Scale factor: **{scale_factor}x**\n"
                        f"Upscaler: **{settings.get('upscaler', 'realesrgan').title()}**\n\n"
                        f"⏳ AI processing will take a while..."
                    )
                else:
                    await safe_edit_text(
                        message,
                        f"⚠️ **Upscaling Warning**\n\n"
                        f"Source is **{src_label}**, target is **{chosen_res.upper()}**.\n"
                        f"Upscaling without AI doesn't add detail.\n\n"
                        f"💡 Enable **AI Upscale** for real detail enhancement.\n"
                        f"Using source resolution instead."
                    )
                    chosen_res = "original"
                    settings["resolution"] = "original"
                    await asyncio.sleep(3)

        # ─── Acquire GPU ───────────────────────────────────────────────
        gpu_id = await acquire_gpu()
        settings["_user_id"] = user_id

        # ─── AI Encode Pipeline ────────────────────────────────────────
        has_ai = settings.get("ai_upscale") or settings.get("ai_interpolate")

        if has_ai:
            success, vmaf_score, ai_info = await ai_encode_pipeline(
                input_path, output_path, settings, gpu_id, message
            )
        else:
            # Direct encode (no AI processing)
            duration = await get_video_duration(input_path)
            use_gpu = settings.get("gpu_enabled", True) and settings["codec"] != "av1"

            if use_gpu:
                cmd = ["ffmpeg", "-y",
                       "-hwaccel", "cuda", "-hwaccel_device", str(gpu_id),
                       "-i", input_path]
                nvenc_codec = CODEC_OPTIONS[settings["codec"]]["nvenc"]
                cmd.extend(["-c:v", nvenc_codec, "-gpu", str(gpu_id)])
                cmd.extend(["-rc", "vbr", "-cq", str(settings["crf"])])
                maxrate = MAXRATE_MAP.get(chosen_res, "8M") if chosen_res != "original" else "8M"
                if src_width >= 3840:
                    maxrate = "20M"
                elif src_width >= 1920:
                    maxrate = "8M"
                cmd.extend(["-maxrate", maxrate, "-bufsize", maxrate])
                cmd.extend(["-preset", NVENC_PRESET_MAP.get(settings["preset"], "p4")])
                vf_filters = []
                if settings["resolution"] != "original":
                    scale = RESOLUTION_MAP.get(settings["resolution"])
                    if scale:
                        vf_filters.append(f"scale={scale}")
                vf_filters.append("format=nv12")
                if vf_filters:
                    cmd.extend(["-vf", ",".join(vf_filters)])
            else:
                cmd = ["ffmpeg", "-y", "-i", input_path]
                if settings["codec"] == "av1":
                    cmd.extend(["-c:v", "libsvtav1",
                               "-crf", str(settings["crf"]),
                               "-preset", str(_preset_to_svtav1(settings["preset"]))])
                elif settings["codec"] == "h265":
                    cmd.extend(["-c:v", "libx265", "-crf", str(settings["crf"]),
                               "-preset", settings["preset"],
                               "-x265-params", "log-level=error"])
                else:
                    cmd.extend(["-c:v", "libx264", "-crf", str(settings["crf"]),
                               "-preset", settings["preset"]])
                maxrate = MAXRATE_MAP.get(chosen_res, "8M") if chosen_res != "original" else "8M"
                cmd.extend(["-maxrate", maxrate, "-bufsize", maxrate])
                if settings["resolution"] != "original":
                    scale = RESOLUTION_MAP.get(settings["resolution"])
                    if scale:
                        cmd.extend(["-vf", f"scale={scale}"])

            cmd.extend(["-c:a", "copy", "-movflags", "+faststart", output_path])

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=1024*1024
            )
            active_tasks[user_id] = process

            last_progress = -1
            last_update_time = 0
            encode_start = time.time()
            codec_label = CODEC_OPTIONS[settings["codec"]]["label"]

            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                line_text = line.decode("utf-8", errors="ignore")
                if duration > 0:
                    progress = parse_ffmpeg_progress(line_text, duration)
                    if progress is not None and progress != last_progress:
                        now = time.time()
                        if now - last_update_time >= 5:
                            last_progress = progress
                            last_update_time = now
                            elapsed = now - encode_start
                            elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
                            bar = make_progress_bar(progress)
                            if progress > 0:
                                eta_seconds = int((elapsed / progress) * (100 - progress))
                                eta_str = f"{eta_seconds // 60}m {eta_seconds % 60}s"
                            else:
                                eta_str = "calculating..."
                            try:
                                await message.edit_text(
                                    f"🔄 **Encoding in Progress...**\n\n"
                                    f"🎬 Codec: **{codec_label}** | CRF: **{settings['crf']}**\n"
                                    f"⚡ Preset: **{settings['preset']}** | Res: **{settings['resolution']}**\n"
                                    f"🖥️ GPU: **T4 #{gpu_id}**\n\n"
                                    f"[{bar}] **{progress}%**\n\n"
                                    f"⏱️ Elapsed: **{elapsed_str}**\n"
                                    f"⏳ ETA: **{eta_str}**"
                                )
                            except Exception:
                                pass

            await process.wait()
            if user_id in active_tasks:
                del active_tasks[user_id]

            success = process.returncode == 0
            vmaf_score = None
            ai_info = {"upscaler": None, "upscale_scale": 1, "interpolated": False,
                       "rife_model": None, "original_fps": 0, "output_fps": 0}

            if success and settings.get("vmaf_check") and check_vmaf():
                vmaf_score = await compute_vmaf(input_path, output_path, message)
                ai_info["vmaf"] = vmaf_score

        # ─── Upload result ─────────────────────────────────────────────
        if not success or not os.path.exists(output_path):
            await safe_edit_text(message, "❌ **Encoding failed.** Check settings and try again.")
            return

        elapsed = time.time() - start_time
        elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s" if elapsed >= 60 else f"{int(elapsed)}s"

        file_size = os.path.getsize(output_path)
        size_mb = file_size / (1024 * 1024)
        orig_size = os.path.getsize(input_path) if os.path.exists(input_path) else 0
        orig_mb = orig_size / (1024 * 1024)
        if orig_mb > 0:
            reduction = ((orig_mb - size_mb) / orig_mb) * 100
            size_info = f"📦 Size: **{size_mb:.1f} MB** (was {orig_mb:.1f} MB, {reduction:+.1f}%)"
        else:
            size_info = f"📦 Size: **{size_mb:.1f} MB**"

        # Build caption
        codec_name = CODEC_OPTIONS[settings["codec"]]["label"]
        caption = (
            f"✅ **AI Encoding Complete**\n\n"
            f"🎬 Codec: **{codec_name}**\n"
            f"📊 CRF: **{settings['crf']}**\n"
            f"⚡ Preset: **{settings['preset']}**\n"
            f"📐 Resolution: **{settings['resolution']}**"
            f"{' (' + str(src_width) + 'x' + str(src_height) + ')' if src_width > 0 else ''}\n"
        )
        if ai_info.get("upscaler"):
            caption += f"🔬 AI Upscale: **{ai_info['upscaler'].title()} {ai_info['upscale_scale']}x**\n"
        if ai_info.get("interpolated"):
            caption += f"🎞️ Frame Interp: **RIFE {settings.get('rife_multiplier', 2)}x** ({ai_info['original_fps']}→{ai_info['output_fps']} fps)\n"
        if ai_info.get("vmaf") is not None:
            vmaf = ai_info["vmaf"]
            quality = "Excellent" if vmaf >= 90 else "Good" if vmaf >= 75 else "Fair" if vmaf >= 50 else "Poor"
            caption += f"📊 VMAF Score: **{vmaf}/100** ({quality})\n"
        caption += (
            f"🚀 GPU Encode: **{'Yes (NVENC)' if settings.get('gpu_enabled') and settings['codec'] != 'av1' else 'No (CPU)'}**\n"
            f"{size_info}\n"
            f"⏱️ Time: **{elapsed_str}**"
        )

        # Upload
        if file_size > 2 * 1024 * 1024 * 1024:
            await safe_edit_text(message, "☁️ **File > 2GB — Uploading to Google Drive...**")
            try:
                gdrive_link = await upload_to_gdrive(output_path, output_name, message)
                await message.edit_text(
                    f"{caption}\n\n☁️ **Uploaded to Google Drive**\n🔗 [Download Link]({gdrive_link})",
                    disable_web_page_preview=True
                )
            except Exception as e:
                await message.edit_text(f"❌ **GDrive upload failed:** `{str(e)}`")
        else:
            await safe_edit_text(message, "⬆️ **Uploading encoded video...**")

            upload_start = time.time()
            ul_last_update = [0]

            async def upload_progress(current, total):
                now = time.time()
                if now - ul_last_update[0] < 3:
                    return
                ul_last_update[0] = now
                pct = int(current / total * 100) if total else 0
                current_mb = current / (1024 * 1024)
                t_mb = total / (1024 * 1024)
                bar = make_progress_bar(pct)
                elapsed_ul = now - upload_start
                speed = current_mb / elapsed_ul if elapsed_ul > 0 else 0
                eta = int((t_mb - current_mb) / speed) if speed > 0 else 0
                eta_str = f"{eta // 60}m {eta % 60}s" if eta > 0 else "calculating..."
                try:
                    await message.edit_text(
                        f"⬆️ **Uploading...**\n\n"
                        f"[{bar}] **{pct}%**\n\n"
                        f"📤 {current_mb:.1f} MB / {t_mb:.1f} MB\n"
                        f"🚀 Speed: **{speed:.1f} MB/s**\n"
                        f"⏳ ETA: **{eta_str}**"
                    )
                except Exception:
                    pass

            await client.send_document(
                chat_id=message.chat.id,
                document=output_path,
                caption=caption,
                force_document=True,
                progress=upload_progress
            )
            await safe_edit_text(message, "✅ **Done!** Send another video to encode.")

        await react_to_message(client, message.chat.id, message.id)

    except asyncio.CancelledError:
        await safe_edit_text(message, "🛑 **Encoding cancelled.**")
    except Exception as e:
        import traceback
        traceback.print_exc()
        await safe_edit_text(message, f"❌ **Error:** `{str(e)[:500]}`")
    finally:
        if gpu_id is not None:
            try:
                await release_gpu(gpu_id)
            except Exception:
                pass
        if user_id in active_tasks:
            del active_tasks[user_id]
        if os.path.exists(download_dir):
            shutil.rmtree(download_dir, ignore_errors=True)
        if os.path.exists(output_path):
            os.remove(output_path)


# ═══════════════════════════════════════════════════════════════════════
#  BACKGROUND CLEANUP
# ═══════════════════════════════════════════════════════════════════════

async def cleanup_old_files():
    """Periodically clean up any leftover files older than 5 minutes."""
    while True:
        await asyncio.sleep(300)
        now = time.time()
        for folder in ["downloads", "encoded"]:
            if os.path.exists(folder):
                for item in os.listdir(folder):
                    item_path = os.path.join(folder, item)
                    try:
                        if now - os.path.getmtime(item_path) > 300:
                            if os.path.isdir(item_path):
                                shutil.rmtree(item_path, ignore_errors=True)
                            else:
                                os.remove(item_path)
                    except Exception:
                        pass


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

async def main():
    async with app:
        asyncio.create_task(cleanup_old_files())

        if LOG_CHANNEL:
            try:
                await app.send_message(
                    LOG_CHANNEL,
                    "✅ **AI Video Encoder Bot Started!**\n\n"
                    "🧠 AI Models: Real-ESRGAN, waifu2x, RIFE\n"
                    "📊 Quality: VMAF scoring\n"
                    "🎬 Codecs: H.264, H.265, AV1\n\n"
                    "Bot is online and ready."
                )
            except Exception as e:
                print(f"Failed to send startup message: {e}")

        for admin_id in ADMINS:
            try:
                await app.send_message(
                    admin_id,
                    "✅ **AI Video Encoder Bot Started!**\n\n"
                    "Ready to encode with AI enhancement."
                )
            except Exception:
                pass

        print("AI Video Encoder Bot is running...")
        await asyncio.Event().wait()


print("AI Video Encoder Bot is starting...")
app.run(main())
