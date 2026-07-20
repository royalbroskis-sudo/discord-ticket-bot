"""
cogs/afk.py

AFK status tracking, with a visible nickname marker.

Setting AFK (via /afk or the AI agent's set_afk tool) prefixes the member's
current display name with "[AFK] " so it's visible everywhere (member list,
mentions, etc.), not just in-channel replies. Coming back — posting any
message, or having AI AutoMod/staff call clear_afk — restores the exact
nickname the member had before going AFK (including no nickname at all).

State lives in a module-level dict rather than on the cog instance so
cogs/ai_chat.py's trusted-staff AI agent (via ai_agent.py's set_afk/
clear_afk tools) and this cog's own /afk command and on_message listener
all read and write the exact same data — same pattern as
cogs.moderation._warnings.
"""

import discord
from discord.ext import commands
from discord import app_commands

# {guild_id: {user_id: {"reason": str, "original_nick": str | None}}}
_afk: dict[int, dict[int, dict]] = {}

AFK_PREFIX = "[AFK] "
NICK_MAX_LEN = 32  # Discord's hard cap on guild nicknames


def is_afk(guild_id: int, user_id: int) -> bool:
    return user_id in _afk.get(guild_id, {})


def get_afk(guild_id: int, user_id: int) -> dict | None:
    return _afk.get(guild_id, {}).get(user_id)


def set_afk_entry(guild_id: int, user_id: int, reason: str, original_nick: str | None):
    _afk.setdefault(guild_id, {})[user_id] = {"reason": reason, "original_nick": original_nick}


def clear_afk_entry(guild_id: int, user_id: int) -> dict | None:
    """Removes and returns the entry, or None if the member wasn't AFK."""
    guild_afk = _afk.get(guild_id)
    if not guild_afk:
        return None
    return guild_afk.pop(user_id, None)


def afk_nickname(base_name: str) -> str:
    """Builds the '[AFK] Name' nickname, trimmed to fit Discord's 32-char cap."""
    return (AFK_PREFIX + base_name)[:NICK_MAX_LEN]


class AFK(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="afk", description="Set your AFK status with an optional reason")
    @app_commands.describe(reason="Why you're going AFK")
    async def afk(self, interaction: discord.Interaction, reason: str = "No reason provided"):
        guild = interaction.guild
        member = interaction.user

        set_afk_entry(guild.id, member.id, reason, member.nick)

        new_nick = afk_nickname(member.display_name)
        try:
            await member.edit(nick=new_nick, reason=f"AFK: {reason}"[:512])
        except discord.Forbidden:
            # Missing Manage Nicknames, or target outranks the bot (e.g. the
            # server owner) — status is still tracked even if the nick can't
            # be changed.
            pass
        except discord.HTTPException:
            pass

        embed = discord.Embed(
            description=f"💤 {member.mention} is now AFK: **{reason}**",
            color=discord.Color.greyple()
        )
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id

        # If the author was AFK, clear their status and restore their nickname.
        entry = clear_afk_entry(guild_id, message.author.id)
        if entry is not None:
            try:
                await message.author.edit(
                    nick=entry["original_nick"], reason="No longer AFK"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            try:
                await message.channel.send(
                    f"✅ Welcome back {message.author.mention}! Your AFK status has been removed.",
                    delete_after=5
                )
            except discord.HTTPException:
                pass

        # Check if any mentioned user or replied-to user is AFK
        guild_afk = _afk.get(guild_id, {})
        afk_hits: list[tuple[discord.Member, str]] = []

        # Direct mentions
        for user in message.mentions:
            if user.id in guild_afk and user.id != message.author.id:
                afk_hits.append((user, guild_afk[user.id]["reason"]))

        # Reply reference
        if message.reference and message.reference.resolved:
            ref = message.reference.resolved
            if isinstance(ref, discord.Message):
                ref_author = ref.author
                if (
                    ref_author.id in guild_afk
                    and ref_author.id != message.author.id
                    and ref_author not in message.mentions  # avoid duplicates
                ):
                    afk_hits.append((ref_author, guild_afk[ref_author.id]["reason"]))

        for afk_user, reason in afk_hits:
            try:
                await message.channel.send(
                    f"💤 {afk_user.mention} is AFK: **{reason}**",
                    delete_after=5
                )
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AFK(bot))
