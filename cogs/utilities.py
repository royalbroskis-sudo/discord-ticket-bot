# cogs/utilities.py — simple, fun/utility slash commands for regular members.
#
# These are intentionally low-permission commands (default: everyone can use
# them), but they still honor whatever role restrictions staff configure on
# the dashboard's Command Permissions page (the "command_perms" collection —
# same one every other cog's admin_only()/checks read from). Add the command
# names to the "Utilities" category in commands.html to make them editable
# there.

import ast
import operator
import random
import re
import discord
from datetime import datetime, timedelta, timezone
from discord.ext import commands, tasks
from discord import app_commands


# ── Dashboard permission check (shared convention) ───────────────────────────
# Mirrors how commands.html / /dashboard/<id>/commands saves role restrictions:
# db["command_perms"].find_one({"guild_id": ..., "command_name": "calc"}) ->
# {"roles": [role_id, ...]}. Empty/missing doc = default perms = everyone.

async def check_command_perm(interaction: discord.Interaction, command_name: str) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    db = interaction.client.db
    if db is None:
        return True
    doc = db["command_perms"].find_one({"guild_id": interaction.guild.id, "command_name": command_name})
    role_ids = doc.get("roles", []) if doc else []
    if not role_ids:
        return True  # no restriction configured -> everyone can use it
    user_role_ids = {r.id for r in interaction.user.roles}
    if not user_role_ids.intersection(set(int(r) for r in role_ids)):
        await interaction.response.send_message(
            f"❌ You don't have permission to use `/{command_name}`.", ephemeral=True
        )
        return False
    return True


# ── /calc — safe arithmetic evaluator ────────────────────────────────────────

_ALLOWED_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos,
}

def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("Only numbers are allowed.")
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise ZeroDivisionError("Cannot divide by zero.")
        return _ALLOWED_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPS:
        return _ALLOWED_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("That expression isn't supported — only + - * / // % ** and parentheses.")


