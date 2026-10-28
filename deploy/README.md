# Deploying to AWS Lightsail at dmvtransit.com

This puts the assistant on the public internet at **https://dmvtransit.com**, on one
small AWS Lightsail server in **Ohio (us-east-2)** running three containers:

| Container | Job |
| --- | --- |
| `caddy` | The front door: HTTPS certificates (automatic, free), security headers, `www` → apex redirect |
| `app` | The chat website + MCP tools (never exposed directly) |
| `scheduler` | Keeps data fresh: loads everything on first boot, refreshes alerts every 15 min, reloads all schedules/fares nightly at 3:30 am Eastern |

Why Ohio: this AWS account uses the **new AWS experience**, where every project has one
selected Region and Regional resources (like a Lightsail server) can only be created
there — check it in AWS Settings → View all projects → Overview → Additional info.
Ohio is a few milliseconds from DC; riders won't notice.

The scripts in [`lightsail/`](lightsail/) do the console work for you (Windows
PowerShell, using your signed-in AWS CLI profile — `aws login`):

| Script | What it does |
| --- | --- |
| `create.ps1` | Creates the server, static IP, firewall and daily snapshots. Safe to re-run. |
| `push-env.ps1` | Sends your settings and API keys to the server (without displaying them) and starts the site. |
| `remote.ps1` | Runs a command on the server, or uploads a file, with short-lived SSH credentials. |

> The site is an **unofficial** tool, not affiliated with WMATA or any transit agency.
> The page says so in its header, welcome message and footer — keep those notices if
> you customise the page, and don't use agency logos.

## What it costs

| Item | Cost |
| --- | --- |
| Lightsail 2 GB server (`small_3_0`: 2 vCPU, 60 GB disk, 3 TB transfer, public IPv4 included) | $12 / month |
| Static IP | free while attached |
| Automatic daily snapshots | a few cents to ~$3 / month |
| dmvtransit.com domain | $16 / year |
| Lightsail DNS zone | free |
| Anthropic API | depends on use — **capped by the limits below** |
| WMATA API, agency GTFS and GTFS-realtime feeds | free |
| Transitland (Explorer plan, live times for some agencies) | free plan — the app stays under Transitland's 5,000 calls/month on its own (`TRANSITLAND_MONTHLY_BUDGET`) |
| Google Geocoding API (address search) | billed per request by Google — the app stops at `GOOGLE_GEOCODING_MONTHLY_BUDGET` (default 9,000) a month; searches are cached for a day |

Why 2 GB: the nightly data load covers 21 GTFS feeds, including WMATA's Metrobus
schedule (4+ million timetable rows). It is processed in a streaming way (the heaviest
step peaked at about 270 MB measured locally) and the database is about 900 MB on
disk, plus about 600 MB for the map; with Ubuntu and Docker (~400 MB) that leaves
comfortable headroom. The first full load takes roughly 15–20 minutes.

## 0. Before you start

