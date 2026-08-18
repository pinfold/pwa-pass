# OpenCommons PWA Capture (pwa-passv3)

A mobile-first Progressive Web App for capturing photo reports of street-level
incidents (encampments, graffiti, hazards, obstructions, etc.) with location
metadata, for later triage and processing by outreach/city teams.

The app is a single Flask service (served by Gunicorn) plus a static PWA
front end (`templates/index.html` + `static/sw.js` + `static/manifest.json`).
It stores uploaded photos in an S3-compatible object store (MinIO or AWS S3)
and stores the searchable metadata (GPS location, incident type, description,
timestamps, status) in a PostgreSQL database with the PostGIS extension.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
  1. [Create a shared Docker network](#1-create-a-shared-docker-network)
  2. [Run PostgreSQL + PostGIS](#2-run-postgresql--postgis)
  3. [Run MinIO (S3-compatible storage)](#3-run-minio-s3-compatible-storage)
  4. [Configure environment variables](#4-configure-environment-variables)
  5. [Build and run the app](#5-build-and-run-the-app)
- [Environment Variables Reference](#environment-variables-reference)
- [Running Locally Without Docker](#running-locally-without-docker)
- [NGINX Reverse Proxy Configuration](#nginx-reverse-proxy-configuration)
- [Project Structure](#project-structure)
- [Suggested Improvements](#suggested-improvements)

---

## How It Works

1. **Capture (client / browser):** The PWA asks for the user's email (stored
   in `localStorage`) and Geolocation permission, then shows a native-camera
   capture button. When the user takes a photo, the browser's file picker
   returns the image; the PWA also continuously tracks GPS position via
   `navigator.geolocation.watchPosition` so the most recent latitude/longitude/
   altitude/heading are attached to the report.

2. **Submission form:** After a photo is taken, a modal lets the user tag the
   incident (type checkboxes, free-text description, "obstruction"/"hazard"
   flags) before submitting.

3. **Upload (`POST /upload` in `app.py`):**
   - The image is opened with Pillow (`pillow-heif` registers HEIF/HEIC
     support for iPhone photos), auto-rotated using its EXIF orientation,
     downscaled to a max dimension of 800px, converted to RGB, and re-encoded
     as a JPEG (quality 70) — this keeps storage and bandwidth costs low.
   - The processed JPEG is uploaded to the configured S3-compatible bucket
     (MinIO in this guide, or AWS S3) under a timestamp-based key.
   - A new row is inserted into the Postgres incidents table containing the
     GPS coordinates (as a PostGIS `geography(Point, 4326)`), the S3 image
     key, a `metadata` JSONB blob (reporter email, incident type, description,
     camera heading, obstruction/hazard flags), and `first_reported`/
     `last_report` timestamps with `status = 'active'`.
   - If the DB insert fails after the image was already uploaded, the app
     deletes the orphaned S3 object to avoid leaking storage.

4. **Nearby incident lookup (`GET /check_nearby`):** Given a lat/lon, the
   backend runs a PostGIS `ST_DWithin` query (~76 meters / 250 ft radius)
   against incidents reported in the last 3 days, so the PWA can show the
   user recent nearby reports before they submit a duplicate.

5. **"Still There" confirmation (`POST /still_there/<id>`):** If a user
   spots an existing incident is still present, this bumps `last_report` to
   `NOW()` instead of creating a new row, keeping the "recently active"
   incident list accurate without duplicating data.

6. **Downstream processing:** Everything needed for later triage — the
   original photo (in the bucket) and its structured metadata + geolocation
   (in Postgres) — is now durably stored and queryable by any downstream
   tool/dashboard (e.g. `pwa-passv2`'s map view) via normal SQL/PostGIS
   queries and S3 GETs. This app itself does no further processing.

## Architecture

```
┌───────────────┐        ┌─────────────────────┐        ┌───────────────────────┐
│  Browser PWA  │  HTTP  │   Flask + Gunicorn   │        │   PostgreSQL+PostGIS  │
│ (camera, GPS, │───────▶│   (app.py)           │───────▶│   incident metadata   │
│  service      │        │                      │        │   & geolocation       │
│  worker)      │        │                      │        └───────────────────────┘
└───────────────┘        │                      │
                          │                      │        ┌───────────────────────┐
                          │                      │───────▶│   MinIO / S3 bucket   │
                          │                      │        │   original photos     │
                          └─────────────────────-┘        └───────────────────────┘
```

All three components run as separate Docker containers on a shared,
user-defined Docker network so they can address each other by container
name (`DB_HOST=pgvector-db`, `S3_ENDPOINT_IP=http://minio:9000`, etc.).

## Prerequisites

- Docker Engine 20.10+ and the Docker Compose plugin (`docker compose`).
- A server/host with a public HTTPS endpoint (or `localhost` for local dev) —
  browsers require a secure origin for Geolocation and Service Worker APIs.

## Quick Start

### 1. Create a shared Docker network

The app's `docker-compose.yml` expects an **externally-created** network
(so the app, database, and object storage containers can all resolve each
other by name, and so the database/storage can be reused by other services
such as a companion dashboard app):

```bash
docker network create postgres-data-net
```

### 2. Run PostgreSQL + PostGIS

The app queries geography columns (`ST_MakePoint`, `ST_DWithin`), so you need
the `postgis/postgis` image (plain `postgres` will not work).

```bash
docker run -d \
  --name pgvector-db \
  --network postgres-data-net \
  -e POSTGRES_USER=passuser \
  -e POSTGRES_PASSWORD=change_me \
  -e POSTGRES_DB=passdb \
  -v pgdata:/var/lib/postgresql/data \
  postgis/postgis:16-3.4
```

> The container name `pgvector-db` matches the default `DB_HOST` used in
> `docker-compose.yml`; rename both consistently if you prefer something else.

Create the incidents table using the provided [`schema.sql`](./schema.sql)
(matches the columns used throughout `app.py`):

```bash
docker exec -i pgvector-db psql -U passuser -d passdb < schema.sql
```

> Set `DB_TABLE` in your `.env` to match whatever table name you create here
> (defaults to `incident_reports_test` if unset). If you use a different
> name, update the `CREATE TABLE`/`CREATE INDEX` statements in `schema.sql`
> to match before running it.
>
> To have this schema applied automatically the first time a fresh
> `postgis/postgis` container starts, mount it into Postgres's init
> directory instead of running it manually:
> ```bash
> docker run -d \
>   --name pgvector-db \
>   --network postgres-data-net \
>   -e POSTGRES_USER=passuser \
>   -e POSTGRES_PASSWORD=change_me \
>   -e POSTGRES_DB=passdb \
>   -v pgdata:/var/lib/postgresql/data \
>   -v "$(pwd)/schema.sql:/docker-entrypoint-initdb.d/schema.sql" \
>   postgis/postgis:16-3.4
> ```

### 3. Run MinIO (S3-compatible storage)

```bash
docker run -d \
  --name minio \
  --network postgres-data-net \
  -e MINIO_ROOT_USER=minioadmin \
  -e MINIO_ROOT_PASSWORD=change_me_too \
  -p 9000:9000 \
  -p 9001:9001 \
  -v miniodata:/data \
  minio/minio server /data --console-address ":9001"
```

Then, using the MinIO Console at `http://<host>:9001` (or the `mc` CLI),
create a bucket (e.g. `incident-photos`) and make it readable so the PWA's
"nearby incidents" thumbnails can load — either mark the bucket/prefix
"public" (read-only) or serve images through a signed-URL/reverse-proxy
route instead (see [Suggested Improvements](#suggested-improvements)).

### 4. Configure environment variables

Copy the example file and fill in the values from steps 2–3:

```bash
cp .env.example .env
```

```dotenv
# S3 / MinIO Configuration
S3_ENDPOINT_IP=http://minio:9000        # internal container-to-container endpoint used by boto3
S3_ENDPOINT_URL=http://<host>:9000      # public/browser-facing endpoint used to build image URLs
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=change_me_too
S3_BUCKET_NAME=incident-photos
S3_REGION=us-east-1                     # MinIO ignores this but boto3 requires a value

# Database Configuration
DB_HOST=pgvector-db
DB_NAME=passdb
DB_TABLE=incident_reports_test
DB_USER=passuser
DB_PASSWORD=change_me
```

### 5. Build and run the app

`docker-compose.yml` already declares the `pwa-app` service, binds it to the
shared network, maps port `5102` on the host to port `5000` in the
container, and mounts `./my_local_logs` for log output:

```bash
docker compose up -d --build
```

> **Log volume permissions:** the container runs as a non-root user
> (`user: "1000:1000"` / `appuser`). If `./my_local_logs` doesn't already
> exist, Docker will create it owned by `root`, which causes a
> `PermissionError` crash loop on startup. Create it and fix ownership
> up front:
> ```bash
> mkdir -p ./my_local_logs && chown -R 1000:1000 ./my_local_logs
> ```

The app is now available at `http://localhost:5102`.

## Environment Variables Reference

| Variable          | Used for                                                      |
|--------------------|----------------------------------------------------------------|
| `S3_ENDPOINT_IP`   | boto3 endpoint URL used by the Flask app to reach the bucket    |
| `S3_ENDPOINT_URL`  | Public base URL used to build browser-facing image links       |
| `S3_ACCESS_KEY`    | MinIO/S3 access key                                             |
| `S3_SECRET_KEY`    | MinIO/S3 secret key                                             |
| `S3_BUCKET_NAME`   | Bucket that stores uploaded incident photos                    |
| `S3_REGION`        | Region string passed to boto3 (required by the SDK even for MinIO) |
| `DB_HOST`          | Postgres host/container name                                    |
| `DB_NAME`          | Postgres database name                                          |
| `DB_TABLE`         | Table name holding incident reports (see schema above)          |
| `DB_USER`          | Postgres user                                                    |
| `DB_PASSWORD`      | Postgres password                                                |

## Running Locally Without Docker

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)   # or use python-dotenv
python app.py                          # dev server on http://localhost:5000
```

Note: `app.py` writes rotating logs to `/app/logs/app.log` by default, which
only exists inside the container image — for local runs, either create a
local `logs/` directory and adjust the path, or see the logging suggestion
below.

## NGINX Reverse Proxy Configuration

Add the following block to your NGINX site configuration (usually in
`/etc/nginx/sites-available/default` or a domain-specific file).

**Crucial:** you must forward the `X-Forwarded-Proto` header. Without it,
Flask won't know the connection is secure (HTTPS), and Service Workers /
Geolocation often refuse to run on origins that don't look secure.

```nginx
server {
    listen 80;
    server_name pass.opencommons.org;

    # Redirect HTTP to HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name pass.opencommons.org;

    # SSL Certificate paths (example using Let's Encrypt)
    ssl_certificate /etc/letsencrypt/live/pass.opencommons.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pass.opencommons.org/privkey.pem;

    location / {
        proxy_pass http://localhost:5102; # matches the host port in docker-compose.yml

        # Standard Headers
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # CRITICAL for PWAs: tells Flask we are using HTTPS
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`app.py` already wraps the WSGI app with `ProxyFix` so it trusts these
forwarded headers:

```python
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
```

## Project Structure

```
pwa-passv3/
├── app.py                  # Flask routes, image processing, DB/S3 access
├── requirements.txt        # Python dependencies
├── Dockerfile              # App container image (non-root user, gunicorn entrypoint)
├── docker-compose.yml      # App service definition (expects external network + DB/MinIO)
├── .env.example            # Template for required environment variables
├── schema.sql              # PostGIS extension + incident_reports table + indexes
├── templates/
│   └── index.html          # PWA UI: capture flow, submission modal, nearby-incidents list
└── static/
    ├── manifest.json        # PWA manifest (icons, name, start URL)
    └── sw.js                 # Service worker (network-first fetch strategy)
```

## Suggested Improvements

A few changes worth considering to simplify onboarding and reduce
maintenance risk, roughly in priority order:

1. **Add a health-check endpoint** (e.g. `GET /healthz` that pings the DB
   pool) so `docker-compose.yml`/orchestrators can detect a broken DB or
   S3 connection distinctly from a normal 200 response, instead of
   relying on the process merely staying alive.
2. **Pin dependency versions in `requirements.txt`.** Currently
   unpinned (`Flask`, `gunicorn`, `Pillow`, etc.), which risks
   reproducibility drift between environments; pin to tested versions
   (or use a lockfile) before publishing publicly.

