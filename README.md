# Augie's Plant Tracker

A self-hosted, single-container web app for tracking houseplants. Upload a
photo, get the species identified by Claude (via your local n8n instance),
edit metadata, and watch a per-plant timeline build up over time.

Repo: https://github.com/AugustWasilowski/AugiePlantTracker

## What's in the box

- **FastAPI** app with server-rendered HTML + a tiny bit of vanilla JS for upload UX
- **SQLite** database — one file on disk, easy to back up
- **EXIF-aware** capture timestamps so the timeline reflects when the photo was *taken*, not when you uploaded it
- **n8n webhook** plug-in for plant identification (Claude vision by default; swap in PlantNet or whatever you like)
- **Mobile-friendly** upload page with `capture="environment"` so phones go straight to the camera

## Layout

```
plant-tracker/
├── app/
│   ├── main.py              # FastAPI app entry
│   ├── config.py            # env config
│   ├── database.py          # SQLAlchemy engine
│   ├── models.py            # Plant, Photo
│   ├── imaging.py           # EXIF + thumbnails
│   ├── n8n.py               # webhook client
│   ├── storage.py           # disk layout for photos
│   ├── routers/
│   │   ├── ui.py            # HTML pages
│   │   ├── plants.py        # JSON API
│   │   └── photos.py        # upload + identify
│   ├── templates/           # Jinja2
│   └── static/styles.css
├── n8n/plant-identify.workflow.json   # importable n8n workflow
├── data/                    # mounted volume — DB + photos live here
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

## First-time setup (on your LAN server)

These instructions are written for the Claude instance on the server to follow.

```bash
# 1. Clone the repo onto the server
git clone https://github.com/AugustWasilowski/AugiePlantTracker.git
cd AugiePlantTracker

# 2. Configure
cp .env.example .env
# edit .env:
#   - set N8N_IDENTIFY_WEBHOOK_URL (e.g. http://n8n:5678/webhook/plant-identify
#     if n8n is on the same Docker network, or http://<lan-ip>:5678/... otherwise)
#   - optionally set N8N_WEBHOOK_TOKEN to a random string
#   - leave DISABLE_IDENTIFY=1 for the very first boot if you want to verify
#     the UI before wiring n8n up

# 3. Build & start
docker compose up -d --build

# 4. Tail logs while you test
docker compose logs -f
```

App listens on `:8000`. Open `http://<server-lan-ip>:8000` in any browser
on your network. To use it from your phone, just bookmark that URL.

### Wiring up to an existing n8n container

If n8n is already running in another compose project on the same host, the
cleanest fix is to attach the plant tracker to n8n's network. Find n8n's
network name (`docker network ls`), then uncomment the `networks` block at
the bottom of `docker-compose.yml` and replace `n8n_default` with the real
name. Then `N8N_IDENTIFY_WEBHOOK_URL` can use the n8n service name as the
hostname (e.g. `http://n8n:5678/...`).

If n8n is on a different host on the LAN, just use its IP and exposed port.

## Importing the n8n workflow

1. In n8n, **Workflows → Import from File**, pick
   `n8n/plant-identify.workflow.json`.
2. The HTTP Request node references `{{$env.ANTHROPIC_API_KEY}}` — set that
   env var in n8n's environment (or replace the header with an n8n credential
   reference if you'd rather store it that way).
3. **Activate** the workflow. The webhook URL will be
   `<n8n-base>/webhook/plant-identify`. Paste that into `.env` as
   `N8N_IDENTIFY_WEBHOOK_URL`.
4. Bounce the plant tracker container (`docker compose restart`) so it picks
   up the new env.

If you'd rather use **PlantNet** or a different model, edit the workflow —
the only contract the app cares about is that the response is a JSON object
with any subset of:

```json
{
  "species": "...",
  "common_name": "...",
  "confidence": 0.0,
  "care_notes": "...",
  "growth": { "height_cm": 0, "leaf_count": 0 }
}
```

n8n often wraps a single response in a one-element array; the app handles
that automatically.

## Backups

Everything lives under `./data` on the host:
- `data/plants.db` — SQLite DB (all metadata)
- `data/photos/YYYY/MM/...` — original uploads
- `data/thumbs/YYYY/MM/...` — generated thumbnails (regeneratable)

A nightly `tar -czf` of `./data` is enough.

## Local dev (without Docker)

```bash
python -m venv .venv
. .venv/bin/activate    # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
DATA_DIR=./data DISABLE_IDENTIFY=1 uvicorn app.main:app --reload
```

Then http://localhost:8000.

## Customising

This is a personal project — feel free to hack it. A few suggested
extensions, in roughly increasing effort:

- **Watering reminders.** Add a `last_watered` column to `Plant` plus a
  homepage section for "needs water this week".
- **Per-photo journal.** The `Photo` model already has a `caption` field
  and a measurements API; surface those in the UI.
- **Growth chart.** Plot `measured_height_cm` over `captured_at` per plant
  with chart.js or recharts.
- **Watched folder.** Add a small background task in `main.py` lifespan
  that watches a Syncthing folder for new images and runs the same upload
  pipeline.
- **Auto-grouping.** Periodically re-run the identifier across all
  unassigned photos and offer to auto-group them by species.
