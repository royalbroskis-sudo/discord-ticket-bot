# cogs/strikes.py — member strike/warning tracking system.
#
# /strike add    — issue a strike to a member
# /strike remove — revoke a strike (most recent active one, or a specific
#                  strike ID)
# /strikes       — check a member's active + all-time strike count/history
#
# Permissions: honors whatever roles are configured for these commands on
# the dashboard's Command Permissions page (commands.html -> command_perms
# collection, same convention every other cog uses). If nothing's
# configured there, falls back to the guild's configured Mod/Staff role
# (bot_config.MOD_ROLE / STAFF_ROLE) so it's usable out of the box.

import uuid
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone

from cogs.config import get_guild_config


# ── Permission check (dashboard-configurable, same convention as utilities.py) ──

async def check_strike_perm(interaction: discord.Interaction, command_name: str) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True

    db = interaction.client.db
    if db is None:
        return True

    doc = db["command_perms"].find_one({"guild_id": interaction.guild.id, "command_name": command_name})
    if doc and doc.get("roles"):
        user_role_ids = {r.id for r in interaction.user.roles}
        allowed_ids = {int(r) for r in doc["roles"]}
        if user_role_ids.intersection(allowed_ids):
            return True
        await interaction.response.send_message(
            f"❌ You don't have permission to use `/{command_name}`.", ephemeral=True
        )
        return False

    # No dashboard override configured -> fall back to Mod/Staff role.
    cfg = get_guild_config(db, interaction.guild.id)
    fallback_role_id = cfg.get("MOD_ROLE") or cfg.get("STAFF_ROLE")
    fallback_role = interaction.guild.get_role(fallback_role_id) if fallback_role_id else None
    if fallback_role and fallback_role in interaction.user.roles:
        return True

    await interaction.response.send_message(
        f"❌ You don't have permission to use `/{command_name}`.", ephemeral=True
    )
    return False


def get_strike_counts(db, guild_id: int, member_id: int) -> tuple[int, int]:
    """Returns (active_count, total_count_all_time)."""
    total = db["strikes"].count_documents({"guild_id": guild_id, "member_id": member_id})
    active = db["strikes"].count_documents({"guild_id": guild_id, "member_id": member_id, "active": True})
    return active, total


class Strikes(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    strike_group = app_commands.Group(name="strike", description="Manage member strikes")

    # /strike add
    @strike_group.command(name="add", description="Add a strike to a member")
    @app_commands.describe(member="The member to strike", reason="Why they're being struck")
    async def strike_add(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if not await check_strike_perm(interaction, "strike add"):
            return

        db = interaction.client.db
        strike_id = uuid.uuid4().hex[:6].upper()
        now = datetime.now(timezone.utc)

        db["strikes"].insert_one({
            "guild_id": interaction.guild.id,
            "member_id": member.id,
            "strike_id": strike_id,
            "reason": reason,
            "issued_by": interaction.user.id,
            "active": True,
            "created_at": now,
        })

        active_count, total_count = get_strike_counts(db, interaction.guild.id, member.id)

        embed = discord.Embed(title="⚠️ Strike Added", color=0xf1c40f, timestamp=now)
        embed.add_field(name="👤 Member", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="🆔 Strike ID", value=f"`{strike_id}`", inline=False)
        embed.add_field(name="📝 Reason", value=reason, inline=False)
        embed.add_field(name="🔴 Active Strikes", value=str(active_count), inline=True)
        embed.add_field(name="📋 Total Strikes (all time)", value=str(total_count), inline=True)
        if active_count >= 2:
            embed.add_field(name="\u200b", value=f"⚠️ This member now has **{active_count}** active strikes.", inline=False)
        embed.add_field(name="🛡️ Issued By", value=interaction.user.mention, inline=False)

        await interaction.response.send_message(embed=embed)

        try:
            await member.send(
                f"⚠️ You received a strike in **{interaction.guild.name}**.\n"
                f"**Reason:** {reason}\n**Strike ID:** `{strike_id}`\n"
                f"You now have **{active_count}** active strike(s)."
            )
        except discord.Forbidden:
            pass

    # /strike remove
    @strike_group.command(name="remove", description="Remove a strike from a member")
    @app_commands.describe(member="The member to remove a strike from",
                            strike_id="Specific strike ID to remove (leave blank to remove their most recent active strike)")
    async def strike_remove(self, interaction: discord.Interaction, member: discord.Member, strike_id: str = None):
        if not await check_strike_perm(interaction, "strike remove"):
            return

        db = interaction.client.db
        query = {"guild_id": interaction.guild.id, "member_id": member.id, "active": True}
        if strike_id:
            query["strike_id"] = strike_id.strip().upper()
            strike = db["strikes"].find_one(query)
        else:
            strike = db["strikes"].find_one(query, sort=[("created_at", -1)])

        if not strike:
            msg = f"❌ No active strike found with ID `{strike_id}` for {member.mention}." if strike_id \
                else f"❌ {member.mention} has no active strikes to remove."
            return await interaction.response.send_message(msg, ephemeral=True)

        db["strikes"].update_one(
            {"_id": strike["_id"]},
            {"$set": {"active": False, "removed_by": interaction.user.id, "removed_at": datetime.now(timezone.utc)}},
        )

        active_count, total_count = get_strike_counts(db, interaction.guild.id, member.id)

        embed = discord.Embed(title="✅ Strike Removed", color=0x2ecc71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="👤 Member", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="🆔 Strike ID", value=f"`{strike['strike_id']}`", inline=False)
        embed.add_field(name="📝 Original Reason", value=strike.get("reason", "*No reason*"), inline=False)
        embed.add_field(name="🔴 Active Strikes", value=str(active_count), inline=True)
        embed.add_field(name="📋 Total Strikes (all time)", value=str(total_count), inline=True)
        embed.add_field(name="🛡️ Removed By", value=interaction.user.mention, inline=False)

        await interaction.response.send_message(embed=embed)

    # /strikes — check a member's strikes
    @app_commands.command(name="strikes", description="Check a member's strikes")
    @app_commands.describe(member="The member to check")
    async def strikes(self, interaction: discord.Interaction, member: discord.Member):
        if not await check_strike_perm(interaction, "strikes"):
            return

        db = interaction.client.db
        active_count, total_count = get_strike_counts(db, interaction.guild.id, member.id)
        active_strikes = list(db["strikes"].find(
            {"guild_id": interaction.guild.id, "member_id": member.id, "active": True}
        ).sort("created_at", -1))

        embed = discord.Embed(title="📋 Strike History", color=0x5865F2, timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="👤 Member", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="🔴 Active Strikes", value=str(active_count), inline=True)
        embed.add_field(name="📋 Total Strikes (all time)", value=str(total_count), inline=True)

        if active_strikes:
            lines = []
            for s in active_strikes[:10]:
                issued = f"<@{s['issued_by']}>"
                ts = int(s["created_at"].replace(tzinfo=timezone.utc).timestamp()) if s.get("created_at") else None
                when = f" • <t:{ts}:R>" if ts else ""
                lines.append(f"`{s['strike_id']}` — {s['reason']} (by {issued}{when})")
            embed.add_field(name="Active Strike Details", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Active Strike Details", value="No active strikes. ✅", inline=False)

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Strikes(bot))
