"""
cogs/staff_points.py — Staff activity / points leaderboard.

/staffstats posts a public leaderboard message in the channel it's run in
(several staff members per page, arrow-paginated). Running it again deletes
the old tracked message for that guild and posts a fresh one; a background
task also edits the tracked message in place every hour so it stays current
without needing anyone to re-run the command.

Points are earned for:
    • Message sent outside a ticket              -> 0.05
    • Message sent inside a ticket                -> 0.25
    • Ticket renamed (via /rename in a ticket)     -> 20
    • Ticket closed                                -> 15
    • Moderation action (mute/kick/ban/warn/etc.)  -> 30
(Weights are configurable per-guild on the dashboard — see get_weights().)

Every event is stored as its own document in the "staff_points_log"
collection ({guild_id, user_id, type, points, ts}), so totals for any
time window are just a date-filtered sum — no separate counters to keep
in sync, and a bot restart never loses history. The currently-posted
board message is tracked in "staff_points_board" ({guild_id, channel_id,
message_id, page}).

Other cogs report points by calling `log_event(db, guild_id, user_id, type)`.
Tickets/moderation/staff_utils import this lazily (inside the function
body) to avoid a circular import, since this module imports helpers from
cogs.tickets at load time.
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timezone, timedelta

from cogs.tickets import has_ticket_topic, is_ticket_channel
from cogs.config import staff_only, get_guild_config, member_has_role_id

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


def _build_ranking(db, guild: discord.Guild) -> list[tuple[int, dict]]:
    """Returns [(user_id, {'all': bucket, '24h': bucket, '7d': bucket, '30d': bucket}), ...]
    sorted by all-time points, descending. Only includes members who
    currently hold the Staff role configured on the dashboard — someone
    who earned points and later lost/never had that role won't show up."""
    guild_id = guild.id
    now = datetime.now(timezone.utc)
    cutoffs = {
        "all": None,
        "24h": now - timedelta(hours=24),
        "7d":  now - timedelta(days=7),
        "30d": now - timedelta(days=30),
    }
    per_window = {key: _aggregate(db, guild_id, cutoff) for key, cutoff in cutoffs.items()}

    cfg = get_guild_config(db, guild_id)
    staff_role_id = cfg.get("STAFF_ROLE_ID")

    merged: dict[int, dict] = {}
    for uid in per_window["all"]:
        if staff_role_id:
            member = guild.get_member(uid)
            if not member or not member_has_role_id(member, staff_role_id):
                continue
        merged[uid] = {w: per_window[w].get(uid, _empty_bucket()) for w in _WINDOW_KEYS}

    return sorted(merged.items(), key=lambda kv: kv[1]["all"]["points"], reverse=True)


# ---------------------------------------------------------------------------
# Leaderboard board (public message, several members per page, persists
# across restarts, auto-refreshes hourly)
# ---------------------------------------------------------------------------
# One tracked message per guild, stored in "staff_points_board":
#   {guild_id, channel_id, message_id, page}
# Running /staffstats again deletes that tracked message and posts a new
# one. A background task edits the tracked message in place every hour so
# it stays current without spamming the channel.

PAGE_SIZE = 6  # staff members shown per page


def _format_member_block(rank: int, guild: discord.Guild, user_id: int, windows: dict) -> str:
    member = guild.get_member(user_id)
    name = member.display_name if member else f"Unknown ({user_id})"
    mention = member.mention if member else name

    points_line = " • ".join(str(round(windows[w]["points"], 2)) for w in _WINDOW_KEYS)
    lines = [f"**#{rank} {name}**", f"{mention} • **Points:** {points_line}"]
    for label, key in _ROWS:
        row = " • ".join(str(windows[w][key]) for w in _WINDOW_KEYS)
        lines.append(f"**{label}:** {row}")
    return "\n".join(lines)


