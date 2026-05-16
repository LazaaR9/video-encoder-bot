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
from googleapiclient.http import MediaFileUpload

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
ADMINS = [int(x) for x in os.environ.get("ADMINS", "").split(",") if x.strip()]
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", "0"))
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")
GDRIVE_SA_FILE = os.environ.get("GDRIVE_SA_FILE", "service_account.json")

app = Client("anime_encoder_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

BOT_START_TIME = time.time()
user_settings = {}
active_tasks = {}
gpu_lock = asyncio.Lock()
gpu_task_count = {0: 0, 1: 0}

# AI binary paths (auto-installed)
WAIFU2X_BIN = None
RIFE_BIN = None

# ═══════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════

ALL_REACTIONS = [
    "🔥", "⚡", "🎬", "👀", "🎯", "💯", "🏆", "⭐", "🎉", "💪",
    "🚀", "❤️", "👍", "🤩", "😎", "🥰", "👏", "🙏", "💫", "✨",
]

THUMB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thumbnails")

RESOLUTION_MAP = {
    "4k": "3840:-2", "2k": "2560:-2", "1080p": "1920:-2",
    "720p": "1280:-2", "540p": "960:-2", "480p": "854:-2", "360p": "640:-2",
    "original": None
}

RESOLUTION_WIDTHS = {
    "4k": 3840, "2k": 2560, "1080p": 1920, "720p": 1280,
    "540p": 960, "480p": 854, "360p": 640
}

NVENC_PRESET_MAP = {
    "fast": "p5", "medium": "p4", "slow": "p6", "veryslow": "p7"
}

PRESET_LIST = ["fast", "medium", "slow", "veryslow"]


async def get_best_gpu():
    async with gpu_lock:
        return 1 if gpu_task_count[1] < gpu_task_count[0] else 0

async def acquire_gpu():
    gpu_id = await get_best_gpu()
    async with gpu_lock:
        gpu_task_count[gpu_id] += 1
    return gpu_id

async def release_gpu(gpu_id):
    async with gpu_lock:
        gpu_task_count[gpu_id] = max(0, gpu_task_count[gpu_id] - 1)

async def safe_edit_text(message, text, reply_markup=None):
    try:
        if reply_markup:
            await message.edit_text(text, reply_markup=reply_markup)
        else:
            await message.edit_text(text)
    except MessageNotModified:
        pass

async def react_to_message(client, chat_id, message_id, emoji=None):
    try:
        await client.send_reaction(chat_id, message_id, emoji=emoji or random.choice(ALL_REACTIONS))
    except Exception:
        pass

def get_random_thumbnail():
    if os.path.exists(THUMB_DIR):
        thumbs = [f for f in os.listdir(THUMB_DIR) if f.endswith(('.jpg', '.png', '.jpeg'))]
        if thumbs:
            return os.path.join(THUMB_DIR, random.choice(thumbs))
    return None

def make_progress_bar(pct, length=10):
    filled = int(length * pct / 100)
    return "█" * filled + "░" * (length - filled)

def parse_ffmpeg_progress(line, duration):
    m = re.search(r"time=(\d+):(\d+):(\d+)\.(\d+)", line)
    if m and duration > 0:
        cur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        return min(int(cur / duration * 100), 99)
    return None

def get_uptime():
    elapsed = int(time.time() - BOT_START_TIME)
    h, r = divmod(elapsed, 3600)
    m, s = divmod(r, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"

def get_resolution_label(w):
    if w >= 3840: return "4K"
    if w >= 2560: return "2K"
    if w >= 1920: return "1080p"
    if w >= 1280: return "720p"
    if w >= 960: return "540p"
    if w >= 854: return "480p"
    if w >= 640: return "360p"
    return f"{w}p"


async def run_cmd(cmd, env=None, timeout=600):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, b"", b"Timeout"
    return proc.returncode, stdout, stderr


async def ffprobe(args, path):
    try:
        rc, stdout, _ = await run_cmd(["ffprobe", "-v", "error"] + args + [path])
        if rc == 0:
            return stdout.decode().strip()
    except Exception:
        pass
    return None

async def get_video_duration(p):
    val = await ffprobe(["-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1"], p)
    return float(val) if val else 0

async def get_video_fps(p):
    val = await ffprobe(["-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "default=noprint_wrappers=1:nokey=1"], p)
    if val and "/" in val:
        n, d = val.split("/")
        return float(n) / float(d)
    return float(val) if val else 24.0

async def get_video_resolution(p):
    val = await ffprobe(["-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "default=noprint_wrappers=1:nokey=1"], p)
    if val:
        lines = val.split("\n")
        return int(lines[0]), int(lines[1])
    return 0, 0


def get_vulkan_env():
    env = os.environ.copy()
    for d in ["/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d"]:
        if os.path.isdir(d):
            for f in os.listdir(d):
                if "nvidia" in f.lower() and f.endswith(".json"):
                    env["VK_ICD_FILENAMES"] = os.path.join(d, f)
                    return env
    return env


# ═══════════════════════════════════════════════════════════════════════
#  AI MODEL INSTALLERS (anime-only: waifu2x + RIFE)
# ═══════════════════════════════════════════════════════════════════════

def _install_ai(name, url, zip_name):
    import subprocess as sp
    try:
        dl = f"/tmp/{name}_install"
        os.makedirs(dl, exist_ok=True)
        zp = os.path.join(dl, zip_name)
        sp.run(["wget", "-q", url, "-O", zp], timeout=120, check=True)
        sp.run(["unzip", "-o", zp, "-d", dl], timeout=60, check=True)
        for root, _, files in os.walk(dl):
            for f in files:
                if name.split("-")[0] in f and not f.endswith((".param", ".bin", ".json")):
                    src = os.path.join(root, f)
                    dst = f"/usr/local/bin/{f}"
                    shutil.move(src, dst)
                    os.chmod(dst, 0o755)
        # Copy models
        for root, _, files in os.walk(dl):
            for f in files:
                if f.endswith((".param", ".bin")):
                    shutil.copy2(os.path.join(root, f), "/usr/local/bin/models/")
        print(f"✅ {name} installed")
        return True
    except Exception as e:
        print(f"❌ {name}: {e}")
    finally:
        shutil.rmtree(dl, ignore_errors=True)
    return False


def check_waifu2x():
    global WAIFU2X_BIN
    if WAIFU2X_BIN and os.path.exists(WAIFU2X_BIN):
        return True
    b = shutil.which("waifu2x-ncnn-vulkan")
    if b:
        WAIFU2X_BIN = b
        return True
    os.makedirs("/usr/local/bin/models", exist_ok=True)
    ok = _install_ai(
        "waifu2x-ncnn-vulkan",
        "https://github.com/nihui/waifu2x-ncnn-vulkan/releases/download/20220728/waifu2x-ncnn-vulkan-20220728-ubuntu.zip",
        "waifu2x-ncnn-vulkan.zip"
    )
    if ok:
        WAIFU2X_BIN = shutil.which("waifu2x-ncnn-vulkan")
    return ok


def check_rife():
    global RIFE_BIN
    if RIFE_BIN and os.path.exists(RIFE_BIN):
        return True
    b = shutil.which("rife-ncnn-vulkan")
    if b:
        RIFE_BIN = b
        return True
    os.makedirs("/usr/local/bin/models", exist_ok=True)
    ok = _install_ai(
        "rife-ncnn-vulkan",
        "https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/rife-ncnn-vulkan-20221029-ubuntu.zip",
        "rife-ncnn-vulkan.zip"
    )
    if ok:
        RIFE_BIN = shutil.which("rife-ncnn-vulkan")
    return ok


# ═══════════════════════════════════════════════════════════════════════
#  ANIME AI PIPELINE
# ═══════════════════════════════════════════════════════════════════════

async def extract_frames(input_path, frames_dir, msg=None):
    if msg:
        await safe_edit_text(msg, "✂️ **Extracting frames...**")
    os.makedirs(frames_dir, exist_ok=True)

    # Get total frames for progress
    total = 0
    try:
        val = await ffprobe(["-select_streams", "v:0", "-show_entries", "stream=nb_frames",
                             "-of", "default=noprint_wrappers=1:nokey=1"], input_path)
        if val and val != "N/A":
            total = int(val)
    except Exception:
        pass

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", input_path, "-vsync", "0",
        os.path.join(frames_dir, "frame_%08d.png"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    # Monitor extraction progress
    start = time.time()
    last_update = 0
    while proc.returncode is None:
        await asyncio.sleep(1)
        done = len([f for f in os.listdir(frames_dir) if f.endswith(".png")])
        now = time.time()
        if msg and now - last_update >= 3 and total > 0:
            last_update = now
            pct = min(int(done * 100 / total), 99)
            elapsed = now - start
            fps = done / elapsed if elapsed > 0 else 0
            bar = make_progress_bar(pct)
            try:
                await msg.edit_text(
                    f"✂️ **Extracting frames...** [{bar}] **{pct}%**\n\n"
                    f"🖼️ Frames: **{done}/{total}**\n"
                    f"⚡ Speed: **{fps:.1f}** frames/sec\n"
                    f"⏱️ {int(elapsed)}s elapsed"
                )
            except Exception:
                pass

    await proc.wait()
    count = len([f for f in os.listdir(frames_dir) if f.endswith(".png")])
    return count


async def upscale_waifu2x(in_dir, out_dir, gpu_id, scale=2, noise=1, msg=None, total_frames=0):
    if not check_waifu2x():
        return False
    os.makedirs(out_dir, exist_ok=True)

    # Start waifu2x process (streams to stderr, we poll output dir for progress)
    env = get_vulkan_env()
    proc = await asyncio.create_subprocess_exec(
        WAIFU2X_BIN, "-i", in_dir, "-o", out_dir,
        "-s", str(scale), "-g", str(gpu_id), "-n", str(noise), "-f", "png",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )

    # Monitor progress by counting output files
    start = time.time()
    last_update = 0
    while proc.returncode is None:
        await asyncio.sleep(2)
        done = len([f for f in os.listdir(out_dir) if f.endswith(".png")])
        now = time.time()
        if msg and now - last_update >= 4 and total_frames > 0:
            last_update = now
            pct = min(int(done * 100 / total_frames), 99)
            elapsed = now - start
            fps = done / elapsed if elapsed > 0 else 0
            remaining = (total_frames - done) / fps if fps > 0 else 0
            bar = make_progress_bar(pct)
            try:
                await msg.edit_text(
                    f"🎨 **waifu2x Upscaling...** [{bar}] **{pct}%**\n\n"
                    f"🖼️ Frames: **{done}/{total_frames}**\n"
                    f"⚡ Speed: **{fps:.1f}** frames/sec\n"
                    f"🖥️ T4 #{gpu_id} | {scale}x | Denoise: {noise}\n"
                    f"⏱️ {int(elapsed//60)}m {int(elapsed%60)}s | ETA {int(remaining//60)}m {int(remaining%60)}s"
                )
            except Exception:
                pass

    await proc.wait()
    ok = len([f for f in os.listdir(out_dir) if f.endswith(".png")]) > 0
    if not ok:
        print(f"waifu2x failed: {proc.stderr}")
    return ok


async def interpolate_rife(in_dir, out_dir, gpu_id, multiplier=2, model="rife-v4", msg=None, total_frames=0):
    if not check_rife():
        return False
    os.makedirs(out_dir, exist_ok=True)

    env = get_vulkan_env()
    proc = await asyncio.create_subprocess_exec(
        RIFE_BIN, "-i", in_dir, "-o", out_dir,
        "-g", str(gpu_id), "-m", model, "-n", str(multiplier),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )

    # RIFE output = total_frames * multiplier
    expected = total_frames * multiplier
    start = time.time()
    last_update = 0
    while proc.returncode is None:
        await asyncio.sleep(2)
        done = len([f for f in os.listdir(out_dir) if f.endswith(".png")])
        now = time.time()
        if msg and now - last_update >= 4 and expected > 0:
            last_update = now
            pct = min(int(done * 100 / expected), 99)
            elapsed = now - start
            fps = done / elapsed if elapsed > 0 else 0
            remaining = (expected - done) / fps if fps > 0 else 0
            bar = make_progress_bar(pct)
            try:
                await msg.edit_text(
                    f"🎞️ **RIFE Interpolation...** [{bar}] **{pct}%**\n\n"
                    f"🖼️ Frames: **{done}/{expected}** ({total_frames} → {expected})\n"
                    f"⚡ Speed: **{fps:.1f}** frames/sec\n"
                    f"🖥️ T4 #{gpu_id} | {multiplier}x | {model}\n"
                    f"⏱️ {int(elapsed//60)}m {int(elapsed%60)}s | ETA {int(remaining//60)}m {int(remaining%60)}s"
                )
            except Exception:
                pass

    await proc.wait()
    ok = len([f for f in os.listdir(out_dir) if f.endswith(".png")]) > 0
    if not ok:
        print(f"RIFE failed: {proc.stderr}")
    return ok


async def encode_frames(frames_dir, output_path, src_path, fps, settings, gpu_id, msg=None):
    """Encode processed frames back to video with NVENC. Uses -progress pipe:1 for detailed stats."""
    use_gpu = settings.get("gpu_enabled", True)

    cmd = ["ffmpeg", "-y",
           "-framerate", str(fps),
           "-i", os.path.join(frames_dir, "frame_%08d.png"),
           "-i", src_path,
           "-map", "0:v:0", "-map", "1:a?"]

    if use_gpu:
        cmd.extend(["-c:v", "hevc_nvenc", "-gpu", str(gpu_id)])
        cmd.extend(["-rc", "vbr", "-cq", str(settings["crf"])])
        cmd.extend(["-preset", NVENC_PRESET_MAP.get(settings["preset"], "p4")])
        cmd.extend(["-maxrate", "15M", "-bufsize", "15M"])
        cmd.extend(["-vf", "format=nv12"])
    else:
        cmd.extend(["-c:v", "libx265", "-crf", str(settings["crf"]),
                    "-preset", settings["preset"],
                    "-x265-params", "log-level=error"])

    cmd.extend(["-c:a", "copy", "-movflags", "+faststart"])
    cmd.extend(["-progress", "pipe:1"])  # Structured progress output
    cmd.append(output_path)

    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    return process


def _parse_progress_line(line):
    """Parse a key=value line from FFmpeg -progress output."""
    line = line.strip()
    if "=" in line:
        k, _, v = line.partition("=")
        return k.strip(), v.strip()
    return None, None


async def _monitor_encode_progress(process, msg, total_frames, duration, gpu_id, ai_info, settings):
    """Read FFmpeg progress output and update the message with detailed stats."""
    start = time.time()
    last_update = 0
    current_frame = 0
    current_fps = 0
    current_bitrate = ""
    current_size = 0
    current_speed = ""
    current_time_us = 0  # microseconds

    while True:
        line = await process.stdout.readline()
        if not line:
            # Also check stderr for any output
            break
        txt = line.decode("utf-8", errors="ignore")

        key, val = _parse_progress_line(txt)
        if key == "frame":
            try:
                current_frame = int(val)
            except ValueError:
                pass
        elif key == "fps":
            try:
                current_fps = float(val)
            except ValueError:
                pass
        elif key == "bitrate":
            current_bitrate = val
        elif key == "total_size":
            try:
                current_size = int(val)
            except ValueError:
                pass
        elif key == "speed":
            current_speed = val
        elif key == "out_time_us":
            try:
                current_time_us = int(val)
            except ValueError:
                pass
        elif key == "progress" and val == "end":
            # Final update
            break

        now = time.time()
        if now - last_update >= 4:
            last_update = now
            elapsed = now - start

            # Calculate percentage from frame count or time
            pct = 0
            if total_frames > 0:
                pct = min(int(current_frame * 100 / total_frames), 99)
            elif duration > 0 and current_time_us > 0:
                pct = min(int((current_time_us / 1_000_000) / duration * 100), 99)

            bar = make_progress_bar(pct)
            size_mb = current_size / (1024 * 1024)

            # Build AI info line
            ai_line = ""
            if ai_info.get("upscaled"):
                ai_line += f"🎨 Upscale: **waifu2x {ai_info['scale']}x**\n"
            if ai_info.get("interpolated"):
                ai_line += f"🎞️ RIFE: **{settings.get('rife_mult', 2)}x** ({ai_info['fps_in']}→{ai_info['fps_out']} fps)\n"

            # ETA from speed
            eta_str = "..."
            if current_fps > 0 and total_frames > 0:
                remaining = (total_frames - current_frame) / current_fps
                eta_str = f"{int(remaining//60)}m {int(remaining%60)}s"
            elif pct > 0:
                eta_s = int((elapsed / pct) * (100 - pct))
                eta_str = f"{eta_s//60}m {eta_s%60}s"

            try:
                await msg.edit_text(
                    f"🔄 **Encoding H.265 (NVENC)...**\n\n"
                    f"{ai_line}"
                    f"🖥️ T4 #{gpu_id}\n\n"
                    f"[{bar}] **{pct}%**\n\n"
                    f"🖼️ Frame: **{current_frame}**/{total_frames or '?'}\n"
                    f"🎞️ FPS: **{current_fps:.1f}** | Speed: **{current_speed or '?'}**\n"
                    f"📊 Bitrate: **{current_bitrate or '?'}**\n"
                    f"📦 Size: **{size_mb:.1f} MB**\n"
                    f"⏱️ {int(elapsed//60)}m {int(elapsed%60)}s | ETA **{eta_str}**"
                )
            except Exception:
                pass

    return current_frame, current_size


async def anime_encode_pipeline(input_path, output_path, settings, gpu_id, msg=None):
    """
    Anime-only pipeline:
    1. Extract frames
    2. waifu2x upscale (with denoise)
    3. RIFE frame interpolation
    4. NVENC encode
    """
    base = os.path.dirname(input_path)
    frames_dir = os.path.join(base, "frames")
    upscaled_dir = os.path.join(base, "upscaled")
    interpolated_dir = os.path.join(base, "interpolated")

    info = {"upscaled": False, "scale": 1, "interpolated": False, "fps_in": 0, "fps_out": 0}

    try:
        src_fps = await get_video_fps(input_path)
        info["fps_in"] = round(src_fps, 2)
        src_w, src_h = await get_video_resolution(input_path)
        duration = await get_video_duration(input_path)

        # 1. Extract
        count = await extract_frames(input_path, frames_dir, msg)
        if count == 0:
            return False, info
        total_frames = count

        current = frames_dir

        # 2. waifu2x upscale
        if settings.get("ai_upscale"):
            scale = 2
            chosen = settings["resolution"]
            if chosen != "original" and src_w > 0:
                target_w = RESOLUTION_WIDTHS.get(chosen, 0)
                if target_w > src_w * 2.5:
                    scale = 4
                elif target_w > src_w:
                    scale = 2

            noise = settings.get("denoise", 1)
            ok = await upscale_waifu2x(
                current, upscaled_dir, gpu_id,
                scale=scale, noise=noise, msg=msg, total_frames=total_frames
            )
            if ok:
                current = upscaled_dir
                info["upscaled"] = True
                info["scale"] = scale
            else:
                if msg:
                    await safe_edit_text(msg, "⚠️ **waifu2x failed — encoding without upscale.**")

        # 3. RIFE interpolation
        if settings.get("ai_interpolate"):
            mult = settings.get("rife_mult", 2)
            model = settings.get("rife_model", "rife-v4")
            ok = await interpolate_rife(
                current, interpolated_dir, gpu_id,
                multiplier=mult, model=model, msg=msg, total_frames=total_frames
            )
            if ok:
                current = interpolated_dir
                info["interpolated"] = True
                info["fps_out"] = round(src_fps * mult, 2)
                total_frames = total_frames * mult  # update for encode progress
            else:
                info["fps_out"] = round(src_fps, 2)
                if msg:
                    await safe_edit_text(msg, "⚠️ **RIFE failed — encoding at original FPS.**")
        else:
            info["fps_out"] = round(src_fps, 2)

        # 4. Encode with detailed progress
        out_fps = info["fps_out"]
        process = await encode_frames(current, output_path, input_path, out_fps, settings, gpu_id, msg)
        active_tasks[settings.get("_uid", 0)] = process

        if msg:
            await safe_edit_text(msg, f"🎬 **Starting encode...** ({total_frames} frames)")

        # Use detailed progress monitor
        final_frame, final_size = await _monitor_encode_progress(
            process, msg, total_frames, duration, gpu_id, info, settings
        )

        await process.wait()
        uid = settings.get("_uid", 0)
        if uid in active_tasks:
            del active_tasks[uid]

        return process.returncode == 0, info

    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, info
    finally:
        for d in [frames_dir, upscaled_dir, interpolated_dir]:
            if d and os.path.exists(d):
                shutil.rmtree(d, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════
#  GOOGLE DRIVE
# ═══════════════════════════════════════════════════════════════════════

def get_gdrive_service():
    sa_json = os.environ.get("GDRIVE_SA_JSON", "")
    if sa_json:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(sa_json), scopes=["https://www.googleapis.com/auth/drive"])
    else:
        creds = service_account.Credentials.from_service_account_file(
            GDRIVE_SA_FILE, scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds)


async def upload_to_gdrive(file_path, file_name, msg):
    service = get_gdrive_service()
    total_mb = os.path.getsize(file_path) / (1024 * 1024)
    media = MediaFileUpload(file_path, resumable=True, chunksize=50 * 1024 * 1024)
    req = service.files().create(
        body={"name": file_name, "parents": [GDRIVE_FOLDER_ID]},
        media_body=media, fields="id, webViewLink"
    )
    start = time.time()
    last = 0
    resp = None
    while resp is None:
        status, resp = await asyncio.to_thread(req.next_chunk)
        if status:
            now = time.time()
            if now - last >= 3:
                last = now
                pct = int(status.progress() * 100)
                up = status.resumable_progress / (1024 * 1024)
                spd = up / (now - start) if now - start > 0 else 0
                eta = int((total_mb - up) / spd) if spd > 0 else 0
                try:
                    await msg.edit_text(
                        f"☁️ **Uploading to Drive...** {pct}%\n"
                        f"📤 {up:.1f}/{total_mb:.1f} MB | {spd:.1f} MB/s | ETA {eta//60}m {eta%60}s"
                    )
                except Exception:
                    pass
    fid = resp.get("id")
    service.permissions().create(fileId=fid, body={"type": "anyone", "role": "reader"}).execute()
    return f"https://drive.google.com/file/d/{fid}/view?usp=sharing"


# ═══════════════════════════════════════════════════════════════════════
#  KEYBOARDS (anime-only, simplified)
# ═══════════════════════════════════════════════════════════════════════

def get_main_keyboard(s):
    up = "ON" if s.get("ai_upscale") else "OFF"
    rife = "ON" if s.get("ai_interpolate") else "OFF"
    gpu = "ON" if s.get("gpu_enabled") else "OFF"
    denoise = s.get("denoise", 1)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎨 Upscale: {up}", callback_data="toggle_upscale"),
         InlineKeyboardButton(f"🔇 Denoise: {denoise}", callback_data="denoise")],
        [InlineKeyboardButton(f"🎞️ RIFE: {rife}", callback_data="toggle_rife"),
         InlineKeyboardButton(f"📊 CRF: {s['crf']}", callback_data="crf")],
        [InlineKeyboardButton(f"⚡ Preset: {s['preset']}", callback_data="preset"),
         InlineKeyboardButton(f"📐 Res: {s['resolution']}", callback_data="resolution")],
        [InlineKeyboardButton(f"🚀 GPU: {gpu}", callback_data="toggle_gpu")],
        [InlineKeyboardButton("✅ Start Encoding", callback_data="encode")],
    ])


def get_resolution_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("4K", callback_data="set_res_4k"),
         InlineKeyboardButton("2K", callback_data="set_res_2k"),
         InlineKeyboardButton("1080p", callback_data="set_res_1080p")],
        [InlineKeyboardButton("720p", callback_data="set_res_720p"),
         InlineKeyboardButton("540p", callback_data="set_res_540p"),
         InlineKeyboardButton("480p", callback_data="set_res_480p")],
        [InlineKeyboardButton("360p", callback_data="set_res_360p"),
         InlineKeyboardButton("Original", callback_data="set_res_original")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])


def get_preset_keyboard():
    rows = []
    for i in range(0, len(PRESET_LIST), 2):
        row = [InlineKeyboardButton(p.capitalize(), callback_data=f"set_preset_{p}") for p in PRESET_LIST[i:i+2]]
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def get_crf_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("18 (High)", callback_data="set_crf_18"),
         InlineKeyboardButton("20", callback_data="set_crf_20"),
         InlineKeyboardButton("23 (Default)", callback_data="set_crf_23")],
        [InlineKeyboardButton("26", callback_data="set_crf_26"),
         InlineKeyboardButton("28", callback_data="set_crf_28"),
         InlineKeyboardButton("30 (Low)", callback_data="set_crf_30")],
        [InlineKeyboardButton("💬 Custom (0-51)", callback_data="custom_crf")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])


