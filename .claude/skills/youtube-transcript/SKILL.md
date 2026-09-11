---
name: youtube-transcript
description: Pull a YouTube transcript with yt-dlp, and fall back to youtube-transcript.ai through playwright only when yt-dlp comes back empty. Use whenever someone drops a YouTube link and asks what the video says.
---

# YouTube transcript

A YouTube link plus "summarize this" → the text comes from `yt-dlp`. Never
open a transcript website first, most of them answer 403 to a plain fetch and
the rest build the page in the browser.

## yt-dlp

```
yt-dlp --skip-download --write-sub --write-auto-sub --sub-lang "en.*" \
  --sub-format vtt -o "clip.%(ext)s" "<url>"
```

- **`--skip-download` skips the video**, so this costs a few seconds and a
  few KB however long the video runs. `yt-dlp` missing → `brew install yt-dlp`.
- **Read the track whose name has no `-orig` in it.** That one is the file
  the creator uploaded: real spelling, real punctuation. The `-orig` track is
  the machine transcription YouTube made on upload, and it mangles every
  product name and acronym, so a summary built on it quotes names that were
  never said.
- **One track answering 429 is fine.** The command asks for every English
  track at once and YouTube rate-limits the tail of that list. You only need
  one to land.
- **Clean the `.vtt` before reading it.** Drop the header, the timestamps and
  the inline `<tags>`, drop any line that repeats the line above it, and
  unescape the HTML entities. These files are full of `&nbsp;`.

## The website is the fallback

Reach for it only when `yt-dlp` lands no track at all.

navigate to `https://youtube-transcript.ai/transcript?v=<videoId>` →
`browser_evaluate` reading `textContent` off the caption rows → clean it the
same way

- **Read `textContent`, never `innerText`.** The page clips the panel to one
  screen and hides the rest, so `innerText` stops a minute into the video
  while the whole transcript sits in the DOM.
- **It drops text, so say so when you hand over a summary built on it.**
  Measured against `yt-dlp` on the same 4-minute video: 603 words against 675,
  five chunks missing, one of them the only line in the video naming a setup
  that errors out.