def safe_calculate(expression: str):
    tree = ast.parse(expression, mode="eval")
    return _safe_eval(tree.body if isinstance(tree, ast.Expression) else tree)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Utilities(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.reminder_loop.start()

    def cog_unload(self):
        self.reminder_loop.cancel()

    # /calc
    @app_commands.command(name="calc", description="Evaluate a math expression, e.g. (5+3)*2/4")
    @app_commands.describe(expression="The expression to evaluate")
    async def calc(self, interaction: discord.Interaction, expression: str):
        if not await check_command_perm(interaction, "calc"):
            return
        try:
            result = safe_calculate(expression)
        except ZeroDivisionError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        except Exception:
            return await interaction.response.send_message(
                "❌ Invalid expression. Only numbers and `+ - * / // % **` with parentheses are supported.",
                ephemeral=True,
            )
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        embed = discord.Embed(title="🧮 Calculator", color=0x5865F2)
        embed.add_field(name="Expression", value=f"`{expression}`", inline=False)
        embed.add_field(name="Result", value=f"**{result}**", inline=False)
        await interaction.response.send_message(embed=embed)

    # /roll
    @app_commands.command(name="roll", description="Roll dice, e.g. 2d6 or 1d20")
    @app_commands.describe(dice="Format: NdM, e.g. 2d6 (max 100 dice, 1000 sides)")
    async def roll(self, interaction: discord.Interaction, dice: str = "1d6"):
        if not await check_command_perm(interaction, "roll"):
            return
        match = re.fullmatch(r"\s*(\d*)d(\d+)\s*", dice.lower())
        if not match:
            return await interaction.response.send_message(
                "❌ Use the format `NdM`, e.g. `2d6` or `d20`.", ephemeral=True
            )
        count = int(match.group(1)) if match.group(1) else 1
        sides = int(match.group(2))
        if not (1 <= count <= 100) or not (1 <= sides <= 1000):
            return await interaction.response.send_message(
                "❌ Keep it reasonable: 1-100 dice, 1-1000 sides.", ephemeral=True
            )
        rolls = [random.randint(1, sides) for _ in range(count)]
        embed = discord.Embed(title="🎲 Dice Roll", color=0x5865F2)
        embed.add_field(name="Rolled", value=f"`{dice}`", inline=True)
        embed.add_field(name="Total", value=f"**{sum(rolls)}**", inline=True)
        if count > 1:
            embed.add_field(name="Individual Rolls", value=", ".join(str(r) for r in rolls), inline=False)
        await interaction.response.send_message(embed=embed)

    # /coinflip
    @app_commands.command(name="coinflip", description="Flip a coin")
    async def coinflip(self, interaction: discord.Interaction):
        if not await check_command_perm(interaction, "coinflip"):
            return
        result = random.choice(["Heads", "Tails"])
        emoji = "🪙"
        await interaction.response.send_message(f"{emoji} The coin landed on **{result}**!")

    # /8ball
    @app_commands.command(name="8ball", description="Ask the magic 8-ball a question")
    @app_commands.describe(question="Your yes/no question")
    async def eightball(self, interaction: discord.Interaction, question: str):
        if not await check_command_perm(interaction, "8ball"):
            return
        responses = [
            "It is certain.", "Without a doubt.", "Yes, definitely.", "You may rely on it.",
            "As I see it, yes.", "Most likely.", "Outlook good.", "Signs point to yes.",
            "Reply hazy, try again.", "Ask again later.", "Better not tell you now.",
            "Cannot predict now.", "Concentrate and ask again.", "Don't count on it.",
            "My reply is no.", "My sources say no.", "Outlook not so good.", "Very doubtful.",
        ]
        embed = discord.Embed(title="🎱 Magic 8-Ball", color=0x2b2d31)
        embed.add_field(name="Question", value=question, inline=False)
        embed.add_field(name="Answer", value=random.choice(responses), inline=False)
        await interaction.response.send_message(embed=embed)

    # /poll
    @app_commands.command(name="poll", description="Create a quick reaction poll")
    @app_commands.describe(question="The poll question", options="Comma-separated options (max 9), leave blank for yes/no")
    async def poll(self, interaction: discord.Interaction, question: str, options: str = ""):
        if not await check_command_perm(interaction, "poll"):
            return
        opts = [o.strip() for o in options.split(",") if o.strip()][:9]
        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]

        embed = discord.Embed(title="📊 Poll", description=question, color=0x5865F2, timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=f"Started by {interaction.user.display_name}")

        if opts:
            embed.add_field(
                name="Options",
                value="\n".join(f"{number_emojis[i]} {opt}" for i, opt in enumerate(opts)),
                inline=False,
            )
            reactions = number_emojis[: len(opts)]
        else:
            reactions = ["👍", "👎"]

        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        for r in reactions:
            try:
                await msg.add_reaction(r)
            except discord.HTTPException:
                pass

    # /remindme — persisted to Mongo so a bot restart doesn't lose reminders
    @app_commands.command(name="remindme", description="Get a DM reminder later")
    @app_commands.describe(time="e.g. 10m, 2h, 1d", message="What to remind you about")
    async def remindme(self, interaction: discord.Interaction, time: str, message: str):
        if not await check_command_perm(interaction, "remindme"):
            return
        match = re.fullmatch(r"(\d+)\s*([smhd])", time.strip().lower())
        if not match:
            return await interaction.response.send_message(
                "❌ Use a duration like `10m`, `2h`, or `1d` (s/m/h/d).", ephemeral=True
            )
        amount, unit = int(match.group(1)), match.group(2)
        seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        if seconds > 30 * 86400:
            return await interaction.response.send_message("❌ Max reminder length is 30 days.", ephemeral=True)

        due_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        db = interaction.client.db
        if db is not None:
            db["reminders"].insert_one({
                "guild_id": interaction.guild.id if interaction.guild else None,
                "user_id": interaction.user.id,
                "channel_id": interaction.channel.id,
                "message": message,
                "created_at": datetime.now(timezone.utc),
                "due_at": due_at,
                "sent": False,
            })
        await interaction.response.send_message(
            f"⏰ Got it! I'll remind you in **{time}**: *{message}*", ephemeral=True
        )

    # Background task: fires due reminders, resilient to bot restarts since
    # everything lives in the "reminders" collection rather than memory.
    @tasks.loop(seconds=30)
    async def reminder_loop(self):
        db = self.bot.db
        if db is None:
            return
        now = datetime.now(timezone.utc)
        due = list(db["reminders"].find({"sent": False, "due_at": {"$lte": now}}))
        for r in due:
            try:
                user = await self.bot.fetch_user(r["user_id"])
                embed = discord.Embed(
                    title="⏰ Reminder",
                    description=r["message"],
                    color=0x5865F2,
                    timestamp=r["created_at"],
                )
                await user.send(embed=embed)
            except Exception as e:
                print(f"[utilities] Failed to send reminder to {r.get('user_id')}: {e}")
            finally:
                db["reminders"].update_one({"_id": r["_id"]}, {"$set": {"sent": True}})

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Utilities(bot))