def get_denoise_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("0 (No denoise)", callback_data="set_denoise_0"),
         InlineKeyboardButton("1 (Light)", callback_data="set_denoise_1")],
        [InlineKeyboardButton("2 (Medium)", callback_data="set_denoise_2"),
         InlineKeyboardButton("3 (Heavy)", callback_data="set_denoise_3")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])


def get_rife_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("2x (24→48 fps)", callback_data="set_rife_2"),
         InlineKeyboardButton("4x (24→96 fps)", callback_data="set_rife_4")],
        [InlineKeyboardButton("Model: RIFE v4", callback_data="set_rmodel_rife-v4"),
         InlineKeyboardButton("Model: RIFE v4.6", callback_data="set_rmodel_rife-v4.6")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])


# ═══════════════════════════════════════════════════════════════════════
#  COMMANDS
# ═══════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("start"))
async def cmd_start(client, message):
    await react_to_message(client, message.chat.id, message.id)
    thumb = get_random_thumbnail()
    text = (
        f"👋 Hey **{message.from_user.first_name}**!\n\n"
        "🎬 **Anime Video Encoder Bot**\n"
        "_AI-powered anime encoding on T4 GPU_\n\n"
        "🎨 **waifu2x** — Anime upscaling + denoise\n"
        "🎞️ **RIFE** — Frame interpolation (2x/4x)\n"
        "🖥️ **NVENC** — H.265 GPU encoding\n\n"
        "**Commands:**\n"
        "/start · /help · /encode · /settings\n"
        "/status · /models · /cancel · /about\n\n"
        "📹 **Send an anime video to start!**"
    )
    if thumb:
        await message.reply_photo(photo=thumb, caption=text)
    else:
        await message.reply_text(text)


