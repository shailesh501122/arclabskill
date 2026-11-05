---
name: arc-lab-series
description: Generate "Arc Lab" series short-form vertical videos (9:16, 1080x1920, YouTube Shorts / Reels / TikTok). Use when the user asks to make, script, or render an Arc Lab episode, a short explainer video, or a vertical video with bold captions from a topic, outline, or script. Writes a storyboard JSON, then renders an MP4 locally with Pillow + ffmpeg, plus an .srt caption file and a cover image. No API keys needed.
---

# Arc Lab series — short-form video generator

Turns a topic or script into a branded 9:16 MP4 with:
title card (series + episode pill) → captioned scenes with highlighted keywords →
"next episode" outro, a slow push-in on every scene, and a progress bar along the bottom.
Optional local text-to-speech narration and background music.

## Workflow

1. **Check the environment** (once per session):
   ```bash
   python3 "$SKILL_DIR/scripts/render_short.py" --check
   ```
   `ffmpeg`/`ffprobe` and Pillow are required (`pip install -r "$SKILL_DIR/scripts/requirements.txt"`).
   TTS (`espeak-ng`, `espeak`, or macOS `say`) is optional.
   `$SKILL_DIR` is the directory containing this SKILL.md.

2. **Write the storyboard.** If the user gave only a topic, write the script yourself:
   - A 30–45 s short has **4–6 scenes**, each **one sentence of 8–20 words**.
   - Scene 1 must hook immediately; the last scene pays off or gives a twist.
   - A 1–3 word `kicker` per scene (e.g. "The setup", "The trick", "Myth", "Step 2").
   - Keep the `episode` number continuous with earlier episodes if the user has any.
   Full format: `references/storyboard-format.md`. Example: `examples/episode-01.json`.
   Save as e.g. `arc-lab/ep-02-<slug>.json` in the user's working directory.

3. **Preview the timing** without rendering:
   ```bash
   python3 "$SKILL_DIR/scripts/render_short.py" arc-lab/ep-02-<slug>.json --dry-run
   ```
   Aim for 20–60 s total. Shorten scenes that run over ~8 s.

4. **Render:**
   ```bash
   python3 "$SKILL_DIR/scripts/render_short.py" arc-lab/ep-02-<slug>.json -o arc-lab/ep-02-<slug>.mp4
   ```
   Useful flags:
   - `--voice` narrate with local TTS (`--tts-voice en-us`, `--tts-rate 170`)
   - `--music bed.mp3` loop a music bed under everything (`--music-volume 0.15`)
   - `--font /path/Bold.ttf` custom font, `--max-words 3` tighter captions
   Rendering takes roughly 1–2 s of wall time per second of video.

5. **Verify and deliver.** Grab a frame or two to check the result visually:
   ```bash
   ffmpeg -v error -ss 4 -i out.mp4 -frames:v 1 -vf scale=540:-1 frame.png
   ```
   Report the paths of the `.mp4`, the `.srt` captions and the `_cover.png` thumbnail.

## Notes
- Without `--voice`, per-scene `audio` or `--music`, the video has a silent audio track
  (platforms accept it; most creators add trending audio in-app).
- Scene length comes from narration length when there is audio, otherwise from reading
  pace (~2.6 words/s). A scene's `duration` overrides both.
- Brand colors live in the storyboard `theme`; keep them the same across episodes so the
  series looks consistent.
