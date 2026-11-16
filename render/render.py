import base64
import wave
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
GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
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
    stop = {"cinematic", "wide", "shot", "medium", "close", "dramatic", "vertical", "logos", "text", "image", "scene", "with", "from", "into", "above", "over", "under", "through", "that", "this", "their", "they", "standing", "looking", "ancient"}
    words = re.findall(r"[A-Za-z]{4,}", prompt.lower())
    keywords = []
    for word in words:
        if word not in stop and word not in keywords:
            keywords.append(word)

    queries = []
    if len(keywords) >= 2:
        queries.extend([" ".join(keywords[:2]), " ".join(keywords[1:3])])
    queries.extend(keywords[:8])
    queries.extend(["ancient architecture", "desert landscape", "sandstorm", "oasis"])
    seen = set()

    for query in queries:
        query = query.strip()
        if not query or query in seen:
            continue
        seen.add(query)
        search_params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srnamespace": 6,
            "srlimit": 15,
            "format": "json",
        }
        search_response = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params=search_params, headers=HEADERS, timeout=45
        )
        search_response.raise_for_status()
        titles = [item.get("title") for item in search_response.json().get("query", {}).get("search", []) if item.get("title")]
        if not titles:
            continue

        info_params = {
            "action": "query",
            "titles": "|".join(titles),
            "prop": "imageinfo",
            "iiprop": "url|mime",
            "iiurlwidth": W,
            "format": "json",
        }
        info_response = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params=info_params, headers=HEADERS, timeout=45
        )
        info_response.raise_for_status()
        pages = info_response.json().get("query", {}).get("pages", {})
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            if info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
                continue
            image_url = info.get("thumburl") or info.get("url")
            if not image_url:
                continue
            try:
                download_valid_image(image_url, path)
                print(f"IMAGE_SOURCE=Wikimedia Commons; query={query}; file={page.get('title', 'unknown')}")
                return True
            except Exception as exc:
                print(f"Skipping Commons image {page.get('title', 'unknown')}: {exc}")
    return False


def get_image(prompt, path, n):
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) + f"?width={W}&height={H}&nologo=true&seed={n}"
    last_error = "unknown image service error"
    for attempt in range(2):
        try:
            response = requests.get(url, headers=HEADERS, timeout=90)
            if response.ok and response.headers.get("content-type", "").startswith("image/"):
                with open(path, "wb") as image_file:
                    image_file.write(response.content)
                with Image.open(path) as image:
                    image.verify()
                print(f"IMAGE_SOURCE=Pollinations; scene={n}")
                return
            last_error = f"HTTP {response.status_code}; content-type={response.headers.get('content-type', 'missing')}"
            if response.status_code in (402, 401, 403, 404):
                break
        except requests.RequestException as exc:
            last_error = str(exc)

    print(f"Primary image provider unavailable for scene {n}: {last_error}. Trying Wikimedia Commons.")
    try:
        if commons_image(prompt, path):
            return
    except Exception as exc:
        print(f"Wikimedia Commons fallback error for scene {n}: {exc}")

    keywords = re.sub(r"[^a-zA-Z0-9,]+", ",", prompt).strip(",")
    fallback_url = f"https://loremflickr.com/{W}/{H}/{urllib.parse.quote(keywords)}"
    try:
        download_valid_image(fallback_url, path)
        print(f"IMAGE_SOURCE=LoremFlickr fallback; scene={n}")
        return
    except Exception as exc:
        raise RuntimeError(f"All image providers failed for scene {n}. Primary: {last_error}. Final fallback: {exc}") from exc


def overlay(path, text):
    image = Image.open(path).convert("RGB").resize((W, H))
    draw = ImageDraw.Draw(image, "RGBA")
    font = ImageFont.truetype(FONT, 56)
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = (current + " " + word).strip()
        shaped = get_display(arabic_reshaper.reshape(candidate))
        if draw.textlength(shaped, font=font) > W - 160 and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    y = H - 160 - 80 * len(lines)
    draw.rectangle([40, y - 30, W - 40, H - 150], fill=(0, 0, 0, 150))
    for line in lines:
        shaped = get_display(arabic_reshaper.reshape(line))
        draw.text(((W - draw.textlength(shaped, font=font)) / 2, y), shaped, font=font, fill="white")
        y += 80
    image.save(path)


def tts(text, path):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "Missing GEMINI_API_KEY GitHub Actions secret. "
            "Add a Gemini API key from Google AI Studio before rendering."
        )

    prompt = (
        "Read the following Arabic narration in a calm, warm, natural, "
        "confident documentary style. Use measured pacing, gentle pauses, "
        "clear Egyptian Arabic pronunciation, and restrained emotion. "
        "Avoid exaggerated acting, shouting, or a robotic cadence.\n\n"
        + text
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + GEMINI_TTS_MODEL + ":generateContent"
    )
    response = requests.post(
        url,
        params={"key": api_key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": GEMINI_TTS_VOICE}
                    }
                }
            }
        },
        headers={"Content-Type": "application/json"},
        timeout=180
    )
    if not response.ok:
        raise RuntimeError(
            f"Gemini TTS failed: HTTP {response.status_code}: {response.text[:1200]}"
        )

    payload = response.json()
    parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    audio_b64 = None
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data") or {}
        if inline.get("data"):
            audio_b64 = inline["data"]
            break
    if not audio_b64:
        raise RuntimeError("Gemini TTS returned no audio data")

    pcm = base64.b64decode(audio_b64)
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(GEMINI_TTS_SAMPLE_RATE)
        wav.writeframes(pcm)


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

        get_image(scene["image_prompt_en"], image_path, number)
        overlay(image_path, scene["narration_ar"])
        tts(scene["narration_ar"], audio_path)

        audio_duration = duration(audio_path)
        clip_duration = audio_duration + 0.4
        run([
            "ffmpeg", "-y", "-loop", "1", "-i", image_path, "-i", audio_path,
            "-t", str(clip_duration),
            "-vf", "zoompan=z='min(zoom+0.0008,1.15)':d=1:s=1080x1920:fps=30",
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
