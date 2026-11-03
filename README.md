# TSB-AF Moderation Bot — Python

Python rewrite of the TSB-AF Discord moderation, applications, clan registration and tryout-stage bot. Built with `discord.py`.

## Features
- `/blacklist`, `/unblacklist`, `/blcheck` with case history and optional blacklist role
- `/say` and `/embed-say`
- `/stage` restricted to configured tryout hosts; creates and assigns a stage role
- `/apply` DM questionnaire with staff Accept/Reject buttons
- `/clan-register` DM invite submission, owner verification, 30-member minimum and staff review
- `/main` one-time clan code redemption and announcement
- `/config` settings and `/config-show`
- MongoDB Atlas persistence for restarts and host changes

## Wispbyte setup
1. Create a **Python** server and connect this repository.
2. Install dependencies from `requirements.txt` (Wispbyte can run `pip install -r requirements.txt`).
3. Set the startup command to `python main.py`.
4. Add these environment variables in the host panel:
   - `DISCORD_TOKEN` — bot token
   - `DISCORD_CLIENT_ID` — Discord application ID (optional for runtime; useful for setup)
   - `DISCORD_GUILD_ID` — optional test server ID for instant guild slash-command sync; omit for global commands
   - `MONGODB_URI` — MongoDB Atlas connection string
   - `MONGODB_DB` — database name, defaults to `tsbaf`
5. Enable **Server Members Intent** and **Message Content Intent** in the Discord Developer Portal.
6. Invite the bot with `bot` and `applications.commands` scopes and permissions to manage roles, send messages, embed links, view channels and read message history. Put the bot role above the stage/blacklist roles it must manage.
7. Start the server. Slash commands sync automatically on startup.

## Commands
- `/blacklist user reason`
- `/unblacklist user`
- `/blcheck user`
- `/say message channel`
- `/embed-say title description color channel`
- `/stage user stage power rank`
- `/apply`
- `/clan-register`
- `/main code`
- `/config logs|applications|staff-role|blacklist-role|tryout-role|clan-register-logs|clan-news|questions`
- `/config-show`

## Persistence and security
All state is stored in MongoDB Atlas, not in GitHub or on the host disk. This keeps records across restarts and host changes as long as the same MongoDB database is used. Never commit your real Discord token or MongoDB URI; use host environment variables. The `.env` file is ignored by Git.
