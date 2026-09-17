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

The application runs entirely client-side and fetches verified market data snapshots directly from the VCP Scanner global CDN:

`https://assets.vcpscanner.com/public-screens/v1/minervini-trend-template/current.json`

- **Authoritative & Verified**: Snapshots are refreshed daily following the US market close.
- **Offline Resilient**: If the CDN snapshot is unreachable or when previewing offline, the application seamlessly falls back to a verified baseline snapshot (`sample-snapshot.json`).
- **Zero Client Credentials**: No API keys, database credentials, or proprietary configurations are required to run or host the application.

## Local preview

You can run the application locally using any standard static file server:

```bash
# Using Python
python -m http.server 8080

# Or using Node.js
npx serve .
```

Open `http://localhost:8080` in your browser. The table will load the latest verified market snapshot.

## Methodology

This list contains US common stocks with at least $500 million market capitalization, price above the 50-day and 200-day moving averages, RS Rating of at least 70, and price within 25% of the 52-week high.

It is a candidate list. The 150-day moving-average alignment and rising 200-day average must still be verified on the chart before treating a result as a complete eight-rule Trend Template qualification.

This project is not affiliated with or endorsed by Mark Minervini and does not provide investment advice.

## License

MIT. See [LICENSE](LICENSE).
