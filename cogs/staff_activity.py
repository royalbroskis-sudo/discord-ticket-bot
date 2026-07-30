# cogs/staff_activity.py — periodic staff activity checks.
#
# Every INTERVAL_DAYS, posts an embed in CHANNEL_ID asking everyone with
# CHECK_ROLE_ID to react with EMOJI within WINDOW_HOURS. When the window
# closes, posts a results embed listing who didn't react.
#
# Everything (role to check, channel, interval, emoji, window) is configured
# on the dashboard (settings.html -> "Staff Activity" tab / staff_activity_config
# collection) — nothing is hardcoded. All check state lives in Mongo
# ("staff_activity_checks"), so a bot restart never loses an in-progress
# check or misses closing one that was already due.

import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta, timezone

from cogs.config import admin_only

DEFAULT_EMOJI = "💎"
DEFAULT_INTERVAL_DAYS = 2
DEFAULT_WINDOW_HOURS = 24


def get_config(db, guild_id: int) -> dict | None:
    if db is None:
        return None
    return db["staff_activity_config"].find_one({"guild_id": guild_id})


class StaffActivity(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.loop_task.start()

    def cog_unload(self):
        self.loop_task.cancel()

    # ── Open a new check ─────────────────────────────────────────────────
    async def open_check(self, guild: discord.Guild, cfg: dict):
        db = self.bot.db
        role = guild.get_role(cfg["CHECK_ROLE_ID"])
        channel = guild.get_channel(cfg["CHANNEL_ID"])
        if not role or not channel:
            return

        emoji = cfg.get("EMOJI") or DEFAULT_EMOJI
        interval_days = cfg.get("INTERVAL_DAYS") or DEFAULT_INTERVAL_DAYS
        window_hours = cfg.get("WINDOW_HOURS") or DEFAULT_WINDOW_HOURS

        now = datetime.now(timezone.utc)
        deadline = now + timedelta(hours=window_hours)
        expected = [m.id for m in role.members if not m.bot]

        embed = discord.Embed(
            title=f"Staff Activity Check • Every {interval_days} Days",
            description=f"React with {emoji} within {window_hours} hours to confirm activity.",
            color=0x5865F2,
        )
        embed.add_field(name="Team", value=role.mention, inline=False)
        embed.add_field(name="Deadline", value=f"<t:{int(deadline.timestamp())}:F>", inline=True)
        embed.add_field(name="Check closes", value=f"<t:{int(deadline.timestamp())}:R>", inline=True)

        try:
            msg = await channel.send(content=role.mention, embed=embed)
            await msg.add_reaction(emoji)
        except discord.HTTPException as e:
            print(f"[staff_activity] Failed to post check in guild {guild.id}: {e}")
            return

        db["staff_activity_checks"].insert_one({
            "guild_id": guild.id,
            "channel_id": channel.id,
            "message_id": msg.id,
            "role_id": role.id,
            "emoji": emoji,
            "expected_reactors": expected,
            "opened_at": now,
            "deadline": deadline,
            "closed": False,
        })
        db["staff_activity_config"].update_one(
            {"guild_id": guild.id}, {"$set": {"last_opened_at": now}}, upsert=True
        )

    # ── Close a due check and post results ───────────────────────────────
    async def close_check(self, check: dict):
        db = self.bot.db
        guild = self.bot.get_guild(check["guild_id"])
        if not guild:
            db["staff_activity_checks"].update_one({"_id": check["_id"]}, {"$set": {"closed": True}})
            return

        channel = guild.get_channel(check["channel_id"])
        role = guild.get_role(check["role_id"])
        expected = set(check.get("expected_reactors", []))
        reacted = set()

        if channel:
            try:
                msg = await channel.fetch_message(check["message_id"])
                for reaction in msg.reactions:
                    if str(reaction.emoji) != check["emoji"]:
                        continue
                    async for user in reaction.users():
                        if not user.bot:
                            reacted.add(user.id)
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                print(f"[staff_activity] Failed to fetch check message: {e}")

        missing = expected - reacted
        matched_reacted = expected & reacted

        title = "✅ Staff Activity Check Results" if not missing else "❌ Staff Activity Check Results"
        embed = discord.Embed(title=title, color=0x2ecc71 if not missing else 0xe74c3c)
        if missing:
            mentions = "\n".join(f"<@{uid}>" for uid in missing)
            embed.description = f"Missing reactions from {len(missing)} member(s) with {role.mention if role else '@unknown role'}:\n\n{mentions}"
        else:
            embed.description = f"All members with {role.mention if role else '@unknown role'} reacted in time. 🎉"

        embed.add_field(name="Expected Reactors", value=str(len(expected)), inline=True)
        embed.add_field(name="Reacted", value=str(len(matched_reacted)), inline=True)
        embed.add_field(name="Missing", value=str(len(missing)), inline=True)

        if channel:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as e:
                print(f"[staff_activity] Failed to post results: {e}")

        db["staff_activity_checks"].update_one(
            {"_id": check["_id"]},
            {"$set": {"closed": True, "closed_at": datetime.now(timezone.utc),
                      "reacted": list(matched_reacted), "missing": list(missing)}},
        )

    # ── Background scheduler (restart-safe: everything is timestamp-driven from Mongo) ──
    @tasks.loop(minutes=5)
    async def loop_task(self):
        db = self.bot.db
        if db is None:
            return
        now = datetime.now(timezone.utc)

        # Close any checks whose deadline has passed.
        for check in db["staff_activity_checks"].find({"closed": False, "deadline": {"$lte": now}}):
            await self.close_check(check)

        # Open new checks for guilds that are due (no open check, and either
        # never run before or interval days have elapsed since last_opened_at).
        for cfg in db["staff_activity_config"].find({"ENABLED": True}):
            guild = self.bot.get_guild(cfg["guild_id"])
            if not guild or not cfg.get("CHECK_ROLE_ID") or not cfg.get("CHANNEL_ID"):
                continue

            has_open = db["staff_activity_checks"].find_one({"guild_id": guild.id, "closed": False})
            if has_open:
                continue

            interval_days = cfg.get("INTERVAL_DAYS") or DEFAULT_INTERVAL_DAYS
            last_opened_at = cfg.get("last_opened_at")
            due = (last_opened_at is None) or (now >= last_opened_at.replace(tzinfo=timezone.utc) + timedelta(days=interval_days))
            if due:
                await self.open_check(guild, cfg)

    @loop_task.before_loop
    async def before_loop_task(self):
        await self.bot.wait_until_ready()

    # ── Manual trigger ────────────────────────────────────────────────────
    @app_commands.command(name="activitycheck", description="Manually start a staff activity check now")
    @admin_only()
    async def activitycheck(self, interaction: discord.Interaction):
        cfg = get_config(interaction.client.db, interaction.guild.id)
        if not cfg or not cfg.get("CHECK_ROLE_ID") or not cfg.get("CHANNEL_ID"):
            return await interaction.response.send_message(
                "❌ Staff Activity isn't configured yet. Set the role and channel on the dashboard first.",
                ephemeral=True,
            )
        await interaction.response.send_message("✅ Starting a staff activity check now...", ephemeral=True)
        await self.open_check(interaction.guild, cfg)


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffActivity(bot))
