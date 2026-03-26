# Fantasy Baseball ROTO Daily Manager

A Python application that acts as your daily fantasy baseball advisor. It connects to
the Yahoo Fantasy Sports API and MLB Stats API to analyze your roster, scout available
players, and write actionable daily suggestions to Google Drive.

## What It Does

1. **Daily Game Analysis** — Pulls today's MLB schedule and identifies which of your
   players are active, on the bench, or have favorable/unfavorable matchups.

2. **Roster vs. Waiver Wire** — Compares your active roster against the top available
   free agents in your league across all ROTO categories.

3. **Daily Report to Google Drive** — Writes a structured recommendations document
   covering waiver pickups, trade targets, and start/sit decisions, with plain-English
   explanations designed for a new player.

## ROTO Categories Supported

The app is pre-configured for the standard 5×5 ROTO format but can be customized:

| Hitting          | Pitching        |
|------------------|-----------------|
| Runs (R)         | Wins (W)        |
| Home Runs (HR)   | Strikeouts (K)  |
| RBI              | ERA             |
| Stolen Bases (SB)| WHIP            |
| Batting Avg (AVG)| Saves (SV)      |

## Prerequisites

- Python 3.10+
- A Yahoo Developer App (OAuth 2.0 credentials)
- A Google Cloud project with the Drive API enabled and a service account JSON key
- Internet access (for API calls)

## Setup

### 1. Install Dependencies

```bash
cd fantasy_baseball_manager
pip install -r requirements.txt
```

### 2. Yahoo Fantasy API Credentials

1. Go to https://developer.yahoo.com/apps/
2. Create a new app → select "Fantasy Sports" as the API.
3. Set the redirect URI to `oob` (out-of-band) for local scripts.
4. Note your **Client ID** and **Client Secret**.

### 3. Google Drive Service Account

1. Go to https://console.cloud.google.com/
2. Create a project (or use an existing one).
3. Enable the **Google Drive API**.
4. Create a **Service Account** and download the JSON key file.
5. Share a Google Drive folder with the service account email address
   (found in the JSON key file as `client_email`).

### 4. Configure the App

Copy the example config and fill in your values:

```bash
cp config_example.yaml config.yaml
```

Edit `config.yaml` with your credentials, league ID, and Drive folder ID.

### 5. First Run — Yahoo OAuth

The first time you run the app, it will open a browser for Yahoo OAuth authorization.
Follow the prompts to grant access, then paste the verification code back into the
terminal. The token is cached in `.yahoo_token.json` for future runs.

```bash
python main.py
```

### 6. Automate Daily Runs (Optional)

Add a cron job to run every morning (e.g., 8 AM ET):

```bash
crontab -e
# Add:
0 8 * * * cd /path/to/fantasy_baseball_manager && python main.py >> cron.log 2>&1
```

## Project Structure

```
fantasy_baseball_manager/
├── main.py                 # Entry point — orchestrates the daily workflow
├── config.yaml             # Your credentials and league settings (git-ignored)
├── config_example.yaml     # Template config
├── requirements.txt        # Python dependencies
│
├── yahoo_client.py         # Yahoo Fantasy Sports API integration
├── mlb_client.py           # MLB Stats API integration
├── analyzer.py             # ROTO analysis engine and recommendation logic
├── drive_writer.py         # Google Drive report writer
├── models.py               # Data classes for players, matchups, recommendations
└── utils.py                # Shared helpers (logging, date formatting, etc.)
```

## Customization

- **League format**: Edit the `roto_categories` section in `config.yaml` to match
  your league's scoring categories (e.g., OBP instead of AVG, QS instead of W).
- **Recommendation thresholds**: Adjust `min_ownership_pct`, `hot_streak_days`, and
  scoring weights in `config.yaml`.
- **Report format**: Modify `drive_writer.py` to change the output document layout.

## Troubleshooting

- **Yahoo 401 errors**: Delete `.yahoo_token.json` and re-authorize.
- **"No games found"**: The MLB schedule API returns empty on off-days / All-Star break.
- **Google Drive permission denied**: Make sure the service account email has Editor
  access to the target folder.
