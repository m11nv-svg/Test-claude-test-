# Content Factory – video renderer
Make sends `repository_dispatch` (event `render-video`) with `client_payload` = {job_id, chat_id, title, scenes[]}.
Workflow renders 9:16 video (Pollinations images, Edge TTS Arabic voice, ffmpeg), checks quality, sends it to Telegram.
Required GitHub secret: `TELEGRAM_BOT_TOKEN` (Settings → Secrets and variables → Actions). No secrets are stored in files.
