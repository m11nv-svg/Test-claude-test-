import asyncio, json, os, subprocess, sys, urllib.parse
import requests, edge_tts
from PIL import Image, ImageDraw, ImageFont
import arabic_reshaper
from bidi.algorithm import get_display

W, H = 1080, 1920
VOICE = "ar-EG-SalmaNeural"
FONT = "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"

def run(cmd): subprocess.run(cmd, check=True)

def get_image(prompt, path, n):
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) + f"?width={W}&height={H}&nologo=true&seed={n}"
    for _ in range(3):
        r = requests.get(url, timeout=120)
        if r.ok and r.headers.get("content-type", "").startswith("image"):
            open(path, "wb").write(r.content); return
    Image.new("RGB", (W, H), (20, 20, 30)).save(path)  # fallback

def overlay(path, text):
    im = Image.open(path).convert("RGB").resize((W, H))
    d = ImageDraw.Draw(im, "RGBA")
    f = ImageFont.truetype(FONT, 56)
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(get_display(arabic_reshaper.reshape(t)), font=f) > W - 160:
            lines.append(cur); cur = w
        else: cur = t
    lines.append(cur)
    y = H - 160 - 80 * len(lines)
    d.rectangle([40, y - 30, W - 40, H - 150], fill=(0, 0, 0, 150))
    for ln in lines:
        s = get_display(arabic_reshaper.reshape(ln))
        d.text(((W - d.textlength(s, font=f)) / 2, y), s, font=f, fill="white"); y += 80
    im.save(path)

async def tts(text, path):
    await edge_tts.Communicate(text, VOICE).save(path)

def duration(p):
    o = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p])
    return float(o)

def main(job_path, out):
    job = json.load(open(job_path)); os.makedirs("work", exist_ok=True); os.makedirs(os.path.dirname(out), exist_ok=True)
    clips = []
    for s in job["scenes"]:
        n = s["n"]; img, aud, clip = f"work/{n}.png", f"work/{n}.mp3", f"work/{n}.mp4"
        get_image(s["image_prompt_en"], img, n); overlay(img, s["narration_ar"])
        asyncio.run(tts(s["narration_ar"], aud))
        d = duration(aud) + 0.4
        run(["ffmpeg", "-y", "-loop", "1", "-i", img, "-i", aud, "-t", str(d), "-vf", "zoompan=z='min(zoom+0.0008,1.15)':d=1:s=1080x1920:fps=30",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", clip])
        clips.append(clip)
    open("work/list.txt", "w").write("".join(f"file '{os.path.abspath(c)}'\n" for c in clips))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "work/list.txt", "-c", "copy", out])
    # quality gate
    dur = duration(out)
    assert 15 <= dur <= 90, f"duration out of range: {dur}"
    assert os.path.getsize(out) < 49_000_000, "file exceeds Telegram bot limit (50MB)"
    print("OK", dur)

if __name__ == "__main__": main(sys.argv[1], sys.argv[2])
