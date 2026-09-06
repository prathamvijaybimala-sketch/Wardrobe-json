# 🧥 Wardrobe Pipeline

An automated AI-powered wardrobe cataloging pipeline that watches a Google Drive folder for new clothing photos, tags them with structured attributes using Google Gemini, assigns deterministic IDs, and commits everything to a GitHub repo as a machine-readable catalog.

## Architecture

```
Google Drive (PNG photos)
       │
       ▼
┌─────────────────┐
│  Pipeline (Railway)  │
│  FastAPI service     │
├─────────────────┤
│ 1. Fetch new images from Drive
│ 2. Tag with Gemini (structured JSON)
│ 3. Assign deterministic IDs (FS04, TR02, …)
│ 4. Rename & commit images + catalog to GitHub
└─────────────────┘
       │
       ▼
GitHub Repo (data store)
├── wardrobe.json        ← catalog
├── counters.json        ← ID counters
├── processed_ids.json   ← idempotency tracker
└── images/              ← renamed PNGs
       │
       ▼
Consumer App (future)
Fetches wardrobe.json + images via raw.githubusercontent.com
```

## Setup

### Prerequisites

- Python 3.11+
- A Google Cloud project with:
  - Google Drive API enabled
  - A service account with Drive access
- A Google AI Studio API key (free tier: [aistudio.google.com](https://aistudio.google.com))
- A GitHub personal access token (repo scope)

### 1. Clone & Install

```bash
git clone https://github.com/<your-user>/wardrobe-pipeline.git
cd wardrobe-pipeline
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Google Drive Service Account Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → IAM & Admin → Service Accounts
2. Create a service account (or use an existing one)
3. Create a JSON key for the service account → download it
4. Save it as `service_account.json` in this directory (it's gitignored)
5. **Share your Google Drive photo folder** with the service account's email address (found in the JSON key as `client_email`) — give it "Viewer" access

### 3. Environment Variables

Copy `.env.example` to `.env` and fill in every value:

```bash
cp .env.example .env
# Edit .env with your actual values
```

| Variable | Description |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Path to service account JSON file |
| `GOOGLE_SERVICE_ACCOUNT_B64` | (Alt) Base64-encoded service account JSON |
| `GOOGLE_DRIVE_FOLDER_ID` | Drive folder ID containing clothing photos |
| `GEMINI_API_KEY` | API key from Google AI Studio |
| `GEMINI_MODEL` | Gemini model name (default: `gemini-2.5-flash-lite`) |
| `GITHUB_TOKEN` | GitHub PAT with repo scope |
| `GITHUB_REPO` | Target repo as `owner/repo` |
| `GITHUB_BRANCH` | Branch to commit to (default: `main`) |
| `IMAGE_ASSETS_PATH` | Image directory in the repo (default: `images`) |

### 4. Run Locally

```bash
# Trigger a sync manually
python -c "from app.pipeline import run_pipeline; run_pipeline()"

# Or start the FastAPI server
uvicorn app.main:app --reload
# Then POST to http://localhost:8000/sync
```

## Railway Deployment

1. Create a new Railway project and connect this repo
2. Set all environment variables in Railway's dashboard (see table above)
3. Deploy — the app will auto-start via the Procfile

### Cron Job Setup (Railway)

To run `/sync` automatically (e.g. every 6 hours):

1. Go to your Railway project → **Settings** → **Cron Jobs**
2. Add a new cron job:
   - **Name**: `wardrobe-sync`
   - **Schedule**: `0 */6 * * *` (every 6 hours)
   - **URL**: `https://<your-app>.up.railway.app/sync`
   - **Method**: `POST`
3. Save — Railway will hit `/sync` on schedule

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check — returns `{"status": "ok"}` |
| `POST` | `/sync` | Trigger a full pipeline run |

## Consumer Contract

Any downstream app (outfit picker, etc.) can consume the catalog with zero dependencies:

### Fetch the catalog

```
GET https://raw.githubusercontent.com/<owner>/<repo>/<branch>/wardrobe.json
```

### Response shape

```json
{
  "items": [
    {
      "id": "FS04",
      "category": "full_sleeve_shirt",
      "color": "dark grey",
      "pattern": "none",
      "sleeve_type": "full",
      "fit": "regular",
      "material_guess": "cotton blend",
      "notes": "spread collar, single chest pocket",
      "image_url": "https://raw.githubusercontent.com/<owner>/<repo>/<branch>/images/FS04.png",
      "date_added": "2026-09-06"
    }
  ]
}
```

### Build image URLs

```
https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<IMAGE_ASSETS_PATH>/<id>.png
```

Example:
```
https://raw.githubusercontent.com/myuser/wardrobe-data/main/images/FS04.png
```

## ID Scheme

IDs are deterministic, generated from category prefix + zero-padded counter:

| Category | Prefix | Example |
|---|---|---|
| Full sleeve shirt | `FS` | `FS04` |
| Half sleeve shirt | `HS` | `HS02` |
| T-shirt | `TT` | `TT07` |
| Jeans | `JN` | `JN01` |
| Trousers | `TR` | `TR03` |
| Jacket | `JK` | `JK02` |
| Shorts | `SH` | `SH01` |
| Dress | `DR` | `DR01` |
| Skirt | `SK` | `SK01` |
| Hoodie/Sweatshirt | `HD` | `HD01` |
| Polo | `PO` | `PO01` |
| Tank top | `TK` | `TK01` |
| Blazer | `BZ` | `BZ01` |
| Other | `OT` | `OT01` |

## License

MIT