@app.on_message(filters.command("help"))
async def cmd_help(client, message):
    await react_to_message(client, message.chat.id, message.id)
    text = (
        "📖 **How to Use**\n\n"
        "1️⃣ Send an anime video\n"
        "2️⃣ Configure settings:\n\n"
        "   🎨 **Upscale** — waifu2x adds real detail to anime\n"
        "   🔇 **Denoise** — Remove compression artifacts (0-3)\n"
        "   🎞️ **RIFE** — AI frame interpolation for smooth motion\n"
        "   📊 **CRF** — Quality (18=high, 23=default, 30=low)\n"
        "   ⚡ **Preset** — Speed vs quality tradeoff\n"
        "   📐 **Resolution** — Output resolution\n\n"
        "3️⃣ Hit **Start Encoding**\n"
        "4️⃣ Wait for the AI pipeline\n"
        "5️⃣ Download your enhanced anime!\n\n"
        "**Tips:**\n"
        "• Denoise 1-2 is great for old/compressed anime\n"
        "• RIFE 2x makes panning shots buttery smooth\n"
        "• Upscale 2x from 720p → 1080p looks great\n"
        "• Lower CRF = bigger file but pristine quality"
    )
    await message.reply_text(text)


@app.on_message(filters.command("models"))
async def cmd_models(client, message):
    await react_to_message(client, message.chat.id, message.id)
    w = check_waifu2x()
    r = check_rife()
    await message.reply_text(
        f"🧠 **AI Model Status**\n\n"
        f"{'✅' if w else '❌'} **waifu2x** — Anime upscaler + denoise\n"
        f"{'✅' if r else '❌'} **RIFE** — Frame interpolation\n\n"
        "All models run via **NCNN + Vulkan** on T4 GPU.\n"
        "Auto-installed on first use."
    )


