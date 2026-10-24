# Deploying to AWS Lightsail at dmvtransit.com

This puts the assistant on the public internet at **https://dmvtransit.com**, on one
small AWS Lightsail server running three containers:

| Container | Job |
| --- | --- |
| `caddy` | The front door: HTTPS certificates (automatic, free), security headers, `www` → apex redirect |
| `app` | The chat website + MCP tools (never exposed directly) |
| `scheduler` | Keeps data fresh: loads everything on first boot, refreshes Metro alerts every 15 min, reloads all schedules/fares nightly at 3:30 am Eastern |

Plan on **about an hour** the first time, most of it waiting for DNS.

> The site is an **unofficial** tool, not affiliated with WMATA or any transit agency.
> The page says so in its header, welcome message and footer — keep those notices if
> you customise the page, and don't use agency logos.

## What it costs

| Item | Cost |
| --- | --- |
| dmvtransit.com domain | about $15 / year |
| Lightsail 2 GB instance (static IP included while attached) | about $12 / month |
| Automatic daily snapshots (backups) | a few cents / month |
| Anthropic API | depends on use — **capped by the limits below** |
| WMATA API, agency GTFS feeds | free |

Why 2 GB: the nightly data load now includes WMATA's Metrobus schedule (4+ million
timetable rows). It is processed in a streaming way — the heaviest step peaked at about
110 MB measured locally — and the database is about 650 MB on disk. With Ubuntu and
Docker (~400 MB) that leaves comfortable headroom; a 1 GB instance would run at the edge.
The first full load takes roughly 6–10 minutes.

## 0. Before you start

