import base64
import json
import math
import os
import re
import subprocess
import sys
import urllib.parse

import requests
from PIL import Image, ImageDraw, ImageFont
import arabic_reshaper
from bidi.algorithm import get_display

W, H = 1080, 1920
GEMINI_TTS_MODEL = "gemini-3.8-flash-tts"
GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"
GEMINI_TTS_VOICE = "Schedar"
GEMINI_TTS_SAMPLE_RATE = 24000
FONT = "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"
HEADERS = {"User-Agent": "ContentFactory/1.0 (https://github.com/m11nv-svg/Test-claude-test-)"}
USED_IMAGE_TITLES = set()


def run(cmd):
    subprocess.run(cmd, check=True)


def validate_job(job):
    if not isinstance(job, dict):
        raise ValueError("Job must be a JSON object")
    for field in ("job_id", "chat_id", "scenes"):
        if field not in job:
            raise ValueError(f"Missing required job field: {field}")
    if not isinstance(job["scenes"], list) or not 6 <= len(job["scenes"]) <= 8:
        raise ValueError("The job must contain 6 to 8 scenes")

    for index, scene in enumerate(job["scenes"], start=1):
        if not isinstance(scene, dict):
            raise ValueError(f"Scene {index} must be an object")
        if scene.get("n") != index:
            raise ValueError(f"Scene numbering must be sequential; expected {index}")
        for field in ("narration_ar", "image_prompt_en"):
            if not isinstance(scene.get(field), str) or not scene[field].strip():
                raise ValueError(f"Scene {index} has an empty or invalid {field}")
        seconds = scene.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 2 <= seconds <= 20:
            raise ValueError(f"Scene {index} seconds must be a number from 2 to 20")


def download_valid_image(url, path):
    response = requests.get(url, headers=HEADERS, timeout=90)
    response.raise_for_status()
    if not response.headers.get("content-type", "").startswith("image/"):
        raise RuntimeError("The image provider returned a non-image response")
    with open(path, "wb") as image_file:
        image_file.write(response.content)
    with Image.open(path) as image:
        image.verify()