@app.on_message(filters.command("encode"))
async def cmd_encode(client, message):
    await react_to_message(client, message.chat.id, message.id)
    uid = message.from_user.id
    if uid in user_settings and user_settings[uid].get("file_id"):
        await message.reply_text("⚙️ **Settings:**", reply_markup=get_main_keyboard(user_settings[uid]))
    else:
        await message.reply_text("📹 Send me an anime video first!")


@app.on_message(filters.command("settings"))
async def cmd_settings(client, message):
    await react_to_message(client, message.chat.id, message.id)
    uid = message.from_user.id
    if uid in user_settings:
        s = user_settings[uid]
        await message.reply_text(
            f"⚙️ **Your Settings:**\n\n"
            f"🎨 Upscale: **{'ON (waifu2x)' if s.get('ai_upscale') else 'OFF'}**\n"
            f"🔇 Denoise: **{s.get('denoise', 1)}**\n"
            f"🎞️ RIFE: **{'ON (' + str(s.get('rife_mult', 2)) + 'x)' if s.get('ai_interpolate') else 'OFF'}**\n"
            f"📊 CRF: **{s['crf']}**\n"
            f"⚡ Preset: **{s['preset']}**\n"
            f"📐 Res: **{s['resolution']}**\n"
            f"🚀 GPU: **{'ON' if s['gpu_enabled'] else 'OFF'}**\n"
            f"📁 File: **{s.get('file_name', 'None')}**"
        )
    else:
        await message.reply_text(
            "⚙️ **Defaults:**\n\n"
            "🎨 Upscale: ON (waifu2x)\n"
            "🔇 Denoise: 1\n"
            "🎞️ RIFE: OFF\n"
            "📊 CRF: 23\n"
            "⚡ Preset: medium\n"
            "📐 Res: original\n"
            "🚀 GPU: ON\n\n"
            "Send a video to customize."
        )


