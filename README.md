# TSB-AF Moderation Bot (Python)

## Dashboard API additions

The bot can optionally host the API for the separate TSB-AF-Dashboard repository. The API uses the bot's existing MongoDB state and Discord OAuth2.

Add these Wispbyte environment variables:
- DISCORD_CLIENT_ID
- DISCORD_CLIENT_SECRET
- DASHBOARD_API_URL
- DASHBOARD_URL
- DASHBOARD_SESSION_SECRET (32+ random characters)
- DASHBOARD_PORT (the public port allocated to this service)

Register DASHBOARD_API_URL/auth/callback as an OAuth2 redirect URL in the Discord Developer Portal. Set the dashboard's Vercel build variable VITE_API_URL to DASHBOARD_API_URL.

Keep DISCORD_TOKEN, MONGODB_URI and MONGODB_DB configured as before. Never commit real secrets. If dashboard variables are absent, the Discord bot continues running and the dashboard API stays disabled.

Application panel submissions are stored in the existing MongoDB state and use the existing staff review buttons. Each panel supports up to five modal questions, per-panel review channels and an optional per-panel reviewer role.
