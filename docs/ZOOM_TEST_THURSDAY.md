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

## Preliminary run - Wed 4 Nov 2026, 13:00-13:10 (Ulaanbaatar)

Separate instant meeting (random ID, not the PMI, not the wedding), Zoom **web client**, host view
only (no second participant yet). SubTalk in mock mode on the dev laptop (prewritten TR + EN),
real caption POSTs. Owner pasted the API token; Claude drove the dashboard and looked at the
caption area. Evidence level: **observed** (web client, host) unless noted.

| # | Check | Result |
|---|---|---|
| 1 | Seq endpoint / first caption | seq started at 1 on the new token; seq known |
| 2 | Turkish ç ğ ı İ ö ş ü | correct |
| 3 | English apostrophe, curly quotes | correct |
| 4 | `\n` between TR and EN | **NOT rendered** in the web client - lines run together |
| 5 | Single-line fallback `TR: ... \| EN: ...` | readable, languages clearly separated -> new default |
| 10 | Order of captions | 13 captions in order, seq 2-16 contiguous |
| 11 | POST latency | p50 0.49 s, p95 1.24 s, max 1.84 s (13 POSTs, HTTP-accepted) |
| 8 | Same-seq retry | first POST timed out at 1.5 s but WAS shown; retry got **HTTP 403**; no duplicate shown. Sender fixed (D-015) |
| 12 | Pause / Resume | 0 captions in 12 s paused; 2 in 10 s after resume (normal rate, no burst) |
| 13 | E-STOP | 0 captions in 10 s after E-STOP; reset -> CONNECTED, not LIVE |
| - | Stop | 0 captions in 8 s; state CONNECTED |
| - | Caption display time | visible ~8-10 s, then the caption area clears |
| - | Long caption parts | "TR: kadın da kocasına derin saygı göstermelidir. \| EN: and a wife must deeply respect her husband." (part 2 of 2, aligned) |

Still open for Thursday: desktop app and phone as separate participants (does `\n` render there?),
network loss while LIVE, host rejoin / expired URL, the sender fix (D-015) against real Zoom,
and a human reading the captions at real speed.

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
| 18 | TR + EN + JA mode: test caption + phrase deck vow readable (length, wrapping, Japanese glyphs) | | | | | D-019 |

### Guest viewer (only if the relay is deployed by Thursday; otherwise move to Friday)

| # | Check | Result | Notes |
|---|---|---|---|
| V1 | Phone on mobile data opens the public link, picks Türkçe; laptop on another network picks English | | |
| V2 | Same caption event appears on both, each in its own language; delay vs Zoom | | |
| V3 | Pause → "paused" on guests; E-STOP → guest screens cleared | | |
| V4 | Phone switches Wi-Fi → mobile data: reconnects by itself, no old captions appear late | | |
| V5 | Computer B internet unplugged 30 s: guests show "source unreachable", recover after | | |
| V6 | Dashboard "Guests" chip shows guest count; Zoom keeps captioning during V5 if internet stays on A | | |
| V7 | `relay/loadtest.py --url <deployed> --clients 1000` from a different network | | the 1,000-viewer claim depends on this |

## Decision after Thursday

- GO for Zoom output if rows 2, 4/5 (chosen mode), 8, 10, 11, 12, 13, 14 pass at the *observed* level.
- If bilingual is unreadable on phones: use Türkçe only (Turkish guests are the primary audience).
- If anything critical fails: NO-GO for Zoom captions; fallback = phrase deck only or no captions.
