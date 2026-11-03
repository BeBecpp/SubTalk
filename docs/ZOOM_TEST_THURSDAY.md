# Thursday 5 Nov - real Zoom TEST meeting

**Never use the wedding meeting.** Use a separate test meeting on the organizer's account.
Fill the tables below with what actually happened. Nothing here is verified until it is filled in.

Evidence levels: **HTTP-accepted** (Zoom answered 2xx) < **observed** (seen in a Zoom client, with a
screenshot in `test-artifacts/`, gitignored) < **human-read** (a person read and judged it).

## People and devices

- Host: starts the test meeting on the organizer account (Computer A or any computer).
- Viewer 1: a second participant on a **desktop** Zoom client (captions on).
- Viewer 2: a participant on a **phone** (captions on).
- Optional: join as a guest in the Claude browser pane (the owner joins; Claude only takes screenshots).

## Steps

1. Host: start the TEST meeting. Captions ▸ *Set up manual captioner* ▸ *Copy the API token*.
2. Computer running SubTalk: `config/local.yaml` has `zoom: {allow_real_zoom: true}` (NOT allow_event_target).
3. Probe (URL is read hidden, never typed on the command line):
   ```powershell
   .venv\Scripts\subtalk.exe zoom-probe --i-confirm-test-meeting --retry-test --gap-test
   ```
   Report: `test-artifacts/zoom_probe_*.json` (contains no URL).
4. Dashboard: sign in → Connect… (target **Test meeting**, paste the URL) → **Test caption** in each
   language mode (Türkçe / English / TR + EN) → hold **go LIVE** → run the mock session or speak →
   **Pause** → **Resume** → **E-STOP** → reset → **Stop** → Disconnect.
5. Failure checks: disconnect the network of the SubTalk computer for 20 s while LIVE, reconnect;
   host leaves and rejoins (does the URL still work?); paste an expired/old URL.

## Results

| # | Check | HTTP-accepted | Observed (desktop) | Observed (phone) | Human-read | Notes |
|---|---|---|---|---|---|---|
| 1 | GET /closedcaption/seq body format | | - | - | - | |
| 2 | Turkish characters ç ğ ı İ ö ş ü | | | | | |
| 3 | English punctuation, apostrophes, “quotes” | | | | | |
| 4 | Two lines (`\n`) render as two lines | | | | | set `zoom.line_breaks` accordingly |
| 5 | Bilingual payload readable | | | | | |
| 6 | Long text (~130 chars) display | | | | | |
| 7 | Empty text is never posted | probe: `empty_text = blocked` | - | - | - | |
| 8 | Same-seq retry shown only once | | | | | |
| 9 | Seq gap (+2) accepted / rejected / shown? | | | | | |
| 10 | Order of captions correct | | | | | |
| 11 | Delay POST -> visible (first caption / later) | | | | | |
| 12 | Pause drops, Resume continues, no burst | | | | | |
| 13 | E-STOP: nothing appears after it | | | | | |
| 14 | Reconnect after network loss: no stale captions | | | | | |
| 15 | Host rejoin: token still valid? new token starts at seq 1? | | | | | |
| 16 | Expired / wrong URL: alert + auto-pause, no loop | | | | | |
| 17 | Guests without captions enabled see nothing different | | | | | |

## Decision after Thursday

- GO for Zoom output if rows 2, 4/5 (chosen mode), 8, 10, 11, 12, 13, 14 pass at the *observed* level.
- If bilingual is unreadable on phones: use Türkçe only (Turkish guests are the primary audience).
- If anything critical fails: NO-GO for Zoom captions; fallback = phrase deck only or no captions.
