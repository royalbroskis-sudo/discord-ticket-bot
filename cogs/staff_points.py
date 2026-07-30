"""
cogs/staff_points.py — Staff activity / points leaderboard.

Tracks staff point-earning actions and shows a ranked, arrow-navigable
leaderboard (All Time / 24h / 7d / 30d), one staff member per page —
same layout as the old points dashboard, minus stream meme approvals
(not a feature this bot has).

Points are earned for:
    • Message sent outside a ticket              -> 0.05
    • Message sent inside a ticket                -> 0.25
    • Ticket renamed (via /rename in a ticket)     -> 20
    • Ticket closed                                -> 15
    • Moderation action (mute/kick/ban/warn/etc.)  -> 30

Every event is stored as its own document in the "staff_points_log"
collection ({guild_id, user_id, type, points, ts}), so totals for any
time window are just a date-filtered sum — no separate counters to keep
in sync, and a bot restart never loses history.

Other cogs report points by calling `log_event(db, guild_id, user_id, type)`.
Tickets/moderation/staff_utils import this lazily (inside the function
body) to avoid a circular import, since this module imports helpers from
cogs.tickets at load time.
"""

import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone, timedelta

from cogs.tickets import has_ticket_topic, is_ticket_channel
from cogs.config import staff_only

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
# Weights are configurable per-guild on the Web Dashboard (Settings ->
# "Staff Points" tab / staff_points_config collection). DEFAULT_POINTS is
# only the fallback used until a guild saves its own values.

DEFAULT_POINTS = {
    "msg_outside":   0.05,
    "msg_ticket":    0.25,
    "ticket_rename": 20,
    "ticket_close":  15,
    "mod_action":    30,
}

# Maps event type -> the dashboard config field name that stores its weight.
_CONFIG_FIELDS = {
    "msg_outside":   "PTS_MSG_OUTSIDE",
    "msg_ticket":    "PTS_MSG_TICKET",
    "ticket_rename": "PTS_TICKET_RENAME",
    "ticket_close":  "PTS_TICKET_CLOSE",
    "mod_action":    "PTS_MOD_ACTION",
}

_ROWS = (
    ("Moderation actions",        "mod_action"),
    ("Ticket closes",             "ticket_close"),
    ("Ticket renames",            "ticket_rename"),
    ("Messages in tickets",       "msg_ticket"),
    ("Messages outside tickets",  "msg_outside"),
)

_WINDOW_KEYS = ("all", "24h", "7d", "30d")


def get_weights(db, guild_id: int) -> dict:
    """Fetches the dashboard-configured point weights for a guild, falling
    back to DEFAULT_POINTS for anything unset."""
    cfg = {}
    if db is not None:
        cfg = db["staff_points_config"].find_one({"guild_id": guild_id}) or {}

    weights = {}
    for event_type, field in _CONFIG_FIELDS.items():
        raw = cfg.get(field)
        if raw is None:
            weights[event_type] = DEFAULT_POINTS[event_type]
        else:
            try:
                weights[event_type] = float(raw)
            except (TypeError, ValueError):
                weights[event_type] = DEFAULT_POINTS[event_type]
    return weights


def log_event(db, guild_id: int, user_id: int, event_type: str):
    """Record a single point-earning event, using the guild's current
    dashboard-configured weight. Safe no-op if db/type is invalid.
    The resolved point value is stored on the event itself, so past totals
    don't shift retroactively if weights are changed later."""
    if db is None or event_type not in DEFAULT_POINTS:
        return
    weight = get_weights(db, guild_id)[event_type]
    try:
        db["staff_points_log"].insert_one({
            "guild_id": guild_id,
            "user_id":  user_id,
            "type":     event_type,
            "points":   weight,
            "ts":       datetime.now(timezone.utc),
        })
    except Exception as e:
        print(f"[staff_points] Failed to log event: {e}")


def is_ticket_channel_now(channel) -> bool:
    """Rename-proof-first ticket check for a live channel object."""
    return isinstance(channel, discord.TextChannel) and (
        has_ticket_topic(channel) or is_ticket_channel(channel)
    )