1. **Set a monthly spend limit in the [Anthropic Console](https://console.anthropic.com/)**
   (billing / limits settings). This is the hard ceiling on what a public site can
   cost you. The app adds its own limits on top: 6 questions/minute and 60/day per
   visitor, and 1,500/day for the whole site (all adjustable in `.env`).
2. Have your `ANTHROPIC_API_KEY` and `WMATA_API_KEY` handy.
3. The code is on GitHub at `Concorde1969/dmv-transit`.

## 1. Register the domain

As of October 2026 **dmvtransit.com was unregistered** — register it before someone
else does.

Lightsail console → **Domains & DNS** → **Register domain** → `dmvtransit.com`.
Registering through Lightsail also creates the DNS zone you need in step 4. (Any
registrar works; you'd then add the DNS records in step 4 there instead.)

## 2. Create the server

Lightsail console → **Create instance**:

- **Region:** Virginia (`us-east-1`) — closest to DMV riders
- **Platform:** Linux/Unix → **OS Only** → **Ubuntu 24.04 LTS**
- **Plan:** 2 GB RAM (≈$12/month)
- **Name:** `dmvtransit`

Then, on the new instance's page:

- **Networking → Create static IP** and attach it to `dmvtransit`. Write the IP down.
- **Networking → IPv4 firewall:** keep SSH (22) and HTTP (80), and **add HTTPS (443)**.
  Do *not* open 8765.
- **Snapshots → Enable automatic snapshots.**

## 3. Give the server read access to GitHub

Open the instance and click **Connect using SSH** (a terminal opens in your browser).
Paste these, one at a time:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/dmvtransit_deploy -N "" -C "dmvtransit server"
```

```bash
cat ~/.ssh/dmvtransit_deploy.pub
```

Copy the line it prints. On GitHub open **Concorde1969/dmv-transit → Settings →
Deploy keys → Add deploy key**, paste it, title it `lightsail`, leave *Allow write
access* **unchecked**, and save. Back in the server terminal:

```bash
printf 'Host github.com\n  IdentityFile ~/.ssh/dmvtransit_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config && chmod 600 ~/.ssh/config
```

```bash
git clone git@github.com:Concorde1969/dmv-transit.git ~/dmvtransit
```

(Answer `yes` if asked to trust github.com.)

## 4. Point the domain at the server

Lightsail → **Domains & DNS** → `dmvtransit.com` → **Add record**:

| Type | Name | Value |
| --- | --- | --- |
| A | `@` (the domain itself) | your static IP |
| A | `www` | your static IP |

DNS can take from a few minutes to an hour. Continue with step 5 meanwhile.

## 5. Install everything

```bash
sudo bash ~/dmvtransit/deploy/server-setup.sh
```

This updates Ubuntu, turns on automatic security updates, installs Docker, adds swap,
and creates `~/dmvtransit/.env`. When it finishes, **close the browser terminal and
reconnect** (so Docker works without `sudo`), then fill in your settings:

```bash
nano ~/dmvtransit/.env
```

Set `ACME_EMAIL`, `ANTHROPIC_API_KEY` and `WMATA_API_KEY` (`SITE_DOMAIN` is already
`dmvtransit.com`). Save with **Ctrl+O, Enter**, exit with **Ctrl+X**.

## 6. Go live

Check DNS has reached the server — this must print your static IP before you continue,
or the HTTPS certificate request will fail:

```bash
getent hosts dmvtransit.com
```

Then start it:

```bash
cd ~/dmvtransit && docker compose up -d --build
```

The first build takes a few minutes. Then watch the first data load (about 2 minutes;
press **Ctrl+C** to stop watching — the site keeps running):

```bash
docker compose logs -f scheduler
```

When you see `full refresh finished`, open **https://dmvtransit.com**. Final check:

```bash
docker compose exec app transit-mcp doctor
```

---

## Day-to-day

**Ship a change** — on your PC, commit and `git push`; then on the server:

```bash
bash ~/dmvtransit/deploy/update.sh
```

**See what's happening**

```bash
cd ~/dmvtransit && docker compose ps
```

```bash
docker compose logs --tail 100 app
```

**Change limits or settings** — edit `.env`, then apply:

```bash
cd ~/dmvtransit && docker compose up -d
```

**Refresh all data right now**

```bash
cd ~/dmvtransit && docker compose exec scheduler transit-mcp ingest all
```

**Add the CUE schedule** (CUE isn't listed in Transitland and blocks automated
downloads; every other agency's schedule is found through Transitland automatically).
Download CUE's `google_transit.zip` in your browser, then from your PC upload it with
the SSH key Lightsail gives you (**Account → SSH keys → Download**):

```bash
scp -i LightsailDefaultKey-us-east-1.pem google_transit.zip ubuntu@YOUR_STATIC_IP:~/dmvtransit/feeds/cue/
```

and load it on the server:

```bash
cd ~/dmvtransit && docker compose exec scheduler transit-mcp ingest gtfs --only cue
```

**Plug in your scheduling engine** — see [docs/EXTENDING.md](../docs/EXTENDING.md).
For a Node engine, rebuild with Node included:

```bash
cd ~/dmvtransit && docker compose build --build-arg WITH_NODE=true && docker compose up -d
```

## Keeping an eye on it

- **Uptime:** add a free external monitor (e.g. UptimeRobot) on
  `https://dmvtransit.com/api/health` — it returns `{"ok": true}` when healthy.
- **Server:** Lightsail → instance → **Metrics** → add alarms for CPU and failed
  status checks.
- **Spend:** watch usage in the Anthropic Console. If the site-wide daily limit is
  being hit, visitors see a polite "come back tomorrow" message instead of errors.
- **Backups:** automatic snapshots cover everything. All transit data can be
  re-downloaded anyway; the only things worth protecting are `.env` and the
  certificates (both inside the snapshot).

## Troubleshooting

| Symptom | Likely cause → fix |
| --- | --- |
| Browser certificate error right after launch | DNS wasn't pointing at the server yet, or port 443 is closed. Fix, then `docker compose restart caddy` and check `docker compose logs caddy`. |
| "The assistant isn't available right now" | Run `docker compose exec app transit-mcp doctor` — usually a missing/invalid key in `.env`. |
| 502 Bad Gateway | The app is starting or crashed: `docker compose logs --tail 50 app`. |
| Answers missing new schedules | `docker compose logs scheduler` — nightly refresh errors are listed per agency. |
| `permission denied` running docker | You didn't reconnect after setup. Close the terminal and connect again. |

## How it's protected

- Only ports 22, 80 and 443 are open; the app itself is reachable only through Caddy.
- HTTPS everywhere with HSTS; strict Content-Security-Policy; no framing.
- Per-visitor rate limits keyed on the real visitor address (forged
  `X-Forwarded-For` headers are discarded by Caddy), a site-wide daily cap, 1,000-
  character questions, 20-question conversations, bounded memory for conversations.
- Visitors see plain-language messages only; technical details go to the server logs.
- Secrets live only in `~/dmvtransit/.env` (`chmod 600`), never in the image or Git.
- Containers run as a non-root user; Ubuntu installs security updates automatically.
