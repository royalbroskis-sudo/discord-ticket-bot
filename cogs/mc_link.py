"""
Per-user Minecraft account linking.

Each Discord user runs /link, signs into their OWN Microsoft account via the
device-code flow, and from then on can run /mc <command> to act as their own
linked Minecraft account. Nobody can see or drive anybody else's session —
every request to the mc-bot service is scoped to the caller's Discord ID.
"""

import os
import re
import asyncio
import logging
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands

from cogs.config import staff_only

logger = logging.getLogger(__name__)

MC_BOT_URL = os.getenv("MC_BOT_URL", "http://127.0.0.1:3001")

POLL_INTERVAL = 3
POLL_TIMEOUT = 300

STATUS_COLORS = {
    "ready": discord.Color.green(),
    "connecting": discord.Color.blue(),
    "awaiting_auth": discord.Color.gold(),
    "awaiting_discord_auth": discord.Color.purple(),
    "disconnected": discord.Color.greyple(),
    "error": discord.Color.red(),
}


def parse_time_to_ms(time_str: str):
    """Parse a time string like '1h', '30m', '2d' into milliseconds."""
    time_str = time_str.strip().lower()
    pattern = r'^(\d+)([smhd])$'
    match = re.match(pattern, time_str)
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)

    multipliers = {
        's': 1000,
        'm': 60 * 1000,
        'h': 60 * 60 * 1000,
        'd': 24 * 60 * 60 * 1000
    }

    return value * multipliers[unit]


