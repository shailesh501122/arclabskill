# Storyboard format

```jsonc
{
  "series": "Arc Lab",              // series name shown in header and title pill
  "episode": 2,                     // number; shown as "EP 02", outro teases EP 03
  "title": "Why the ocean is blue", // title card headline
  "subtitle": "Physics in 30 seconds", // optional line under the title
  "hook": "It's not the sky!",      // optional, spoken before the title when --voice
  "handle": "@arclab",              // optional footer text
  "font": "/path/to/Bold.ttf",      // optional, same as --font
  "title_duration": 2.5,            // optional seconds for the title card
  "theme": {                        // optional, every key optional
    "bg_top": "#0B1020", "bg_bottom": "#1B1140",
    "accent": "#00E5FF", "accent2": "#FF3D81",
    "text": "#FFFFFF", "muted": "#9AA4C7"
  },
  "scenes": [
    "A plain string is a scene with just caption text.",
    {
      "kicker": "The trick",       // small pill above the caption
      "text": "Caption text shown on screen, a few words at a time.",
      "narration": "Optional different text for TTS (defaults to text).",
      "audio": "vo/scene2.wav",    // optional recorded voice, relative to this JSON
      "duration": 5.0              // optional seconds; overrides auto timing
    }
  ],
  "outro": {                        // or a string, or false to drop the outro
    "kicker": "NEXT UP",
    "text": "Follow Arc Lab for Episode 03",
    "narration": "...", "duration": 4
  }
}
```

Outputs, next to `-o out.mp4`: `out.mp4` (H.264/AAC, 1080x1920, 30 fps),
`out.srt` (caption chunks with timings) and `out_cover.png` (the title card as a thumbnail).