1. **Set a monthly spend limit in the [Anthropic Console](https://console.anthropic.com/)**.
   This is the hard ceiling on what a public site can cost you. The app adds its own
   limits on top: 6 questions/minute and 60/day per visitor, and 1,500/day for the whole
   site (adjustable in the server's `.env`).
2. **Get a fresh WMATA API key** at [developer.wmata.com](https://developer.wmata.com/)
   and put it in your local `.env` (older versions of this app sent the key to other
   agencies' servers by mistake — replace it before going live).
3. Sign in to AWS on this PC: `aws login` (credentials last 12 hours).

## 1. Create the server

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\create.ps1
```

It prints the static IP and a test hostname like `3-14-15-92.sslip.io` (a free public
DNS name that points at that IP, so HTTPS can be tested before you own the domain).
On its first boot the server clones this repository and installs Docker; wait for that
to finish:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cloud-init status --wait'
```

## 2. Start the site on the test hostname

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\push-env.ps1 -SiteDomain 3-14-15-92.sslip.io -AcmeEmail you@example.com
```

(Use the hostname `create.ps1` printed and the email that should get certificate
notices.) Watch the first data load — `full refresh finished` means it's ready:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cd ~/dmvtransit && docker compose logs --tail 40 scheduler'
```

Download the regional map (about 600 MB, a few minutes; re-run a few times a year to
refresh it):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'bash ~/dmvtransit/deploy/map/fetch-basemap.sh'
```

Then open `https://<test hostname>`, plan a trip on the map and ask the assistant a
question.

## 3. Register the domain

Lightsail console → **Domains & DNS** → **Register domain** → `dmvtransit.com` (you enter
the contact details and confirm the $16/year charge). Registering through Lightsail
also creates its DNS zone. Then add two records (Lightsail → Domains & DNS →
`dmvtransit.com` → **Add record**), or ask Claude to add them:

| Type | Name | Value |
| --- | --- | --- |
| A | `@` (the domain itself) | your static IP |
| A | `www` | your static IP |

## 4. Switch to dmvtransit.com

Once `nslookup dmvtransit.com` returns your static IP:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 "cd ~/dmvtransit && sed -i 's/^SITE_DOMAIN=.*/SITE_DOMAIN=dmvtransit.com/' .env && docker compose up -d"
```

Caddy fetches the certificate within a minute. Open **https://dmvtransit.com**.

---

## Day-to-day

All commands below run from the project folder on your PC.

**Ship a change** — commit and `git push`, then:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'bash ~/dmvtransit/deploy/update.sh'
```

**Health check**

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cd ~/dmvtransit && docker compose ps && docker compose exec -T app transit-mcp doctor'
```

**Logs** — swap `app` for `scheduler` or `caddy`:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cd ~/dmvtransit && docker compose logs --tail 100 app'
```

**Review usage** — every question and answer, every map search (start and end rounded
to ~100 m, nearest stations, the options shown) and page activity (options opened,
panels used, visits) is saved for a year (see "Chat log" below). Download the last 30
days — `chats-`, `searches-` and `events-` files, each as a spreadsheet plus a detailed
JSON file — into `chat-exports\`:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\download-chats.ps1
```

`-Days 7`, or `-Since 2026-10-01 -Until 2026-11-01`, picks the period. For totals only:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cd ~/dmvtransit && docker compose exec -T app transit-mcp chatlog stats --days 7'
```

To honour a removal request, delete a conversation (or `--visitor <id>` for all of
one visitor's chats); the IDs are in the downloaded files:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cd ~/dmvtransit && docker compose exec -T app transit-mcp chatlog delete --conversation <id>'
```

**Change keys or limits** — edit your local `.env` (keys) and re-run `push-env.ps1`, or
edit the server's `.env` (`~/dmvtransit/.env`) through Lightsail's browser SSH and run
`docker compose up -d`.

**Refresh all data right now**

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cd ~/dmvtransit && docker compose exec -T scheduler transit-mcp ingest all'
```

**Check this month's Transitland calls** (the planner falls back to scheduled times
once `TRANSITLAND_MONTHLY_BUDGET` is used up):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cd ~/dmvtransit && docker compose exec -T app transit-mcp doctor'
```

**Add a feed by hand** (if an agency's download stops working). Download its
`google_transit.zip` in your browser, then (swap `cue` for the agency's slug):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 -Upload $HOME\Downloads\google_transit.zip -To '~/dmvtransit/feeds/cue/google_transit.zip'
```

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cd ~/dmvtransit && docker compose exec -T scheduler transit-mcp ingest gtfs --only cue'
```

**Plug in your scheduling engine** — see [docs/EXTENDING.md](../docs/EXTENDING.md).
For a Node engine, rebuild with Node included:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cd ~/dmvtransit && docker compose build --build-arg WITH_NODE=true && docker compose up -d'
```

**Shut it all down** (stops the charges; the domain stays yours):

```powershell
aws lightsail delete-instance --instance-name dmvtransit --region us-east-2
```

```powershell
aws lightsail release-static-ip --static-ip-name dmvtransit-ip --region us-east-2
```

Automatic snapshots are deleted with the instance; manual snapshots are not.

## Without the scripts (console only)

1. Lightsail → **Create instance** → Region **Ohio (us-east-2)** → Linux/Unix → OS only →
   **Ubuntu 24.04 LTS** → 2 GB plan → name `dmvtransit`. Under *Launch script*, paste the
   contents of [`lightsail/user-data.sh`](lightsail/user-data.sh).
2. **Networking → Create static IP** and attach it; in **IPv4 firewall** add HTTPS (443).
   **Snapshots → Enable automatic snapshots.**
3. **Connect using SSH**, then `nano ~/dmvtransit/.env` and fill in `SITE_DOMAIN`,
   `ACME_EMAIL`, `ANTHROPIC_API_KEY`, `WMATA_API_KEY`.
4. `cd ~/dmvtransit && docker compose up -d --build`, then continue with steps 3–4 above
   (run the commands in the browser terminal instead of through `remote.ps1`).

## Keeping an eye on it

- **Uptime:** add a free external monitor (e.g. UptimeRobot) on
  `https://dmvtransit.com/api/health` — it returns `{"ok": true}` when healthy.
- **Server:** Lightsail → instance → **Metrics** → add alarms for CPU and failed
  status checks.
- **Spend:** watch usage in the Anthropic Console and AWS Settings → Billing (and any
  spend limit on the AWS project). If the site-wide daily limit is hit, visitors see a
  polite "come back tomorrow" message instead of errors.
- **Backups:** automatic snapshots (08:00 UTC daily) cover everything. All transit data
  can be re-downloaded anyway; the things worth protecting are `.env`, the
  certificates and the chat log (all inside the snapshot).

## Rider reports

Riders' reports (Report an issue on the map page) are stored in the transit database
and checked by `REPORTS_MODEL` before other riders see them. Limits: 2 a minute and 10 a
day per visitor, 300 a day for the whole site (`REPORTS_*` in the server's `.env`). To
review or remove one:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cd ~/dmvtransit && docker compose exec -T app transit-mcp reports list --days 2'
```

```powershell
powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cd ~/dmvtransit && docker compose exec -T app transit-mcp reports hide 42'
```

## Chat log

The site saves each question with its answer, the data tools used, timing, token
counts and — for refused questions — the reason (rate limit, daily cap, too long).
Visitors get an anonymous ID (a keyed hash of their IP address, never the address);
email addresses, phone numbers and long numbers are removed before saving; rows older
than a year are deleted every night. The page tells visitors this in its footer and
"About this site". Settings (`CHATLOG_*`) are in the server's `.env`; set
`CHATLOG_ENABLED=false` to stop saving. Downloaded files in `chat-exports\` contain
what visitors typed: keep them private (the folder is git-ignored).

## Troubleshooting

| Symptom | Likely cause → fix |
| --- | --- |
| `remote.ps1` can't connect | Your sign-in expired (`aws login`), or the server is still booting. The script re-opens SSH for your current IP automatically. |
| Browser certificate error right after launch | DNS wasn't pointing at the server yet, or port 443 is closed. Fix, then `docker compose restart caddy` and check `docker compose logs caddy`. |
| "The assistant isn't available right now" | Run the health check — usually a missing/invalid key in the server's `.env`. |
| 502 Bad Gateway | The app is starting or crashed: `docker compose logs --tail 50 app`. |
| Answers missing new schedules | `docker compose logs scheduler` — nightly refresh errors are listed per agency. |
| "Access Denied" from AWS on something that used to work | Check the project's spend limit and billing status in AWS Settings → Billing. |

## How it's protected

- Open ports: 80 and 443 for everyone; SSH (22) only from Lightsail's browser SSH and
  the PC that last ran these scripts, with short-lived certificate credentials. The app
  itself is reachable only through Caddy.
- HTTPS everywhere with HSTS; strict Content-Security-Policy; no framing.
- Per-visitor rate limits keyed on the real visitor address (forged
  `X-Forwarded-For` headers are discarded by Caddy), a site-wide daily cap, 1,000-
  character questions, 20-question conversations, bounded memory for conversations.
- Visitors see plain-language messages only; technical details go to the server logs.
- No IP addresses are stored: the chat log uses anonymous visitor IDs, the app's
  per-request log is off, and Caddy's access log masks addresses to their network.
- Location: the browser asks before sharing it, it is used only to find nearby stops,
  and map searches are logged with points rounded to about 100 meters. The geocoder and
  Transitland keys stay on the server; browsers only talk to dmvtransit.com.
- Secrets live only in `~/dmvtransit/.env` (`chmod 600`), never in the image or Git.
- Containers run as a non-root user; Ubuntu installs security updates automatically.
