# cogs/spawners.py — Spawner buy/sell ticket panel.
#
# Everything here is dashboard-configured: the spawner types (emoji, buy
# price, sell price), the panel channel, the ticket category, and the
# roles pinged for buy vs. sell tickets. The panel embed itself is posted
# straight from the dashboard (app.py's "send_spawner_panel" form) using a
# raw Discord REST call, the same way the Applications panel is sent — the
# buttons on it use static custom_ids ("spawner_buy_btn" / "spawner_sell_btn")
# that this cog's persistent SpawnerPanelView handles regardless of which
# process actually posted the message.

import logging
import re
from datetime import datetime, timezone

import discord
from discord.ext import commands

from cogs.tickets import TicketView, record_open_ticket

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────

def format_price(value) -> str:
    """Turn a raw number back into '5.5m' / '6.2b' style text for display."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "0"
    for suffix, divisor in (("b", 1_000_000_000), ("m", 1_000_000), ("k", 1_000)):
        if abs(value) >= divisor:
            n = value / divisor
            text = f"{n:.2f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return f"{value:.0f}"


def get_spawner_config(db, guild_id: int) -> dict:
    return (db["spawner_config"].find_one({"guild_id": guild_id}) or {}) if db is not None else {}


def get_spawner_types(db, guild_id: int) -> list:
    if db is None:
        return []
    doc = db["spawner_panels"].find_one({"guild_id": guild_id})
    return (doc or {}).get("types", [])


def build_spawner_panel_embed(types: list) -> discord.Embed:
    embed = discord.Embed(title="Spawner Prices 🛒", color=0x2b2d31)

    buying = "\n".join(
        f"{t.get('emoji', '📦')} {t['name']} **{format_price(t.get('buy_price', 0))}** each" for t in types
    ) or "—"
    selling = "\n".join(
        f"{t.get('emoji', '📦')} {t['name']} **{format_price(t.get('sell_price', 0))}** each" for t in types
    ) or "—"

    embed.add_field(name="Buying:", value=buying, inline=False)
    embed.add_field(name="Selling:", value=selling, inline=False)
    embed.add_field(name="Notes", value="Open a ticket below to buy or sell spawners.", inline=False)
    return embed


def _slugify(text: str) -> str:
    """Discord channel-name safe slug: lowercase, spaces/underscores -> hyphens,
    strip anything that isn't alnum or hyphen."""
    text = str(text).strip().lower().replace(" ", "-").replace("_", "-")
    text = re.sub(r"[^a-z0-9\-]", "", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "spawner"


async def create_spawner_ticket(bot, guild: discord.Guild, member: discord.Member,
                                 trade_side: str, spawner: dict, ign: str, quantity: int):
    """Creates the ticket channel, pings the configured roles, and posts the
    order summary embed (with the auto-calculated total) alongside the
    Staff Controls / Close button."""
    db = bot.db
    cfg = get_spawner_config(db, guild.id)

    category_id = cfg.get("CATEGORY_ID")
    category = guild.get_channel(category_id) if category_id else None
    if not isinstance(category, discord.CategoryChannel):
        category = await guild.create_category("Spawner Tickets")
        await category.set_permissions(guild.default_role, read_messages=False)

    role_key = "SELL_PING_ROLE_IDS" if trade_side == "sell" else "BUY_PING_ROLE_IDS"
    ping_roles = [r for r in (guild.get_role(rid) for rid in cfg.get(role_key, [])) if r]

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        member: discord.PermissionOverwrite(
            read_messages=True, send_messages=True, read_message_history=True, attach_files=True
        ),
    }
    for role in ping_roles:
        overwrites[role] = discord.PermissionOverwrite(
            read_messages=True, send_messages=True, read_message_history=True
        )

    side_label = {"buy": "buy", "sell": "sell", "partial": "partial"}.get(trade_side, trade_side)
    channel_name = f"{side_label}-{quantity}-{_slugify(spawner['name'])}"[:100]

    channel = await guild.create_text_channel(
        name=channel_name,
        category=category,
        overwrites=overwrites,
        # NOTE: must start with "Ticket by " (capital T) — tickets.py's
        # has_ticket_topic()/_close_ticket() parse on that exact prefix to
        # fill in Creator/Category on the transcript. The old "Spawner
        # ticket by ..." prefix didn't match, which is why transcripts were
        # showing "Category: Unknown  Creator: Unknown" for spawner tickets.
        topic=f"Ticket by {member.name} | Spawner {trade_side} x{quantity} {spawner['name']}",
    )

    # "partial" = a partial sell (customer selling less than full bulk qty),
    # so it uses the sell price same as a regular sell.
    # Pricing is from the shop's perspective (buy_price/sell_price fields are
    # set on the dashboard as "what WE pay/charge"), so it's inverted from
    # the customer's trade_side: a customer "buying" pays our sell_price,
    # a customer "selling" (full or partial) gets paid our buy_price.
    price_per = float(spawner.get("buy_price" if trade_side in ("sell", "partial") else "sell_price") or 0)
    total = price_per * quantity

    price_label = "Our Selling Price" if trade_side == "buy" else "Our Buying Price"
    trade_display = {"buy": "Buy", "sell": "Sell", "partial": "Sell (Partial)"}.get(trade_side, trade_side.title())
    embed_color = {"buy": 0x2ecc71, "sell": 0xe74c3c, "partial": 0xf1c40f}.get(trade_side, 0x5865F2)

    embed = discord.Embed(
        title="🎫 Ticket Opened",
        description="A team member will help you shortly.",
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="👤 Opened By", value=member.mention, inline=True)
    embed.add_field(name="🏷️ Ticket Type", value="spawners", inline=True)
    embed.add_field(name="🚦 Status", value="🟢 Open", inline=True)
    embed.add_field(
        name="📝 Details",
        value=(
            f"🔄 Trade Side: **{trade_display}**\n"
            f"{spawner.get('emoji', '📦')} Spawner: **{spawner['name']}**\n"
            f"🔢 Quantity: **{quantity}**\n"
            f"🧑 Customer IGN: **{ign}**\n"
            f"💵 {price_label}: **{format_price(price_per)}** each\n"
            f"💰 Total Price: **{format_price(total)}**{' (bulk)' if trade_side != 'partial' else ' (partial)'}"
        ),
        inline=False,
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    ping_text = " ".join(r.mention for r in ping_roles) if ping_roles else None
    await channel.send(content=ping_text, embed=embed)

    # Staff Controls — just Close for now (Claim isn't needed here).
    await channel.send("**Staff Controls**", view=TicketView())

    record_open_ticket(
        db, guild.id, channel.id, member.id, member.name,
        f"Spawner {trade_side} x{quantity} {spawner['name']}", source="spawner",
    )

    if db is not None:
        db["spawner_tickets"].insert_one({
            "guild_id": guild.id,
            "channel_id": channel.id,
            "opened_by": member.id,
            "trade_side": trade_side,
            "spawner_id": spawner.get("id"),
            "spawner_name": spawner["name"],
            "quantity": quantity,
            "ign": ign,
            "price_per": price_per,
            "total_price": total,
            "status": "open",
            "created_at": datetime.now(timezone.utc),
        })

    return channel


# ── UI components ─────────────────────────────────────────────────────

class SpawnerOrderModal(discord.ui.Modal):
    def __init__(self, trade_side: str, spawner: dict):
        verb = {"buy": "Buy", "sell": "Sell", "partial": "Sell (Partial)"}.get(trade_side, trade_side.title())
        super().__init__(title=f"{verb} {spawner['name']} Spawners"[:45])
        self.trade_side = trade_side
        self.spawner = spawner
        self.ign_input = discord.ui.TextInput(label="Your IGN", required=True, max_length=32)

        price_per = float(spawner.get("buy_price" if trade_side in ("sell", "partial") else "sell_price") or 0)
        price_word = "Selling" if trade_side == "buy" else "Buying"
        qty_placeholder = f"Our {price_word} Price: {format_price(price_per)} each — e.g. 64"
        self.qty_input = discord.ui.TextInput(label="Quantity", required=True, placeholder=qty_placeholder[:100])
        self.add_item(self.ign_input)
        self.add_item(self.qty_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_qty = str(self.qty_input.value).strip().replace(",", "")
        try:
            quantity = int(raw_qty)
            if quantity <= 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "❌ Quantity must be a whole number greater than 0.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        try:
            channel = await create_spawner_ticket(
                interaction.client, interaction.guild, interaction.user,
                self.trade_side, self.spawner, str(self.ign_input.value).strip(), quantity,
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ I don't have permission to create a ticket channel here.", ephemeral=True
            )
        await interaction.followup.send(f"✅ Your ticket has been created: {channel.mention}", ephemeral=True)


class SpawnerTypeSelect(discord.ui.Select):
    def __init__(self, types: list, trade_side: str):
        self.trade_side = trade_side
        self.types_by_id = {t["id"]: t for t in types}
        options = [
            discord.SelectOption(label=t["name"][:100], value=t["id"], emoji=t.get("emoji") or None)
            for t in types[:25]
        ]
        super().__init__(placeholder="Select a spawner type...", options=options)

    async def callback(self, interaction: discord.Interaction):
        spawner = self.types_by_id.get(self.values[0])
        if not spawner:
            return await interaction.response.send_message("❌ That spawner type is no longer available.", ephemeral=True)
        await interaction.response.send_modal(SpawnerOrderModal(self.trade_side, spawner))


class SpawnerTypeSelectView(discord.ui.View):
    def __init__(self, types: list, trade_side: str):
        super().__init__(timeout=120)
        self.add_item(SpawnerTypeSelect(types, trade_side))


class SpawnerPanelView(discord.ui.View):
    """Static, persistent view — safe to register once globally since it
    holds no per-guild state; the guild is read off the interaction."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="I'm Buying", style=discord.ButtonStyle.red, emoji="⬇️", custom_id="spawner_buy_btn")
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_select(interaction, "buy")

    @discord.ui.button(label="I'm Selling", style=discord.ButtonStyle.green, emoji="⬆️", custom_id="spawner_sell_btn")
    async def sell_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_select(interaction, "sell")

    @discord.ui.button(label="I'm Selling (Partial)", style=discord.ButtonStyle.blurple, emoji="🔸", custom_id="spawner_partial_btn")
    async def partial_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_select(interaction, "partial")

    async def _open_select(self, interaction: discord.Interaction, trade_side: str):
        types = get_spawner_types(interaction.client.db, interaction.guild.id)
        if not types:
            return await interaction.response.send_message(
                "❌ No spawner types have been configured yet. Ask an admin to add some on the dashboard.",
                ephemeral=True,
            )
        price_note = "You'll pay **our Selling Price**." if trade_side == "buy" else "You'll be paid **our Buying Price**."
        verb = {"buy": "buy", "sell": "sell", "partial": "sell (partially)"}.get(trade_side, trade_side)
        await interaction.response.send_message(
            f"Select the spawner type you want to {verb}:\n{price_note}",
            view=SpawnerTypeSelectView(types, trade_side),
            ephemeral=True,
        )


# ── Cog ────────────────────────────────────────────────────────────────

class Spawners(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Register the persistent panel view so buttons on old panel
        # messages (posted from the dashboard, possibly before a restart)
        # keep working.
        self.bot.add_view(SpawnerPanelView())


async def setup(bot: commands.Bot):
    await bot.add_cog(Spawners(bot))