def _empty_bucket() -> dict:
    bucket = {k: 0 for k in DEFAULT_POINTS}
    bucket["points"] = 0.0
    return bucket


def _aggregate(db, guild_id: int, cutoff: datetime | None) -> dict:
    """{user_id: {event_type: count, ..., 'points': total}} for one window."""
    query = {"guild_id": guild_id}
    if cutoff is not None:
        query["ts"] = {"$gte": cutoff}

    out: dict[int, dict] = {}
    for doc in db["staff_points_log"].find(query):
        uid = doc["user_id"]
        entry = out.setdefault(uid, _empty_bucket())
        etype = doc.get("type")
        if etype in entry:
            entry[etype] += 1
        entry["points"] += doc.get("points", 0)
    return out


def _build_ranking(db, guild_id: int) -> list[tuple[int, dict]]:
    """Returns [(user_id, {'all': bucket, '24h': bucket, '7d': bucket, '30d': bucket}), ...]
    sorted by all-time points, descending."""
    now = datetime.now(timezone.utc)
    cutoffs = {
        "all": None,
        "24h": now - timedelta(hours=24),
        "7d":  now - timedelta(days=7),
        "30d": now - timedelta(days=30),
    }
    per_window = {key: _aggregate(db, guild_id, cutoff) for key, cutoff in cutoffs.items()}

    merged: dict[int, dict] = {}
    for uid in per_window["all"]:
        merged[uid] = {w: per_window[w].get(uid, _empty_bucket()) for w in _WINDOW_KEYS}

    return sorted(merged.items(), key=lambda kv: kv[1]["all"]["points"], reverse=True)


# ---------------------------------------------------------------------------
# Leaderboard view (arrow pagination, one member per page)
# ---------------------------------------------------------------------------

class StaffLeaderboardView(discord.ui.View):
    def __init__(self, guild: discord.Guild, ranking: list[tuple[int, dict]], weights: dict):
        super().__init__(timeout=180)
        self.guild = guild
        self.ranking = ranking
        self.weights = weights
        self.index = 0
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_btn.disabled = self.index <= 0
        self.next_btn.disabled = self.index >= len(self.ranking) - 1

    def build_embed(self) -> discord.Embed:
        user_id, windows = self.ranking[self.index]
        member = self.guild.get_member(user_id)
        name = member.display_name if member else f"Unknown ({user_id})"
        mention = member.mention if member else name

        embed = discord.Embed(title=f"#{self.index + 1} {name}", color=0x5865F2)

        points_line = " • ".join(str(round(windows[w]["points"], 2)) for w in _WINDOW_KEYS)
        w = self.weights
        embed.description = (
            "**Format:** All Time • 🟢 24h • 🔵 7d • 🟣 30d\n"
            f"**Scoring:** non-ticket msg `{w['msg_outside']}` • ticket msg `{w['msg_ticket']}` • "
            f"rename `{w['ticket_rename']}` • close `{w['ticket_close']}` • mod action `{w['mod_action']}`\n"
            "*Use the arrows below to page through the leaderboard.*\n\n"
            f"{mention} • **Points:** {points_line}"
        )

        for label, key in _ROWS:
            row = " • ".join(str(windows[w][key]) for w in _WINDOW_KEYS)
            embed.add_field(name=label, value=row, inline=False)

        embed.set_footer(text=f"Rank {self.index + 1} of {len(self.ranking)}")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.ranking) - 1, self.index + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class StaffPoints(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        event = "msg_ticket" if is_ticket_channel_now(message.channel) else "msg_outside"
        log_event(self.bot.db, message.guild.id, message.author.id, event)

    @app_commands.command(name="staffstats", description="View the staff activity points leaderboard")
    @staff_only()
    async def staffstats(self, interaction: discord.Interaction):
        db = self.bot.db
        if db is None:
            await interaction.response.send_message("❌ Database unavailable.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        ranking = _build_ranking(db, interaction.guild.id)

        if not ranking:
            await interaction.followup.send("No staff activity has been recorded yet.", ephemeral=True)
            return

        view = StaffLeaderboardView(interaction.guild, ranking, get_weights(db, interaction.guild.id))
        await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffPoints(bot))