class AuthorizedView(discord.ui.View):
    """Shown while we're waiting on the Discord-DM authorization step."""

    def __init__(self, cog: "MCLink", discord_id: str):
        super().__init__(timeout=POLL_TIMEOUT)
        self.cog = cog
        self.discord_id = discord_id

    @discord.ui.button(label="✅ I Authorized", style=discord.ButtonStyle.blurple)
    async def authorized(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.discord_id:
            await interaction.response.send_message("This isn't your link session.", ephemeral=True)
            return
        
        # Disable the button immediately to prevent double-clicks race conditions
        button.disabled = True
        await interaction.response.edit_message(view=self)
        
        await self.cog._post(f"/reconnect/{self.discord_id}")
        await self.cog._poll_until_settled(interaction, self.discord_id)


class MCLink(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    # ── mc-bot HTTP helpers ─────────────────────────────────────────────────
    async def _get(self, path: str) -> dict:
        try:
            async with self.session.get(f"{MC_BOT_URL}{path}", timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()
        except Exception as e:
            logger.warning(f"MC bot GET {path} failed: {e}")
            return {"status": "error", "error": f"MC bot unreachable: {e}"}

    async def _post(self, path: str, json: dict | None = None) -> dict:
        try:
            async with self.session.post(f"{MC_BOT_URL}{path}", json=json, timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()
        except Exception as e:
            logger.warning(f"MC bot POST {path} failed: {e}")
            return {"ok": False, "error": f"MC bot unreachable: {e}"}

    # ── UI helpers ───────────────────────────────────────────────────────────
    def _embed(self, status: dict) -> discord.Embed:
        s = status.get("status")
        embed = discord.Embed(color=STATUS_COLORS.get(s, discord.Color.greyple()))

        if s == "ready":
            embed.title = "✅ Linked & Connected"
            embed.description = f"Playing as **{status.get('mcUsername', 'unknown')}**"
        elif s == "awaiting_auth":
            embed.title = "🔑 Microsoft Sign-In Required"
            embed.description = (
                f"Go to **{status.get('url')}** and enter this code:\n\n"
                f"## `{status.get('code')}`\n\nThis message updates automatically once you sign in."
            )
        elif s == "awaiting_discord_auth":
            embed.title = "🔔 Discord Authorization Needed"
            embed.description = (
                "The server sent you a Discord DM asking you to authorize this login.\n"
                "Check your DMs, click **Authorize**, then press the button below."
            )
        elif s == "connecting":
            embed.title = "⏳ Connecting…"
        elif s == "error":
            embed.title = "❌ Error"
            embed.description = status.get("error", "Unknown error")
        else:
            embed.title = "⚪ Not Linked"
            embed.description = "Run `/link` to connect your own Minecraft account."
        return embed

    async def _poll_until_settled(self, interaction: discord.Interaction, discord_id: str):
        elapsed = 0
        last_status = None
        while elapsed < POLL_TIMEOUT:
            status = await self._get(f"/status/{discord_id}")
            if status.get("status") != last_status:
                view = AuthorizedView(self, discord_id) if status.get("status") == "awaiting_discord_auth" else None
                try:
                    await interaction.edit_original_response(embed=self._embed(status), view=view)
                except discord.HTTPException:
                    pass
                last_status = status.get("status")
            if status.get("status") in ("ready", "error"):
                return
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
        try:
            await interaction.edit_original_response(
                content="⏱️ Timed out waiting for login. Run `/link` again when you're ready.",
                embed=None,
                view=None,
            )
        except discord.HTTPException:
            pass

    # ── Commands ─────────────────────────────────────────────────────────────
    @app_commands.command(name="link", description="Link your own Minecraft (Microsoft) account to the bot.")
    @staff_only()
    async def link(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        discord_id = str(interaction.user.id)

        current = await self._get(f"/status/{discord_id}")
        if current.get("status") == "ready":
            await interaction.edit_original_response(embed=self._embed(current))
            return

        await self._post(f"/start-login/{discord_id}")
        await interaction.edit_original_response(embed=discord.Embed(title="⏳ Starting…"))
        await self._poll_until_settled(interaction, discord_id)

    @app_commands.command(name="unlink", description="Remove your linked Minecraft account (requires a fresh login next time).")
    @staff_only()
    async def unlink(self, interaction: discord.Interaction):
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)
        await self._post(f"/full-logout/{discord_id}")
        await interaction.followup.send("🗑️ Your Minecraft account has been unlinked.", ephemeral=True)

    @app_commands.command(name="mcstatus", description="Check your Minecraft link status.")
    @staff_only()
    async def mcstatus(self, interaction: discord.Interaction):
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)
        status = await self._get(f"/status/{discord_id}")
        await interaction.followup.send(embed=self._embed(status), ephemeral=True)

    @app_commands.command(name="mc", description="Run an in-game command as your own linked Minecraft account.")
    @app_commands.describe(command="The command to run, without a leading slash")
    @staff_only()
    async def mc(self, interaction: discord.Interaction, command: str):
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)
        result = await self._post(f"/run-command/{discord_id}", json={"command": command})
        if not result.get("ok"):
            await interaction.followup.send(f"❌ {result.get('error', 'Failed to run command')}", ephemeral=True)
            return
        output = result.get("output") or []
        text = "\n".join(output)[:1900] if output else "*(no output captured)*"
        await interaction.followup.send(f"```\n{text}\n```", ephemeral=True)

    @app_commands.command(name="leave", description="Disconnect your Minecraft account (keeps your link saved).")
    @staff_only()
    async def leave(self, interaction: discord.Interaction):
        """Leave the server but keep the link saved."""
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)
        result = await self._post(f"/logout/{discord_id}")
        if result.get("ok"):
            await interaction.followup.send("✅ Disconnected from Minecraft server. Your link is still saved.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ {result.get('error', 'Failed to disconnect')}", ephemeral=True)

    @app_commands.command(name="run", description="Schedule a command to run repeatedly.")
    @app_commands.describe(
        command="The command to run (e.g., /home 2)",
        interval="How often to run it (e.g., 30m, 1h, 2d)"
    )
    @staff_only()
    async def run(self, interaction: discord.Interaction, command: str, interval: str):
        """Schedule a command to run repeatedly at the specified interval."""
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)

        interval_ms = parse_time_to_ms(interval)
        if interval_ms is None:
            await interaction.followup.send(
                "❌ Invalid interval format. Use: `30s`, `5m`, `1h`, `2d` (minimum 60s)",
                ephemeral=True
            )
            return

        if interval_ms < 60000:
            await interaction.followup.send(
                "❌ Interval must be at least 60 seconds (e.g., `1m`, `5m`, `1h`).",
                ephemeral=True
            )
            return

        # Check if already scheduled
        status = await self._get(f"/schedule/{discord_id}")
        if status.get("ok"):
            existing = [c for c in status.get("commands", []) if c["command"] == command]
            if existing:
                await interaction.followup.send(
                    f"⚠️ Command `{command}` is already scheduled. Use `/rundisable` to stop it first.",
                    ephemeral=True
                )
                return

        result = await self._post(f"/schedule/{discord_id}", json={
            "command": command,
            "interval": interval_ms
        })

        if result.get("ok"):
            await interaction.followup.send(
                f"✅ Scheduled `{command}` to run every **{interval}**.\n"
                f"Use `/runstatus` to see all scheduled commands.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"❌ {result.get('error', 'Failed to schedule command')}", ephemeral=True)

    @app_commands.command(name="runstatus", description="List all scheduled commands you have set.")
    @staff_only()
    async def runstatus(self, interaction: discord.Interaction):
        """Show all currently scheduled commands for the user."""
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)

        result = await self._get(f"/schedule/{discord_id}")
        if not result.get("ok"):
            await interaction.followup.send(f"❌ {result.get('error', 'Failed to fetch scheduled commands')}", ephemeral=True)
            return

        commands = result.get("commands", [])
        if not commands:
            await interaction.followup.send(
                "📭 You have no scheduled commands.\n"
                "Use `/run` to schedule one!",
                ephemeral=True
            )
            return

        lines = ["**📋 Your Scheduled Commands:**", ""]
        for i, cmd in enumerate(commands, 1):
            interval_min = cmd["interval"] / 60000
            if interval_min >= 1440:
                interval_str = f"{interval_min/1440:.1f}d"
            elif interval_min >= 60:
                interval_str = f"{interval_min/60:.1f}h"
            else:
                interval_str = f"{interval_min:.0f}m"

            next_run = cmd.get("nextRun")
            if next_run:
                try:
                    from datetime import datetime
                    if isinstance(next_run, str):
                        next_dt = datetime.fromisoformat(next_run.replace('Z', '+00:00'))
                    else:
                        next_dt = next_run
                    next_run = f"<t:{int(next_dt.timestamp())}:R>"
                except Exception:
                    next_run = "Unknown"
            else:
                next_run = "Unknown"

            lines.append(f"**{i}.** `{cmd['command']}`")
            lines.append(f"   ⏱️ Every {interval_str} | Next: {next_run}")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="rundisable", description="Disable a scheduled command.")
    @app_commands.describe(command="The command to stop running")
    @staff_only()
    async def rundisable(self, interaction: discord.Interaction, command: str):
        """Disable a specific scheduled command."""
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)

        # Check if command exists
        status = await self._get(f"/schedule/{discord_id}")
        if status.get("ok"):
            existing = [c for c in status.get("commands", []) if c["command"] == command]
            if not existing:
                await interaction.followup.send(
                    f"❌ No scheduled command found for `{command}`.\n"
                    f"Use `/runstatus` to see all scheduled commands.",
                    ephemeral=True
                )
                return

        result = await self._post(f"/schedule/disable/{discord_id}", json={"command": command})
        if result.get("ok"):
            await interaction.followup.send(
                f"✅ Disabled scheduled command: `{command}`.\n"
                f"Use `/run` again to re-schedule it.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"❌ {result.get('error', 'Failed to disable command')}", ephemeral=True)

    @app_commands.command(name="rundelete", description="Delete a scheduled command completely.")
    @app_commands.describe(command="The command to delete")
    @staff_only()
    async def rundelete(self, interaction: discord.Interaction, command: str):
        """Delete a scheduled command (remove it entirely)."""
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)

        result = await self._post(f"/schedule/delete/{discord_id}", json={"command": command})
        if result.get("ok"):
            await interaction.followup.send(f"✅ Deleted scheduled command: `{command}`", ephemeral=True)
        else:
            error_msg = result.get("error", "Failed to delete command")
            # Surface a clear message if the MC bot doesn't support the delete endpoint
            if "unreachable" in error_msg.lower() or "404" in str(result):
                error_msg += " (The MC bot may not support the delete endpoint. Use `/rundisable` instead.)"
            await interaction.followup.send(f"❌ {error_msg}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MCLink(bot))