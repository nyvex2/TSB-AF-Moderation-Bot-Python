import os
import re
import asyncio
import time
import secrets
import string
from datetime import datetime, timezone, timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from pymongo import MongoClient
from dashboard_api import start_dashboard_api, ApplicationPanelView, MultiApplicationPanelView

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")
MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB = os.getenv("MONGODB_DB", "tsbaf")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")
if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is missing")

mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
collection = mongo[MONGODB_DB]["state"]
DEFAULT_DB: dict[str, Any] = {
    "nextCaseId": 1, "guilds": {}, "blacklists": {}, "blacklistHistory": [], "moderationCases": [], "warnings": {},
    "stages": [], "applications": {}, "clanRegistrations": {}, "clanMains": [], "tempBans": [], "modNotes": {}
}

def load_state() -> dict[str, Any]:
    mongo.admin.command("ping")
    saved = collection.find_one({"_id": "main"})
    state = dict(DEFAULT_DB)
    if saved and isinstance(saved.get("data"), dict):
        state.update(saved["data"])
    return state

def save_state() -> None:
    collection.replace_one({"_id": "main"}, {"_id": "main", "data": state}, upsert=True)

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def guild_config(guild_id: int) -> dict[str, Any]:
    cfg = state["guilds"].setdefault(str(guild_id), {"applicationQuestions": []})
    power_roles = cfg.setdefault("stagePowerRoles", {})
    renamed = False
    for old_label, new_label in (("weak", "low"), ("strong", "high"), ("stable", "mid")):
        if old_label in power_roles:
            power_roles.setdefault(new_label, power_roles[old_label])
            del power_roles[old_label]
            renamed = True
    if renamed:
        save_state()
    return cfg

def is_admin(interaction: discord.Interaction) -> bool:
    p = interaction.user.guild_permissions if isinstance(interaction.user, discord.Member) else None
    return bool(p and p.administrator)

def is_staff(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if is_admin(interaction):
        return True
    cfg = guild_config(interaction.guild.id)
    role_id = cfg.get("staffRoleId")
    if role_id:
        return any(r.id == int(role_id) for r in interaction.user.roles)
    return interaction.user.guild_permissions.manage_guild

def staff_button(interaction: discord.Interaction) -> bool:
    return is_staff(interaction)

def can_blacklist(interaction: discord.Interaction) -> bool:
    if is_staff(interaction):
        return True
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    role_id = guild_config(interaction.guild.id).get("blacklistPermsRoleId")
    return bool(role_id and any(role.id == int(role_id) for role in interaction.user.roles))

def prefix_blacklist_check(ctx) -> bool:
    if not ctx.guild or not isinstance(ctx.author, discord.Member):
        return False
    if ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.manage_messages:
        return True
    cfg = guild_config(ctx.guild.id)
    allowed_ids = {str(cfg.get("staffRoleId")), str(cfg.get("blacklistPermsRoleId"))}
    return any(str(role.id) in allowed_ids for role in ctx.author.roles)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
async def get_prefix(client, message):
    if message.guild is None:
        return commands.when_mentioned_or(",")(client, message)
    return commands.when_mentioned_or(guild_config(message.guild.id).get("prefix", ","))(client, message)

bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)
tree = bot.tree
state: dict[str, Any] = {}
BOT_STARTED_AT = time.monotonic()


def format_uptime(seconds: float) -> str:
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


@tree.command(name="ping", description="Show the bot's latency, uptime, and service status.")
async def ping(interaction: discord.Interaction):
    started = time.perf_counter()
    await interaction.response.defer(thinking=True)

    database_ok = False
    try:
        await asyncio.to_thread(mongo.admin.command, "ping")
        database_ok = True
    except Exception:
        database_ok = False

    response_ms = (time.perf_counter() - started) * 1000
    websocket_ms = bot.latency * 1000 if bot.latency is not None else None
    status_text = "All systems operational" if database_ok else "Degraded — database unavailable"
    status_color = discord.Color.green() if database_ok else discord.Color.orange()

    embed = discord.Embed(
        title="TSBAF Bot Status",
        description=f"**Status:** {status_text}",
        color=status_color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="WebSocket Latency", value=f"`{websocket_ms:.0f} ms`" if websocket_ms is not None else "`N/A`", inline=True)
    embed.add_field(name="Response Time", value=f"`{response_ms:.0f} ms`", inline=True)
    embed.add_field(name="Uptime", value=f"`{format_uptime(time.monotonic() - BOT_STARTED_AT)}`", inline=True)
    embed.add_field(name="Servers", value=f"`{len(bot.guilds):,}`", inline=True)
    embed.add_field(name="Database", value="`Connected`" if database_ok else "`Disconnected`", inline=True)
    embed.add_field(name="Bot", value=f"`Online` · {bot.user.mention if bot.user else 'Connected'}", inline=True)
    embed.set_footer(text="TSBAF Moderation • Live status")
    await interaction.edit_original_response(embed=embed)

async def reply_private(interaction: discord.Interaction, content: str) -> None:
    embed = discord.Embed(description=content, color=CYAN)
    embed.set_footer(text="TSBAF Moderation")
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=False)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=False)

async def send_log(guild: discord.Guild, embed: discord.Embed) -> None:
    channel_id = guild_config(guild.id).get("logChannelId")
    if channel_id:
        channel = guild.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
        if isinstance(channel, discord.TextChannel):
            await channel.send(embed=embed)

async def send_blacklist_log(guild: discord.Guild, embed: discord.Embed) -> bool:
    channel_id = guild_config(guild.id).get("blacklistLogChannelId")
    if not channel_id:
        return False
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False
    if not isinstance(channel, discord.TextChannel):
        return False
    await channel.send(embed=embed)
    return True

PURPLE = discord.Color.from_rgb(139, 92, 246)
CYAN = PURPLE  # Backwards-compatible alias: all standard bot embeds use the purple theme.

async def get_jail_role(guild: discord.Guild) -> discord.Role:
    cfg = guild_config(guild.id)
    role_id = cfg.get("blacklistRoleId")
    role = guild.get_role(int(role_id)) if role_id else None
    if role is None:
        role = discord.utils.get(guild.roles, name="Blacklisted")
    if role is None:
        role = await guild.create_role(name="Blacklisted", color=CYAN, reason="Automatic blacklist jail role")
    if str(role.id) != str(role_id):
        cfg["jailSetupComplete"] = False
    cfg["blacklistRoleId"] = str(role.id)
    return role

async def apply_jail_permissions(guild: discord.Guild, role: discord.Role, channels=None) -> int:
    """Apply jail overwrites only where they differ; optionally target specific channels."""
    allowed = {int(cid) for cid in guild_config(guild.id).get("jailChannelIds", [])}
    targets = list(channels) if channels is not None else list(guild.channels)
    updated = 0
    for channel in targets:
        current = channel.overwrites_for(role)
        desired = channel.overwrites_for(role)
        desired.view_channel = channel.id in allowed
        if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            if channel.id in allowed:
                desired.send_messages = True
                desired.read_message_history = True
            else:
                desired.send_messages = False
        elif isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            desired.connect = channel.id in allowed
        if desired != current:
            await channel.set_permissions(role, overwrite=desired, reason="Configure blacklist jail access")
            updated += 1
    return updated

async def ensure_jail_setup(guild: discord.Guild, role: discord.Role) -> None:
    cfg = guild_config(guild.id)
    if not cfg.get("jailSetupComplete"):
        await apply_jail_permissions(guild, role)
        cfg["jailSetupComplete"] = True

async def blacklist_member(guild: discord.Guild, member: discord.Member, reason: str, moderator: discord.abc.User, case_id: int) -> discord.Role:
    role = await get_jail_role(guild)
    await ensure_jail_setup(guild, role)
    old_nickname = member.nick
    bot_member = guild.me
    if bot_member is None:
        raise RuntimeError("Bot member is unavailable; cannot safely compare role hierarchy.")
    # Save and remove every normal role the bot is allowed to manage. Keep @everyone,
    # managed/integration roles, and roles above the bot's hierarchy untouched.
    removable_roles = [r for r in member.roles if not r.is_default() and not r.managed and r.id != role.id and r < bot_member.top_role]
    previous_role_ids = [str(r.id) for r in removable_roles]
    await member.add_roles(role, reason=f"Blacklist case #{case_id}")
    if removable_roles:
        await member.remove_roles(*removable_roles, reason=f"Blacklist case #{case_id}: remove previous roles")
    nickname_error = None
    try:
        await member.edit(nick="[ blacklisted ]", reason=f"Blacklist case #{case_id}")
    except discord.Forbidden:
        nickname_error = "Nickname could not be changed (check role hierarchy and Manage Nicknames)."
    key = f"{guild.id}:{member.id}"
    record = state["blacklists"].get(key)
    if record is not None:
        record["previousNickname"] = old_nickname
        record["previousRoleIds"] = previous_role_ids
        record["jailRoleId"] = str(role.id)
        if nickname_error:
            record["nicknameNote"] = nickname_error
    return role

async def unblacklist_member(guild: discord.Guild, member: discord.Member, record: dict[str, Any]) -> None:
    role_id = record.get("jailRoleId") or guild_config(guild.id).get("blacklistRoleId")
    role = guild.get_role(int(role_id)) if role_id else None
    if role and role in member.roles:
        await member.remove_roles(role, reason="User unblacklisted")
    bot_member = guild.me
    restore_roles = []
    if bot_member:
        for saved_id in record.get("previousRoleIds", []):
            saved_role = guild.get_role(int(saved_id))
            if saved_role and not saved_role.managed and saved_role < bot_member.top_role and saved_role not in member.roles:
                restore_roles.append(saved_role)
    if restore_roles:
        await member.add_roles(*restore_roles, reason="Restore roles after unblacklist")
    try:
        await member.edit(nick=record.get("previousNickname"), reason="User unblacklisted")
    except discord.Forbidden:
        pass