@app.on_message(filters.command("status"))
async def cmd_status(client, message):
    await react_to_message(client, message.chat.id, message.id)
    try:
        rc, stdout, _ = await run_cmd(["ffmpeg", "-version"])
        ffver = stdout.decode().split('\n')[0] if stdout else "?"
    except Exception:
        ffver = "❌ not found"
    gpu_info = "?"
    try:
        rc, stdout, _ = await run_cmd(["nvidia-smi", "--query-gpu=name,memory.free", "--format=csv,noheader"])
        if rc == 0:
            gpu_info = stdout.decode().strip().split("\n")[0]
    except Exception:
        pass
    await message.reply_text(
        f"📊 **Status**\n\n"
        f"🟢 Online | ⏱️ {get_uptime()}\n"
        f"🎬 Active: **{len(active_tasks)}**\n"
        f"🔧 FFmpeg: `{ffver}`\n"
        f"🖥️ GPU: `{gpu_info}`"
    )


@app.on_message(filters.command("cancel"))
async def cmd_cancel(client, message):
    await react_to_message(client, message.chat.id, message.id)
    uid = message.from_user.id
    if uid in active_tasks:
        try:
            active_tasks[uid].kill()
        except Exception:
            pass
        del active_tasks[uid]
        await message.reply_text("🛑 **Cancelled.** Send another video to start fresh.")
    else:
        await message.reply_text("ℹ️ Nothing to cancel.")


