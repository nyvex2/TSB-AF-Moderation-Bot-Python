import os
import re
import asyncio
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from pymongo import MongoClient

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
    "stages": [], "applications": {}, "clanRegistrations": {}, "clanMains": []
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
    return state["guilds"].setdefault(str(guild_id), {"applicationQuestions": []})

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

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
async def get_prefix(client, message):
    if message.guild is None:
        return commands.when_mentioned_or("!")(client, message)
    return commands.when_mentioned_or(guild_config(message.guild.id).get("prefix", "!"))(client, message)

bot = commands.Bot(command_prefix=get_prefix, intents=intents)
tree = bot.tree
state: dict[str, Any] = {}

async def reply_private(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)

async def send_log(guild: discord.Guild, embed: discord.Embed) -> None:
    channel_id = guild_config(guild.id).get("logChannelId")
    if channel_id:
        channel = guild.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
        if isinstance(channel, discord.TextChannel):
            await channel.send(embed=embed)

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
            if not staff_button(interaction):
                await reply_private(interaction, "Only configured staff can review this request.")
                return
            bucket = "applications" if self.kind == "app" else "clanRegistrations"
            record = state[bucket].get(self.record_id)
            if not record or record.get("guildId") != str(interaction.guild_id):
                await reply_private(interaction, "Request not found.")
                return
            if record.get("status") != "pending":
                await reply_private(interaction, f"This request is already {record.get('status')}.")
                return
            record["status"] = decision
            record["reviewedBy"] = str(interaction.user.id)
            user = await bot.fetch_user(int(record["userId"]))
            if self.kind == "clan" and decision == "accepted":
                code = str(secrets.randbelow(900000) + 100000)
                used = {r.get("clanCode") for r in state["clanRegistrations"].values()}
                while code in used:
                    code = str(secrets.randbelow(900000) + 100000)
                record["clanCode"] = code
                record.pop("mainUserId", None)
                record.pop("mainedAt", None)
                await user.send(f"Your clan **{record['clanName']}** has been registered.\n\nSecret clan code: **{code}**\nKeep it private. The member who mains your clan can use `/main code:{code}`.")
            else:
                label = "application" if self.kind == "app" else "clan registration"
                await user.send(f"Your {label} for **{interaction.guild.name}** was **{decision}**.")
            save_state()
            await interaction.response.edit_message(content=f"Reviewed: **{decision}** by {interaction.user.mention}.", view=None)
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
        timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Case #{case_id}")
    await send_log(ctx.guild, embed)
    return case_id

async def prefix_dm(user, message):
    try:
        await user.send(message)
    except (discord.Forbidden, discord.HTTPException):
        pass

@bot.command(name="modhelp", aliases=["moderation"])
async def modhelp(ctx):
    p = guild_config(ctx.guild.id).get("prefix", "!") if ctx.guild else "!"
    await ctx.send(
        f"**Moderation commands ({p})**\n"
        f"{p}ban @user [reason] | {p}unban user_id [reason] | {p}kick @user [reason]\n"
        f"{p}timeout @user 10m [reason] | {p}untimeout @user [reason]\n"
        f"{p}warn @user [reason] | {p}warnings @user | {p}clearwarnings @user\n"
        f"{p}purge amount | {p}slowmode seconds | {p}lock [#channel] | {p}unlock [#channel]\n"
        f"{p}bl @user [reason] | {p}unbl @user | {p}blcheck @user\n"
        f"{p}prefix | {p}setprefix new_prefix"
    )

@bot.command(name="prefix")
async def show_prefix(ctx):
    await ctx.send(f"Current prefix: {guild_config(ctx.guild.id).get('prefix', '!') if ctx.guild else '!'}")

@bot.command(name="setprefix")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def set_prefix(ctx, prefix: str):
    if not 1 <= len(prefix) <= 5 or any(c.isspace() for c in prefix):
        return await ctx.send("Prefix must be 1–5 non-space characters.")
    guild_config(ctx.guild.id)["prefix"] = prefix
    save_state()
    await ctx.send(f"Prefix changed to {prefix}")

