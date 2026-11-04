# TSB-AF Dashboard API Setup

The dashboard is hosted separately from the Python bot. The Vercel frontend proxies /auth and /api requests to the bot API, keeping OAuth session cookies on the dashboard's own domain.

## Discord Developer Portal
Register this OAuth2 redirect URL:
https://YOUR-DASHBOARD.vercel.app/auth/callback

## Wispbyte environment variables
- DISCORD_CLIENT_ID
- DISCORD_CLIENT_SECRET
- DASHBOARD_API_URL = the deployed dashboard URL (same as DASHBOARD_URL)
- DASHBOARD_SESSION_SECRET = long random secret, at least 32 characters
- DASHBOARD_PORT = public port allocated to the bot service

Keep DISCORD_TOKEN, MONGODB_URI and MONGODB_DB. The Wispbyte bot service must expose DASHBOARD_PORT publicly over HTTPS.

## Vercel environment variables
- BOT_API_URL = the actual public HTTPS URL of the Wispbyte bot API

Redeploy the Vercel project after adding the variables. Do not expose bot credentials or database secrets as VITE_ variables.

Application panel submissions are saved in the existing MongoDB state. Panels support up to five modal questions, a separate panel channel, review channel and reviewer role. The bot restores published application buttons after restart.