class ReviewView(discord.ui.View):
    def __init__(self, kind: str, record_id: str):
        super().__init__(timeout=None)
        self.kind = kind
        self.record_id = record_id
        for decision, label, style in (("accepted", "Accept", discord.ButtonStyle.success), ("rejected", "Reject", discord.ButtonStyle.danger)):
            button = discord.ui.Button(label=label, style=style, custom_id=f"review:{kind}:{decision}:{record_id}")
            button.callback = self.make_callback(decision)
            self.add_item(button)

    def make_callback(self, decision: str):
        async def callback(interaction: discord.Interaction):
            bucket = "applications" if self.kind == "app" else "clanRegistrations"
            record = state[bucket].get(self.record_id)
            if not staff_button(interaction):
                required_role = record.get("reviewRoleId") if record else None
                member = interaction.user
                has_panel_role = bool(required_role and isinstance(member, discord.Member) and any(role.id == int(required_role) for role in member.roles))
                if not has_panel_role:
                    await reply_private(interaction, "Only configured staff can review this request.")
                    return
            if not record or record.get("guildId") != str(interaction.guild_id):
                await reply_private(interaction, "Request not found.")
                return
            if record.get("status") != "pending":
                await reply_private(interaction, f"This request is already {record.get('status')}.")
                return
            record["status"] = decision
            record["reviewedBy"] = str(interaction.user.id)
            user = await bot.fetch_user(int(record["userId"]))
            role_status = ""
            if self.kind == "app" and decision == "accepted":
                accepted_role_id = record.get("acceptedRoleId")
                if accepted_role_id:
                    accepted_role = interaction.guild.get_role(int(accepted_role_id))
                    accepted_member = interaction.guild.get_member(int(record["userId"]))
                    if accepted_member is None:
                        try:
                            accepted_member = await interaction.guild.fetch_member(int(record["userId"]))
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            accepted_member = None
                    bot_member = interaction.guild.me
                    if not accepted_role or accepted_role.is_default() or accepted_role.managed:
                        role_status = "\n\n**Role notice:** The configured accepted role is missing or cannot be assigned."
                    elif not bot_member or accepted_role >= bot_member.top_role:
                        role_status = "\n\n**Role notice:** Move the bot's highest role above the accepted role."
                    elif accepted_member is None:
                        role_status = "\n\n**Role notice:** The applicant is not currently in this server, so the accepted role was not assigned."
                    elif accepted_role not in accepted_member.roles:
                        try:
                            await accepted_member.add_roles(accepted_role, reason=f"Application accepted: {record.get('panelName', 'Application')}")
                            record["acceptedRoleAssigned"] = True
                        except discord.Forbidden:
                            role_status = "\n\n**Role notice:** Discord denied the accepted role assignment. Check Manage Roles and role hierarchy."
                try:
                    await user.send(f"Your **{record.get('panelName', 'application')}** for **{interaction.guild.name}** was **accepted**.{role_status}")
                except (discord.Forbidden, discord.HTTPException):
                    role_status += "\n\n**Notice:** I could not DM the applicant with the decision."
            elif self.kind == "clan" and decision == "accepted":
                alphabet = string.ascii_uppercase + string.digits
                code = "".join(secrets.choice(alphabet) for _ in range(5))
                used = {str(r.get("clanCode", "")).upper() for r in state["clanRegistrations"].values()}
                while code in used:
                    code = "".join(secrets.choice(alphabet) for _ in range(5))
                record["clanCode"] = code
                record.pop("mainUserId", None)
                record.pop("mainedAt", None)

                leader_role_id = guild_config(interaction.guild.id).get("clanLeaderRoleId")
                if leader_role_id:
                    leader_role = interaction.guild.get_role(int(leader_role_id))
                    bot_member = interaction.guild.me
                    leader_member = interaction.guild.get_member(int(record["userId"]))
                    if leader_member is None:
                        try:
                            leader_member = await interaction.guild.fetch_member(int(record["userId"]))
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            leader_member = None
                    if not leader_role or leader_role.is_default() or leader_role.managed:
                        role_status = "\n\n**Role notice:** The configured clan leader role is missing or cannot be assigned."
                    elif not bot_member or leader_role >= bot_member.top_role:
                        role_status = "\n\n**Role notice:** Move the bot's highest role above the configured clan leader role."
                    elif leader_member is None:
                        role_status = "\n\n**Role notice:** The clan owner is not currently in this server, so the leader role was not assigned."
                    elif leader_role not in leader_member.roles:
                        try:
                            await leader_member.add_roles(leader_role, reason=f"Clan registration accepted: {record['clanName']}")
                            record["leaderRoleAssigned"] = True
                        except discord.Forbidden:
                            role_status = "\n\n**Role notice:** Discord denied the clan leader role assignment. Check bot permissions and role hierarchy."

                await user.send(f"Your clan **{record['clanName']}** has been registered.\n\nPermanent clan code: **{code}**\nThis 5-character code can be used by any member, any number of times, with no expiry. Use `/main code:{code}` in the main server.")
            else:
                label = "application" if self.kind == "app" else "clan registration"
                try:
                    await user.send(f"Your {label} for **{interaction.guild.name}** was **{decision}**.")
                except (discord.Forbidden, discord.HTTPException):
                    role_status += "\n\n**Notice:** I could not DM the applicant with the decision."
            save_state()
            await interaction.response.edit_message(content=f"Reviewed: **{decision}** by {interaction.user.mention}.{role_status}", view=None)
        return callback

async def wait_for_dm(user: discord.User, prompt: str, timeout: int = 300) -> discord.Message | None:
    dm = await user.create_dm()
    await dm.send(prompt)
    try:
        return await bot.wait_for("message", timeout=timeout, check=lambda m: m.author.id == user.id and isinstance(m.channel, discord.DMChannel))
    except asyncio.TimeoutError:
        await dm.send("This request timed out. You can run the command again later.")
        return None


# Prefix moderation commands
def prefix_command_check(permission: str):
    def predicate(ctx):
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return False
        if ctx.author.guild_permissions.administrator:
            return True
        role_id = guild_config(ctx.guild.id).get("staffRoleId")
        if role_id and any(r.id == int(role_id) for r in ctx.author.roles):
            return True
        return bool(getattr(ctx.author.guild_permissions, permission, False))
    return predicate

def prefix_staff_check(ctx):
    if not ctx.guild or not isinstance(ctx.author, discord.Member):
        return False
    if ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.manage_messages:
        return True
    role_id = guild_config(ctx.guild.id).get("staffRoleId")
    return bool(role_id and any(r.id == int(role_id) for r in ctx.author.roles))

def parse_duration(value):
    m = re.fullmatch(r"(\d{1,4})(s|m|h|d|w)", value.lower())
    if not m:
        return None
    seconds = int(m.group(1)) * {"s":1,"m":60,"h":3600,"d":86400,"w":604800}[m.group(2)]
    return timedelta(seconds=seconds) if 1 <= seconds <= 2419200 else None

async def prefix_case(ctx, action, target, reason):
    case_id = state["nextCaseId"]
    state["nextCaseId"] += 1
    state.setdefault("moderationCases", []).append({
        "caseId":case_id,"guildId":str(ctx.guild.id),"userId":str(target.id),
        "action":action,"reason":reason[:500],"moderatorId":str(ctx.author.id),"createdAt":now()
    })
    save_state()
    embed = discord.Embed(title=f"Moderation: {action.title()}",
        description=f"User: {target.mention}\nModerator: {ctx.author.mention}\nReason: {reason[:1000]}",
        color=CYAN, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Case #{case_id}")
    await send_log(ctx.guild, embed)
    return case_id

async def prefix_dm(user, message):
    try:
        await user.send(message)
    except (discord.Forbidden, discord.HTTPException):
        pass

@bot.command(name="help", aliases=["modhelp", "moderation", "h"])
async def modhelp(ctx):
    p = guild_config(ctx.guild.id).get("prefix", ",") if ctx.guild else ","
    embed = discord.Embed(
        title="TSBAF | Moderation Command Center",
        description=("A quick reference for authorized moderation and server-management commands.\nSlash commands are also available for the core moderation actions.\n"
                     f"**Server prefix:** `{p}`  â¢  **Need help?** Contact the server's moderation team."),
        color=CYAN,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Member moderation", value=(
        f"`{p}ban @member [reason]` â Permanently ban a member\n"
        f"`{p}unban user_id [reason]` â Revoke a ban\n"
        f"`{p}kick @member [reason]` â Remove a member\n"
        f"`{p}timeout @member 10m [reason]` â Apply a timeout\n"
        f"`{p}untimeout @member [reason]` â Remove a timeout\n"
        f"`{p}warn @member [reason]` â Issue a warning\n"
        f"`{p}softban @member [reason]` â Ban, clear recent messages, then unban\n"
        f"`{p}tempban @member 7d [reason]` â Apply a timed ban\n"
        f"`{p}nick @member nickname` â Update a member nickname\n"
        f"`{p}roleadd @member @role` / `{p}roleremove @member @role` â Manage member roles"), inline=False)
    embed.add_field(name="Cases & blacklist", value=(
        f"`{p}warnings @member` â Review warning history\n"
        f"`{p}clearwarnings @member` â Clear warning history\n"
        f"`{p}bl @member [reason]` â Add to the server blacklist\n"
        f"`{p}unbl @member` â Remove from the blacklist\n"
        f"`{p}blcheck @member` â Check blacklist status"), inline=False)
    embed.add_field(name="Channel management", value=(
        f"`{p}purge amount` â Delete recent messages\n"
        f"`{p}purgeuser @member amount` â Delete a member's recent messages\n"
        f"`{p}purgebots amount` / `{p}purgelinks amount` / `{p}purgefiles amount` â Filtered cleanup\n"
        f"`{p}slowmode seconds` â Configure channel slowmode\n"
        f"`{p}lock [#channel]` / `{p}unlock [#channel]` â Manage channel access\n"
        f"`{p}voicekick @member` / `{p}voicemute @member` / `{p}voiceunmute @member` â Voice moderation"), inline=False)
    embed.add_field(name="Bot settings", value=(
        f"`{p}prefix` â View the current prefix\n"
        f"`{p}setprefix new_prefix` â Update the prefix (Manage Server)\n"
        f"`{p}case case_id` â View a moderation case\n"
        f"`{p}history @member` â View a member's case history\n"
        f"`{p}note @member text` / `{p}notes @member` â Manage staff-only notes"), inline=False)
    embed.set_footer(text="TSBAF Moderation â¢ Actions are recorded in the moderation log")
    await ctx.send(embed=embed)

