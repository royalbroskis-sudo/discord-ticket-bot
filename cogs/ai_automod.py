"""
cogs/ai_automod.py

Autonomous AI-moderated chat monitoring. Unlike cogs/automod.py (a static
keyword/link/invite blocklist, opt-in per channel via /automod addchannel),
this watches EVERY channel in a guild and asks Gemini to judge each message
directly — no per-channel opt-in, no human confirmation step, and no ban
capability (the model can only choose none/warn/timeout; banning stays a
manual, explicit action via chat or the dashboard).

Off by default per guild — turn on with /aimod enable.

IMPORTANT rate-limit note: this calls the Gemini API on every non-empty
message in every channel of every guild that has it enabled. Gemini's free
tier is roughly 30 requests/minute and ~1,000-1,500/day depending on model
— an active server will burn through that fast, and once it's exhausted,
autonomous moderation just stops working until quota resets. When a call
fails (quota, network, bad response) this cog fails OPEN — it never guesses,
it just skips moderating that one message — and pauses all AI AutoMod calls
for a short cooldown instead of retrying on every subsequent message, which
would otherwise turn one rate-limit hit into hundreds of wasted calls.
"""

import json
import re
import time
import logging
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands
from discord import app_commands

import ai_agent
from cogs.config import admin_only, get_guild_config, member_has_role_id

logger = logging.getLogger(__name__)

FAILURE_COOLDOWN_SECONDS = 30  # after an API failure, stop trying for this long

MODERATION_SYSTEM_PROMPT = """You are an automated moderation classifier for a Discord server. You are given one message's text. Decide whether it breaks ordinary server conduct rules: harassment, slurs, hate speech, explicit sexual content, threats, or profanity clearly used to attack/insult someone. Casual swearing that isn't directed at anyone is normal on most Discord servers and is NOT a violation by itself.

Respond with ONLY a JSON object and nothing else — no markdown fences, no explanation outside the JSON:
{"action": "none" | "warn" | "timeout", "timeout_minutes": <int, 5-1440, only include if action is "timeout">, "reason": "<one short sentence>"}

Guidance:
- "none": message is fine, or borderline/casual language not directed at anyone.
- "warn": breaks the rules but is minor — a one-off insult, mild harassment, a casual slur without clear malicious intent.
- "timeout": a clear, serious violation — targeted harassment, slurs directed at someone, hate speech, explicit threats, sexual content.
You cannot ban. That action does not exist for you — never output anything other than none/warn/timeout."""


class AIAutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self._paused_until = 0.0  # time.time() value; skip AI calls until then

    aimod_group = app_commands.Group(name="aimod", description="Configure autonomous AI moderation")

    # ---------------------------------------------------------------------
    # Settings
    # ---------------------------------------------------------------------

    def _is_enabled(self, guild_id: int) -> bool:
        settings = self.db["ai_automod_settings"].find_one({"guild_id": guild_id})
        return bool(settings and settings.get("enabled"))

    def get_log_channel(self, guild_id: int):
        # Same log_settings collection/channel as cogs/logging.py, on purpose
        # — one log channel per server, not a second one to configure.
        settings = self.db["log_settings"].find_one({"guild_id": guild_id})
        if settings:
            return self.bot.get_channel(settings.get("channel_id"))
        return None

    @aimod_group.command(name="enable", description="Turn on autonomous AI moderation (watches every channel, warns/times out on its own)")
    @admin_only()
    async def enable(self, interaction: discord.Interaction):
        self.db["ai_automod_settings"].update_one(
            {"guild_id": interaction.guild.id}, {"$set": {"enabled": True}}, upsert=True
        )
        await interaction.response.send_message(
            "✅ AI AutoMod is now **on**. It watches every channel, warns or times out on its own for clear "
            "violations, logs every action, and never bans on its own. Set a log channel with `/setlogchannel` "
            "if you haven't already — that's where its actions get reported."
        )

    @aimod_group.command(name="disable", description="Turn off autonomous AI moderation")
    @admin_only()
    async def disable(self, interaction: discord.Interaction):
        self.db["ai_automod_settings"].update_one(
            {"guild_id": interaction.guild.id}, {"$set": {"enabled": False}}, upsert=True
        )
        await interaction.response.send_message("✅ AI AutoMod is now **off**.")

    @aimod_group.command(name="status", description="Check whether AI AutoMod is currently on")
    @admin_only()
    async def status(self, interaction: discord.Interaction):
        state = "🟢 ON" if self._is_enabled(interaction.guild.id) else "🔴 OFF"
        await interaction.response.send_message(f"AI AutoMod is currently **{state}**.", ephemeral=True)

    # ---------------------------------------------------------------------
    # The listener
    # ---------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not message.content.strip():
            return

        if not self._is_enabled(message.guild.id):
            return

        # Same staff/admin exemption cogs/automod.py already uses.
        cfg = get_guild_config(self.db, message.guild.id)
        if member_has_role_id(message.author, cfg.get("STAFF_ROLE_ID")) or message.author.guild_permissions.administrator:
            return

        if time.time() < self._paused_until:
            return  # backing off after a recent API failure — don't pile on more wasted calls

        decision = await self._classify(message.content)
        if decision is None or decision.get("action") == "none":
            return

        await self._act_on(message, decision)

    async def _classify(self, content: str) -> dict | None:
        """Asks Gemini to judge one message. Returns a dict with 'action'
        (none/warn/timeout) and optional 'timeout_minutes'/'reason', or
        None if the call failed or came back unparseable — callers must
        treat None the same as 'none' (fail open, never guess)."""
        messages = [
            {"role": "system", "content": MODERATION_SYSTEM_PROMPT},
            {"role": "user", "content": content[:2000]},
        ]
        try:
            data = await self.bot.loop.run_in_executor(
                None, lambda: ai_agent._call_llm(messages, temperature=0, max_tokens=150)
            )
            text = data["choices"][0]["message"]["content"] or ""
        except Exception as e:
            logger.warning(f"AIAutoMod: classification call failed, backing off {FAILURE_COOLDOWN_SECONDS}s: {e}")
            self._paused_until = time.time() + FAILURE_COOLDOWN_SECONDS
            return None

        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        try:
            decision = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"AIAutoMod: couldn't parse classifier response, skipping: {text[:200]!r}")
            return None

        if decision.get("action") not in ("none", "warn", "timeout"):
            return None
        return decision

    async def _act_on(self, message: discord.Message, decision: dict):
        action = decision["action"]
        reason = str(decision.get("reason") or "AI AutoMod violation")[:200]
        member = message.author

        entry = {
            "reason": f"AI AutoMod: {reason}",
            "mod": str(self.bot.user),
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }
        collection = self.db["warnings"]
        doc = collection.find_one({"guild_id": message.guild.id, "user_id": member.id})
        current = doc.get("warnings", []) if doc else []
        current.append(entry)
        collection.update_one(
            {"guild_id": message.guild.id, "user_id": member.id},
            {"$set": {"warnings": current}},
            upsert=True,
        )
        # Keep the Moderation cog's in-memory cache in sync, same as automod.py does
        mod_cog = self.bot.get_cog("Moderation")
        if mod_cog and hasattr(mod_cog, "_warnings"):
            mod_cog._warnings[message.guild.id][member.id].append(entry)

        timeout_minutes = None
        timeout_ok = True
        if action == "timeout":
            timeout_minutes = max(5, min(int(decision.get("timeout_minutes") or 10), 1440))
            try:
                await member.timeout(timedelta(minutes=timeout_minutes), reason=reason)
            except discord.HTTPException as e:
                timeout_ok = False
                logger.warning(f"AIAutoMod: timeout failed for {member.id}: {e}")

        try:
            note = f"⚠️ {member.mention}, you've been warned by AI AutoMod: **{reason}**"
            if action == "timeout" and timeout_ok:
                note += f" (timed out {timeout_minutes}m)"
            await message.channel.send(note, delete_after=8)
        except discord.HTTPException:
            pass

        await self._log(message, action, reason, timeout_minutes if timeout_ok else None)

    async def _log(self, message: discord.Message, action: str, reason: str, timeout_minutes):
        log_channel = self.get_log_channel(message.guild.id)
        if not log_channel:
            return

        title = "⏳ AI AutoMod: Timeout" if action == "timeout" else "⚠️ AI AutoMod: Warn"
        color = discord.Color.orange() if action == "timeout" else discord.Color.gold()
        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Member", value=f"{message.author.mention} (`{message.author.id}`)", inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        if action == "timeout":
            duration_text = f"{timeout_minutes} minute(s)" if timeout_minutes else "failed — check bot's Moderate Members permission/role position"
            embed.add_field(name="Duration", value=duration_text, inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        content = message.content if len(message.content) <= 1000 else message.content[:997] + "..."
        embed.add_field(name="Message", value=content or "*empty*", inline=False)
        embed.set_footer(text=f"Message ID: {message.id}")

        try:
            await log_channel.send(embed=embed)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AIAutoMod(bot))
