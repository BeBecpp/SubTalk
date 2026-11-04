# Friday 6 Nov - full dress rehearsal (production equipment)

Goal: run the wedding path end to end with the real microphone and mixer, Computer B (RTX 4060,
32 GB, i9), real Whisper, real Claude and a separate Zoom **TEST** meeting; measure latency per
stage; decide GO / NO-GO per feature; freeze the build. Never the wedding meeting.

Record every result in `docs/TEST_REPORT.md` (evidence level: observed / human-read).

## 0. Before Friday (Thursday at the latest)

- [ ] Computer B: NVIDIA driver current; `scripts\install.ps1 -Engine`; `subtalk doctor` shows the GPU.
- [ ] Download `large-v3` once (3 GB, slow link): start SubTalk with `asr.engine: whisper` and wait
      until the Whisper chip is green, or run `subtalk bench-asr` on any WAV.
- [ ] Copy `event/` (`event_names.json`, `phrases.yaml`; `event/talk/` also comes with `git clone`) to Computer B. The rest of `event/` never into git.
- [ ] `.env` on Computer B: `ANTHROPIC_API_KEY`, `SUBTALK_AUDIO_TOKEN` (same token on Computer A).
- [ ] Windows Update paused until 8 Nov, sleep off, no GPU driver updates after this.

## 1. ASR benchmark on the GPU (30 min)

- [ ] Record 3 clips (1-2 min each) of someone reading the S-41 talk in Mongolian through the real
      mic + mixer (WAV, 16-bit). Put them in a folder with the talk text as `<name>.mn.txt`.
- [ ] `subtalk bench-asr <folder> --models large-v3` → `bench_out/summary.csv`: latency p50/p95, CER.
- [ ] Target: Whisper ≤ 1.2 s per segment at p50 (so speech-end → visible stays ≤ 4 s).
      If slower: try `beam_size: 1-2`, then `large-v3-turbo`; compare CER before choosing.

## 2. Live run in a TEST meeting (60 min)

Setup: Computer A runs Zoom (host) + the sender; Computer B runs SubTalk; a second person on a
desktop Zoom app and one on a phone, both with **Show Captions** on.

- [ ] Connect (TEST) → Test caption: TR + EN visible on desktop and phone. Layout: 1 line, or 2 lines
      if both devices render the line break.
- [ ] The speaker reads 5-10 minutes of the talk into the real mic. Check on the guest devices:
      readable speed, TR first, names Galkhuu / Akane / Bat-Erdene correct, references
      (Başlangıç 2:21, 22 / Efesliler 5:25, 28, 29 / 5:22-24 / 5:33) correct.
- [ ] Bible reading aloud (5 readings): the read verse text is translated as spoken, never invented.
- [ ] Japanese interpreter: interpreted repeats suppressed in both languages; an independent Japanese
      sentence is translated.
- [ ] Vows: try both - (a) automatic captions, (b) Pause + phrase deck "Vow - groom / bride"
      + Resume. Pick one for Saturday (b avoids showing each vow line twice).
- [ ] Music mode during a song; phrase deck "[Music]" / "[Song]".
- [ ] **TR + EN + JA** mode for an MC part and the vows: a Japanese family member reads the JA
      part on a phone and judges it; the Turkish/English readers still find their line. Phrase deck
      vow in TR + EN + JA: complete, paced, readable. Switch back to TR + EN for the talk.
- [ ] Dashboard Diagnostics ▸ latency: record p50/p95 per stage (VAD, ASR, Claude, guards, order,
      Zoom). **Target: speech end → visible p50 ≤ 4 s, p95 ≤ 7 s** (check visible time by eye on a phone
      once or twice with a stopwatch).

## 3. Recovery drills (20 min)

- [ ] E-STOP while speaking: nothing more appears; reset → not LIVE until held again.
- [ ] Pull Computer B's network for 20 s while LIVE; plug back: no burst of old captions.
- [ ] Close and reopen the dashboard; restart the SubTalk engine: output comes back OFF.
- [ ] Unplug the audio sender: Audio chip red; plug back: recovers by itself.
- [ ] Host leaves and rejoins the TEST meeting: does the old caption URL still work? (If not, paste the new one.)

## 4. GO / NO-GO and freeze

| Feature | GO if | Fallback |
|---|---|---|
| TR + EN automatic captions | readable on desktop + phone, latency target met or close, no wrong names/references | TR only |
| Japanese interpreter handling | no duplicate captions | Japanese ▸ Off during the interpreted talk |
| Phrase deck vows | shown once, complete | automatic captions |
| TR + EN + JA (vows / MC) | readable on desktop + phone, Japanese judged OK by a native reader | TR + EN (Japanese guests: phrase deck JA only, or none) |
| Any critical failure | - | phrase deck only, or no captions |

- [ ] Write the decisions into `docs/TEST_REPORT.md` and `docs/RUNBOOK.md`.
- [ ] Freeze: no code changes after this; note the commit hash in `docs/SESSION_HANDOFF.md`.
