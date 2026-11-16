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
GEMINI_TTS_VOICE = "Schedar"
GEMINI_TTS_SAMPLE_RATE = 24000
FONT = "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"
HEADERS = {"User-Agent": "ContentFactory/1.0 (https://github.com/m11nv-svg/Test-claude-test-)"}


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


def commons_image(prompt, path):
    # Search using meaningful noun phrases, then reject titles that do not
    # contain at least two scene-specific keywords. Never use the first hit blindly.
    stop = {
        "cinematic", "wide", "shot", "medium", "close", "dramatic", "vertical",
        "logos", "text", "image", "scene", "with", "from", "into", "above",
        "over", "under", "through", "that", "this", "their", "they", "standing",
        "looking", "ancient", "beautiful", "realistic", "historical", "detailed",
        "photorealistic", "lighting", "camera", "view", "style"
    }
    words = re.findall(r"[A-Za-z]{3,}", prompt.lower())
    keywords = []
    for word in words:
        if word not in stop and word not in keywords:
            keywords.append(word)
    if len(keywords) < 2:
        raise RuntimeError(f"Scene image prompt is not specific enough: {prompt}")

    queries = []
    for index in range(min(len(keywords) - 1, 7)):
        queries.append(" ".join(keywords[index:index + 2]))
    queries.append(" ".join(keywords[:min(5, len(keywords))]))

    candidates = {}
    for query in dict.fromkeys(queries):
        search_params = {
            "action": "query", "list": "search", "srsearch": query,
            "srnamespace": 6, "srlimit": 30, "format": "json",
        }
        response = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params=search_params, headers=HEADERS, timeout=45
        )
        response.raise_for_status()
        for item in response.json().get("query", {}).get("search", []):
            title = item.get("title", "")
            title_words = set(re.findall(r"[a-z]{3,}", title.lower()))
            overlap = title_words.intersection(keywords)
            # A result must identify the actual subject, not just be returned
            # by a broad or noisy search.
            if len(overlap) >= 2:
                candidates[title] = max(candidates.get(title, 0), len(overlap))

    if not candidates:
        print(f"NO_RELEVANT_COMMONS_IMAGE prompt={prompt}")
        return False

    ranked = sorted(candidates.items(), key=lambda item: (-item[1], item[0]))
    titles = [title for title, _score in ranked[:20]]
    info_params = {
        "action": "query", "titles": "|".join(titles),
        "prop": "imageinfo", "iiprop": "url|mime",
        "iiurlwidth": W, "format": "json",
    }
    info_response = requests.get(
        "https://commons.wikimedia.org/w/api.php",
        params=info_params, headers=HEADERS, timeout=45
    )
    info_response.raise_for_status()
    pages = info_response.json().get("query", {}).get("pages", {})
    ranked_pages = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
            continue
        title = page.get("title", "")
        score = candidates.get(title, 0)
        image_url = info.get("thumburl") or info.get("url")
        if image_url:
            ranked_pages.append((score, title, image_url))

    for score, title, image_url in sorted(ranked_pages, reverse=True):
        try:
            download_valid_image(image_url, path)
            print(f"IMAGE_SOURCE=Wikimedia Commons; relevance_score={score}; file={title}")
            return True
        except Exception as exc:
            print(f"Skipping Commons image {title}: {exc}")
    return False

def get_image(prompt, path, n):
    # Prefer authentic, verifiable Commons media over synthetic or random images.
    try:
        if commons_image(prompt, path):
            print(f"IMAGE_SOURCE=Wikimedia Commons; scene={n}")
            return
    except Exception as exc:
        print(f"Wikimedia Commons lookup failed for scene {n}: {exc}")

    raise RuntimeError(
        f"No suitable verified Wikimedia Commons image was found for scene {n}. "
        "The renderer stopped instead of inserting an unrelated or visibly synthetic image."
    )


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

    for scene in job["scenes"]:
        number = scene["n"]
        image_path = f"work/{number}.png"
        audio_path = f"work/{number}.wav"
        clip_path = f"work/{number}.mp4"

        if not get_image(scene["image_prompt_en"], image_path, number):
            raise RuntimeError(
                f"No sufficiently relevant image found for scene {number}; "
                "stopping rather than publishing an unrelated image."
            )
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
        run([
            "ffmpeg", "-y", "-loop", "1", "-i", image_path, "-i", audio_path,
            "-t", str(clip_duration),
            "-vf", f"zoompan=z='min(zoom+0.0008,1.15)':d=1:s=1080x1920:fps=30,{subtitle_filter}",
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
