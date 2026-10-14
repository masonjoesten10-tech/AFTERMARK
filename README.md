# AFTERMARK — public beta package

AFTERMARK is a Solana market-trajectory research interface. It retrieves public market snapshots, stores timestamped observations, supports bounded cohort collection (up to 15 mints per request), and can retrieve pool-specific historical candles. Cross-token historical analogues remain disabled until a dataset and out-of-sample validation exist.

## Run locally on Windows
1. Install Python 3.10 or newer.
2. Extract this folder to a normal folder (not inside the ZIP preview).
3. Double-click `start_windows.bat`.
4. Open `http://127.0.0.1:8765/` in Chrome.

## Deploy publicly on Render
This package includes `render.yaml` and `Dockerfile`. Deployment creates a web service with a persistent disk for SQLite. A Render account is required; the included blueprint uses a paid Starter web service and a 1 GB persistent disk.

1. Create a GitHub repository and upload the contents of this folder (do not upload your local `aftermark.db`).
2. In Render, choose **New + → Blueprint**, connect the repository, and deploy the detected `render.yaml`.
3. Wait for the deploy to finish. Open the generated `https://...onrender.com/` URL.
4. Check `/api/health`; expect JSON with `ok: true` and `trading_enabled: false`.
5. Test SOL first, then test the cohort collector with verified mints.

### Database and existing local records
The public deployment starts with a fresh database on its persistent disk. Your local `aftermark.db` remains on your PC. Do not publish or commit that database. If you want to migrate local snapshots later, export and import only the snapshot records intentionally; do not expose the database file over HTTP.

## Important production notes
- The app has no wallet connection, private-key handling, transaction signing, or trading features.
- Public data sources can rate-limit or change. This is a beta and needs monitoring and stronger abuse controls before broad promotion.
- Cohort collection is limited to 15 unique mints per request, but this is not a full public-service rate limiter. Do not share the public URL widely until usage limits, caching, error monitoring, and a privacy/terms page are reviewed.
- Each snapshot is an observation at collection time; it does not reconstruct earlier states. Pool candles are pool-specific. Historical analogues are not enabled and no performance is guaranteed.
- The backend validates that the resolved token mint matches the requested mint. When the selected pair has the requested token on the quote side, it reorients the pair and omits market cap/FDV rather than misattributing those values.
- Review current provider API terms, quotas, and the hosting provider's pricing before public launch.
