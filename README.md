# Minervini Trend Template Stock List by VCPScanner

A table-first GitHub Pages application for traders who want a current, ranked list of Minervini-style trend candidates.

The page provides:

- Authentic VCP Scanner branding and verified Stage 2 market breadth statistics.
- Current qualified-stock count and list freshness.
- Quick filter lenses (⚡ RS 90+ Leaders, 🎯 Within 5% High, 📦 Volume Expansion, ★ Watchlist).
- Search and filters for RS Rating, 52-week-high proximity, and volume activity.
- Interactive slide-over Inspector Drawer with **Two-Tier Stage 2 Diagnostic Checklist** and interactive chart verification links.
- Stock logos with deterministic fallback avatars.
- Local browser watchlist pinning (persisted in localStorage).
- Sorting across ticker, price, daily move, RS, moving-average distance, and volume.
- Direct VCPScanner interactive chart deep-links.
- One-click CSV export of the filtered candidate universe.

## Data architecture

The application runs entirely client-side and serves `current.json` from the
same GitHub Pages artifact as the HTML, JavaScript, and CSS. A scheduled GitHub
Action reads the database with a dedicated read-only credential, generates the
snapshot, validates it, and deploys the Pages artifact. It does not call R2 and
does not modify the VCPScanner backend or its pipeline.

- **Daily refresh**: The workflow runs at 8:17 PM and 10:17 PM America/New_York on weekdays. The second run is an idempotent retry for slow database refreshes.
- **Session-based freshness**: Every generated payload includes `session_date`, the completed market session represented by the data.
- **Stale-data guard**: The builder refuses to publish when `screening_metrics` has not caught up with the latest completed `price_daily` session.
- **Offline resilient**: A successful payload is cached in the browser. Offline visits show the cached session date; a first-time offline visit is explicitly labelled sample data.
- **Zero client credentials**: No API keys, database credentials, or proprietary configurations are shipped to the browser.

### GitHub Actions setup

Configure GitHub Pages to use **GitHub Actions** as its publishing source. The
workflow opens a short-lived SSH tunnel to the database server; PostgreSQL is
never exposed to GitHub's changing runner IP ranges. Add these repository
secrets:

`PUBLIC_SNAPSHOT_DATABASE_URL`

This is the `public_snapshot_reader` credential, pointed at the tunnel endpoint
(`127.0.0.1:15433` in the workflow). It has `SELECT` on `screening_metrics` and
`price_daily`, plus column-level `SELECT` only for `ticker`, `company_name`, and
`country` on `companies`, which the snapshot join needs.

`PUBLIC_SNAPSHOT_SSH_KEY`

The private Ed25519 key for the dedicated `snapshot-tunnel` OS account on the
database server. It must not be a root or pipeline-server key.

`PUBLIC_SNAPSHOT_SSH_KNOWN_HOSTS`

The pinned SSH host-key line for the database server. This prevents the Action
from trusting an unverified host during tunnel setup.

The workflow derives the standard Pages `current.json` URL automatically. If
you use a custom Pages domain, add the
repository variable `PUBLIC_SNAPSHOT_URL` with that URL; this lets retries
preserve the existing payload when the database session has not changed.

The workflow file is `.github/workflows/refresh-and-deploy.yml`. It can also be
started manually with **Run workflow** for a controlled refresh.

## Local preview

You can run the application locally using any standard static file server:

```bash
# Using Python
python -m http.server 8080

# Or using Node.js
npx serve .
```

Open `http://localhost:8080` in your browser. The table will load `current.json`
when the file exists in the served directory, otherwise it will show the
clearly labelled sample fallback.

## Methodology

This list contains US common stocks with at least $500 million market capitalization, price above the 50-day and 200-day moving averages, RS Rating of at least 70, and price within 25% of the 52-week high.

It is a candidate list. The 150-day moving-average alignment and rising 200-day average must still be verified on the chart before treating a result as a complete eight-rule Trend Template qualification.

This project is not affiliated with or endorsed by Mark Minervini and does not provide investment advice.

## License

MIT. See [LICENSE](LICENSE).
