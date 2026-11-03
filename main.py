import os
import re
import asyncio
import secrets
from datetime import datetime, timezone
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
    "nextCaseId": 1, "guilds": {}, "blacklists": {}, "blacklistHistory": [],
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
bot = commands.Bot(command_prefix="!", intents=intents)
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
