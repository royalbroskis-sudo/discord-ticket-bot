"""
cogs/ai_chat.py

Conversational AI in Discord.
"""

import discord
from discord.ext import commands
from datetime import datetime, timezone

import ai_agent
from app import _discord_api
from cogs.config import get_guild_config, member_has_role_id
from personality import PERSONALITY

MAX_HISTORY_TURNS = 6
COOLDOWN_SECONDS = 4
DISCORD_CHUNK = 1900

SYSTEM_PROMPT_TEMPLATE = PERSONALITY + "\n\n" + (
    "The above is tone only — the rules below always win if they ever pull in "
    "different directions.\n\n"
    "You are {bot_name}, chatting in the Discord server \"{guild_name}\". Keep "
    "replies conversational and fairly short (a few sentences, unless the "
    "person clearly wants something longer or more detailed). You are NOT a "
    "moderation tool in this conversation and cannot mute, kick, ban, or "
    "change anything on the server — if someone asks you to do that, tell "
    "them to use the actual mod commands or the dashboard instead. Never "
    "claim to have taken an action you didn't actually take."
)


class AIChat(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._history: dict[int, list[dict]] = {}
        self._last_reply_at: dict[int, datetime] = {}

    def _get_history(self, channel_id: int) -> list[dict]:
        return self._history.setdefault(channel_id, [])

    def _trim_history(self, channel_id: int):
        hist = self._history.get(channel_id, [])
        limit = MAX_HISTORY_TURNS * 2
        if len(hist) > limit:
            self._history[channel_id] = hist[-limit:]

    def _on_cooldown(self, user_id: int) -> bool:
        now = datetime.now(timezone.utc)
        last = self._last_reply_at.get(user_id)
        if last and (now - last).total_seconds() < COOLDOWN_SECONDS:
            return True
        self._last_reply_at[user_id] = now
        return False

    async def _is_reply_to_bot(self, message: discord.Message) -> bool:
        ref = message.reference
        if not ref:
            return False
        if ref.resolved and isinstance(ref.resolved, discord.Message):
            return ref.resolved.author.id == self.bot.user.id
        try:
            ref_msg = await message.channel.fetch_message(ref.message_id)
            return ref_msg.author.id == self.bot.user.id
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False

    def _is_trusted(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        if self.bot.db is None:
            return False
        cfg = get_guild_config(self.bot.db, member.guild.id)
        return member_has_role_id(member, cfg.get("TRUSTED_STAFF_ROLE_ID"))

    def _log_action(self, guild_id: int, actor: discord.Member):
        def _log(tool_name, args, ok, error, detail):
            if self.bot.db is None:
                return
            target = str(args.get("user_id") or args.get("channel_id") or "")
            try:
                self.bot.db["console_actions"].insert_one({
                    "guild_id": guild_id,
                    "action": f"aichat_{tool_name}",
                    "target_id": target,
                    "detail": detail,
                    "ok": ok,
                    "error": error,
                    "actor_id": str(actor.id),
                    "actor_name": str(actor),
                    "timestamp": datetime.utcnow(),
                })
            except Exception as e:
                print(f"❌ AIChat: failed to log console action: {e}")
        return _log

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        is_mentioned = self.bot.user in message.mentions and not message.mention_everyone
        is_reply_to_bot = await self._is_reply_to_bot(message)

        if not is_mentioned and not is_reply_to_bot:
            return

        if not ai_agent.GEMINI_API_KEY:
            return

        if self._on_cooldown(message.author.id):
            return

        content = message.content
        for m in message.mentions:
            content = content.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
        content = content.strip() or "(no text, just a mention)"

        channel_id = message.channel.id
        history = self._get_history(channel_id)
        trusted = self._is_trusted(message.author)

        try:
            async with message.channel.typing():
                if trusted:
                    # Pass BOTH the display name AND the user ID
                    # The actor_name is used for display/audit logs
                    # The discord_id is used for the MC bot payment system
                    result = await self.bot.loop.run_in_executor(
                        None,
                        lambda: ai_agent.run_agent_turn(
                            message.guild.id,
                            history,
                            f"{message.author.display_name} (trusted staff): {content}",
                            _discord_api,
                            self.bot.db,
                            auto_execute=True,
                            log_action=self._log_action(message.guild.id, message.author),
                            actor_name=str(message.author),  # Display name for logs
                            discord_id=str(message.author.id),  # <-- ADD THIS: numeric ID for MC bot
                            bot=self.bot,
                        ),
                    )
                    reply = result["reply"]
                else:
                    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                        bot_name=self.bot.user.display_name, guild_name=message.guild.name
                    )
                    messages = (
                        [{"role": "system", "content": system_prompt}]
                        + history
                        + [{"role": "user", "content": f"{message.author.display_name}: {content}"}]
                    )
                    reply = await self.bot.loop.run_in_executor(None, ai_agent.simple_chat, messages)
        except Exception as e:
            print(f"❌ AIChat: Gemini call failed: {e}")
            try:
                await message.reply(
                    "⚠️ Something went wrong talking to the AI just now — try again in a few seconds.",
                    mention_author=False,
                )
            except discord.HTTPException:
                pass
            return

        history.append({"role": "user", "content": f"{message.author.display_name}: {content}"})
        history.append({"role": "assistant", "content": reply})
        self._trim_history(channel_id)

        for start in range(0, len(reply), DISCORD_CHUNK):
            chunk = reply[start:start + DISCORD_CHUNK]
            try:
                await message.reply(chunk, mention_author=False)
            except discord.HTTPException as e:
                print(f"❌ AIChat: failed to send reply: {e}")
                break


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChat(bot))