async def send_action_confirmation(ctx, action, target, reason, case_id, details=None):
    embed = discord.Embed(
        title=f"Moderation Action â¢ {action}",
        description="The action was completed successfully and recorded in the moderation system.",
        color=CYAN,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Member", value=f"{target.mention}\n`{target}`", inline=True)
    embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
    embed.add_field(name="Case ID", value=f"`#{case_id}`", inline=True)
    if details:
        embed.add_field(name="Duration", value=details, inline=True)
    embed.add_field(name="Reason", value=reason[:1024], inline=False)
    embed.set_footer(text="TSBAF Moderation â¢ Case recorded")
    await ctx.send(embed=embed)

@bot.command(name="prefix")
async def show_prefix(ctx):
    await ctx.send(embed=discord.Embed(title="Server Prefix", description=f"Current prefix: `{guild_config(ctx.guild.id).get('prefix', ',') if ctx.guild else ','}`", color=CYAN))

@bot.command(name="setprefix")
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def set_prefix(ctx, prefix: str):
    if not 1 <= len(prefix) <= 5 or any(c.isspace() for c in prefix):
        return await ctx.send("Prefix must be 1â5 non-space characters.")
    guild_config(ctx.guild.id)["prefix"] = prefix
    save_state()
    await ctx.send(embed=discord.Embed(title="Server Prefix Updated", description=f"The command prefix is now `{prefix}`.", color=CYAN))

@bot.hybrid_command(name="ban", aliases=["b"])
@commands.guild_only()
@commands.check(prefix_command_check("ban_members"))
async def prefix_ban(ctx, member: discord.Member, *, reason="No reason provided"):
    await prefix_dm(member, f"You were banned from {ctx.guild.name}. Reason: {reason}")
    await ctx.guild.ban(member, reason=f"{ctx.author}: {reason}", delete_message_seconds=0)
    case = await prefix_case(ctx, "ban", member, reason)
    await send_action_confirmation(ctx, "Ban", member, reason, case)

@bot.hybrid_command(name="unban", aliases=["ub"])
@commands.guild_only()
@commands.check(prefix_command_check("ban_members"))
async def prefix_unban(ctx, user_id: int, *, reason="No reason provided"):
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
    except discord.NotFound:
        return await ctx.send("That user is not banned or the ID is invalid.")
    case = await prefix_case(ctx, "unban", user, reason)
    await send_action_confirmation(ctx, "Unban", user, reason, case)

@bot.hybrid_command(name="kick", aliases=["k"])
@commands.guild_only()
@commands.check(prefix_command_check("kick_members"))
async def prefix_kick(ctx, member: discord.Member, *, reason="No reason provided"):
    await prefix_dm(member, f"You were kicked from {ctx.guild.name}. Reason: {reason}")
    await ctx.guild.kick(member, reason=f"{ctx.author}: {reason}")
    case = await prefix_case(ctx, "kick", member, reason)
    await send_action_confirmation(ctx, "Kick", member, reason, case)

@bot.hybrid_command(name="timeout", aliases=["mute", "to"])
@commands.guild_only()
@commands.check(prefix_command_check("moderate_members"))
async def prefix_timeout(ctx, member: discord.Member, duration: str, *, reason="No reason provided"):
    delta = parse_duration(duration)
    if delta is None:
        return await ctx.send("Use 10m, 2h, 1d, etc. Maximum is 28 days.")
    await member.timeout(delta, reason=f"{ctx.author}: {reason}")
    await prefix_dm(member, f"You were timed out in {ctx.guild.name} for {duration}. Reason: {reason}")
    case = await prefix_case(ctx, "timeout", member, f"{duration} â {reason}")
    await send_action_confirmation(ctx, "Timeout", member, reason, case, details=duration)

@bot.hybrid_command(name="untimeout", aliases=["unmute", "uto"])
@commands.guild_only()
@commands.check(prefix_command_check("moderate_members"))
async def prefix_untimeout(ctx, member: discord.Member, *, reason="No reason provided"):
    await member.timeout(None, reason=f"{ctx.author}: {reason}")
    case = await prefix_case(ctx, "untimeout", member, reason)
    await send_action_confirmation(ctx, "Timeout Removed", member, reason, case)

@bot.hybrid_command(name="warn", aliases=["w"])
@commands.guild_only()
@commands.check(prefix_command_check("moderate_members"))
async def prefix_warn(ctx, member: discord.Member, *, reason="No reason provided"):
    key = f"{ctx.guild.id}:{member.id}"
    rows = state.setdefault("warnings", {}).setdefault(key, [])
    rows.append({"reason":reason[:500],"moderatorId":str(ctx.author.id),"createdAt":now()})
    save_state()
    case = await prefix_case(ctx, "warn", member, reason)
    await prefix_dm(member, f"You received a warning in {ctx.guild.name}. Reason: {reason}")
    await send_action_confirmation(ctx, f"Warning Issued â¢ {len(rows)} Total", member, reason, case)

@bot.hybrid_command(name="warnings", aliases=["infractions", "ws"])
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_warnings(ctx, member: discord.Member):
    rows = state.setdefault("warnings", {}).get(f"{ctx.guild.id}:{member.id}", [])
    if not rows:
        return await ctx.send(f"{member.mention} has no warnings.")
    lines = [f"{i}. {r['reason']} â <@{r['moderatorId']}>" for i,r in enumerate(rows[-15:],1)]
    await ctx.send(embed=discord.Embed(title=f"Warnings for {member}",description="\n".join(lines)[:4000], color=CYAN))

@bot.hybrid_command(name="clearwarnings", aliases=["cw"])
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_clearwarnings(ctx, member: discord.Member):
    key = f"{ctx.guild.id}:{member.id}"
    count = len(state.setdefault("warnings", {}).get(key, []))
    state["warnings"].pop(key, None)
    save_state()
    await prefix_case(ctx, "clearwarnings", member, f"Cleared {count} warning(s)")
    await ctx.send(f"Cleared {count} warning(s) for {member.mention}.")

@bot.hybrid_command(name="purge", aliases=["clear", "p"])
@commands.guild_only()
@commands.check(prefix_command_check("manage_messages"))
async def prefix_purge(ctx, amount: int):
    if not 1 <= amount <= 100:
        return await ctx.send("Choose an amount from 1 to 100.")
    if isinstance(ctx.channel, discord.TextChannel):
        deleted = await ctx.channel.purge(limit=amount+1)
        msg = await ctx.send(f"Deleted {max(0,len(deleted)-1)} message(s).")
        await msg.delete(delay=5)

@bot.hybrid_command(name="slowmode", aliases=["sm"])
@commands.guild_only()
@commands.check(prefix_command_check("manage_channels"))
async def prefix_slowmode(ctx, seconds: int):
    if not isinstance(ctx.channel, discord.TextChannel):
        return
    if not 0 <= seconds <= 21600:
        return await ctx.send("Slowmode must be between 0 and 21600 seconds.")
    await ctx.channel.edit(slowmode_delay=seconds, reason=f"Set by {ctx.author}")
    await ctx.send(f"Slowmode set to {seconds} second(s).")

@bot.hybrid_command(name="lock", aliases=["l"])
@commands.guild_only()
@commands.check(prefix_command_check("manage_channels"))
async def prefix_lock(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    if not isinstance(channel, discord.TextChannel):
        return await ctx.send("Choose a text channel.")
    ow = channel.overwrites_for(ctx.guild.default_role)
    ow.send_messages = False
    await channel.set_permissions(ctx.guild.default_role, overwrite=ow, reason=f"Locked by {ctx.author}")
    await ctx.send(f"Locked {channel.mention}.")

@bot.hybrid_command(name="unlock", aliases=["ul"])
@commands.guild_only()
@commands.check(prefix_command_check("manage_channels"))
async def prefix_unlock(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    if not isinstance(channel, discord.TextChannel):
        return await ctx.send("Choose a text channel.")
    ow = channel.overwrites_for(ctx.guild.default_role)
    ow.send_messages = None
    await channel.set_permissions(ctx.guild.default_role, overwrite=ow, reason=f"Unlocked by {ctx.author}")
    await ctx.send(f"Unlocked {channel.mention}.")

@bot.hybrid_command(name="softban", description="Ban a member and immediately unban them to clear recent messages.")
@commands.guild_only()
@commands.check(prefix_command_check("ban_members"))
async def softban(ctx, member: discord.Member, *, reason="No reason provided"):
    if member == ctx.author or member == ctx.guild.owner or member.top_role >= ctx.author.top_role:
        return await ctx.send("This member cannot be softbanned due to role hierarchy or target restrictions.")
    await prefix_dm(member, f"You were softbanned from {ctx.guild.name}. Reason: {reason}")
    await ctx.guild.ban(member, reason=f"Softban by {ctx.author}: {reason}", delete_message_seconds=86400)
    await ctx.guild.unban(discord.Object(id=member.id), reason=f"Softban completed by {ctx.author}")
    case = await prefix_case(ctx, "softban", member, reason)
    await send_action_confirmation(ctx, "Softban", member, reason, case, details="1 day of messages removed")

@bot.hybrid_command(name="tempban", description="Temporarily ban a member for a specified duration.")
@commands.guild_only()
@commands.check(prefix_command_check("ban_members"))
async def tempban(ctx, member: discord.Member, duration: str, *, reason="No reason provided"):
    delta = parse_duration(duration)
    if delta is None:
        return await ctx.send("Use a duration such as `30m`, `12h`, or `7d` (maximum 28 days).")
    if member == ctx.author or member == ctx.guild.owner or member.top_role >= ctx.author.top_role:
        return await ctx.send("This member cannot be banned due to role hierarchy or target restrictions.")
    await prefix_dm(member, f"You were temporarily banned from {ctx.guild.name} for {duration}. Reason: {reason}")
    await ctx.guild.ban(member, reason=f"Temporary ban by {ctx.author}: {reason}", delete_message_seconds=0)
    case = await prefix_case(ctx, "tempban", member, f"Duration: {duration} â¢ {reason}")
    state.setdefault("tempBans", []).append({"guildId":str(ctx.guild.id),"userId":str(member.id),"unbanAt":(datetime.now(timezone.utc)+delta).isoformat(),"caseId":case})
    save_state()
    await send_action_confirmation(ctx, "Temporary Ban", member, reason, case, details=duration)

@bot.hybrid_command(name="nick", description="Change a member's server nickname.")
@commands.guild_only()
@commands.check(prefix_command_check("manage_nicknames"))
async def nick(ctx, member: discord.Member, *, nickname: str = None):
    old = member.display_name
    await member.edit(nick=nickname, reason=f"Nickname updated by {ctx.author}")
    case = await prefix_case(ctx, "nickname", member, f"{old} â {nickname or 'Reset to username'}")
    await send_action_confirmation(ctx, "Nickname Updated", member, f"{old} â {nickname or 'Reset to username'}", case)

@bot.hybrid_command(name="roleadd", description="Add a role to a member.")
@commands.guild_only()
@commands.check(prefix_command_check("manage_roles"))
async def roleadd(ctx, member: discord.Member, role: discord.Role):
    if role.is_default() or role.managed or role >= ctx.guild.me.top_role or (not ctx.author.guild_permissions.administrator and role >= ctx.author.top_role):
        return await ctx.send("That role cannot be assigned due to staff or bot role hierarchy.")
    await member.add_roles(role, reason=f"Role assigned by {ctx.author}")
    case = await prefix_case(ctx, "role-add", member, f"Added role {role.name} ({role.id})")
    await send_action_confirmation(ctx, "Role Added", member, f"Added {role.mention}", case)

@bot.hybrid_command(name="roleremove", description="Remove a role from a member.")
@commands.guild_only()
@commands.check(prefix_command_check("manage_roles"))
async def roleremove(ctx, member: discord.Member, role: discord.Role):
    if role.is_default() or role.managed or role >= ctx.guild.me.top_role or (not ctx.author.guild_permissions.administrator and role >= ctx.author.top_role):
        return await ctx.send("That role cannot be removed due to staff or bot role hierarchy.")
    await member.remove_roles(role, reason=f"Role removed by {ctx.author}")
    case = await prefix_case(ctx, "role-remove", member, f"Removed role {role.name} ({role.id})")
    await send_action_confirmation(ctx, "Role Removed", member, f"Removed {role.mention}", case)

async def purge_matching(ctx, amount: int, predicate, action: str, target=None):
    if not 1 <= amount <= 100:
        return await ctx.send("Choose an amount from 1 to 100.")
    if not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
        return await ctx.send("This command can only be used in a text channel or thread.")
    matched = 0
    def limited_check(message):
        nonlocal matched
        if matched >= amount or not predicate(message):
            return False
        matched += 1
        return True
    deleted = await ctx.channel.purge(limit=1000, check=limited_check, bulk=True)
    case = await prefix_case(ctx, action, target or ctx.author, f"Deleted {len(deleted)} messages in {ctx.channel.mention}")
    msg = await ctx.send(embed=discord.Embed(title="Message Cleanup Complete", description=f"**Deleted:** {len(deleted)}\n**Channel:** {ctx.channel.mention}\n**Case:** `#{case}`", color=CYAN))
    await msg.delete(delay=8)

@bot.hybrid_command(name="purgeuser", description="Delete recent messages from one member in this channel.")
@commands.guild_only()
@commands.check(prefix_command_check("manage_messages"))
async def purgeuser(ctx, member: discord.Member, amount: int):
    if not 1 <= amount <= 100: return await ctx.send("Choose an amount from 1 to 100.")
    await purge_matching(ctx, amount, lambda m: m.author.id == member.id, "purge-user", target=member)

@bot.hybrid_command(name="purgebots", description="Delete recent messages sent by bots in this channel.")
@commands.guild_only()
@commands.check(prefix_command_check("manage_messages"))
async def purgebots(ctx, amount: int):
    if not 1 <= amount <= 100: return await ctx.send("Choose an amount from 1 to 100.")
    await purge_matching(ctx, amount, lambda m: m.author.bot, "purge-bots")

@bot.hybrid_command(name="purgelinks", description="Delete recent messages containing links.")
@commands.guild_only()
@commands.check(prefix_command_check("manage_messages"))
async def purgelinks(ctx, amount: int):
    if not 1 <= amount <= 100: return await ctx.send("Choose an amount from 1 to 100.")
    await purge_matching(ctx, amount, lambda m: bool(re.search(r"https?://|discord\.gg/", m.content, re.I)), "purge-links")

@bot.hybrid_command(name="purgefiles", description="Delete recent messages containing file attachments.")
@commands.guild_only()
@commands.check(prefix_command_check("manage_messages"))
async def purgefiles(ctx, amount: int):
    if not 1 <= amount <= 100: return await ctx.send("Choose an amount from 1 to 100.")
    await purge_matching(ctx, amount, lambda m: bool(m.attachments), "purge-files")

@bot.hybrid_command(name="case", description="Look up a moderation case by its case ID.")
@commands.guild_only()
@commands.check(prefix_command_check("manage_messages"))
async def case_lookup(ctx, case_id: int):
    record = next((r for r in reversed(state.get("moderationCases", [])) if int(r.get("caseId", 0)) == case_id and str(r.get("guildId")) == str(ctx.guild.id)), None)
    if not record: return await ctx.send("No moderation case with that ID was found in this server.")
    embed = discord.Embed(title=f"Moderation Case #{case_id}", description=f"**Action:** {record.get('action','Unknown').title()}\n**Member:** <@{record.get('userId')}>\n**Moderator:** <@{record.get('moderatorId')}>\n**Reason:** {record.get('reason','No reason recorded')}\n**Created:** {record.get('createdAt','Unknown')}", color=CYAN)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="history", description="View recent moderation cases for a member.")
@commands.guild_only()
@commands.check(prefix_staff_check)
async def member_history(ctx, member: discord.User):
    rows = [r for r in state.get("moderationCases", []) if str(r.get("guildId")) == str(ctx.guild.id) and str(r.get("userId")) == str(member.id)][-10:]
    if not rows: return await ctx.send(f"No moderation history found for {member.mention}.")
    lines = [f"`#{r['caseId']}` **{r['action'].title()}** â¢ {r.get('reason','No reason')[:120]}" for r in reversed(rows)]
    await ctx.send(embed=discord.Embed(title=f"Moderation History â¢ {member}", description="\n".join(lines), color=CYAN))

@bot.hybrid_command(name="note", description="Add a private staff note to a member's record.")
@commands.guild_only()
@commands.check(prefix_staff_check)
async def mod_note(ctx, member: discord.User, *, note: str):
    key = f"{ctx.guild.id}:{member.id}"
    rows = state.setdefault("modNotes", {}).setdefault(key, [])
    rows.append({"note":note[:1000],"moderatorId":str(ctx.author.id),"createdAt":now()})
    save_state()
    await ctx.send(embed=discord.Embed(title="Staff Note Added", description=f"A private note was saved to {member.mention}'s moderation record.\n**Total notes:** {len(rows)}", color=CYAN))

@bot.hybrid_command(name="notes", description="View staff-only notes on a member.")
@commands.guild_only()
@commands.check(prefix_staff_check)
async def mod_notes(ctx, member: discord.User):
    rows = state.get("modNotes", {}).get(f"{ctx.guild.id}:{member.id}", [])[-10:]
    if not rows: return await ctx.send(f"No staff notes are stored for {member.mention}.")
    lines = [f"**{i}.** {r['note']}\n<text>By <@{r['moderatorId']}> â¢ {r['createdAt']}</text>" for i,r in enumerate(rows,1)]
    await ctx.send(embed=discord.Embed(title=f"Staff Notes â¢ {member}", description="\n\n".join(lines)[:4000], color=CYAN))

@bot.hybrid_command(name="voicekick", description="Disconnect a member from their current voice channel.")
@commands.guild_only()
@commands.check(prefix_command_check("kick_members"))
async def voicekick(ctx, member: discord.Member, *, reason="No reason provided"):
    if not member.voice or not member.voice.channel: return await ctx.send("That member is not connected to a voice channel.")
    await member.move_to(None, reason=f"Voice disconnect by {ctx.author}: {reason}")
    case = await prefix_case(ctx, "voice-disconnect", member, reason)
    await send_action_confirmation(ctx, "Voice Disconnect", member, reason, case)

@bot.hybrid_command(name="voicemute", description="Server-mute a member in voice channels.")
@commands.guild_only()
@commands.check(prefix_command_check("moderate_members"))
async def voicemute(ctx, member: discord.Member, *, reason="No reason provided"):
    if not member.voice: return await ctx.send("That member is not connected to a voice channel.")
    await member.edit(mute=True, reason=f"Server mute by {ctx.author}: {reason}")
    case = await prefix_case(ctx, "voice-mute", member, reason)
    await send_action_confirmation(ctx, "Voice Mute", member, reason, case)

@bot.hybrid_command(name="voiceunmute", description="Remove a server mute from a member.")
@commands.guild_only()
@commands.check(prefix_command_check("moderate_members"))
async def voiceunmute(ctx, member: discord.Member, *, reason="No reason provided"):
    if not member.voice: return await ctx.send("That member is not connected to a voice channel.")
    await member.edit(mute=False, reason=f"Server unmute by {ctx.author}: {reason}")
    case = await prefix_case(ctx, "voice-unmute", member, reason)
    await send_action_confirmation(ctx, "Voice Unmute", member, reason, case)

@bot.command(name="bl", aliases=["blacklist"])
@commands.guild_only()
@commands.check(prefix_blacklist_check)
async def prefix_bl(ctx, member: discord.Member, *, reason="No reason provided"):
    key = f"{ctx.guild.id}:{member.id}"
    if key in state["blacklists"]:
        return await ctx.send(f"{member.mention} is already blacklisted.")
    case = await prefix_case(ctx, "blacklist", member, reason)
    record = {"guildId":str(ctx.guild.id),"userId":str(member.id),"reason":reason,"moderatorId":str(ctx.author.id),"caseId":case,"status":"blacklisted","createdAt":now()}
    state["blacklists"][key] = record
    state["blacklistHistory"].append(record.copy())
    try:
        await blacklist_member(ctx.guild, member, reason, ctx.author, case)
    except discord.Forbidden:
        state["blacklists"].pop(key, None)
        return await ctx.send("I cannot apply the jail role. Move my bot role above the member and grant Manage Roles/Manage Channels.")
    state["blacklistHistory"][-1] = record.copy()
    save_state()
    embed = discord.Embed(title="User Blacklisted", description=f"{member.mention} **was blacklisted.**\n\n**Blacklisted by:** {ctx.author.mention}\n**Reason:** {reason}", color=CYAN, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Case #{case} â¢ TSBAF Moderation")
    await ctx.send(embed=embed)
    forwarded = await send_blacklist_log(ctx.guild, embed)
    await ctx.send(f"Case #{case} forwarded to bl logs." if forwarded else f"Case #{case} created. BL logs channel is not configured.")

@bot.command(name="unbl", aliases=["unblacklist"])
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_unbl(ctx, member: discord.Member):
    key = f"{ctx.guild.id}:{member.id}"
    record = state["blacklists"].pop(key, None)
    if not record:
        return await ctx.send("That user is not blacklisted.")
    record.update({"status":"unblacklisted","unblacklistedAt":now(),"unblacklistedBy":str(ctx.author.id)})
    state["blacklistHistory"].append(record.copy())
    await unblacklist_member(ctx.guild, member, record)
    save_state()
    case = await prefix_case(ctx, "unblacklist", member, f"Original reason: {record['reason']}")
    embed = discord.Embed(title="User Unblacklisted", description=f"**Member**\n{member.mention}\n\n**Moderator**\n{ctx.author.mention}\n\n**Original reason**\n{record['reason']}", color=CYAN, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Case #{case} â¢ TSBAF Moderation")
    await ctx.send(embed=embed)
    forwarded = await send_blacklist_log(ctx.guild, embed)
    await ctx.send("Unblacklist case forwarded to bl logs." if forwarded else "Unblacklist completed. BL logs channel is not configured.")

@bot.command(name="blcheck")
@commands.guild_only()
async def prefix_blcheck(ctx, member: discord.User):
    record = state["blacklists"].get(f"{ctx.guild.id}:{member.id}")
    if not record:
        return await ctx.send(f"{member.mention} is not blacklisted.")
    await ctx.send(f"{member.mention} is blacklisted. Reason: {record['reason']} | Case #{record['caseId']}")

@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    cfg = guild_config(channel.guild.id)
    role_id = cfg.get("blacklistRoleId")
    role = channel.guild.get_role(int(role_id)) if role_id else None
    if role and cfg.get("jailSetupComplete"):
        try:
            await apply_jail_permissions(channel.guild, role, channels=[channel])
        except discord.Forbidden:
            print(f"Could not apply jail permissions to new channel {channel.id}; check Manage Channels.")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send("You need Manage Server to change the prefix.")
    if isinstance(error, commands.CheckFailure):
        return await ctx.send(embed=discord.Embed(title="Access Denied", description="You do not have the required role or permissions to use this command.", color=discord.Color.red()))
    if isinstance(error, commands.MissingRequiredArgument):
        p = guild_config(ctx.guild.id).get("prefix",",") if ctx.guild else "!"
        return await ctx.send(embed=discord.Embed(title="Missing Required Argument", description=f"`{error.param.name}` is required. Use `{p}help` to view command syntax.", color=discord.Color.orange()))
    if isinstance(error, commands.BadArgument):
        return await ctx.send(embed=discord.Embed(title="Invalid Argument", description="I could not resolve that input. Mention a server member or provide a valid user ID, then try again.", color=discord.Color.orange()))
    if isinstance(error, discord.Forbidden):
        return await ctx.send(embed=discord.Embed(title="Action Could Not Be Completed", description="The bot is missing a required permission, or its highest role is not above the target member. Check the role hierarchy and channel permissions.", color=discord.Color.red()))
    await ctx.send(embed=discord.Embed(title="Command Error", description="The command could not be completed. Check the syntax and bot permissions; if the issue persists, contact an administrator.", color=discord.Color.red()))

@tree.command(name="blacklist", description="Blacklist a user and create a moderation case.")
@app_commands.describe(user="User to blacklist", reason="Reason for the blacklist")
async def blacklist(interaction: discord.Interaction, user: discord.User, reason: app_commands.Range[str, 1, 500]):
    if not interaction.guild or not can_blacklist(interaction):
        return await reply_private(interaction, "You do not have permission to use this command.")
    await interaction.response.defer(thinking=True)
    key = f"{interaction.guild.id}:{user.id}"
    if key in state["blacklists"]:
        return await reply_private(interaction, f"Already blacklisted as case #{state['blacklists'][key]['caseId']}.")
    case_id = state["nextCaseId"]
    state["nextCaseId"] += 1
    record = {"guildId": str(interaction.guild.id), "userId": str(user.id), "reason": reason,
              "moderatorId": str(interaction.user.id), "caseId": case_id, "status": "blacklisted", "createdAt": now()}
    state["blacklists"][key] = record
    state["blacklistHistory"].append(record.copy())
    member = interaction.guild.get_member(user.id)
    if member is None:
        try:
            member = await interaction.guild.fetch_member(user.id)
        except discord.NotFound:
            member = None
    if member is None:
        state["blacklists"].pop(key, None)
        state["blacklistHistory"].pop()
        return await reply_private(interaction, "That user must be a member of this server to be jailed.")
    try:
        await blacklist_member(interaction.guild, member, reason, interaction.user, case_id)
    except discord.Forbidden:
        state["blacklists"].pop(key, None)
        state["blacklistHistory"].pop()
        return await reply_private(interaction, "I cannot configure the jail. Give me Manage Roles and Manage Channels, and move my bot role above the target member.")
    state["blacklistHistory"][-1] = record.copy()
    save_state()
    embed = discord.Embed(title="User Blacklisted", description=f"{member.mention} **was blacklisted.**\n\n**Blacklisted by:** {interaction.user.mention}\n**Reason:** {reason}\n\n**Jail status:** Role applied â¢ {('Nickname updated' if not state['blacklists'][key].get('nicknameNote') else 'Nickname could not be changed')}", color=CYAN, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Case #{case_id} â¢ TSBAF Moderation")
    await interaction.followup.send(embed=embed)
    forwarded = await send_blacklist_log(interaction.guild, embed)
    await interaction.followup.send(content=(f"Case #{case_id} forwarded to bl logs." if forwarded else f"Case #{case_id} created. BL logs channel is not configured."))

@tree.command(name="unblacklist", description="Remove a user from the active blacklist and keep history.")
async def unblacklist(interaction: discord.Interaction, user: discord.User):
    if not interaction.guild or not is_staff(interaction):
        return await reply_private(interaction, "You do not have permission to use this command.")
    await interaction.response.defer(thinking=True)
    key = f"{interaction.guild.id}:{user.id}"
    record = state["blacklists"].pop(key, None)
    if not record:
        return await reply_private(interaction, "That user is not blacklisted.")
    record.update({"status": "unblacklisted", "unblacklistedAt": now(), "unblacklistedBy": str(interaction.user.id)})
    state["blacklistHistory"].append(record.copy())
    member = interaction.guild.get_member(user.id)
    if member is None:
        try:
            member = await interaction.guild.fetch_member(user.id)
        except discord.NotFound:
            member = None
    if member:
        await unblacklist_member(interaction.guild, member, record)
    save_state()
    embed = discord.Embed(title="User Unblacklisted", description=f"**Unblacklisted by**\n{interaction.user.mention}\n\n**Member**\n{user.mention}\n\n**Original reason**\n{record['reason']}", color=CYAN, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Original Case #{record['caseId']} â¢ TSBAF Moderation")
    await interaction.followup.send(embed=embed)
    forwarded = await send_blacklist_log(interaction.guild, embed)
    if not forwarded:
        await interaction.followup.send(content="Unblacklist completed. BL logs channel is not configured.")

@tree.command(name="blcheck", description="Check whether a user is currently blacklisted.")
async def blcheck(interaction: discord.Interaction, user: discord.User):
    if not interaction.guild:
        return await reply_private(interaction, "Use this command in a server.")
    record = state["blacklists"].get(f"{interaction.guild.id}:{user.id}")
    if not record:
        return await reply_private(interaction, f"{user.mention} is **not blacklisted**.")
    embed = discord.Embed(title="Blacklist Check", description=f"**User**\n{user.mention}\n\n**Status**\nBlacklisted\n\n**Reason**\n{record['reason']}", color=CYAN)
    embed.set_footer(text=f"Case #{record['caseId']}")
    await interaction.response.send_message(embed=embed, ephemeral=False)

@tree.command(name="say", description="Make the bot send a message.")
@app_commands.describe(message="Message to send", channel="Destination channel (defaults to this channel)")
async def say(interaction: discord.Interaction, message: app_commands.Range[str, 1, 2000], channel: discord.TextChannel | None = None):
    if not interaction.user.guild_permissions.manage_messages:
        return await reply_private(interaction, "You need Manage Messages.")
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await reply_private(interaction, "That is not a text channel.")
    await target.send(message)
    await reply_private(interaction, "Message sent.")

@tree.command(name="embed-say", description="Send a custom embed.")
@app_commands.describe(title="Embed title", description="Embed description", color="Hex color, e.g. #5865F2", channel="Destination channel")
async def embed_say(interaction: discord.Interaction, title: app_commands.Range[str, 1, 256], description: app_commands.Range[str, 1, 4000], color: str = "#8B5CF6", channel: discord.TextChannel | None = None):
    if not interaction.user.guild_permissions.manage_messages:
        return await reply_private(interaction, "You need Manage Messages.")
    raw = color.lstrip("#")
    try:
        value = int(raw, 16) if len(raw) == 6 else PURPLE.value
    except ValueError:
        value = PURPLE.value
    embed = discord.Embed(title=title, description=description, color=value, timestamp=datetime.now(timezone.utc))
    await (channel or interaction.channel).send(embed=embed)
    await reply_private(interaction, "Embed sent.")

async def stage_power_autocomplete(interaction: discord.Interaction, current: str):
    cfg = guild_config(interaction.guild.id) if interaction.guild else {}
    options = cfg.get("stagePowerRoles", {})
    labels = ("low", "mid", "high")
    return [app_commands.Choice(name=name.title(), value=name) for name in labels if name in options and current.lower() in name][:25]

async def stage_rank_autocomplete(interaction: discord.Interaction, current: str):
    cfg = guild_config(interaction.guild.id) if interaction.guild else {}
    options = cfg.get("stageRankRoles", {})
    return [app_commands.Choice(name=name[:100], value=name) for name in options if current.lower() in name.lower()][:25]

@tree.command(name="stage", description="Award a tryout stage and its configured power/rank roles.")
@app_commands.describe(user="Player who passed", stage="Stage number", power="Configured power level: Low, Mid, or High", rank="Configured rank role")
@app_commands.autocomplete(power=stage_power_autocomplete, rank=stage_rank_autocomplete)
async def stage(interaction: discord.Interaction, user: discord.Member, stage: app_commands.Range[int, 1, 100], power: str, rank: str):
    if not interaction.guild:
        return await reply_private(interaction, "This command can only be used inside a server.")
    cfg = guild_config(interaction.guild.id)
    host_role_id = cfg.get("tryoutHostRoleId")
    is_host = isinstance(interaction.user, discord.Member) and host_role_id and any(r.id == int(host_role_id) for r in interaction.user.roles)
    if not (is_admin(interaction) or is_host):
        return await reply_private(interaction, "Only members with the configured tryout-host role can use /stage.")
    power_key, rank_key = power.strip().lower(), rank.strip().lower()
    if power_key not in {"low", "mid", "high"}:
        return await reply_private(interaction, "Power must be `low`, `mid`, or `high`.")
    stage_id = cfg.get("stageRoles", {}).get(str(stage))
    power_id = cfg.get("stagePowerRoles", {}).get(power_key)
    rank_id = cfg.get("stageRankRoles", {}).get(rank_key)
    if not stage_id or not power_id or not rank_id:
        return await reply_private(interaction, "That stage, power, or rank is not configured. Ask an administrator to configure them under `/config`.")
    stage_role = interaction.guild.get_role(int(stage_id))
    power_role = interaction.guild.get_role(int(power_id))
    rank_role = interaction.guild.get_role(int(rank_id))
    if not stage_role or not power_role or not rank_role:
        return await reply_private(interaction, "A configured stage/power/rank role no longer exists. An administrator must update the stage role configuration.")
    bot_member = interaction.guild.me
    if not bot_member or any(role >= bot_member.top_role for role in (stage_role, power_role, rank_role)):
        return await reply_private(interaction, "I cannot assign one of those roles. Move my bot role above the configured stage, power, and rank roles.")
    await interaction.response.defer(thinking=True)
    # Keep one active role from each configured category; preserve unrelated member roles.
    managed_ids = {int(rid) for mapping in (cfg.get("stageRoles", {}), cfg.get("stagePowerRoles", {}), cfg.get("stageRankRoles", {})) for rid in mapping.values()}
    selected_ids = {stage_role.id, power_role.id, rank_role.id}
    remove_roles = [r for r in user.roles if r.id in managed_ids and r.id not in selected_ids]
    if remove_roles:
        await user.remove_roles(*remove_roles, reason=f"Stage roles updated by {interaction.user}")
    await user.add_roles(stage_role, power_role, rank_role, reason=f"Stage {stage} awarded by {interaction.user}")
    record = {"guildId":str(interaction.guild.id),"userId":str(user.id),"stage":stage,"stageRoleId":str(stage_role.id),"power":power_key,"rank":rank_key,"powerRoleId":str(power_role.id),"rankRoleId":str(rank_role.id),"hostId":str(interaction.user.id),"createdAt":now()}
    state.setdefault("stages", []).append(record)
    save_state()
    embed = discord.Embed(title="Stage Passed", description=f"**Player**\n{user.mention}\n\n**Stage**\n{stage_role.mention} (Stage {stage})\n\n**Power**\n{power_role.mention}\n\n**Rank**\n{rank_role.mention}\n\n**Tryout Host**\n{interaction.user.mention}", color=CYAN, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text="TSBAF â¢ Tryout System")
    await interaction.followup.send(embed=embed)
    await send_log(interaction.guild, embed)

@tree.command(name="stage-remove", description="Remove a player's configured stage, power, and rank roles.")
@app_commands.describe(user="Player whose stage assignment should be removed")
async def stage_remove(interaction: discord.Interaction, user: discord.Member):
    if not interaction.guild:
        return await reply_private(interaction, "This command can only be used inside a server.")
    cfg = guild_config(interaction.guild.id)
    host_role_id = cfg.get("tryoutHostRoleId")
    is_host = isinstance(interaction.user, discord.Member) and host_role_id and any(r.id == int(host_role_id) for r in interaction.user.roles)
    if not (is_admin(interaction) or is_host):
        return await reply_private(interaction, "Only members with the configured tryout-host role can use /stage-remove.")
    managed_ids = {int(rid) for mapping in (cfg.get("stageRoles", {}), cfg.get("stagePowerRoles", {}), cfg.get("stageRankRoles", {})) for rid in mapping.values()}
    active_record = next((record for record in reversed(state.get("stages", [])) if record.get("guildId") == str(interaction.guild.id) and record.get("userId") == str(user.id) and not record.get("removedAt")), None)
    if active_record:
        managed_ids.update(int(active_record[key]) for key in ("stageRoleId", "powerRoleId", "rankRoleId") if active_record.get(key))
    remove_roles = [role for role in user.roles if role.id in managed_ids]
    if not remove_roles:
        return await reply_private(interaction, f"{user.mention} has no configured stage, power, or rank roles to remove.")
    bot_member = interaction.guild.me
    if not bot_member or any(role >= bot_member.top_role for role in remove_roles):
        return await reply_private(interaction, "I cannot remove one of those roles. Move my bot role above the configured stage roles.")
    await interaction.response.defer(thinking=True)
    await user.remove_roles(*remove_roles, reason=f"Stage assignment removed by {interaction.user}")
    if active_record:
        active_record["removedAt"] = now()
        active_record["removedBy"] = str(interaction.user.id)
    save_state()
    embed = discord.Embed(title="Stage Assignment Removed", description=f"**Player**\n{user.mention}\n\n**Removed roles**\n" + ", ".join(role.mention for role in remove_roles) + f"\n\n**Removed by**\n{interaction.user.mention}", color=CYAN, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text="TSBAF â¢ Tryout System")
    await interaction.followup.send(embed=embed)
    await send_log(interaction.guild, embed)



async def clan_registration_for_leader(guild_id: int, user_id: int):
    return next((r for r in state["clanRegistrations"].values() if r.get("guildId") == str(guild_id) and r.get("status") == "accepted" and r.get("userId") == str(user_id)), None)

def active_clan_mainers(guild_id: int, registration: dict[str, Any]):
    reg_id = str(registration.get("id", ""))
    clan_id = str(registration.get("clanGuildId", ""))
    found = {}
    for entry in state.get("clanMains", []):
        if entry.get("guildId") != str(guild_id) or entry.get("status", "active") != "active":
            continue
        belongs = entry.get("clanRegistrationId") == reg_id or (not entry.get("clanRegistrationId") and str(entry.get("clanGuildId", "")) == clan_id)
        if belongs:
            found[str(entry.get("userId"))] = entry
    return found

async def sync_clan_blacklist_role(guild: discord.Guild, registration: dict[str, Any], add: bool):
    role_id = guild_config(guild.id).get("clanBlacklistRoleId")
    role = guild.get_role(int(role_id)) if role_id else None
    if role is None:
        return 0, "Clan blacklist role is not configured or no longer exists."
    bot_member = guild.me
    if not bot_member or role >= bot_member.top_role or role.managed or role.is_default():
        return 0, "The bot cannot manage the configured clan-blacklist role; check role hierarchy."
    mainers = active_clan_mainers(guild.id, registration)
    changed = 0
    for user_id in mainers:
        member = guild.get_member(int(user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
        # Do not remove the marker while the member still belongs to another blacklisted clan.
        other_blacklisted = any(r.get("clanBlacklisted") and r.get("guildId") == str(guild.id) and r.get("id") != registration.get("id") and str(member.id) in active_clan_mainers(guild.id, r) for r in state["clanRegistrations"].values())
        if add and role not in member.roles:
            await member.add_roles(role, reason=f"Clan blacklisted: {registration.get('clanName')}"); changed += 1
        elif not add and not other_blacklisted and role in member.roles:
            await member.remove_roles(role, reason=f"Clan unblacklisted: {registration.get('clanName')}"); changed += 1
    return changed, None

@tree.command(name="blacklist-clan", description="Blacklist a registered clan and mark all its mainers.")
@app_commands.describe(clan="Registered clan name or clan server ID", reason="Reason for the clan blacklist")
async def blacklist_clan(interaction: discord.Interaction, clan: str, reason: app_commands.Range[str, 1, 500]):
    if not interaction.guild or not is_staff(interaction):
        return await reply_private(interaction, "Only configured staff or administrators can blacklist a clan.")
    matches = [r for r in state["clanRegistrations"].values() if r.get("guildId") == str(interaction.guild.id) and r.get("status") == "accepted" and (r.get("clanName", "").casefold() == clan.casefold() or str(r.get("clanGuildId")) == clan)]
    if len(matches) != 1:
        return await reply_private(interaction, "I could not find exactly one accepted clan. Use its exact clan name or clan server ID.")
    registration = matches[0]
    if registration.get("clanBlacklisted"):
        return await reply_private(interaction, "That clan is already blacklisted.")
    await interaction.response.defer(thinking=True)
    registration.update({"clanBlacklisted": True, "clanBlacklistReason": reason, "clanBlacklistedAt": now(), "clanBlacklistedBy": str(interaction.user.id)})
    try:
        changed, warning = await sync_clan_blacklist_role(interaction.guild, registration, True)
    except discord.Forbidden:
        registration["clanBlacklisted"] = False
        return await reply_private(interaction, "Discord denied role assignment. Check Manage Roles and role hierarchy; no blacklist was applied.")
    save_state()
    embed = discord.Embed(title="Clan Blacklisted", description=f"**Clan:** {registration['clanName']}\n**Mainers marked:** {changed}\n**Reason:** {reason}\n**By:** {interaction.user.mention}" + (f"\n\n{warning}" if warning else ""), color=CYAN)
    await reply_private(interaction, embed.description)

@tree.command(name="unblacklist-clan", description="Unblacklist a clan and clear its mainers' blacklist role.")
@app_commands.describe(clan="Registered clan name or clan server ID")
async def unblacklist_clan(interaction: discord.Interaction, clan: str):
    if not interaction.guild or not is_staff(interaction):
        return await reply_private(interaction, "Only configured staff or administrators can unblacklist a clan.")
    matches = [r for r in state["clanRegistrations"].values() if r.get("guildId") == str(interaction.guild.id) and r.get("status") == "accepted" and (r.get("clanName", "").casefold() == clan.casefold() or str(r.get("clanGuildId")) == clan)]
    if len(matches) != 1:
        return await reply_private(interaction, "I could not find exactly one accepted clan. Use its exact clan name or clan server ID.")
    registration = matches[0]
    if not registration.get("clanBlacklisted"):
        return await reply_private(interaction, "That clan is not blacklisted.")
    await interaction.response.defer(thinking=True)
    registration["clanBlacklisted"] = False
    registration["clanUnblacklistedAt"] = now()
    registration["clanUnblacklistedBy"] = str(interaction.user.id)
    changed, warning = await sync_clan_blacklist_role(interaction.guild, registration, False)
    save_state()
    await reply_private(interaction, f"Clan **{registration['clanName']}** unblacklisted. Removed the clan-blacklist role from {changed} mainer(s)." + (f" {warning}" if warning else ""))

async def clan_mainer_autocomplete(interaction: discord.Interaction, current: str):
    """Search accepted clans; Discord returns at most 25 suggestions per request."""
    if not interaction.guild:
        return []
    query = current.strip().casefold()
    registrations = [
        r for r in state.get("clanRegistrations", {}).values()
        if r.get("guildId") == str(interaction.guild.id) and r.get("status") == "accepted"
    ]
    registrations.sort(key=lambda r: str(r.get("clanName", "")).casefold())
    matches = [r for r in registrations if not query or query in str(r.get("clanName", "")).casefold() or query in str(r.get("clanGuildId", ""))]
    return [app_commands.Choice(name=f"{str(r.get('clanName', 'Unnamed clan'))[:80]} ({str(r.get('clanGuildId', ''))[-8:]})", value=str(r.get("id", ""))[:100]) for r in matches[:25]]


@tree.command(name="clan-mainers", description="View the active mainers of any registered clan.")
@app_commands.describe(clan="Choose a registered clan to view its active mainers")
@app_commands.autocomplete(clan=clan_mainer_autocomplete)
async def clan_mainers(interaction: discord.Interaction, clan: str):
    if not interaction.guild:
        return await reply_private(interaction, "Use this command in the main server.")
    registration = state.get("clanRegistrations", {}).get(clan)
    if not registration or registration.get("guildId") != str(interaction.guild.id) or registration.get("status") != "accepted":
        return await reply_private(interaction, "That clan is not an accepted registration in this server. Choose a clan from the autocomplete list.")
    mainers = active_clan_mainers(interaction.guild.id, registration)
    names = [f"<@{uid}>" for uid in mainers]
    description = "\n".join(f"{i+1}. {name}" for i, name in enumerate(names[:50])) or "No active mainers yet."
    if len(names) > 50:
        description += f"\n…and {len(names)-50} more."
    embed = discord.Embed(title=f"{registration['clanName']} | Mainers ({len(names)})", description=description, color=CYAN)
    embed.set_footer(text="Public clan directory • Active mainers only")
    await interaction.response.send_message(embed=embed)


@tree.command(name="clan-remove-mainer", description="Remove a member from your clan's active mainer list.")
@app_commands.describe(member="Member to remove from your clan")
async def clan_remove_mainer(interaction: discord.Interaction, member: discord.Member):
    if not interaction.guild:
        return await reply_private(interaction, "Use this command in the main server.")
    registration = await clan_registration_for_leader(interaction.guild.id, interaction.user.id)
    if not registration:
        return await reply_private(interaction, "Only a registered clan owner can remove their clan's mainers.")
    entries = [e for e in state.get("clanMains", []) if e.get("guildId") == str(interaction.guild.id) and e.get("userId") == str(member.id) and e.get("status", "active") == "active" and (e.get("clanRegistrationId") == registration.get("id") or (not e.get("clanRegistrationId") and str(e.get("clanGuildId")) == str(registration.get("clanGuildId"))))]
    if not entries:
        return await reply_private(interaction, "That member is not an active mainer of your clan.")
    await interaction.response.defer(thinking=True)
    for entry in entries:
        entry["status"] = "removed"
        entry["removedAt"] = now()
        entry["removedBy"] = str(interaction.user.id)
    # Remove the shared role only if they have no other active clan membership.
    still_maining = any(e.get("guildId") == str(interaction.guild.id) and e.get("userId") == str(member.id) and e.get("status", "active") == "active" for e in state.get("clanMains", []))
    role_id = guild_config(interaction.guild.id).get("clanMainerRoleId")
    role = interaction.guild.get_role(int(role_id)) if role_id else None
    if role and not still_maining and role in member.roles and interaction.guild.me and role < interaction.guild.me.top_role:
        await member.remove_roles(role, reason=f"Removed from {registration['clanName']} by its owner")
    save_state()
    await reply_private(interaction, f"Removed {member.mention} from **{registration['clanName']}**. Their other clan memberships were left unchanged.")

@tree.command(name="lfclan", description="Post your looking-for-clan profile.")
@app_commands.describe(kills="Your kill count", device="Your gaming device")
@app_commands.choices(device=[app_commands.Choice(name="PC", value="PC"), app_commands.Choice(name="Mobile", value="Mobile"), app_commands.Choice(name="Console", value="Console"), app_commands.Choice(name="Other", value="Other")])
async def lfclan(interaction: discord.Interaction, kills: app_commands.Range[int, 0, 1000000], device: app_commands.Choice[str]):
    if not interaction.guild:
        return await reply_private(interaction, "Use this command in the main server.")
    cfg = guild_config(interaction.guild.id)
    channel_id = cfg.get("lfClanChannelId")
    channel = interaction.guild.get_channel(int(channel_id)) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return await reply_private(interaction, "The LFClan channel is not configured. Ask an administrator to use `/config lfclan-channel`.")
    await interaction.response.defer(thinking=True)
    member = interaction.user
    def role_label(mapping):
        for label, rid in mapping.items():
            role = interaction.guild.get_role(int(rid))
            if role and role in member.roles:
                return str(label).upper()
        return "Not set"
    stage = role_label(cfg.get("stageRoles", {}))
    power = role_label(cfg.get("stagePowerRoles", {}))
    rank = role_label(cfg.get("stageRankRoles", {}))
    embed = discord.Embed(title="Looking For Clan", description=f"**Member:** {member.mention}\n**Kills:** {kills:,}\n**Stage:** {stage}\n**Power:** {power}\n**Rank:** {rank}\n**Device:** {device.value}", color=CYAN, timestamp=datetime.now(timezone.utc))
    await channel.send(embed=embed)
    await reply_private(interaction, f"Your LFClan profile was posted in {channel.mention}.")

config_group = app_commands.Group(name="config", description="Configure the moderation bot.")

async def config_guard(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await reply_private(interaction, "Only server administrators can use /config.")
        return False
    return True

@config_group.command(name="logs", description="Set the moderation log channel.")
async def config_logs(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await config_guard(interaction): return
    guild_config(interaction.guild.id)["logChannelId"] = str(channel.id); save_state()
    await reply_private(interaction, f"Moderation logs set to {channel.mention}.")

@config_group.command(name="applications", description="Set the application review channel.")
async def config_applications(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await config_guard(interaction): return
    guild_config(interaction.guild.id)["applicationChannelId"] = str(channel.id); save_state()
    await reply_private(interaction, f"Application channel set to {channel.mention}.")

@config_group.command(name="staff-role", description="Set the staff role.")
async def config_staff_role(interaction: discord.Interaction, role: discord.Role):
    if not await config_guard(interaction): return
    guild_config(interaction.guild.id)["staffRoleId"] = str(role.id); save_state()
    await reply_private(interaction, f"Staff role set to {role.mention}.")

@config_group.command(name="blacklist-perms-role", description="Set the role allowed to use blacklist commands.")
async def config_blacklist_perms_role(interaction: discord.Interaction, role: discord.Role):
    if not await config_guard(interaction): return
    guild_config(interaction.guild.id)["blacklistPermsRoleId"] = str(role.id)
    save_state()
    await reply_private(interaction, f"{role.mention} can now use `/blacklist` and the `,bl` command.")

@config_group.command(name="bl-logs", description="Set the channel for blacklist case logs.")
async def config_bl_logs(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await config_guard(interaction): return
    guild_config(interaction.guild.id)["blacklistLogChannelId"] = str(channel.id)
    save_state()
    await reply_private(interaction, f"Blacklist logs will be forwarded to {channel.mention}.")

@config_group.command(name="blacklist-role", description="Set the blacklist role.")
async def config_blacklist_role(interaction: discord.Interaction, role: discord.Role):
    if not await config_guard(interaction): return
    cfg = guild_config(interaction.guild.id)
    cfg["blacklistRoleId"] = str(role.id)
    cfg["jailSetupComplete"] = False
    save_state()
    await reply_private(interaction, f"Blacklist role set to {role.mention}. Run `/config jail-setup` to apply jail permissions.")

@config_group.command(name="jail-setup", description="Create/update the blacklist jail role and channel permissions.")
async def config_jail_setup(interaction: discord.Interaction):
    if not await config_guard(interaction): return
    await interaction.response.defer(ephemeral=False, thinking=True)
    role = await get_jail_role(interaction.guild)
    updated = await apply_jail_permissions(interaction.guild, role)
    guild_config(interaction.guild.id)["jailSetupComplete"] = True
    save_state()
    await reply_private(interaction, f"Jail role {role.mention} is ready. Updated {updated} channel permission(s). Add allowed channels with `/config jail-channel`.")

@config_group.command(name="jail-channel", description="Allow jailed members to access one channel.")
@app_commands.describe(channel="Channel jailed members may access")
async def config_jail_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await config_guard(interaction): return
    await interaction.response.defer(ephemeral=False, thinking=True)
    cfg = guild_config(interaction.guild.id)
    ids = {str(cid) for cid in cfg.get("jailChannelIds", [])}
    ids.add(str(channel.id))
    cfg["jailChannelIds"] = sorted(ids)
    role = await get_jail_role(interaction.guild)
    await ensure_jail_setup(interaction.guild, role)
    updated = await apply_jail_permissions(interaction.guild, role, channels=[channel])
    save_state()
    await reply_private(interaction, f"{channel.mention} is now allowed for the {role.mention} role. Updated {updated} channel permission(s).")

@config_group.command(name="jail-channel-remove", description="Remove a channel from the jail allowlist.")
@app_commands.describe(channel="Channel to remove from jail access")
async def config_jail_channel_remove(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await config_guard(interaction): return
    await interaction.response.defer(ephemeral=False, thinking=True)
    cfg = guild_config(interaction.guild.id)
    cfg["jailChannelIds"] = [str(cid) for cid in cfg.get("jailChannelIds", []) if str(cid) != str(channel.id)]
    role = await get_jail_role(interaction.guild)
    await ensure_jail_setup(interaction.guild, role)
    updated = await apply_jail_permissions(interaction.guild, role, channels=[channel])
    save_state()
    await reply_private(interaction, f"{channel.mention} was removed from the jail allowlist. Updated {updated} channel permission(s).")

@config_group.command(name="tryout-role", description="Set the role allowed to use /stage.")
async def config_tryout_role(interaction: discord.Interaction, role: discord.Role):
    if not await config_guard(interaction): return
    guild_config(interaction.guild.id)["tryoutHostRoleId"] = str(role.id); save_state()
    await reply_private(interaction, f"Tryout host role set to {role.mention}.")

@config_group.command(name="stage-power-role", description="Map Low, Mid, or High power to an existing role.")
@app_commands.choices(power=[app_commands.Choice(name="Low", value="low"), app_commands.Choice(name="Mid", value="mid"), app_commands.Choice(name="High", value="high")])
@app_commands.describe(power="Power level used by /stage", role="Existing server role to assign")
async def config_stage_power_role(interaction: discord.Interaction, power: app_commands.Choice[str], role: discord.Role):
    if not await config_guard(interaction): return
    power_key = power.value
    if role.is_default() or role.managed:
        return await reply_private(interaction, "Choose a normal, assignable server role.")
    cfg = guild_config(interaction.guild.id)
    cfg.setdefault("stagePowerRoles", {})[power_key] = str(role.id)
    save_state()
    await reply_private(interaction, f"Stage power `{power_key}` now assigns {role.mention}. It will appear in /stage autocomplete.")

@config_group.command(name="stage-rank-role", description="Map a rank label to an existing server role.")
@app_commands.describe(rank="Rank label used by /stage", role="Existing server role to assign")
async def config_stage_rank_role(interaction: discord.Interaction, rank: app_commands.Range[str, 1, 30], role: discord.Role):
    if not await config_guard(interaction): return
    if not rank.strip() or len(rank.strip()) > 30:
        return await reply_private(interaction, "Rank labels must be between 1 and 30 characters.")
    if role.is_default() or role.managed:
        return await reply_private(interaction, "Choose a normal, assignable server role.")
    cfg = guild_config(interaction.guild.id)
    cfg.setdefault("stageRankRoles", {})[rank.strip().lower()] = str(role.id)
    save_state()
    await reply_private(interaction, f"Stage rank `{rank.strip().lower()}` now assigns {role.mention}. It will appear in /stage autocomplete.")

@config_group.command(name="stage-role", description="Map a numbered stage (1, 2, 3...) to its existing role.")
@app_commands.describe(stage="Stage number, from 1 to 100", role="Existing server role to assign for this stage")
async def config_stage_role(interaction: discord.Interaction, stage: app_commands.Range[int, 1, 100], role: discord.Role):
    if not await config_guard(interaction): return
    if role.is_default() or role.managed:
        return await reply_private(interaction, "Choose a normal, assignable server role.")
    cfg = guild_config(interaction.guild.id)
    cfg.setdefault("stageRoles", {})[str(stage)] = str(role.id)
    save_state()
    await reply_private(interaction, f"Stage {stage} now assigns {role.mention}.")

@config_group.command(name="stage-power-role-remove", description="Remove an incorrect power-to-role mapping.")
@app_commands.choices(power=[app_commands.Choice(name="Low", value="low"), app_commands.Choice(name="Mid", value="mid"), app_commands.Choice(name="High", value="high")])
@app_commands.describe(power="Power level to remove")
async def config_stage_power_role_remove(interaction: discord.Interaction, power: app_commands.Choice[str]):
    if not await config_guard(interaction): return
    cfg = guild_config(interaction.guild.id)
    power_key = power.value
    mapping = cfg.setdefault("stagePowerRoles", {})
    removed = mapping.pop(power_key, None)
    if not removed:
        return await reply_private(interaction, f"No power mapping exists for `{power_key}`.")
    save_state()
    await reply_private(interaction, f"Removed the power mapping for `{power_key}`. You can now set it again with `/config stage-power-role`.")

@config_group.command(name="stage-rank-role-remove", description="Remove an incorrect rank-to-role mapping.")
@app_commands.describe(rank="Rank label to remove")
async def config_stage_rank_role_remove(interaction: discord.Interaction, rank: app_commands.Range[str, 1, 30]):
    if not await config_guard(interaction): return
    cfg = guild_config(interaction.guild.id)
    mapping = cfg.setdefault("stageRankRoles", {})
    removed = mapping.pop(rank.strip().lower(), None)
    if not removed:
        return await reply_private(interaction, f"No rank mapping exists for `{rank.strip().lower()}`.")
    save_state()
    await reply_private(interaction, f"Removed the rank mapping for `{rank.strip().lower()}`. You can now set it again with `/config stage-rank-role`.")

@config_group.command(name="stage-role-remove", description="Remove a numbered stage-to-role mapping.")
@app_commands.describe(stage="Stage number whose mapping should be removed")
async def config_stage_role_remove(interaction: discord.Interaction, stage: app_commands.Range[int, 1, 100]):
    if not await config_guard(interaction): return
    cfg = guild_config(interaction.guild.id)
    removed = cfg.setdefault("stageRoles", {}).pop(str(stage), None)
    if not removed:
        return await reply_private(interaction, f"Stage {stage} has no configured role.")
    save_state()
    await reply_private(interaction, f"Removed the role mapping for Stage {stage}. You can set it again with `/config stage-role`.")

@config_group.command(name="stage-reset", description="Clear all numbered stage, power, and rank role mappings.")
async def config_stage_reset(interaction: discord.Interaction):
    if not await config_guard(interaction): return
    cfg = guild_config(interaction.guild.id)
    cfg["stageRoles"] = {}
    cfg["stagePowerRoles"] = {}
    cfg["stageRankRoles"] = {}
    save_state()
    await reply_private(interaction, "All stage, power, and rank role mappings have been reset. The tryout-host permission role was kept. Reconfigure power roles with the labels `low`, `mid`, and `high` using `/config stage-power-role`.")

@config_group.command(name="clan-register-logs", description="Set the clan registration review channel.")
async def config_clan_logs(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await config_guard(interaction): return
    guild_config(interaction.guild.id)["clanRegistrationChannelId"] = str(channel.id); save_state()
    await reply_private(interaction, f"Clan registration logs set to {channel.mention}.")

@config_group.command(name="clan-news", description="Set the channel for clan-main announcements.")
async def config_clan_news(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await config_guard(interaction): return
    cfg = guild_config(interaction.guild.id)
    cfg["clanNewsChannelId"] = str(channel.id)
    cfg["clanMainLogChannelId"] = str(channel.id)
    save_state()
    await reply_private(interaction, f"Clan main logs set to {channel.mention}.")

@config_group.command(name="clan-main-logs", description="Set the channel where every clan main is logged.")
async def config_clan_main_logs(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await config_guard(interaction): return
    guild_config(interaction.guild.id)["clanMainLogChannelId"] = str(channel.id)
    save_state()
    await reply_private(interaction, f"Clan main logs set to {channel.mention}.")

@config_group.command(name="clan-leader-role", description="Set the role given to a clan owner when their registration is accepted.")
async def config_clan_leader_role(interaction: discord.Interaction, role: discord.Role):
    if not await config_guard(interaction): return
    if role.is_default() or role.managed:
        return await reply_private(interaction, "Choose a normal, assignable server role.")
    bot_member = interaction.guild.me
    if not bot_member or role >= bot_member.top_role:
        return await reply_private(interaction, "Move the bot's highest role above the clan leader role before configuring it.")
    guild_config(interaction.guild.id)["clanLeaderRoleId"] = str(role.id)
    save_state()
    await reply_private(interaction, f"Clan leader role set to {role.mention}. It will be assigned when a clan registration is accepted.")

@config_group.command(name="clan-mainer-role", description="Set the role automatically given to members when they main a clan.")
async def config_clan_mainer_role(interaction: discord.Interaction, role: discord.Role):
    if not await config_guard(interaction): return
    if role.is_default() or role.managed:
        return await reply_private(interaction, "Choose a normal, assignable server role.")
    bot_member = interaction.guild.me
    if not bot_member or role >= bot_member.top_role:
        return await reply_private(interaction, "Move the bot's highest role above the mainer role before configuring it.")
    guild_config(interaction.guild.id)["clanMainerRoleId"] = str(role.id)
    save_state()
    await reply_private(interaction, f"Clan mainer role set to {role.mention}. It will be assigned whenever a member successfully uses `/main`.")

@config_group.command(name="clan-blacklist-role", description="Set the role assigned to all mainers of blacklisted clans.")
async def config_clan_blacklist_role(interaction: discord.Interaction, role: discord.Role):
    if not await config_guard(interaction): return
    bot_member = interaction.guild.me
    if role.is_default() or role.managed or not bot_member or role >= bot_member.top_role:
        return await reply_private(interaction, "Choose a normal role below the bot's highest role.")
    guild_config(interaction.guild.id)["clanBlacklistRoleId"] = str(role.id); save_state()
    await reply_private(interaction, f"Clan blacklist role set to {role.mention}. It is assigned to every active mainer when a clan is blacklisted.")

@config_group.command(name="lfclan-channel", description="Set the channel for /lfclan posts.")
async def config_lfclan_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await config_guard(interaction): return
    guild_config(interaction.guild.id)["lfClanChannelId"] = str(channel.id); save_state()
    await reply_private(interaction, f"LFClan posts will be sent to {channel.mention}.")

@config_group.command(name="questions", description="Set up to five DM application questions separated by |.")
async def config_questions(interaction: discord.Interaction, questions: app_commands.Range[str, 1, 1000]):
    if not await config_guard(interaction): return
    if not questions.strip() or len(questions) > 1000:
        return await reply_private(interaction, "Provide 1 to 1000 characters of application questions.")
    qs = [q.strip() for q in questions.split("|") if q.strip()][:5]
    guild_config(interaction.guild.id)["applicationQuestions"] = qs; save_state()
    await reply_private(interaction, f"Saved {len(qs)} application question(s).")

tree.add_command(config_group)

@tree.command(name="config-show", description="Show the current bot configuration.")
async def config_show(interaction: discord.Interaction):
    if not await config_guard(interaction): return
    c = guild_config(interaction.guild.id)
    lines = [f"**{label}:** {f'<#{c[key]}>' if c.get(key) else 'Not configured'}" for label, key in [("Logs", "logChannelId"), ("Blacklist logs", "blacklistLogChannelId"), ("Applications", "applicationChannelId"), ("Clan registration logs", "clanRegistrationChannelId"), ("Clan main logs", "clanMainLogChannelId"), ("LFClan channel", "lfClanChannelId")]]
    lines += [f"**{label}:** {f'<@&{c[key]}>' if c.get(key) else 'Not configured'}" for label, key in [("Staff role", "staffRoleId"), ("Blacklist permission role", "blacklistPermsRoleId"), ("Blacklist / jail role", "blacklistRoleId"), ("Tryout host role", "tryoutHostRoleId"), ("Clan leader role", "clanLeaderRoleId"), ("Clan mainer role", "clanMainerRoleId"), ("Clan blacklist role", "clanBlacklistRoleId")]]
    jail_channels = c.get("jailChannelIds", [])
    lines.append("**Jail allowed channels:** " + (", ".join(f"<#{cid}>" for cid in jail_channels) if jail_channels else "None configured"))
    lines.append("**Numbered stage roles:** " + (", ".join(f"Stage {number}: <@&{rid}>" for number, rid in sorted(c.get("stageRoles", {}).items(), key=lambda item: int(item[0]))) or "Not configured"))
    lines.append("**Stage power roles:** " + (", ".join(f"{name}: <@&{rid}>" for name, rid in c.get("stagePowerRoles", {}).items()) or "Not configured"))
    lines.append("**Stage rank roles:** " + (", ".join(f"{name}: <@&{rid}>" for name, rid in c.get("stageRankRoles", {}).items()) or "Not configured"))
    lines.append("**Questions:** " + (" | ".join(c.get("applicationQuestions", [])) or "Not configured"))
    await interaction.response.send_message(embed=discord.Embed(title="Bot Configuration", description="\n".join(lines), color=CYAN), ephemeral=False)

@tree.command(name="apply", description="Start the application process in DMs.")
async def apply(interaction: discord.Interaction):
    if not interaction.guild:
        return await reply_private(interaction, "Use this command in a server.")
    cfg = guild_config(interaction.guild.id)
    channel_id = cfg.get("applicationChannelId")
    questions = cfg.get("applicationQuestions", [])
    if not channel_id or not questions:
        return await reply_private(interaction, "Applications are not configured. Ask staff to set /config applications and /config questions.")
    await reply_private(interaction, "Check your DMs. I will send the application questions there.")
    answers = []
    try:
        dm = await interaction.user.create_dm()
        for i, question in enumerate(questions, 1):
            msg = await wait_for_dm(interaction.user, f"**Question {i}/{len(questions)}**\n{question}\n\nReply within 5 minutes.")
            if msg is None: return
            answers.append(msg.content[:1000])
        app_id = f"{interaction.guild.id}-{interaction.user.id}-{int(datetime.now().timestamp())}"
        state["applications"][app_id] = {"id": app_id, "guildId": str(interaction.guild.id), "userId": str(interaction.user.id), "answers": answers, "status": "pending", "createdAt": now()}
        save_state()
        channel = interaction.guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return await dm.send("Your application was saved, but the staff channel is unavailable.")
        embed = discord.Embed(title="New Application", description=f"Applicant: {interaction.user.mention}\nID: `{app_id}`", color=CYAN, timestamp=datetime.now(timezone.utc))
        for q, answer in zip(questions, answers): embed.add_field(name=q[:256], value=answer or "No answer", inline=False)
        await channel.send(embed=embed, view=ReviewView("app", app_id))
        await dm.send("Your application has been submitted. Staff will review it.")
    except discord.Forbidden:
        await reply_private(interaction, "I could not DM you. Enable server-member DMs and try again.")

@tree.command(name="clan-register", description="Register your clan server. The actual server owner must apply.")
async def clan_register(interaction: discord.Interaction):
    if not interaction.guild:
        return await reply_private(interaction, "Use this command in the main server.")
    cfg = guild_config(interaction.guild.id)
    review_channel_id = cfg.get("clanRegistrationChannelId")
    if not review_channel_id:
        return await reply_private(interaction, "Clan registration is not configured. Ask staff to set /config clan-register-logs.")
    if any(r.get("guildId") == str(interaction.guild.id) and r.get("userId") == str(interaction.user.id) and r.get("status") == "pending" for r in state["clanRegistrations"].values()):
        return await reply_private(interaction, "You already have a clan registration waiting for staff review.")
    await reply_private(interaction, "Check your DMs. Send your clan server invite. You must be the actual server owner; admin permissions do not count.")
    dm = await interaction.user.create_dm()
    msg = await wait_for_dm(interaction.user, "Send a working Discord invite to your clan server. You have 5 minutes.")
    if msg is None: return
    match = re.search(r"(?:https?://)?(?:www\.)?(?:discord\.gg/|discord\.com/invite/)([A-Za-z0-9-]+)", msg.content.strip(), re.I)
    if not match:
        return await dm.send("That does not look like a valid Discord invite. Run /clan-register again and send a valid invite.")
    try:
        invite = await bot.fetch_invite(match.group(1))
    except discord.HTTPException:
        return await dm.send("I could not verify that invite. Create a working invite and try again.")
    target = bot.get_guild(invite.guild.id) if invite.guild else None
    if target is None:
        bot_id = bot.user.id
        perms = discord.Permissions(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
        add_url = discord.utils.oauth_url(bot_id, permissions=perms, scopes=("bot", "applications.commands"))
        await dm.send(f"I need to join your clan server to verify ownership. Add me here: {add_url}\nAfter adding me, I will continue automatically for up to 5 minutes.")
        deadline = asyncio.get_running_loop().time() + 300
        while asyncio.get_running_loop().time() < deadline:
            target = bot.get_guild(invite.guild.id) if invite.guild else None
            if target: break
            await asyncio.sleep(5)
    if target is None:
        return await dm.send("I did not join your server in time. Add the bot, then run /clan-register again.")
    if target.owner_id != interaction.user.id:
        return await dm.send("You are not the owner of that server. Only the actual owner can register it; admin is not enough.")
    if target.member_count < 30:
        return await dm.send(f"Your server has {target.member_count} members. A clan needs at least 30 members to register.")
    reg_id = f"{interaction.guild.id}-{target.id}-{int(datetime.now().timestamp())}"
    record = {"id": reg_id, "guildId": str(interaction.guild.id), "userId": str(interaction.user.id), "clanGuildId": str(target.id), "clanName": target.name, "serverInvite": f"https://discord.gg/{match.group(1)}", "status": "pending", "createdAt": now()}
    state["clanRegistrations"][reg_id] = record
    save_state()
    channel = interaction.guild.get_channel(int(review_channel_id))
    if not isinstance(channel, discord.TextChannel):
        return await dm.send("Your registration was saved, but the review channel is unavailable. Contact staff.")
    embed = discord.Embed(title="New Clan Registration", description=f"**Owner**\n{interaction.user.mention}\n\n**Clan**\n{target.name}\n\n**Invite**\n{record['serverInvite']}\n\n**Registration ID**\n`{reg_id}`", color=CYAN, timestamp=datetime.now(timezone.utc))
    await channel.send(embed=embed, view=ReviewView("clan", reg_id))
    await dm.send("Your clan registration has been submitted. Staff will review it.")

@tree.command(name="main", description="Main a registered clan using its permanent 5-character code.")
@app_commands.describe(code="The 5-character clan code (letters and numbers)")
async def main(interaction: discord.Interaction, code: app_commands.Range[str, 5, 6]):
    if not interaction.guild:
        return await reply_private(interaction, "Use this command in the main server.")
    code = code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{5,6}", code):
        return await reply_private(interaction, "Clan codes must contain 5 letters/numbers. Older 6-digit codes remain supported.")
    record = next((r for r in state["clanRegistrations"].values() if r.get("guildId") == str(interaction.guild.id) and r.get("status") == "accepted" and str(r.get("clanCode", "")).upper() == code), None)
    if not record:
        return await reply_private(interaction, "That clan code is invalid or is not registered in this server.")
    if record.get("clanBlacklisted"):
        return await reply_private(interaction, "This clan is currently blacklisted and cannot be mained.")

    await interaction.response.defer(thinking=True)
    created_at = now()
    main_id = f"{record['id']}-{interaction.user.id}-{secrets.token_hex(4)}"
    state.setdefault("clanMains", []).append({"id": main_id, "guildId": str(interaction.guild.id), "clanGuildId": record["clanGuildId"], "clanName": record["clanName"], "clanLeaderId": record["userId"], "userId": str(interaction.user.id), "createdAt": created_at})
    save_state()

    cfg = guild_config(interaction.guild.id)
    role_status = ""
    mainer_role_id = cfg.get("clanMainerRoleId")
    if mainer_role_id:
        mainer_role = interaction.guild.get_role(int(mainer_role_id))
        bot_member = interaction.guild.me
        if not mainer_role or mainer_role.is_default() or mainer_role.managed:
            role_status = "\n\nThe configured clan mainer role is missing or cannot be assigned."
        elif not bot_member or mainer_role >= bot_member.top_role:
            role_status = "\n\nThe bot cannot assign the configured mainer role. Move its highest role above it."
        elif mainer_role not in interaction.user.roles:
            try:
                await interaction.user.add_roles(mainer_role, reason=f"Mained clan: {record['clanName']}")
            except discord.Forbidden:
                role_status = "\n\nI could not assign the mainer role. Check my Manage Roles permission and role hierarchy."
            except discord.HTTPException:
                role_status = "\n\nDiscord returned an error while assigning the mainer role."
    news_id = cfg.get("clanMainLogChannelId") or cfg.get("clanNewsChannelId")
    logged = False
    if news_id:
        channel = interaction.guild.get_channel(int(news_id))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(news_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if isinstance(channel, discord.TextChannel):
            embed = discord.Embed(title="Clan Mained", description=f"**Member**\n{interaction.user.mention}\n\n**Clan**\n{record['clanName']}\n\n**Clan Leader / Owner**\n<@{record['userId']}>\n\n**Clan Server**\n`{record['clanGuildId']}`", color=CYAN, timestamp=datetime.now(timezone.utc))
            embed.set_footer(text=f"Main record: {main_id}")
            await channel.send(embed=embed)
            logged = True
    status = f"You have successfully mained **{record['clanName']}**. This code remains active and can be reused without a limit." + role_status
    if not logged:
        status += "\n\nClan main logs are not configured or the log channel is unavailable. Ask staff to run `/config clan-main-logs`."
    await reply_private(interaction, status)

@tasks.loop(minutes=1)
async def process_temporary_bans():
    entries = state.setdefault("tempBans", [])
    current = datetime.now(timezone.utc)
    changed = False
    for entry in list(entries):
        try:
            expires = datetime.fromisoformat(entry["unbanAt"])
            if expires.tzinfo is None: expires = expires.replace(tzinfo=timezone.utc)
            if expires > current: continue
            guild = bot.get_guild(int(entry["guildId"]))
            if guild is None: continue
            user_obj = discord.Object(id=int(entry["userId"]))
            try:
                await guild.unban(user_obj, reason=f"Temporary ban expired â¢ Case #{entry.get('caseId','?')}")
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                continue
            embed = discord.Embed(title="Temporary Ban Expired", description=f"**Member ID:** `{entry['userId']}`\n**Original case:** `#{entry.get('caseId','?')}`\nThe scheduled unban was processed.", color=CYAN, timestamp=current)
            embed.set_footer(text="TSBAF Moderation â¢ Automated action")
            await send_log(guild, embed)
            entries.remove(entry)
            changed = True
        except (KeyError, ValueError, TypeError):
            continue
    if changed: save_state()

@bot.event
async def on_ready():
    if not process_temporary_bans.is_running():
        process_temporary_bans.start()
    print(f"Logged in as {bot.user} ({bot.user.id})")
    if not getattr(bot, "_synced", False):
        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild_obj)
            synced = await tree.sync(guild=guild_obj)
        else:
            synced = await tree.sync()
        print(f"Synced {len(synced)} slash commands")
        bot._synced = True
    for guild in bot.guilds:
        cfg=guild_config(guild.id)
        if cfg.get("multiApplicationPanel") and cfg.get("multiApplicationMessageId"):
            enabled=[p for p in cfg.get("dashboardApplications",[]) if p.get("enabled")]
            if enabled:
                bot.add_view(MultiApplicationPanelView(bot,state,guild_config,save_state,ReviewView,guild.id,enabled))
        else:
            for panel in cfg.get("dashboardApplications", []):
                if panel.get("enabled") and panel.get("messageId"):
                    bot.add_view(ApplicationPanelView(bot, state, guild_config, save_state, ReviewView, guild.id, panel))
    for kind, bucket in (("app", "applications"), ("clan", "clanRegistrations")):
        for record_id, record in state[bucket].items():
            if record.get("status") == "pending":
                bot.add_view(ReviewView(kind, record_id))

async def main_async():
    global state
    state = load_state()
    try:
        asyncio.create_task(start_dashboard_api(bot, state, guild_config, save_state, ReviewView))
        await bot.start(TOKEN)
    finally:
        save_state()
        mongo.close()

if __name__ == "__main__":
    asyncio.run(main_async())
