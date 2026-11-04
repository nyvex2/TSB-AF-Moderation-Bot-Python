# TSB-AF Moderation Bot — Python

Python rewrite of the TSB-AF Discord moderation, applications, clan registration and tryout-stage bot. Built with `discord.py`.

## Features
- `/blacklist`, `/unblacklist`, `/blcheck` with cyan embeds, case history, automatic jail role, nickname tagging and channel allowlist
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
- `/config logs|applications|staff-role|blacklist-role|jail-setup|jail-channel|jail-channel-remove|tryout-role|clan-register-logs|clan-news|questions`
- `/config-show`

### Jail setup
1. Run `/config jail-setup` to create or refresh the `Blacklisted` role and restrict it from every server channel.
2. Run `/config jail-channel channel:#rules` for each channel jailed members may access (for example, rules, appeals, or a jail chat).
3. Use `/config jail-channel-remove` to revoke access to a channel.
4. `/config-show` displays the current jail role and allowed channels.

The bot saves each member's previous nickname and restores it when `/unblacklist` is used. The bot needs **Manage Roles**, **Manage Channels**, and **Manage Nicknames**, and its highest role must be above the members it moderates. Make sure the configured allowed channels are safe for jailed members to access.

## Persistence and security
All state is stored in MongoDB Atlas, not in GitHub or on the host disk. This keeps records across restarts and host changes as long as the same MongoDB database is used. Never commit your real Discord token or MongoDB URI; use host environment variables. The `.env` file is ignored by Git.


## Prefix moderation commands

The default text-command prefix is a comma (,). The bot also accepts command shortcuts:

- ,b @user reason — ban; ,ub user_id — unban
- ,k @user reason — kick
- ,to @user 10m reason — timeout; ,uto @user — remove timeout
- ,w @user reason — warn; ,ws @user — view warnings; ,cw @user — clear warnings
- ,p 25 — purge; ,sm 5 — set slowmode
- ,l / ,ul — lock / unlock the current channel
- ,bl @user reason / ,unbl @user / ,blcheck @user
- ,h — show the moderation help

Full command names also work, for example ,timeout and ,ban. Use ,setprefix ! if you want to change the prefix. Prefix moderation commands require the appropriate staff permissions, and the bot needs the corresponding Discord permissions.


## Clan moderation and recruitment

- `/config clan-blacklist-role role:@Role` configures the role assigned to every active mainer of a blacklisted clan. Staff use `/blacklist-clan clan:<name-or-server-id> reason:<reason>` and `/unblacklist-clan clan:<name-or-server-id>`. Blacklisted clans cannot be mained.
- `/clan-mainers clan:<clan>` is public: anyone can select any accepted clan from searchable autocomplete and view its active mainers. `/clan-remove-mainer member:@Member` remains restricted to that clan's owner; other clan memberships are preserved.
- `/config lfclan-channel channel:#channel` configures the recruitment channel. Members use `/lfclan kills:<number> device:<PC/Mobile/Console/Other>`; the bot reads their stage, power, and rank from their configured roles.
- All `/config` commands, including `/config-show`, require the Discord Administrator permission. Prefix `,setprefix` is Administrator-only. Moderation commands check the corresponding Discord permission (Ban Members, Kick Members, Moderate Members, Manage Messages, Manage Channels, Manage Nicknames, or Manage Roles) or the configured staff role/admin access.

Clan main records retain history; leader removals mark a membership inactive rather than deleting its historical record.


### Slash-command response timing
Commands that perform slower Discord API work defer their interaction response first, so Discord shows that the bot is processing instead of timing out before the operation completes.