def build_board_embed(guild: discord.Guild, ranking: list[tuple[int, dict]], weights: dict,
                       page: int, bot_user: discord.ClientUser | None):
    """Returns (embed, resolved_page, total_pages)."""
    total_pages = max(1, -(-len(ranking) // PAGE_SIZE))  # ceil div
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = ranking[start:start + PAGE_SIZE]

    w = weights
    legend = (
        "**Format:** All Time • 🟢 24h • 🔵 7d • 🟣 30d\n"
        f"**Scoring:** non-ticket msg `{w['msg_outside']}` • ticket msg `{w['msg_ticket']}` • "
        f"rename `{w['ticket_rename']}` • close `{w['ticket_close']}` • mod action `{w['mod_action']}`"
    )

    embed = discord.Embed(title=f"🏆 Staff Leaderboard (Page {page + 1}/{total_pages})", color=0x5865F2)

    if not chunk:
        embed.description = legend + "\n\nNo staff activity has been recorded yet."
    else:
        blocks = [legend] + [
            _format_member_block(start + i + 1, guild, uid, windows)
            for i, (uid, windows) in enumerate(chunk)
        ]
        embed.description = "\n\n".join(blocks)

    footer = "Updates hourly"
    if bot_user:
        footer = f"Made by {bot_user.name} • {footer}"
    embed.set_footer(text=footer)
    return embed, page, total_pages


class StaffLeaderboardBoardView(discord.ui.View):
    """Persistent (timeout=None) pagination for the public board message.
    A single instance is registered with bot.add_view() at startup so
    button presses still route correctly after a restart; every send/edit
    uses a freshly-built instance so the page-number label is per-message."""

    def __init__(self, page: int = 0, total_pages: int = 1):
        super().__init__(timeout=None)
        self.page_info_btn.label = f"Page {page + 1}/{total_pages}"

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="staffpoints_board_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._paginate(interaction, -1)

    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.secondary,
                        custom_id="staffpoints_board_pageinfo", disabled=True)
    async def page_info_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass  # display only, never actually clickable

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="staffpoints_board_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._paginate(interaction, 1)

    async def _paginate(self, interaction: discord.Interaction, delta: int):
        bot = interaction.client
        db = getattr(bot, "db", None)
        if db is None or interaction.message is None:
            await interaction.response.defer()
            return

        board = db["staff_points_board"].find_one({"message_id": interaction.message.id})
        if not board:
            await interaction.response.defer()
            return

        guild = bot.get_guild(board["guild_id"])
        if guild is None:
            await interaction.response.defer()
            return

        ranking = _build_ranking(db, guild)
        weights = get_weights(db, board["guild_id"])
        new_page = board.get("page", 0) + delta
        embed, resolved_page, total_pages = build_board_embed(guild, ranking, weights, new_page, bot.user)

        db["staff_points_board"].update_one({"_id": board["_id"]}, {"$set": {"page": resolved_page}})
        await interaction.response.edit_message(
            embed=embed, view=StaffLeaderboardBoardView(page=resolved_page, total_pages=total_pages)
        )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class StaffPoints(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Lets button presses on old board messages route correctly even
        # after a restart, before any message has been edited this session.
        bot.add_view(StaffLeaderboardBoardView())
        self.refresh_loop.start()

    def cog_unload(self):
        self.refresh_loop.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        event = "msg_ticket" if is_ticket_channel_now(message.channel) else "msg_outside"
        log_event(self.bot.db, message.guild.id, message.author.id, event)

    # ── Hourly auto-refresh of every guild's tracked board message ────────
    @tasks.loop(hours=1)
    async def refresh_loop(self):
        db = self.bot.db
        if db is None:
            return
        for board in list(db["staff_points_board"].find({})):
            guild = self.bot.get_guild(board["guild_id"])
            if not guild:
                continue
            channel = guild.get_channel(board["channel_id"])
            if not channel:
                continue
            try:
                msg = await channel.fetch_message(board["message_id"])
            except discord.NotFound:
                db["staff_points_board"].delete_one({"_id": board["_id"]})
                continue
            except discord.HTTPException as e:
                print(f"[staff_points] Failed to fetch board message in guild {board['guild_id']}: {e}")
                continue

            ranking = _build_ranking(db, guild)
            weights = get_weights(db, board["guild_id"])
            embed, resolved_page, total_pages = build_board_embed(
                guild, ranking, weights, board.get("page", 0), self.bot.user
            )
            try:
                await msg.edit(
                    embed=embed,
                    view=StaffLeaderboardBoardView(page=resolved_page, total_pages=total_pages),
                )
            except discord.HTTPException as e:
                print(f"[staff_points] Failed to refresh board in guild {board['guild_id']}: {e}")

    @refresh_loop.before_loop
    async def before_refresh_loop(self):
        await self.bot.wait_until_ready()

    # ── Post (or replace) the public leaderboard in this channel ──────────
    @app_commands.command(name="staffstats", description="Post or refresh the public staff points leaderboard in this channel")
    @staff_only()
    async def staffstats(self, interaction: discord.Interaction):
        db = self.bot.db
        if db is None:
            await interaction.response.send_message("❌ Database unavailable.", ephemeral=True)
            return

        # If a board message already exists anywhere in this guild, delete it first.
        existing = db["staff_points_board"].find_one({"guild_id": interaction.guild.id})
        if existing:
            old_channel = interaction.guild.get_channel(existing["channel_id"])
            if old_channel:
                try:
                    old_msg = await old_channel.fetch_message(existing["message_id"])
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                except discord.HTTPException as e:
                    print(f"[staff_points] Failed to delete old board message: {e}")

        ranking = _build_ranking(db, interaction.guild)
        weights = get_weights(db, interaction.guild.id)
        embed, page, total_pages = build_board_embed(interaction.guild, ranking, weights, 0, self.bot.user)

        await interaction.response.send_message(
            embed=embed, view=StaffLeaderboardBoardView(page=page, total_pages=total_pages)
        )
        msg = await interaction.original_response()

        db["staff_points_board"].update_one(
            {"guild_id": interaction.guild.id},
            {"$set": {
                "guild_id":   interaction.guild.id,
                "channel_id": interaction.channel.id,
                "message_id": msg.id,
                "page":       0,
            }},
            upsert=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffPoints(bot))

