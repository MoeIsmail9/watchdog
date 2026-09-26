# Vitool

A personal Vinted Germany watchlist with a local settings page and Telegram alerts.
Defaults: Ralph Lauren / Polo Ralph Lauren / Gant, M, brown or black, up to €20 before fees,
very good or new. Men's pullovers are the initial department; choose men, women, or both in Settings.
The brand selector contains Vinted Germany's 50 current popular pullover brands, combined into
49 unique choices because Ralph Lauren and Polo Ralph Lauren share one watch option.

## Run locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
cp .env.example .env
uv run vitool
```

Open http://localhost:8765. Start watching to enable scheduled checks, or Check now for one scan.
The initial state is paused. Settings and history persist in `data/vitool.sqlite3`.
Your computer must remain awake and connected. No account or payment information is required.
Only run one process per data directory; a process lock enforces this.

## Connect Telegram

1. Create a bot using `/newbot` with [@BotFather](https://t.me/BotFather).
2. Put its token in `TELEGRAM_BOT_TOKEN` in `.env`. Never commit or share that file.
3. Send `/start` to your new bot.
4. Stop Vitool with Ctrl+C, then run `uv run vitool --telegram-chats`.
5. Put your own private chat ID in `TELEGRAM_CHAT_ID` and restart Vitool.
6. Click Send test message in the settings page.

The bot only processes commands from the configured chat. `/help` lists commands, including
`/price 20`, `/size M`, `/colors brown,black`, `/brands Ralph Lauren,Gant`,
`/materials cotton,wool,cashmere`, `/necklines half_zip,v_neck`, `/interval 60`, `/pause`,
`/resume`, `/settings`, and `/status`. `/interval` is measured in seconds.
All settings can also be changed through the local page. Credentials load at startup.
Alerts include the listing link; Telegram may show a photo via its link preview.

## What to expect

- Public HTML pages are the current, unofficial data source. A successful read test is **not**
  permission from Vinted. [Vinted's terms](https://www.vinted.de/terms-and-conditions)
  restrict unauthorized automation. This source may change, stop working, or be blocked.
  There is no block-proof request rate. The official Pro API is for allowlisted sellers,
  not a general buyer search API: https://pro-docs.svc.vinted.com/.
- One combined catalog page per selected department, filtered by all selected brands (up to 10),
  colour, price, category and (for men) size, plus up to six previously unchecked candidate detail
  pages, alternating between brands per cycle. Requests are sequential and at least five seconds apart.
  Interval minimum: 10 seconds locally and approximately five minutes through GitHub Actions.
  A scan may take longer than the selected interval and scans never overlap.
  There is no deep pagination: busy searches can miss items. Deferred detail checks are reported.
- The first complete scan records a baseline without sending old listings to Telegram.
  Each brand scan is sorted `newest_first` and remembers the newest item as its cursor. The next scan
  processes only unseen item IDs above that cursor. This tracks uploads since the previous successful
  check; Vinted's public pages do not expose an exact upload timestamp. The first successful scan after
  changing search filters creates a fresh baseline, so existing items from a newly selected brand do
  not alert. Changing filters does not resend previously delivered or baseline items.
  The dashboard keeps saved finds, which may have sold.
- Colours come from seller-entered detail attributes. Size and condition are also seller-stated.
  Neckline matching uses multilingual text; unmentioned styles cannot be identified reliably.
  Multiple selected necklines use “any of these” matching; no selection accepts any neckline.
  Some explicit damage phrases are excluded;
  this is not a complete defect detector or authenticity check.
- AI is not connected in this version. The `matching.py` boundary is where an optional image/text
  reviewer can be added later, after basic matching, with cached decisions and a spending cap.
- HTTP 401/403 or a recognized challenge pauses the watcher until you resume it. HTTP 429 respects
  Retry-After; transient errors trigger exponential cooldowns. Cooldowns survive restarts and manual checks.
  No proxies, login-cookie harvesting, fingerprint spoofing or CAPTCHA bypass are included.
- Failed Telegram delivery is retried on later successful scans only while the item still appears
  in the results and was first seen within the past hour. A network timeout after Telegram accepts a
  message can cause a duplicate; Telegram does not provide an idempotency key for sendMessage.
- A connected bot receives the first scan failure and access-block notifications. The dashboard
  always shows the last successful check. No connection means no phone alerts.

## VPS

Use Docker Compose on a Linux VPS. No public web port is required:

```sh
cp .env.example .env
# Fill in Telegram credentials.
docker compose up -d --build
```

From your laptop, open an SSH tunnel:

```sh
ssh -L 8765:127.0.0.1:8765 user@your-vps
```

Then visit http://localhost:8765. Keep the dashboard bound to localhost: it has no login and is
intended for a single owner over a local connection or SSH tunnel. Preferences and listing history
live in a persistent Docker volume. To carry local history over, stop both instances and copy the
SQLite file into that volume with ownership `10001:10001` before starting the VPS instance.
Hosting and any future AI service can have separate costs. A VPS does not solve Vinted blocking.

## Free cloud deployment

The included deployment separates the sleeping dashboard from scheduled scans:

- Render serves the phone-friendly dashboard from `render.yaml`. Free services can sleep; opening
  the URL wakes the dashboard without interrupting scheduled scans.
- GitHub Actions runs `.github/workflows/scan.yml` approximately every five minutes.
- Turso stores settings, cursors, results and Telegram command offsets for both services.

Create these GitHub repository settings under **Settings → Secrets and variables → Actions**:

- Variable: `TURSO_DATABASE_URL`
- Secrets: `TURSO_AUTH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

In Render, create a Blueprint from this repository and provide the same four values when prompted,
plus a strong `VITOOL_DASHBOARD_PASSWORD`. The dashboard username is `watchdog`. Render automatically
redeploys the dashboard after commits to the linked branch; scheduled Actions use the latest commit.
Cloud scans have an effective minimum interval of about five minutes regardless of a lower setting.
GitHub schedules can start late, so this free deployment does not provide exact timing guarantees.

## Development

```sh
uv run pytest
uv run vitool --once
```

Tests use fake sources and Telegram clients; they do not hit Vinted or send real messages.
`--once` uses the real source and still obeys persisted block/cooldown state.