@app.on_message(filters.command("about"))
async def cmd_about(client, message):
    await react_to_message(client, message.chat.id, message.id)
    thumb = get_random_thumbnail()
    text = (
        "ℹ️ **Anime Video Encoder Bot**\n\n"
        "AI-enhanced anime encoding for T4 GPU.\n\n"
        "🎨 **waifu2x** — Neural network upscaling\n"
        "   Optimized for anime/art. Includes denoise.\n\n"
        "🎞️ **RIFE** — AI frame interpolation\n"
        "   Generates intermediate frames for smooth motion.\n\n"
        "🖥️ **NVENC** — H.265 GPU encoding\n"
        "   Hardware encoder on T4 for fast output.\n\n"
        "**Pipeline:** Extract → Upscale → Interpolate → Encode\n\n"
        "Powered by FFmpeg + Pyrogram + NCNN/Vulkan"
    )
    if thumb:
        await message.reply_photo(photo=thumb, caption=text)
    else:
        await message.reply_text(text)


# ═══════════════════════════════════════════════════════════════════════
#  VIDEO HANDLER
# ═══════════════════════════════════════════════════════════════════════

@app.on_message(filters.video | filters.document)
async def video_handler(client, message):
    if message.document:
        mime = message.document.mime_type or ""
        if not mime.startswith("video/"):
            return

    uid = message.from_user.id
    fid = message.video.file_id if message.video else message.document.file_id
    fname = (message.video.file_name if message.video else message.document.file_name) or f"video_{uid}.mp4"
    fsize = (message.video.file_size if message.video else message.document.file_size) or 0

    await react_to_message(client, message.chat.id, message.id)

    user_settings[uid] = {
        "file_id": fid, "file_name": fname, "file_size": fsize,
        "crf": 23, "preset": "medium", "resolution": "original",
        "gpu_enabled": True,
        "ai_upscale": True, "denoise": 1,
        "ai_interpolate": False, "rife_mult": 2, "rife_model": "rife-v4",
        "awaiting_crf": False, "source_w": 0, "source_h": 0,
        "_uid": uid,
    }

    msg = await message.reply_text(
        "✅ **Video received!**\n\n"
        "Configure your anime encoding pipeline:",
        reply_markup=get_main_keyboard(user_settings[uid])
    )
    user_settings[uid]["msg_id"] = msg.id


# ═══════════════════════════════════════════════════════════════════════
#  CALLBACKS
# ═══════════════════════════════════════════════════════════════════════

@app.on_callback_query()
async def callback_handler(client, cb: CallbackQuery):
    uid = cb.from_user.id
    data = cb.data
    msg = cb.message

    if uid not in user_settings:
        await cb.answer("Send a video first.", show_alert=True)
        return

    s = user_settings[uid]

    # ── Upscale toggle ─────────────────────────────────────────────────
    if data == "toggle_upscale":
        if not s.get("ai_upscale"):
            s["ai_upscale"] = True
            if not check_waifu2x():
                await cb.answer("❌ waifu2x not available", show_alert=True)
                s["ai_upscale"] = False
                return
            await cb.answer("Upscale: ON")
        else:
            s["ai_upscale"] = False
            await cb.answer("Upscale: OFF")
        await safe_edit_text(msg, "⚙️ Settings:", reply_markup=get_main_keyboard(s))

    # ── Denoise ────────────────────────────────────────────────────────
    elif data == "denoise":
        await safe_edit_text(msg, "🔇 **Denoise Level:**\n\nHigher = cleaner but may lose detail.", reply_markup=get_denoise_keyboard())

    elif data.startswith("set_denoise_"):
        level = int(data.replace("set_denoise_", ""))
        s["denoise"] = level
        await cb.answer(f"Denoise: {level}")
        await safe_edit_text(msg, "⚙️ Settings:", reply_markup=get_main_keyboard(s))

    # ── RIFE toggle ────────────────────────────────────────────────────
    elif data == "toggle_rife":
        if not s.get("ai_interpolate"):
            s["ai_interpolate"] = True
            if not check_rife():
                await cb.answer("❌ RIFE not available", show_alert=True)
                s["ai_interpolate"] = False
                return
            await cb.answer("RIFE: ON")
            await safe_edit_text(msg, "🎞️ **RIFE Settings:**", reply_markup=get_rife_keyboard())
        else:
            s["ai_interpolate"] = False
            await cb.answer("RIFE: OFF")
            await safe_edit_text(msg, "⚙️ Settings:", reply_markup=get_main_keyboard(s))

    elif data.startswith("set_rife_"):
        mult = int(data.replace("set_rife_", ""))
        s["rife_mult"] = mult
        await cb.answer(f"RIFE: {mult}x")
        await safe_edit_text(msg, "⚙️ Settings:", reply_markup=get_main_keyboard(s))

    elif data.startswith("set_rmodel_"):
        model = data.replace("set_rmodel_", "")
        s["rife_model"] = model
        await cb.answer(f"RIFE model: {model}")
        await safe_edit_text(msg, "⚙️ Settings:", reply_markup=get_main_keyboard(s))

    # ── CRF ────────────────────────────────────────────────────────────
    elif data == "crf":
        await safe_edit_text(msg, "📊 **CRF** (lower = better quality):", reply_markup=get_crf_keyboard())

    elif data.startswith("set_crf_"):
        s["crf"] = int(data.replace("set_crf_", ""))
        await cb.answer(f"CRF: {s['crf']}")
        await safe_edit_text(msg, "⚙️ Settings:", reply_markup=get_main_keyboard(s))

    elif data == "custom_crf":
        s["awaiting_crf"] = True
        await cb.answer()
        await safe_edit_text(msg, "💬 **Enter CRF (0-51):**\n0 = lossless, 23 = default, 51 = worst")

    # ── Preset ─────────────────────────────────────────────────────────
    elif data == "preset":
        await safe_edit_text(msg, "⚡ **Preset** (slower = better compression):", reply_markup=get_preset_keyboard())

    elif data.startswith("set_preset_"):
        s["preset"] = data.replace("set_preset_", "")
        await cb.answer(f"Preset: {s['preset']}")
        await safe_edit_text(msg, "⚙️ Settings:", reply_markup=get_main_keyboard(s))

    # ── Resolution ─────────────────────────────────────────────────────
    elif data == "resolution":
        await safe_edit_text(msg, "📐 **Output Resolution:**", reply_markup=get_resolution_keyboard())

    elif data.startswith("set_res_"):
        res = data.replace("set_res_", "")
        s["resolution"] = res
        await cb.answer(f"Res: {res}")
        await safe_edit_text(msg, "⚙️ Settings:", reply_markup=get_main_keyboard(s))

    # ── GPU ────────────────────────────────────────────────────────────
    elif data == "toggle_gpu":
        s["gpu_enabled"] = not s["gpu_enabled"]
        await cb.answer(f"GPU: {'ON' if s['gpu_enabled'] else 'OFF'}")
        await safe_edit_text(msg, "⚙️ Settings:", reply_markup=get_main_keyboard(s))

    # ── Encode ─────────────────────────────────────────────────────────
    elif data == "encode":
        await cb.answer("Starting! 🚀")
        await safe_edit_text(msg, "⏳ **Starting anime encoding pipeline...**")
        await process_video(client, msg, uid, s)

    # ── Back ───────────────────────────────────────────────────────────
    elif data == "back_main":
        s["awaiting_crf"] = False
        await safe_edit_text(msg, "⚙️ Settings:", reply_markup=get_main_keyboard(s))


