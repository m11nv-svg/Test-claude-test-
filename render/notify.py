import json, os, sys, requests
job = json.load(open(sys.argv[1])); tok = os.environ["TG_TOKEN"]
with open(sys.argv[2], "rb") as f:
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendVideo",
        data={"chat_id": job["chat_id"], "caption": job.get("title", "الفيديو جاهز للمراجعة"), "supports_streaming": "true"},
        files={"video": f}, timeout=300)
print(r.status_code); r.raise_for_status()
