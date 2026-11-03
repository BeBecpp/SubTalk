# SubTalk public caption relay (Cloudflare Worker + Durable Object)

Guests outside the venue network open one HTTPS link, pick **Türkçe** or **English**,
and receive captions. Computer B publishes each caption **once** (outbound HTTPS);
Cloudflare fans it out. Nothing on Computer B or Computer A is exposed to the internet.

```
Computer B engine --POST /publish (Bearer token)--> Worker --> CaptionRoom (Durable Object)
                                                                 |  fan-out over WebSocket
Guests (phones / laptops) <--wss /ws/viewer (read-only)----------+
```

What the relay serves: `/` and `/viewer` (the guest page), `/ws/viewer` (read-only socket),
`/healthz` (viewer count, age of the last engine message), `/publish` (engine only, token).
Everything else is 404. There is no admin page, no Zoom URL, no Claude key, and no database:
the last 30 captions live in memory only.

## Local test (no Cloudflare account needed)

```
cd relay
npm install                     # wrangler 4.151.2 (pinned)
python -c "import secrets; print('PUBLISH_TOKEN=' + secrets.token_urlsafe(32))" > .dev.vars
npx wrangler dev --local --ip 127.0.0.1 --port 8787
```

Engine (`config/local.yaml`):

```yaml
viewer:
  enabled: true
  relay_url: "http://127.0.0.1:8787"
```

and `SUBTALK_RELAY_TOKEN=<same value>` in `.env`. Load test:
`.venv\Scripts\python.exe relay\loadtest.py --clients 1000` (results label: local only).

## Deploy (owner, on Computer B or the dev laptop; needs a Cloudflare login)

1. `cd relay && npx wrangler login` (browser sign-in; Claude does not do this step).
2. `npx wrangler secret put PUBLISH_TOKEN` and paste a NEW random token
   (`python -c "import secrets; print(secrets.token_urlsafe(32))"`). Do not reuse the dev token.
3. `npx wrangler deploy`, which prints `https://subtalk-captions.<your-subdomain>.workers.dev`.
4. On Computer B set `viewer.relay_url` to that URL in `config/local.yaml` (gitignored) and
   `SUBTALK_RELAY_TOKEN` in `.env`. Restart SubTalk; the dashboard "Guests" chip must turn green.
5. Run the load test against the deployed URL from a different network (e.g. phone hotspot):
   `.venv\Scripts\python.exe relay\loadtest.py --url https://... --clients 1000 --label "cloudflare deployed"`.
   Until this passes, **1,000 viewers is unverified**.

Plan: SQLite-backed Durable Objects run on the Workers Free plan, but Free has daily request caps
(each guest connection and each batch of 20 incoming WebSocket messages counts). For a ~3-hour event
with ~1,000 guests the estimate fits, but check the current numbers on Cloudflare's pricing page.
Workers Paid ($5/month) removes the daily cap. That choice and its cost are the owner's to make.

## Rotating / disabling

- Leaked link: the link is read-only and only shows captions; changing it means a new Worker name.
- Leaked publish token: `npx wrangler secret put PUBLISH_TOKEN` (new value) and update `.env`.
- Disable the public viewer: set `viewer.relay_url: ""` (or `viewer.enabled: false`) and restart,
  or `npx wrangler delete`. Zoom captions are unaffected.