@app.on_message(filters.text & filters.private)
async def text_handler(client, message):
    uid = message.from_user.id
    if uid in user_settings and user_settings[uid].get("awaiting_crf"):
        try:
            v = int(message.text.strip())
            if 0 <= v <= 51:
                user_settings[uid]["crf"] = v
                user_settings[uid]["awaiting_crf"] = False
                await message.reply_text(f"✅ CRF: **{v}**", reply_markup=get_main_keyboard(user_settings[uid]))
            else:
                await message.reply_text("❌ 0-51 only.")
        except ValueError:
            await message.reply_text("❌ Enter a number.")


# ═══════════════════════════════════════════════════════════════════════
#  MAIN PROCESS
# ═══════════════════════════════════════════════════════════════════════

async def process_video(client, message, uid, settings):
    dl_dir = f"downloads/{uid}_{int(time.time())}"
    os.makedirs(dl_dir, exist_ok=True)
    os.makedirs("encoded", exist_ok=True)

    input_path = os.path.join(dl_dir, settings["file_name"])
    base = os.path.splitext(settings["file_name"])[0]
    out_name = f"{base}_anime_crf{settings['crf']}_{settings['resolution']}.mp4"
    out_path = os.path.join("encoded", out_name)

    start = time.time()
    gpu_id = None

    try:
        # ── Download ───────────────────────────────────────────────────
        total = settings.get("file_size", 0)
        total_mb = total / (1024 * 1024)
        dl_start = time.time()

        async def dl_monitor():
            while True:
                await asyncio.sleep(4)
                try:
                    cur = 0
                    if os.path.exists(input_path):
                        cur = os.path.getsize(input_path)
                    else:
                        for f in os.listdir(dl_dir):
                            fp = os.path.join(dl_dir, f)
                            if os.path.isfile(fp):
                                cur += os.path.getsize(fp)
                    if total > 0 and cur > 0:
                        pct = min(int(cur * 100 / total), 99)
                        cmb = cur / (1024 * 1024)
                        spd = cmb / (time.time() - dl_start) if time.time() - dl_start > 0 else 0
                        eta = int((total_mb - cmb) / spd) if spd > 0 else 0
                        await message.edit_text(
                            f"⬇️ **Downloading...** [{make_progress_bar(pct)}] **{pct}%**\n"
                            f"📥 {cmb:.1f}/{total_mb:.1f} MB | {spd:.1f} MB/s | ETA {eta//60}m {eta%60}s"
                        )
                except Exception:
                    pass

        await safe_edit_text(message, f"⬇️ **Downloading ({total_mb:.1f} MB)...**")
        task = asyncio.create_task(dl_monitor())
        try:
            await client.download_media(settings["file_id"], file_name=input_path)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        dl_time = time.time() - dl_start
        await safe_edit_text(message, f"✅ **Downloaded!** ({total_mb:.1f} MB in {int(dl_time)}s)")
        await react_to_message(client, message.chat.id, message.id)

        # ── Detect source ──────────────────────────────────────────────
        src_w, src_h = await get_video_resolution(input_path)
        settings["source_w"] = src_w
        settings["source_h"] = src_h
        src_label = get_resolution_label(src_w) if src_w > 0 else "?"

        # Warn if upscaling without AI
        if settings["resolution"] != "original" and src_w > 0:
            tw = RESOLUTION_WIDTHS.get(settings["resolution"], 0)
            if tw > src_w and not settings.get("ai_upscale"):
                await safe_edit_text(
                    message,
                    f"⚠️ Source is {src_label}. Enable **Upscale** for real detail.\n"
                    f"Using source resolution."
                )
                settings["resolution"] = "original"
                await asyncio.sleep(2)

        # ── Acquire GPU ────────────────────────────────────────────────
        gpu_id = await acquire_gpu()

        # ── Encode ─────────────────────────────────────────────────────
        has_ai = settings.get("ai_upscale") or settings.get("ai_interpolate")

        if has_ai:
            ok, info = await anime_encode_pipeline(input_path, out_path, settings, gpu_id, message)
        else:
            # Direct NVENC encode (no AI) with detailed progress
            duration = await get_video_duration(input_path)
            total_frames = 0
            try:
                val = await ffprobe(["-select_streams", "v:0", "-show_entries", "stream=nb_frames",
                                     "-of", "default=noprint_wrappers=1:nokey=1"], input_path)
                if val and val != "N/A":
                    total_frames = int(val)
            except Exception:
                pass

            cmd = ["ffmpeg", "-y", "-hwaccel", "cuda", "-hwaccel_device", str(gpu_id), "-i", input_path]
            cmd.extend(["-c:v", "hevc_nvenc", "-gpu", str(gpu_id)])
            cmd.extend(["-rc", "vbr", "-cq", str(settings["crf"])])
            cmd.extend(["-preset", NVENC_PRESET_MAP.get(settings["preset"], "p4")])
            cmd.extend(["-maxrate", "15M", "-bufsize", "15M"])
            vf = []
            if settings["resolution"] != "original":
                sc = RESOLUTION_MAP.get(settings["resolution"])
                if sc:
                    vf.append(f"scale={sc}")
            vf.append("format=nv12")
            cmd.extend(["-vf", ",".join(vf)])
            cmd.extend(["-c:a", "copy", "-movflags", "+faststart"])
            cmd.extend(["-progress", "pipe:1"])
            cmd.append(out_path)

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            active_tasks[uid] = process

            if message:
                await safe_edit_text(message, f"🎬 **Starting encode...** ({total_frames} frames)")

            ai_info = {"upscaled": False, "scale": 1, "interpolated": False, "fps_in": 0, "fps_out": 0}
            final_frame, final_size = await _monitor_encode_progress(
                process, message, total_frames, duration, gpu_id, ai_info, settings
            )

            await process.wait()
            if uid in active_tasks:
                del active_tasks[uid]
            ok = process.returncode == 0
            info = ai_info

        # ── Upload ─────────────────────────────────────────────────────
        if not ok or not os.path.exists(out_path):
            await safe_edit_text(message, "❌ **Encoding failed.** Try different settings.")
            return

        elapsed = time.time() - start
        fsize = os.path.getsize(out_path)
        fmb = fsize / (1024 * 1024)
        orig_mb = os.path.getsize(input_path) / (1024 * 1024) if os.path.exists(input_path) else 0
        red = ((orig_mb - fmb) / orig_mb * 100) if orig_mb > 0 else 0

        cap = f"✅ **Anime Encoding Complete**\n\n"
        if info.get("upscaled"):
            cap += f"🎨 Upscale: **waifu2x {info['scale']}x**\n"
        if info.get("interpolated"):
            cap += f"🎞️ RIFE: **{settings.get('rife_mult', 2)}x** ({info['fps_in']}→{info['fps_out']} fps)\n"
        cap += (
            f"📊 CRF: {settings['crf']} | ⚡ {settings['preset']}\n"
            f"📐 Res: {settings['resolution']}\n"
            f"📦 {fmb:.1f} MB (was {orig_mb:.1f} MB, {red:+.1f}%)\n"
            f"⏱️ {int(elapsed//60)}m {int(elapsed%60)}s"
        )

        if fsize > 2 * 1024 * 1024 * 1024:
            await safe_edit_text(message, "☁️ **File > 2GB — uploading to Drive...**")
            try:
                link = await upload_to_gdrive(out_path, out_name, message)
                await message.edit_text(f"{cap}\n\n☁️ [Drive Link]({link})", disable_web_page_preview=True)
            except Exception as e:
                await message.edit_text(f"❌ Drive upload failed: `{e}`")
        else:
            await safe_edit_text(message, "⬆️ **Uploading...**")
            ul_start = time.time()
            ul_last = [0]

            async def ul_progress(cur, tot):
                now = time.time()
                if now - ul_last[0] < 3:
                    return
                ul_last[0] = now
                pct = int(cur / tot * 100) if tot else 0
                cmb = cur / (1024 * 1024)
                tmb = tot / (1024 * 1024)
                spd = cmb / (now - ul_start) if now - ul_start > 0 else 0
                eta = int((tmb - cmb) / spd) if spd > 0 else 0
                try:
                    await message.edit_text(
                        f"⬆️ **Uploading...** [{make_progress_bar(pct)}] **{pct}%**\n"
                        f"📤 {cmb:.1f}/{tmb:.1f} MB | {spd:.1f} MB/s | ETA {eta//60}m {eta%60}s"
                    )
                except Exception:
                    pass

            await client.send_document(
                chat_id=message.chat.id, document=out_path,
                caption=cap, force_document=True, progress=ul_progress
            )
            await safe_edit_text(message, "✅ **Done!** Send another anime video.")

        await react_to_message(client, message.chat.id, message.id)

    except asyncio.CancelledError:
        await safe_edit_text(message, "🛑 **Cancelled.**")
    except Exception as e:
        import traceback
        traceback.print_exc()
        await safe_edit_text(message, f"❌ **Error:** `{str(e)[:400]}`")
    finally:
        if gpu_id is not None:
            try:
                await release_gpu(gpu_id)
            except Exception:
                pass
        if uid in active_tasks:
            del active_tasks[uid]
        if os.path.exists(dl_dir):
            shutil.rmtree(dl_dir, ignore_errors=True)
        if os.path.exists(out_path):
            os.remove(out_path)


# ═══════════════════════════════════════════════════════════════════════
#  CLEANUP + MAIN
# ═══════════════════════════════════════════════════════════════════════

async def cleanup():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        for folder in ["downloads", "encoded"]:
            if os.path.exists(folder):
                for item in os.listdir(folder):
                    p = os.path.join(folder, item)
                    try:
                        if now - os.path.getmtime(p) > 300:
                            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
                    except Exception:
                        pass


async def main():
    async with app:
        asyncio.create_task(cleanup())
        if LOG_CHANNEL:
            try:
                await app.send_message(LOG_CHANNEL,
                    "✅ **Anime Encoder Bot Started!**\n\n"
                    "🎨 waifu2x | 🎞️ RIFE | 🖥️ NVENC\n"
                    "Ready to encode anime.")
            except Exception:
                pass
        for aid in ADMINS:
            try:
                await app.send_message(aid, "✅ **Anime Encoder Bot is online!**")
            except Exception:
                pass
        print("Anime Encoder Bot running...")
        await asyncio.Event().wait()


print("Starting Anime Encoder Bot...")
app.run(main())