@bot.command(name="ban")
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_ban(ctx, member: discord.Member, *, reason="No reason provided"):
    await prefix_dm(member, f"You were banned from {ctx.guild.name}. Reason: {reason}")
    await ctx.guild.ban(member, reason=f"{ctx.author}: {reason}", delete_message_seconds=0)
    case = await prefix_case(ctx, "ban", member, reason)
    await ctx.send(f"Banned {member.mention}. Case #{case}.")

@bot.command(name="unban")
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_unban(ctx, user_id: int, *, reason="No reason provided"):
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
    except discord.NotFound:
        return await ctx.send("That user is not banned or the ID is invalid.")
    case = await prefix_case(ctx, "unban", user, reason)
    await ctx.send(f"Unbanned {user}. Case #{case}.")

@bot.command(name="kick")
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_kick(ctx, member: discord.Member, *, reason="No reason provided"):
    await prefix_dm(member, f"You were kicked from {ctx.guild.name}. Reason: {reason}")
    await ctx.guild.kick(member, reason=f"{ctx.author}: {reason}")
    case = await prefix_case(ctx, "kick", member, reason)
    await ctx.send(f"Kicked {member.mention}. Case #{case}.")

@bot.command(name="timeout", aliases=["mute"])
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_timeout(ctx, member: discord.Member, duration: str, *, reason="No reason provided"):
    delta = parse_duration(duration)
    if delta is None:
        return await ctx.send("Use 10m, 2h, 1d, etc. Maximum is 28 days.")
    await member.timeout(delta, reason=f"{ctx.author}: {reason}")
    await prefix_dm(member, f"You were timed out in {ctx.guild.name} for {duration}. Reason: {reason}")
    case = await prefix_case(ctx, "timeout", member, f"{duration} — {reason}")
    await ctx.send(f"Timed out {member.mention} for {duration}. Case #{case}.")

@bot.command(name="untimeout", aliases=["unmute"])
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_untimeout(ctx, member: discord.Member, *, reason="No reason provided"):
    await member.timeout(None, reason=f"{ctx.author}: {reason}")
    case = await prefix_case(ctx, "untimeout", member, reason)
    await ctx.send(f"Removed timeout from {member.mention}. Case #{case}.")

@bot.command(name="warn")
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_warn(ctx, member: discord.Member, *, reason="No reason provided"):
    key = f"{ctx.guild.id}:{member.id}"
    rows = state.setdefault("warnings", {}).setdefault(key, [])
    rows.append({"reason":reason[:500],"moderatorId":str(ctx.author.id),"createdAt":now()})
    save_state()
    case = await prefix_case(ctx, "warn", member, reason)
    await prefix_dm(member, f"You received a warning in {ctx.guild.name}. Reason: {reason}")
    await ctx.send(f"Warned {member.mention}; total warnings: {len(rows)}. Case #{case}.")

@bot.command(name="warnings", aliases=["infractions"])
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_warnings(ctx, member: discord.Member):
    rows = state.setdefault("warnings", {}).get(f"{ctx.guild.id}:{member.id}", [])
    if not rows:
        return await ctx.send(f"{member.mention} has no warnings.")
    lines = [f"{i}. {r['reason']} — <@{r['moderatorId']}>" for i,r in enumerate(rows[-15:],1)]
    await ctx.send(embed=discord.Embed(title=f"Warnings for {member}",description="\n".join(lines)[:4000]))

@bot.command(name="clearwarnings")
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_clearwarnings(ctx, member: discord.Member):
    key = f"{ctx.guild.id}:{member.id}"
    count = len(state.setdefault("warnings", {}).get(key, []))
    state["warnings"].pop(key, None)
    save_state()
    await prefix_case(ctx, "clearwarnings", member, f"Cleared {count} warning(s)")
    await ctx.send(f"Cleared {count} warning(s) for {member.mention}.")

@bot.command(name="purge", aliases=["clear"])
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_purge(ctx, amount: int):
    if not 1 <= amount <= 100:
        return await ctx.send("Choose an amount from 1 to 100.")
    if isinstance(ctx.channel, discord.TextChannel):
        deleted = await ctx.channel.purge(limit=amount+1)
        msg = await ctx.send(f"Deleted {max(0,len(deleted)-1)} message(s).")
        await msg.delete(delay=5)