def generate_cartoon_image(prompt, path, n, style_reference=None):
    """Generate a scene-matched 2D cartoon illustration with Gemini Image."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY GitHub Actions secret.")

    style = (
        "Create one polished frame for a premium Arabic educational 2D animated short. "
        "Visual language: clean hand-drawn flat cartoon illustration, expressive but "
        "tasteful non-prophet characters, crisp dark-blue outlines, simplified readable "
        "shapes, layered foreground/midground/background for parallax, rich deep navy "
        "and cobalt-blue gradient atmosphere with restrained cyan and warm-gold accents, "
        "soft cinematic rim light, subtle texture, strong visual hierarchy, beautiful "
        "professional explainer-animation art direction. Vertical 9:16 composition. "
        "The scene must depict the described event literally and clearly. No photorealism, "
        "no stock-photo look, no random symbolic objects, no modern objects, no logos, "
        "no lettering, no Arabic or English text. Never depict, silhouette, portray, or "
        "show the face or body of any prophet; represent prophets only through environment, "
        "objects, light, landscape, or other people who are explicitly not the prophet. "
        "Maintain a coherent palette and illustration technique across every scene."
    )
    full_prompt = style + "\n\nSCENE DESCRIPTION:\n" + prompt
    input_parts = [{"type": "text", "text": full_prompt}]
    if style_reference and os.path.exists(style_reference):
        with open(style_reference, "rb") as reference_file:
            reference_data = base64.b64encode(reference_file.read()).decode("ascii")
        input_parts.append({
            "type": "image",
            "data": reference_data,
            "mime_type": "image/png",
        })
        input_parts[0]["text"] += (
            "\n\nUse the supplied first-scene image strictly as a style and palette "
            "reference. Do not copy its scene content or composition."
        )

    response = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json={
            "model": GEMINI_IMAGE_MODEL,
            "input": input_parts,
            "response_format": {
                "type": "image",
                "aspect_ratio": "9:16",
                "image_size": "1K",
            },
        },
        timeout=300,
    )
    if not response.ok:
        raise RuntimeError(
            f"Gemini cartoon image generation failed for scene {n}: "
            f"HTTP {response.status_code}: {response.text[:1200]}"
        )

    payload = response.json()
    image_b64 = None
    direct = payload.get("output_image")
    if isinstance(direct, dict):
        image_b64 = direct.get("data")
    if not image_b64:
        for step in reversed(payload.get("steps", [])):
            if step.get("type") != "model_output":
                continue
            for part in reversed(step.get("content", [])):
                if part.get("type") in ("image", "output_image") and part.get("data"):
                    image_b64 = part["data"]
                    break
            if image_b64:
                break
    if not image_b64:
        raise RuntimeError(
            f"Gemini returned no image for scene {n}; response keys: {list(payload.keys())}"
        )

    image_bytes = base64.b64decode(image_b64)
    with open(path, "wb") as image_file:
        image_file.write(image_bytes)
    with Image.open(path) as generated:
        generated.verify()
    print(f"IMAGE_SOURCE=Gemini {GEMINI_IMAGE_MODEL}; scene={n}; style_reference={bool(style_reference)}")
    return True


def get_image(prompt, path, n, style_reference=None):
    return generate_cartoon_image(prompt, path, n, style_reference)


def fit_image(path):
    # Fill a 9:16 canvas without stretching the source image.
    with Image.open(path) as source:
        image = source.convert("RGB")
        scale = max(W / image.width, H / image.height)
        resized = image.resize((int(image.width * scale), int(image.height * scale)), Image.Resampling.LANCZOS)
        left = (resized.width - W) // 2
        top = (resized.height - H) // 2
        image = resized.crop((left, top, left + W, top + H))
        image.save(path, quality=95)


def srt_timestamp(seconds):
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def write_scene_subtitles(text, audio_seconds, path):
    # Short timed caption cards are much easier to read than a full paragraph
    # burned onto every frame. Timing is proportionally distributed over the
    # narration duration because the TTS endpoint does not return word timings.
    words = text.split()
    chunks, current = [], []
    for word in words:
        candidate = " ".join(current + [word])
        if current and (len(candidate) > 34 or len(current) >= 5):
            chunks.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        chunks.append(" ".join(current))
    if not chunks:
        raise ValueError("Cannot create subtitles for empty narration")

    weights = [max(1, len(chunk.split())) for chunk in chunks]
    total_weight = sum(weights)
    cursor = 0.0
    entries = []
    for index, (chunk, weight) in enumerate(zip(chunks, weights), start=1):
        start = cursor
        end = audio_seconds if index == len(chunks) else cursor + audio_seconds * weight / total_weight
        entries.append(f"{index}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{chunk}\n")
        cursor = end
    with open(path, "w", encoding="utf-8-sig") as subtitle_file:
        subtitle_file.write("\n".join(entries))

def tts(text, path):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "Missing GEMINI_API_KEY GitHub Actions secret. "
            "Add a Gemini API key from Google AI Studio before rendering."
        )

    url = "https://generativelanguage.googleapis.com/v1beta/interactions"
    response = requests.post(
        url,
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        json={
            "model": GEMINI_TTS_MODEL,
            "input": [{
                "type": "user_input",
                "content": [{
                    "type": "text",
                    "text": text,
                    "annotations": [{
                        "type": "speech_metadata",
                        "style": (
                            "Speak in natural, unmistakably Egyptian Arabic, "
                            "but keep it broadly understandable across the Arab world: "
                            "about 75% simple Egyptian colloquial phrasing and 25% "
                            "clear shared pan-Arabic wording. Avoid heavy formal Arabic, "
                            "obscure local slang, and complex vocabulary. Sound like a "
                            "real Egyptian documentary storyteller speaking warmly to "
                            "a friend: calm, confident, human, conversational, measured "
                            "pace, natural pauses, clear pronunciation, gentle emphasis, "
                            "restrained emotion, steady volume. Do not sound robotic, "
                            "theatrical, or like a news announcer. Preserve any Quranic "
                            "verse or exact religious quotation in its original correct "
                            "Arabic pronunciation and wording; never colloquialize or alter it."
                        ),
                    }],
                }],
            }],
            "response_format": {"type": "audio"},
            "generation_config": {
                "speech_config": [{"voice": GEMINI_TTS_VOICE}]
            },
        },
        timeout=240,
    )
    if not response.ok:
        raise RuntimeError(
            f"Gemini TTS failed: HTTP {response.status_code}: {response.text[:1200]}"
        )

    payload = response.json()
    audio_b64 = None
    for step in reversed(payload.get("steps", [])):
        if step.get("type") != "model_output":
            continue
        for part in reversed(step.get("content", [])):
            if part.get("type") == "audio" and part.get("data"):
                audio_b64 = part["data"]
                break
        if audio_b64:
            break

    if not audio_b64:
        raise RuntimeError("Gemini TTS returned no audio data")

    audio_bytes = base64.b64decode(audio_b64)
    if not audio_bytes.startswith(b"RIFF"):
        raise RuntimeError("Gemini TTS response was not a WAV audio file")
    with open(path, "wb") as audio_file:
        audio_file.write(audio_bytes)


def duration(path):
    output = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", path
    ])
    return float(output)


def main(job_path, output_path):
    with open(job_path, encoding="utf-8") as job_file:
        job = json.load(job_file)
    validate_job(job)

    os.makedirs("work", exist_ok=True)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    clips = []
    style_reference = None

    for scene in job["scenes"]:
        number = scene["n"]
        image_path = f"work/{number}.png"
        audio_path = f"work/{number}.wav"
        clip_path = f"work/{number}.mp4"

        if not get_image(scene["image_prompt_en"], image_path, number, style_reference):
            raise RuntimeError(
                f"No sufficiently relevant image found for scene {number}; "
                "stopping rather than publishing an unrelated image."
            )
        if style_reference is None:
            style_reference = image_path
        fit_image(image_path)
        tts(scene["narration_ar"], audio_path)

        audio_duration = duration(audio_path)
        subtitle_path = f"work/{number}.srt"
        write_scene_subtitles(scene["narration_ar"], audio_duration, subtitle_path)
        clip_duration = audio_duration + 0.4
        subtitle_filter = (
            f"subtitles={subtitle_path}:force_style="
            "'FontName=Noto Sans Arabic,FontSize=52,Bold=1,"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,"
            "BackColour=&H99000000,BorderStyle=3,Outline=1,"
            "Shadow=0,Alignment=2,MarginV=235,WrapStyle=2'"
        )
        fade_out_start = max(0.0, audio_duration - 0.25)
        video_filter = (
            "zoompan=z='min(zoom+0.0008,1.15)':d=1:s=1080x1920:fps=30,"
            f"fade=t=in:st=0:d=0.20,fade=t=out:st={fade_out_start:.3f}:d=0.20,"
            f"{subtitle_filter}"
        )
        run([
            "ffmpeg", "-y", "-loop", "1", "-i", image_path, "-i", audio_path,
            "-t", str(clip_duration),
            "-vf", video_filter,
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", clip_path
        ])
        clips.append(clip_path)

    with open("work/list.txt", "w", encoding="utf-8") as concat_file:
        concat_file.write("".join(f"file '{os.path.abspath(clip)}'\n" for clip in clips))

    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", "work/list.txt", "-c", "copy", output_path
    ])

    final_duration = duration(output_path)
    file_size = os.path.getsize(output_path)
    if not 15 <= final_duration <= 90:
        raise ValueError(f"Final video duration is outside 15–90 seconds: {final_duration:.1f}")
    if file_size >= 49_000_000:
        raise ValueError(f"Final video is too large for reliable Telegram delivery: {file_size} bytes")
    print(f"QUALITY_OK duration={final_duration:.1f}s size={file_size} bytes")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python render/render.py job.json out/final.mp4")
    main(sys.argv[1], sys.argv[2])
