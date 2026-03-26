# Fantasy Baseball ROTO Daily Manager

A Python app that acts as your daily fantasy baseball advisor for a **6×6 ROTO**
league on Yahoo. It pulls data from the Yahoo Fantasy API and the free MLB Stats
API, analyzes your roster against the waiver wire, and writes a clean Markdown
report to a local folder that syncs to your phone via Google Drive (or Dropbox,
OneDrive, etc.).

## What It Does

1. **Pulls today's MLB schedule** — sees which of your players are in action
   and who they're facing (including probable pitcher ERA).

2. **Compares your roster to free agents** — scores every available player by
   how much they'd help your weakest ROTO categories.

3. **Writes a daily report** — a single `.md` file with start/sit decisions,
   waiver pickups with drop candidates, and trade ideas, all explained in
   plain English for someone without deep fantasy experience.

## Your League Format (6×6 ROTO)

| Hitting                       | Pitching            |
|-------------------------------|---------------------|
| Runs (R)                      | Wins (W)            |
| Home Runs (HR)                | Saves (SV)          |
| RBI                           | Strikeouts (K)      |
| Stolen Bases (SB)             | Holds (HLD)         |
| On-Base Percentage (OBP)      | ERA                 |
| OPS (OBP + Slugging)          | WHIP                |

**Roster:** C, 1B, 2B, 3B, SS, 3×OF, 2×Util, 2×SP, 2×RP, 4×P, 4×BN, 3×IL

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Yahoo Fantasy API Credentials

1. Go to https://developer.yahoo.com/apps/
2. Create a new app → select **Fantasy Sports** as the API.
3. Set redirect URI to `oob` (out-of-band).
4. Note your **Client ID** and **Client Secret**.

### 3. Configure

```bash
cp config_example.yaml config.yaml
```

Edit `config.yaml`:
- Paste your Yahoo Client ID and Secret.
- Set your `league_id` (visible in the URL when you view your league on Yahoo).
- Set `output.directory` to a folder that syncs to Google Drive
  (e.g. `~/Google Drive/Fantasy Baseball`).

### 4. Run

```bash
python main.py             # Full run — writes report to your synced folder
python main.py --dry-run   # Preview in terminal, no file written
```

The first run will open a browser for Yahoo OAuth. Authorize, paste the code
back into the terminal. After that, the token is cached.

### 5. Automate (Optional)

Run it every morning with cron:

```bash
crontab -e
# Add:
0 8 * * * cd /path/to/fantasy_baseball_manager && python main.py >> cron.log 2>&1
```

Then just open the Markdown file on your phone each morning.

## Project Structure

```
fantasy_baseball_manager/
├── main.py              # Entry point — runs the 5-step daily workflow
├── config.yaml          # Your credentials + league settings (git-ignored)
├── config_example.yaml  # Template config
├── requirements.txt     # Python dependencies
├── yahoo_client.py      # Yahoo Fantasy Sports API
├── mlb_client.py        # MLB Stats API (free, no auth)
├── analyzer.py          # ROTO scoring engine + recommendations
├── report_writer.py     # Local Markdown file writer
├── models.py            # Data classes (Player, GameMatchup, etc.)
└── utils.py             # Logging, config loading, stat helpers
```

## How the Analysis Works

The analyzer scores players across your 12 ROTO categories, weighted by how
badly you need each one. Key strategies built in:

- **OBP/OPS league awareness**: Hitters with high walk rates (plate discipline)
  are valued more than free-swingers with comparable batting averages.
- **Holds league awareness**: Middle relievers with HLD upside are scored as
  real assets. Dual SV+HLD contributors are flagged as especially valuable.
- **Recency weighting**: Recent performance (last 14 days) is blended with
  season stats to surface hot streaks for daily action.
- **Beginner-friendly**: Recommendations include plain-English explanations
  of *why* each move helps, including ROTO strategy tips.

## Troubleshooting

- **Yahoo 401 errors**: Delete `.yahoo_token.json` and re-authorize.
- **"No games found"**: Normal on off-days or during the All-Star break.
- **Stats missing for a player**: The MLB API occasionally can't resolve
  names that differ between Yahoo and MLB (nicknames, accents). The player
  is skipped gracefully.