@bot.command(name="slowmode")
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_slowmode(ctx, seconds: int):
    if not isinstance(ctx.channel, discord.TextChannel):
        return
    if not 0 <= seconds <= 21600:
        return await ctx.send("Slowmode must be between 0 and 21600 seconds.")
    await ctx.channel.edit(slowmode_delay=seconds, reason=f"Set by {ctx.author}")
    await ctx.send(f"Slowmode set to {seconds} second(s).")

@bot.command(name="lock")
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_lock(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    if not isinstance(channel, discord.TextChannel):
        return await ctx.send("Choose a text channel.")
    ow = channel.overwrites_for(ctx.guild.default_role)
    ow.send_messages = False
    await channel.set_permissions(ctx.guild.default_role, overwrite=ow, reason=f"Locked by {ctx.author}")
    await ctx.send(f"Locked {channel.mention}.")

@bot.command(name="unlock")
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_unlock(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    if not isinstance(channel, discord.TextChannel):
        return await ctx.send("Choose a text channel.")
    ow = channel.overwrites_for(ctx.guild.default_role)
    ow.send_messages = None
    await channel.set_permissions(ctx.guild.default_role, overwrite=ow, reason=f"Unlocked by {ctx.author}")
    await ctx.send(f"Unlocked {channel.mention}.")

@bot.command(name="bl", aliases=["blacklist"])
@commands.guild_only()
@commands.check(prefix_staff_check)
async def prefix_bl(ctx, member: discord.Member, *, reason="No reason provided"):
    key = f"{ctx.guild.id}:{member.id}"
    if key in state["blacklists"]:
        return await ctx.send(f"{member.mention} is already blacklisted.")
    case = state["nextCaseId"]
    state["nextCaseId"] += 1
    record = {"guildId":str(ctx.guild.id),"userId":str(member.id),"reason":reason,"moderatorId":str(ctx.author.id),"caseId":case,"status":"blacklisted","createdAt":now()}
    state["blacklists"][key] = record
    state["blacklistHistory"].append(record.copy())
    role_id = guild_config(ctx.guild.id).get("blacklistRoleId")
    role = ctx.guild.get_role(int(role_id)) if role_id else None
    if role:
        await member.add_roles(role, reason=f"Blacklist case #{case}")
    save_state()
    await prefix_dm(member, f"You were blacklisted in {ctx.guild.name}. Reason: {reason}")
    await prefix_case(ctx, "blacklist", member, reason)
    await ctx.send(f"Blacklisted {member.mention}. Case #{case}.")

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
    role_id = guild_config(ctx.guild.id).get("blacklistRoleId")
    role = ctx.guild.get_role(int(role_id)) if role_id else None
    if role:
        await member.remove_roles(role, reason="User unblacklisted")
    save_state()
    await prefix_case(ctx, "unblacklist", member, f"Original reason: {record['reason']}")
    await ctx.send(f"Unblacklisted {member.mention}.")

@bot.command(name="blcheck")
@commands.guild_only()
async def prefix_blcheck(ctx, member: discord.User):
    record = state["blacklists"].get(f"{ctx.guild.id}:{member.id}")
    if not record:
        return await ctx.send(f"{member.mention} is not blacklisted.")
    await ctx.send(f"{member.mention} is blacklisted. Reason: {record['reason']} | Case #{record['caseId']}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send("You need Manage Server to change the prefix.")
    if isinstance(error, commands.CheckFailure):
        return await ctx.send("You do not have permission to use that moderation command.")
    if isinstance(error, commands.MissingRequiredArgument):
        p = guild_config(ctx.guild.id).get("prefix","!") if ctx.guild else "!"
        return await ctx.send(f"Missing argument: {error.param.name}. Use {p}modhelp.")
    if isinstance(error, commands.BadArgument):
        return await ctx.send("I could not understand that argument. Mention a member or provide a valid user ID.")
    if isinstance(error, discord.Forbidden):
        return await ctx.send("I do not have permission, or my role is below the target member's role.")
    await ctx.send(f"Command failed: {str(error)[:250]}")

@tree.command(name="blacklist", description="Blacklist a user and create a moderation case.")
@app_commands.describe(user="User to blacklist", reason="Reason for the blacklist")
async def blacklist(interaction: discord.Interaction, user: discord.User, reason: app_commands.Range[str, 1, 500]):
    if not interaction.guild or not is_staff(interaction):
        return await reply_private(interaction, "You do not have permission to use this command.")
    key = f"{interaction.guild.id}:{user.id}"
    if key in state["blacklists"]:
        return await reply_private(interaction, f"Already blacklisted as case #{state['blacklists'][key]['caseId']}.")
    case_id = state["nextCaseId"]
    state["nextCaseId"] += 1
    record = {"guildId": str(interaction.guild.id), "userId": str(user.id), "reason": reason,
              "moderatorId": str(interaction.user.id), "caseId": case_id, "status": "blacklisted", "createdAt": now()}
    state["blacklists"][key] = record
    state["blacklistHistory"].append(record.copy())
    role_id = guild_config(interaction.guild.id).get("blacklistRoleId")
    member = interaction.guild.get_member(user.id)
    if role_id and member:
        role = interaction.guild.get_role(int(role_id))
        if role:
            await member.add_roles(role, reason=f"Blacklist case #{case_id}")
    save_state()
    embed = discord.Embed(title="User Blacklisted", description=f"**Blacklisted by**\n{interaction.user.mention}\n\n**Offender**\n{user.mention}\n\n**Reason**\n{reason}", timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Case #{case_id}")
    await reply_private(interaction, f"{user.mention} has been blacklisted. Case #{case_id} forwarded to logs.")
    await send_log(interaction.guild, embed)

@tree.command(name="unblacklist", description="Remove a user from the active blacklist and keep history.")
async def unblacklist(interaction: discord.Interaction, user: discord.User):
    if not interaction.guild or not is_staff(interaction):
        return await reply_private(interaction, "You do not have permission to use this command.")
    key = f"{interaction.guild.id}:{user.id}"
    record = state["blacklists"].pop(key, None)
    if not record:
        return await reply_private(interaction, "That user is not blacklisted.")
    record.update({"status": "unblacklisted", "unblacklistedAt": now(), "unblacklistedBy": str(interaction.user.id)})
    state["blacklistHistory"].append(record.copy())
    role_id = guild_config(interaction.guild.id).get("blacklistRoleId")
    member = interaction.guild.get_member(user.id)
    role = interaction.guild.get_role(int(role_id)) if role_id else None
    if member and role:
        await member.remove_roles(role, reason="User unblacklisted")
    save_state()
    embed = discord.Embed(title="User Unblacklisted", description=f"**Unblacklisted by**\n{interaction.user.mention}\n\n**User**\n{user.mention}\n\n**Original reason**\n{record['reason']}", timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Original Case #{record['caseId']}")
    await reply_private(interaction, f"{user.mention} has been unblacklisted. Original case: #{record['caseId']}.")
    await send_log(interaction.guild, embed)

@tree.command(name="blcheck", description="Check whether a user is currently blacklisted.")
async def blcheck(interaction: discord.Interaction, user: discord.User):
    if not interaction.guild:
        return await reply_private(interaction, "Use this command in a server.")
    record = state["blacklists"].get(f"{interaction.guild.id}:{user.id}")
    if not record:
        return await reply_private(interaction, f"{user.mention} is **not blacklisted**.")
    embed = discord.Embed(title="Blacklist Check", description=f"**User**\n{user.mention}\n\n**Status**\nBlacklisted\n\n**Reason**\n{record['reason']}")
    embed.set_footer(text=f"Case #{record['caseId']}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

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
async def embed_say(interaction: discord.Interaction, title: app_commands.Range[str, 1, 256], description: app_commands.Range[str, 1, 4000], color: str = "#5865F2", channel: discord.TextChannel | None = None):
    if not interaction.user.guild_permissions.manage_messages:
        return await reply_private(interaction, "You need Manage Messages.")
    raw = color.lstrip("#")
    try:
        value = int(raw, 16) if len(raw) == 6 else 0x5865F2
    except ValueError:
        value = 0x5865F2
    embed = discord.Embed(title=title, description=description, color=value, timestamp=datetime.now(timezone.utc))
    await (channel or interaction.channel).send(embed=embed)
    await reply_private(interaction, "Embed sent.")

@tree.command(name="stage", description="Give a tryout stage role to a user.")
@app_commands.describe(user="Player who passed", stage="Stage number", power="Stage power", rank="Stage rank")
async def stage(interaction: discord.Interaction, user: discord.Member, stage: app_commands.Range[int, 1, 100], power: app_commands.Range[str, 1, 50], rank: app_commands.Range[str, 1, 50]):
    cfg = guild_config(interaction.guild.id) if interaction.guild else {}
    host_role = cfg.get("tryoutHostRoleId")
    allowed = is_admin(interaction) or (host_role and isinstance(interaction.user, discord.Member) and any(r.id == int(host_role) for r in interaction.user.roles))
    if not interaction.guild or not allowed:
        return await reply_private(interaction, "Only configured tryout hosts can use /stage.")
    role_name = f"Stage {stage} • {power.strip()} • {rank.strip()}"[:100]
    role = discord.utils.get(interaction.guild.roles, name=role_name)
    if role is None:
        role = await interaction.guild.create_role(name=role_name, reason=f"Stage role created by {interaction.user}")
    if role >= interaction.guild.me.top_role:
        return await reply_private(interaction, "I cannot assign that role because it is above my highest role.")
    await user.add_roles(role)
    state["stages"].append({"guildId": str(interaction.guild.id), "userId": str(user.id), "stage": stage, "power": power, "rank": rank, "roleId": str(role.id), "hostId": str(interaction.user.id), "createdAt": now()})
    save_state()
    embed = discord.Embed(title="Stage Passed", description=f"**Player**\n{user.mention}\n\n**Stage**\n{stage}\n\n**Stage Power**\n{power}\n\n**Stage Rank**\n{rank}\n\n**Tryout Host**\n{interaction.user.mention}", timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Role", value=role.mention)
    await interaction.response.send_message(embed=embed)
    await send_log(interaction.guild, embed)

config_group = app_commands.Group(name="config", description="Configure the moderation bot.")

async def config_guard(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not interaction.user.guild_permissions.manage_guild:
        await reply_private(interaction, "You need Manage Server.")
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

@config_group.command(name="blacklist-role", description="Set the blacklist role.")
async def config_blacklist_role(interaction: discord.Interaction, role: discord.Role):
    if not await config_guard(interaction): return
    guild_config(interaction.guild.id)["blacklistRoleId"] = str(role.id); save_state()
    await reply_private(interaction, f"Blacklist role set to {role.mention}.")

@config_group.command(name="tryout-role", description="Set the role allowed to use /stage.")
async def config_tryout_role(interaction: discord.Interaction, role: discord.Role):
    if not await config_guard(interaction): return
    guild_config(interaction.guild.id)["tryoutHostRoleId"] = str(role.id); save_state()
    await reply_private(interaction, f"Tryout host role set to {role.mention}.")

@config_group.command(name="clan-register-logs", description="Set the clan registration review channel.")
async def config_clan_logs(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await config_guard(interaction): return
    guild_config(interaction.guild.id)["clanRegistrationChannelId"] = str(channel.id); save_state()
    await reply_private(interaction, f"Clan registration logs set to {channel.mention}.")

@config_group.command(name="clan-news", description="Set the channel for clan-main announcements.")
async def config_clan_news(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await config_guard(interaction): return
    guild_config(interaction.guild.id)["clanNewsChannelId"] = str(channel.id); save_state()
    await reply_private(interaction, f"Clan news channel set to {channel.mention}.")

@config_group.command(name="questions", description="Set up to five DM application questions separated by |.")
async def config_questions(interaction: discord.Interaction, questions: app_commands.Range[str, 1, 1000]):
    if not await config_guard(interaction): return
    qs = [q.strip() for q in questions.split("|") if q.strip()][:5]
    guild_config(interaction.guild.id)["applicationQuestions"] = qs; save_state()
    await reply_private(interaction, f"Saved {len(qs)} application question(s).")

tree.add_command(config_group)

@tree.command(name="config-show", description="Show the current bot configuration.")
async def config_show(interaction: discord.Interaction):
    if not await config_guard(interaction): return
    c = guild_config(interaction.guild.id)
    lines = [f"**{label}:** {f'<#{c[key]}>' if c.get(key) else 'Not configured'}" for label, key in [("Logs", "logChannelId"), ("Applications", "applicationChannelId"), ("Clan registration logs", "clanRegistrationChannelId"), ("Clan news", "clanNewsChannelId")]]
    lines += [f"**{label}:** {f'<@&{c[key]}>' if c.get(key) else 'Not configured'}" for label, key in [("Staff role", "staffRoleId"), ("Blacklist role", "blacklistRoleId"), ("Tryout host role", "tryoutHostRoleId")]]
    lines.append("**Questions:** " + (" | ".join(c.get("applicationQuestions", [])) or "Not configured"))
    await interaction.response.send_message(embed=discord.Embed(title="Bot Configuration", description="\n".join(lines)), ephemeral=True)

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
        embed = discord.Embed(title="New Application", description=f"Applicant: {interaction.user.mention}\nID: `{app_id}`", timestamp=datetime.now(timezone.utc))
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
    embed = discord.Embed(title="New Clan Registration", description=f"**Owner**\n{interaction.user.mention}\n\n**Clan**\n{target.name}\n\n**Invite**\n{record['serverInvite']}\n\n**Registration ID**\n`{reg_id}`", timestamp=datetime.now(timezone.utc))
    await channel.send(embed=embed, view=ReviewView("clan", reg_id))
    await dm.send("Your clan registration has been submitted. Staff will review it.")

@tree.command(name="main", description="Main a registered clan using its one-time secret code.")
@app_commands.describe(code="The six-digit secret clan code")
async def main(interaction: discord.Interaction, code: app_commands.Range[str, 6, 6]):
    if not interaction.guild:
        return await reply_private(interaction, "Use this command in the main server.")
    record = next((r for r in state["clanRegistrations"].values() if r.get("guildId") == str(interaction.guild.id) and r.get("status") == "accepted" and r.get("clanCode") == code), None)
    if not record:
        return await reply_private(interaction, "That clan code is invalid or is not registered in this server.")
    if record.get("mainUserId"):
        return await reply_private(interaction, "This clan has already been mained. The code can only be used once.")
    record["mainUserId"] = str(interaction.user.id)
    record["mainedAt"] = now()
    state["clanMains"].append({"id": f"{record['id']}-{interaction.user.id}", "guildId": str(interaction.guild.id), "clanGuildId": record["clanGuildId"], "clanName": record["clanName"], "clanLeaderId": record["userId"], "userId": str(interaction.user.id), "createdAt": record["mainedAt"]})
    save_state()
    news_id = guild_config(interaction.guild.id).get("clanNewsChannelId")
    if news_id:
        channel = interaction.guild.get_channel(int(news_id))
        if isinstance(channel, discord.TextChannel):
            await channel.send(embed=discord.Embed(title="Clan Mained", description=f"{interaction.user.mention} has mained **{record['clanName']}**.\n\n**Clan Leader:** <@{record['userId']}>", timestamp=datetime.now(timezone.utc)))
    await reply_private(interaction, f"You have successfully mained **{record['clanName']}**. The code has been used.")

@bot.event
async def on_ready():
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
    for kind, bucket in (("app", "applications"), ("clan", "clanRegistrations")):
        for record_id, record in state[bucket].items():
            if record.get("status") == "pending":
                bot.add_view(ReviewView(kind, record_id))

async def main_async():
    global state
    state = load_state()
    try:
        await bot.start(TOKEN)
    finally:
        save_state()
        mongo.close()

if __name__ == "__main__":
    asyncio.run(main_async())
