"""
ai_agent.py — Natural-language admin agent for the dashboard.

Talks to Gemini's OpenAI-compatible chat-completions endpoint (tool
calling included) so no extra SDK is needed beyond `requests`, which
app.py already depends on.

How it fits together:
  - TOOLS / TOOL_FUNCTIONS define what the model can do. Every tool maps
    to a real Discord REST call made with the bot token, the same way
    app.py's existing `bot_console` form handlers already work.
  - Read-only tools (lookup_user, list_channels, list_roles,
    summarize_channel, list_warnings, server_stats, member_insights)
    execute immediately and their result is fed back to the model so it
    can keep reasoning.
  - Destructive tools (anything that changes the server) are NOT
    executed here. run_agent_turn() stops and hands the proposed call
    back to app.py, which shows the user a Confirm/Cancel step. Only
    app.py's /agent/execute route actually performs those.

app.py is expected to provide:
  - a `discord_api(method, path, reason=None, **kwargs)` callable
    (this is just _discord_api from app.py, passed in to avoid a
    circular import)
  - GEMINI_API_KEY from the environment

Tone is defined once in personality.py and prepended to SYSTEM_PROMPT
below — edit that file to change how the bot talks, not this one.
"""

import os
import json
import logging
import time
import asyncio
import re
import requests

from personality import PERSONALITY
from cogs import moderation as moderation_cog
from cogs import afk as afk_cog
from cogs import tickets as tickets_cog

logger = logging.getLogger(__name__)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

MAX_TOOL_ITERATIONS = int(os.getenv("AGENT_MAX_TOOL_ITERATIONS", "15"))  # safety cap so a confused model can't loop forever

SYSTEM_PROMPT = PERSONALITY + "\n\n" + """The above is tone only — everything below is operational and always wins
if the two ever pull in different directions.

You are the admin assistant for a Discord server's web dashboard.
Staff type natural-language requests and you either answer directly, call a
read-only tool to look something up, or propose a moderation/action tool call.

Rules:
- Always resolve a username to a numeric user_id via lookup_user before calling
  any tool that needs user_id. Never guess an ID.
- Always resolve a giveaway to its message_id via list_giveaways before calling
  end_giveaway, reroll_giveaway, delete_giveaway, or pay_giveaway_claims. Never guess an ID. If
  several giveaways plausibly match, ask which one instead of picking.
- Keep replies short and concrete. State exactly what you're about to do before
  proposing a destructive action.
- If a request is ambiguous (e.g. which of several matching users), ask a short
  clarifying question instead of guessing.
- You cannot see channels/roles unless you call list_channels / list_roles, or
  they were already given to you in this conversation — don't assume IDs.
- Destructive tools are never executed by you directly; proposing the call is
  enough, the dashboard will ask the human to confirm.
- When a request involves the same action on multiple targets (e.g. warning
  several users, creating several channels), include all of those tool calls
  in the same response instead of spreading them one-per-turn — you have a
  limited number of turns to finish a request.
- When confirming an action you just took (or several), state plainly and
  accurately what was done — target, action, reason if any. Personality can
  flavor the wording around it, but never at the cost of clarity: someone
  reading only that sentence should know exactly what happened on the server.
"""

# ---------------------------------------------------------------------------
# Tool schema (OpenAI-compatible function-calling format)
# ---------------------------------------------------------------------------

EMBED_SCHEMA = {
    "type": "object",
    "description": "Optional rich embed to attach to the message. Provide content, embed, or both.",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "url": {"type": "string", "description": "Makes the title a clickable link"},
        "color": {"type": "string", "description": "Hex color like #3498db"},
        "footer": {"type": "string"},
        "thumbnail_url": {"type": "string"},
        "image_url": {"type": "string"},
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                    "inline": {"type": "boolean"},
                },
                "required": ["name", "value"],
            },
        },
    },
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_user",
            "description": "Search guild members by username/display name substring. Returns matching users with their numeric IDs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Username or partial username to search for"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_channels",
            "description": "List text channels in this guild with their IDs.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_roles",
            "description": "List roles in this guild with their IDs.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_channel",
            "description": "Fetch the most recent messages from a channel — including embed content (title/description/fields/footer), not just plain text — so you can summarize or analyze them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "limit": {"type": "integer", "description": "How many recent messages to fetch (max 100)", "default": 30},
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_warnings",
            "description": "List a member's stored moderation warnings.",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "server_stats",
            "description": "Get overall server statistics: member count, channel count/breakdown by type, role count, boost tier and boost count, and server creation date.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "member_insights",
            "description": "Get detailed info on a single member: join date, account creation date, current roles, timeout status, and warning count.",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_audit_log",
            "description": "Get recent server audit log entries (who did what — bans, kicks, channel/role changes, etc).",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "How many entries to fetch, default 10, max 50"},
                    "user_id": {"type": "string", "description": "Optional — filter to actions performed by this user"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_invites",
            "description": "List active invite links for the server, with their channel, uses, and expiry.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    # ---- destructive: proposed only, executed after human confirms ----
    {
        "type": "function",
        "function": {
            "name": "kick_member",
            "description": "Kick a member from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ban_member",
            "description": "Ban a member from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "delete_days": {"type": "integer", "description": "Days of recent messages to delete (0-7)", "default": 0},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unban_member",
            "description": "Remove a ban for a user ID.",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "timeout_member",
            "description": "Apply a timeout (mute) to a member for a number of minutes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "minutes": {"type": "integer", "default": 10},
                    "reason": {"type": "string"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_timeout",
            "description": "Remove an active timeout from a member.",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_role",
            "description": "Add a role to a member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "role_id": {"type": "string"},
                },
                "required": ["user_id", "role_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_role",
            "description": "Remove a role from a member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "role_id": {"type": "string"},
                },
                "required": ["user_id", "role_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Post a message as the bot in a channel. Supports plain text, a rich embed, or both.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "content": {"type": "string"},
                    "embed": EMBED_SCHEMA,
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dm_user",
            "description": "Send a direct message to a user as the bot. Supports plain text, a rich embed, or both.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "content": {"type": "string"},
                    "embed": EMBED_SCHEMA,
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_channel",
            "description": "Create a new text channel in this guild.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "topic": {"type": "string"},
                    "parent_id": {"type": "string", "description": "Category channel ID to nest the new channel under"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_channel",
            "description": "Rename an existing channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["channel_id", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_channel",
            "description": "Delete a channel.",
            "parameters": {
                "type": "object",
                "properties": {"channel_id": {"type": "string"}},
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_role",
            "description": "Create a new role in this guild.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "color": {"type": "integer", "description": "Decimal RGB color value, e.g. 0x3498db"},
                    "hoist": {"type": "boolean", "description": "Display role members separately in the member list"},
                    "mentionable": {"type": "boolean"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_role",
            "description": "Edit an existing role's name, color, hoist, or mentionable settings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role_id": {"type": "string"},
                    "name": {"type": "string"},
                    "color": {"type": "integer"},
                    "hoist": {"type": "boolean"},
                    "mentionable": {"type": "boolean"},
                },
                "required": ["role_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_role",
            "description": "Delete a role from this guild.",
            "parameters": {
                "type": "object",
                "properties": {"role_id": {"type": "string"}},
                "required": ["role_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_message",
            "description": "Delete a specific message from a channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "message_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["channel_id", "message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pin_message",
            "description": "Pin a specific message in a channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "message_id": {"type": "string"},
                },
                "required": ["channel_id", "message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unpin_message",
            "description": "Unpin a specific message in a channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "message_id": {"type": "string"},
                },
                "required": ["channel_id", "message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "warn_member",
            "description": "Issue a moderation warning to a member, stored in the warnings collection.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["user_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_warnings",
            "description": "Clear all stored warnings for a member.",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_nickname",
            "description": "Change a member's server nickname. Pass an empty string to reset to their username.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "nickname": {"type": "string"},
                },
                "required": ["user_id", "nickname"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_afk",
            "description": "Mark a member AFK: records the reason and prefixes their server nickname with '[AFK] ' so it's visible everywhere. Their original nickname is remembered and restored automatically the moment they next send a message, or via clear_afk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "reason": {"type": "string", "description": "Why they're AFK. Defaults to 'No reason provided' if omitted."},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_afk",
            "description": "Manually clear a member's AFK status and restore their original nickname. Normally this happens automatically when they post a message — use this tool only if asked to clear it early on their behalf.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_channel_settings",
            "description": "Edit a channel's topic, slowmode, NSFW flag, or category (parent).",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "topic": {"type": "string"},
                    "slowmode_seconds": {"type": "integer", "description": "Rate limit per user, 0-21600, 0 disables it"},
                    "nsfw": {"type": "boolean"},
                    "parent_id": {"type": "string", "description": "Category channel ID to move this channel under"},
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_voice_state",
            "description": "Move a member to a different voice channel, or server-mute/deafen them. Any field left unset is unchanged.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "voice_channel_id": {"type": "string", "description": "Channel to move them to, or omit to leave unchanged"},
                    "mute": {"type": "boolean"},
                    "deafen": {"type": "boolean"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_invite",
            "description": "Create an invite link for a channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "max_age_seconds": {"type": "integer", "description": "0 = never expires. Default 86400 (24h)."},
                    "max_uses": {"type": "integer", "description": "0 = unlimited. Default 0."},
                    "temporary": {"type": "boolean", "description": "Grants temporary membership (kicked on disconnect unless a role is given)"},
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_invite",
            "description": "Revoke an invite by its code (the part after discord.gg/).",
            "parameters": {
                "type": "object",
                "properties": {"invite_code": {"type": "string"}},
                "required": ["invite_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_emoji",
            "description": "Delete a custom server emoji by its ID.",
            "parameters": {
                "type": "object",
                "properties": {"emoji_id": {"type": "string"}},
                "required": ["emoji_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prune_members",
            "description": "Kick all members who have been inactive (no roles, not seen) for at least the given number of days. Returns how many were removed. Use with caution — this is bulk and irreversible.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Inactivity threshold in days, minimum 1"},
                },
                "required": ["days"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_server_settings",
            "description": "Edit server-wide settings: name, description, or verification level.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "verification_level": {
                        "type": "integer",
                        "description": "0=none 1=low 2=medium 3=high 4=very high",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "purge_messages",
            "description": "Bulk-delete recent messages in a channel (only messages under 14 days old, Discord limitation). Optionally restrict to one user's messages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "count": {"type": "integer", "description": "How many recent messages to scan/delete, 2-100"},
                    "only_from_user_id": {"type": "string", "description": "Optional — only delete messages from this user"},
                },
                "required": ["channel_id", "count"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_ticket",
            "description": (
                "Close a support ticket channel: generates and saves a full transcript, "
                "posts a summary to the configured transcript log channel (if any), DMs the "
                "ticket creator a link to the transcript, then deletes the channel. This only "
                "works on real ticket channels (ones created through the ticket panel system, "
                "identified by their topic) — it refuses on regular channels, use delete_channel "
                "for those instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_ticket",
            "description": "Rename a ticket channel. Only works on real ticket channels — refuses on regular channels (use rename_channel for those).",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["channel_id", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_user_to_ticket",
            "description": "Give a user view/send access to an existing ticket channel — e.g. pulling in another staff member or a second customer. Only works on real ticket channels.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "user_id": {"type": "string"},
                },
                "required": ["channel_id", "user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_user_from_ticket",
            "description": "Remove a user's view/send access to a ticket channel. Only works on real ticket channels.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "user_id": {"type": "string"},
                },
                "required": ["channel_id", "user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_giveaways",
            "description": "List giveaways with their claims and payment status. For each giveaway, shows: claimed_users (users who opened claim tickets), total_claims (total claims filed), unpaid_claims (claims not yet paid), paid_claims (claims already paid), all_claims_paid (whether all claims are paid). Use this to check which giveaways still have unpaid claims.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Optional title/prize substring to filter by"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_giveaway",
            "description": "Force-end an active giveaway early and announce the winner(s).",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "The giveaway's original message ID (from list_giveaways)"},
                },
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reroll_giveaway",
            "description": "Reroll a giveaway that has already ended, picking new winner(s) from the remaining entries. Fails if any winner has already opened a claim ticket.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "The giveaway's original message ID (from list_giveaways)"},
                },
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_giveaway",
            "description": "Permanently delete a giveaway — removes the giveaway/winner messages and its database entry. Cannot be undone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "The giveaway's original message ID (from list_giveaways)"},
                },
                "required": ["message_id"],
            },
        },
    },
    # ---- Promotion tools (mirrors cogs/promotion.py's /promote and /demote) ----
    {
        "type": "function",
        "function": {
            "name": "promote_member",
            "description": "Promote a member by giving them a role, and announce it in the configured promotion-announcement channel (same behavior as the /promote slash command). Resolve user_id via lookup_user and new_role_id/old_role_id via list_roles first — never guess IDs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "new_role_id": {"type": "string", "description": "The role to give them"},
                    "old_role_id": {"type": "string", "description": "Optional: their previous rank, shown in the announcement (not removed)"},
                    "reason": {"type": "string"},
                },
                "required": ["user_id", "new_role_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "demote_member",
            "description": "Demote a member by removing a role (optionally giving them a lower role in its place), and announce it in the configured promotion-announcement channel (same behavior as the /demote slash command). Resolve user_id via lookup_user and old_role_id/new_role_id via list_roles first — never guess IDs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "old_role_id": {"type": "string", "description": "The role to remove from them"},
                    "new_role_id": {"type": "string", "description": "Optional: the role to drop them down to (leave blank for plain Member)"},
                    "reason": {"type": "string"},
                },
                "required": ["user_id", "old_role_id"],
            },
        },
    },
    # ---- Payment tools ----
    {
        "type": "function",
        "function": {
            "name": "pay_giveaway_claims",
            "description": "Pay all UNPAID winners for a specific giveaway. This only pays claims that have not been paid yet. If a claim is already paid, it will be skipped. Use this when someone says 'pay giveaway <message_id>' or 'pay everyone for this giveaway'. You MUST resolve the giveaway's message_id via list_giveaways first — never guess an ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "giveaway_message_id": {"type": "string", "description": "The giveaway's original message ID (from list_giveaways)"},
                },
                "required": ["giveaway_message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pay_all_claims",
            "description": "Pay all UNPAID winners across ALL giveaways. This only pays claims that have not been paid yet. If a claim is already paid, it will be skipped. Use this when someone says 'pay all claim tickets' or 'pay all winners'.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]

READ_ONLY_TOOLS = {
    "lookup_user", "list_channels", "list_roles", "summarize_channel", "list_warnings",
    "server_stats", "member_insights", "get_audit_log", "list_invites", "list_giveaways",
}
DESTRUCTIVE_TOOLS = {t["function"]["name"] for t in TOOLS} - READ_ONLY_TOOLS


def _friendly_action_description(tool_name: str, args: dict) -> str:
    """Human-readable one-liner shown in the Confirm/Cancel UI."""
    uid = args.get("user_id", "?")
    if tool_name == "kick_member":
        return f"Kick user `{uid}` — reason: {args.get('reason') or 'none given'}"
    if tool_name == "ban_member":
        return f"Ban user `{uid}` (delete {args.get('delete_days', 0)}d of messages) — reason: {args.get('reason') or 'none given'}"
    if tool_name == "unban_member":
        return f"Unban user `{uid}`"
    if tool_name == "timeout_member":
        return f"Timeout user `{uid}` for {args.get('minutes', 10)} minute(s) — reason: {args.get('reason') or 'none given'}"
    if tool_name == "remove_timeout":
        return f"Remove timeout from user `{uid}`"
    if tool_name == "add_role":
        return f"Add role `{args.get('role_id')}` to user `{uid}`"
    if tool_name == "remove_role":
        return f"Remove role `{args.get('role_id')}` from user `{uid}`"
    if tool_name == "send_message":
        text = (args.get("content") or "").strip()
        embed_title = (args.get("embed") or {}).get("title")
        label = f"\"{text[:80]}\"" if text else f"embed \"{embed_title}\"" if embed_title else "an embed"
        return f"Send message to channel `{args.get('channel_id')}`: {label}"
    if tool_name == "dm_user":
        text = (args.get("content") or "").strip()
        embed_title = (args.get("embed") or {}).get("title")
        label = f"\"{text[:80]}\"" if text else f"embed \"{embed_title}\"" if embed_title else "an embed"
        return f"DM user `{uid}`: {label}"
    if tool_name == "create_channel":
        return f"Create channel `#{args.get('name')}`" + (f" under category `{args.get('parent_id')}`" if args.get("parent_id") else "")
    if tool_name == "rename_channel":
        return f"Rename channel `{args.get('channel_id')}` to `#{args.get('name')}`"
    if tool_name == "delete_channel":
        return f"Delete channel `{args.get('channel_id')}`"
    if tool_name == "create_role":
        return f"Create role `{args.get('name')}`"
    if tool_name == "edit_role":
        return f"Edit role `{args.get('role_id')}`" + (f" — rename to `{args.get('name')}`" if args.get("name") else "")
    if tool_name == "delete_role":
        return f"Delete role `{args.get('role_id')}`"
    if tool_name == "delete_message":
        return f"Delete message `{args.get('message_id')}` in channel `{args.get('channel_id')}`" + (f" — reason: {args.get('reason')}" if args.get("reason") else "")
    if tool_name == "pin_message":
        return f"Pin message `{args.get('message_id')}` in channel `{args.get('channel_id')}`"
    if tool_name == "unpin_message":
        return f"Unpin message `{args.get('message_id')}` in channel `{args.get('channel_id')}`"
    if tool_name == "warn_member":
        return f"Warn user `{uid}` — reason: {args.get('reason') or 'none given'}"
    if tool_name == "clear_warnings":
        return f"Clear all warnings for user `{uid}`"
    if tool_name == "set_nickname":
        nick = args.get("nickname")
        return f"Reset nickname for user `{uid}`" if not nick else f"Set nickname for user `{uid}` to `{nick}`"
    if tool_name == "set_afk":
        return f"Mark user `{uid}` AFK — reason: {args.get('reason') or 'No reason provided'}"
    if tool_name == "clear_afk":
        return f"Clear AFK status for user `{uid}`"
    if tool_name == "edit_channel_settings":
        parts = []
        if args.get("topic") is not None:
            parts.append(f"topic to \"{args['topic'][:40]}\"")
        if args.get("slowmode_seconds") is not None:
            parts.append(f"slowmode to {args['slowmode_seconds']}s")
        if args.get("nsfw") is not None:
            parts.append(f"NSFW to {args['nsfw']}")
        if args.get("parent_id") is not None:
            parts.append(f"category to `{args['parent_id']}`")
        return f"Edit channel `{args.get('channel_id')}` — set " + ", ".join(parts) if parts else f"Edit channel `{args.get('channel_id')}`"
    if tool_name == "set_voice_state":
        bits = []
        if args.get("voice_channel_id"):
            bits.append(f"move to voice channel `{args['voice_channel_id']}`")
        if args.get("mute") is not None:
            bits.append(f"mute={args['mute']}")
        if args.get("deafen") is not None:
            bits.append(f"deafen={args['deafen']}")
        return f"User `{uid}`: " + ", ".join(bits) if bits else f"Edit voice state for user `{uid}`"
    if tool_name == "create_invite":
        return f"Create invite link for channel `{args.get('channel_id')}`"
    if tool_name == "delete_invite":
        return f"Revoke invite `{args.get('invite_code')}`"
    if tool_name == "delete_emoji":
        return f"Delete emoji `{args.get('emoji_id')}`"
    if tool_name == "prune_members":
        return f"Kick all members inactive for {args.get('days')}+ days"
    if tool_name == "edit_server_settings":
        return f"Edit server settings: {args}"
    if tool_name == "purge_messages":
        who = f" from user `{args.get('only_from_user_id')}`" if args.get("only_from_user_id") else ""
        return f"Bulk-delete up to {args.get('count')} recent messages{who} in channel `{args.get('channel_id')}`"
    if tool_name == "close_ticket":
        return f"Close ticket `{args.get('channel_id')}` — save transcript, notify creator, and delete the channel"
    if tool_name == "rename_ticket":
        return f"Rename ticket `{args.get('channel_id')}` to `#{args.get('name')}`"
    if tool_name == "add_user_to_ticket":
        return f"Add user `{uid}` to ticket `{args.get('channel_id')}`"
    if tool_name == "remove_user_from_ticket":
        return f"Remove user `{uid}` from ticket `{args.get('channel_id')}`"
    if tool_name == "end_giveaway":
        return f"Force-end giveaway `{args.get('message_id')}` and announce winner(s)"
    if tool_name == "reroll_giveaway":
        return f"Reroll giveaway `{args.get('message_id')}` — pick new winner(s)"
    if tool_name == "delete_giveaway":
        return f"Permanently delete giveaway `{args.get('message_id')}` (cannot be undone)"
    if tool_name == "pay_giveaway_claims":
        return f"Pay all UNPAID winners for giveaway `{args.get('giveaway_message_id')}`"
    if tool_name == "pay_all_claims":
        return f"Pay all UNPAID winners across ALL giveaways"
    if tool_name == "list_giveaways":
        return f"List giveaways with payment status (unpaid claims count)"
    if tool_name == "promote_member":
        base = f"Promote user `{uid}` — give role `{args.get('new_role_id')}`"
        if args.get("old_role_id"):
            base += f" (was role `{args.get('old_role_id')}`)"
        return base + f" — reason: {args.get('reason') or 'none given'}"
    if tool_name == "demote_member":
        base = f"Demote user `{uid}` — remove role `{args.get('old_role_id')}`"
        if args.get("new_role_id"):
            base += f" (drop to role `{args.get('new_role_id')}`)"
        return base + f" — reason: {args.get('reason') or 'none given'}"
    return f"{tool_name}({args})"


def _parse_embed(embed_args) -> dict | None:
    """Turns the model's embed args (see EMBED_SCHEMA) into a Discord embed
    object. Returns None if embed_args is empty/absent so callers can tell
    'no embed' apart from 'empty embed'."""
    if not embed_args:
        return None
    embed = {}
    if embed_args.get("title"):
        embed["title"] = str(embed_args["title"])[:256]
    if embed_args.get("description"):
        embed["description"] = str(embed_args["description"])[:4096]
    if embed_args.get("url"):
        embed["url"] = str(embed_args["url"])
    if embed_args.get("color"):
        c = str(embed_args["color"]).strip().lstrip("#")
        try:
            embed["color"] = int(c, 16) if not c.isdigit() else int(c)
        except ValueError:
            pass  # bad color string — just skip it rather than fail the whole send
    if embed_args.get("footer"):
        embed["footer"] = {"text": str(embed_args["footer"])[:2048]}
    if embed_args.get("thumbnail_url"):
        embed["thumbnail"] = {"url": str(embed_args["thumbnail_url"])}
    if embed_args.get("image_url"):
        embed["image"] = {"url": str(embed_args["image_url"])}
    fields = embed_args.get("fields") or []
    if fields:
        embed["fields"] = [
            {
                "name": str(f.get("name", ""))[:256],
                "value": str(f.get("value", ""))[:1024],
                "inline": bool(f.get("inline", False)),
            }
            for f in fields[:25]
        ]
    return embed or None


DISCORD_EPOCH_MS = 1420070400000  # 2015-01-01T00:00:00Z, per Discord's snowflake spec


def _snowflake_to_iso(snowflake) -> str | None:
    """Decode the timestamp embedded in a Discord snowflake ID (used for
    account/server creation dates — Discord doesn't return these directly)."""
    from datetime import datetime, timezone
    try:
        ms = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _snowflake_to_datetime(snowflake):
    """Same decode as _snowflake_to_iso but returns a datetime, for callers
    (like the ticket transcript generator) that need to call .strftime()."""
    from datetime import datetime, timezone
    try:
        ms = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


GIVEAWAY_ACTION_TIMEOUT = 20  # seconds — generous, but bounded so a hung request can't wedge the dashboard thread forever


def _run_on_bot_loop(bot, coro):
    """Runs a coroutine on the live discord.py bot's event loop from
    whatever thread called us (the Flask dashboard runs in its own thread —
    see bot.py's run_web()), and blocks for the result.

    Needed specifically for giveaway actions: unlike every other tool here,
    which talks to Discord purely over REST, giveaways rely on real
    discord.py View objects (persistent Claim buttons) living in the bot's
    process, so we have to call into the actual Giveaways cog rather than
    re-implement it — see cogs/giveaway.py's *_action methods.

    Raises RuntimeError if no live bot is wired up (e.g. local/dev runs of
    the dashboard without the bot process)."""
    if bot is None or getattr(bot, "loop", None) is None or not bot.loop.is_running():
        raise RuntimeError("The Discord bot isn't connected right now — try again in a moment.")
    future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
    return future.result(timeout=GIVEAWAY_ACTION_TIMEOUT)


def _get_giveaways_cog(bot):
    cog = bot.get_cog("Giveaways") if bot else None
    if cog is None:
        raise RuntimeError("The Giveaways cog isn't loaded.")
    return cog


class _ChannelShim:
    """Minimal stand-in for a discord.py TextChannel object — just enough to
    satisfy cogs.tickets' helper functions (has_ticket_topic, get_creator_name,
    _generate_html), which only ever touch .name / .topic / .created_at.
    Needed because the agent talks to Discord over raw REST (no live
    discord.py Client here), so there's no real channel object to pass in."""
    def __init__(self, name, topic, created_at=None):
        self.name = name
        self.topic = topic
        self.created_at = created_at


def _fetch_all_ticket_messages(discord_api, channel_id, hard_cap: int = 2000):
    """Fetch a ticket channel's full history (oldest-first) for transcript
    generation, in the shape cogs.tickets._generate_html expects. Paginates
    in batches of 100 (Discord's max per request) up to hard_cap messages so
    an unusually long-lived ticket can't loop forever.
    Returns (messages, error) — error is None on success."""
    from datetime import datetime as _dt

    messages = []
    before = None
    while len(messages) < hard_cap:
        params = {"limit": 100}
        if before:
            params["before"] = before
        r = discord_api("GET", f"/channels/{channel_id}/messages", params=params)
        if not r.ok:
            return None, f"HTTP {r.status_code}: {r.text[:200]}"
        batch = r.json()
        if not batch:
            break
        for m in batch:
            author = m.get("author", {}) or {}
            avatar_url = None
            if author.get("avatar"):
                avatar_url = f"https://cdn.discordapp.com/avatars/{author.get('id')}/{author['avatar']}.png"
            ts_raw = m.get("timestamp", "") or ""
            try:
                ts_fmt = _dt.fromisoformat(ts_raw).strftime("%Y-%m-%d %H:%M:%S UTC")
            except ValueError:
                ts_fmt = ts_raw
            messages.append({
                "id": m.get("id"),
                "author": author.get("username", "Unknown"),
                "author_id": author.get("id"),
                "timestamp": ts_fmt,
                "content": m.get("content") or "",
                "attachments": [
                    {"filename": a.get("filename", ""), "url": a.get("url", "")}
                    for a in m.get("attachments", [])
                ],
                "is_bot": bool(author.get("bot")),
                "is_system": False,
                "avatar_url": avatar_url,
            })
        before = batch[-1]["id"]
        if len(batch) < 100:
            break
    messages.reverse()  # Discord returns newest-first; transcripts read oldest-first
    return messages, None


def _parse_ticket_topic(topic: str) -> tuple[str, str]:
    """Mirrors the topic-parsing block in cogs.tickets._close_ticket so the
    agent's transcript looks the same as one closed via /close."""
    creator_name, category = "Unknown", "Unknown"
    topic = topic or ""
    if "Ticket by " in topic:
        creator_name = topic.split("Ticket by ")[1].split(" |")[0].strip()
        if "|" in topic:
            category = topic.split("|")[1].strip()
    elif "Buyer: " in topic:
        creator_name = topic.split("Buyer: ")[1].split(" |")[0].strip()
        if "Build: " in topic:
            category = "Build: " + topic.split("Build: ")[1].split(" |")[0].strip()
    return creator_name, category


# ---------------------------------------------------------------------------
# Read-only tool implementations — need guild_id + the discord_api callable
# ---------------------------------------------------------------------------

def _run_read_only_tool(tool_name: str, args: dict, guild_id: int, discord_api, db, bot=None):
    try:
        if tool_name == "lookup_user":
            query = (args.get("query") or "").lower()
            members = []
            after = None
            for _ in range(5):  # up to ~500 members scanned, matches console_lookup_user's spirit
                params = {"limit": 1000}
                if after:
                    params["after"] = after
                r = discord_api("GET", f"/guilds/{guild_id}/members", params=params)
                if not r.ok:
                    break
                page = r.json()
                if not page:
                    break
                members.extend(page)
                if len(page) < 1000:
                    break
                after = page[-1]["user"]["id"]

            matches = []
            for m in members:
                user = m.get("user", {})
                uname = user.get("username", "")
                nick = m.get("nick") or user.get("global_name") or uname
                if query in uname.lower() or query in nick.lower():
                    matches.append({"id": user.get("id"), "username": uname, "display_name": nick})
            return {"matches": matches[:15]}

        if tool_name == "list_channels":
            r = discord_api("GET", f"/guilds/{guild_id}/channels")
            if not r.ok:
                return {"error": "Could not fetch channels"}
            chans = [{"id": c["id"], "name": c["name"]} for c in r.json() if c.get("type") == 0]
            return {"channels": chans}

        if tool_name == "list_roles":
            r = discord_api("GET", f"/guilds/{guild_id}/roles")
            if not r.ok:
                return {"error": "Could not fetch roles"}
            roles = [{"id": ro["id"], "name": ro["name"]} for ro in r.json() if ro["name"] != "@everyone"]
            return {"roles": roles}

        if tool_name == "summarize_channel":
            channel_id = args.get("channel_id")
            limit = max(1, min(100, int(args.get("limit", 30))))
            r = discord_api("GET", f"/channels/{channel_id}/messages", params={"limit": limit})
            if not r.ok:
                return {"error": "Could not fetch messages — check the bot can view that channel."}
            msgs = r.json()
            simplified = []
            for m in reversed(msgs):
                entry = {"author": m.get("author", {}).get("username", "?"), "content": m.get("content", "")}
                embeds = m.get("embeds") or []
                if embeds:
                    # Bots/webhooks often post info as embeds with little or no
                    # plain `content` — without this, those messages looked
                    # blank to the model even though they clearly weren't.
                    entry["embeds"] = [
                        {
                            "title": (e.get("title") or "")[:200] or None,
                            "description": (e.get("description") or "")[:500] or None,
                            "fields": [
                                {"name": f.get("name", "")[:100], "value": f.get("value", "")[:200]}
                                for f in (e.get("fields") or [])[:10]
                            ] or None,
                            "footer": ((e.get("footer") or {}).get("text") or "")[:200] or None,
                        }
                        for e in embeds[:5]
                    ]
                simplified.append(entry)
            return {"messages": simplified}

        if tool_name == "list_warnings":
            if db is None:
                return {"error": "Database unavailable"}
            user_id = args.get("user_id")
            doc = db["warnings"].find_one({"guild_id": guild_id, "user_id": int(user_id)})
            return {"warnings": doc.get("warnings", []) if doc else []}

        if tool_name == "server_stats":
            r = discord_api("GET", f"/guilds/{guild_id}", params={"with_counts": "true"})
            if not r.ok:
                return {"error": "Could not fetch guild info"}
            guild = r.json()

            rc = discord_api("GET", f"/guilds/{guild_id}/channels")
            channel_breakdown = {}
            if rc.ok:
                # 0=text 2=voice 4=category 5=announcement 13=stage 15=forum
                type_names = {0: "text", 2: "voice", 4: "category", 5: "announcement", 13: "stage", 15: "forum"}
                for c in rc.json():
                    label = type_names.get(c.get("type"), f"type_{c.get('type')}")
                    channel_breakdown[label] = channel_breakdown.get(label, 0) + 1

            rr = discord_api("GET", f"/guilds/{guild_id}/roles")
            role_count = len(rr.json()) if rr.ok else None

            return {
                "name": guild.get("name"),
                "member_count": guild.get("approximate_member_count"),
                "online_count": guild.get("approximate_presence_count"),
                "channel_count": sum(channel_breakdown.values()) if channel_breakdown else None,
                "channel_breakdown": channel_breakdown,
                "role_count": role_count,
                "boost_tier": guild.get("premium_tier"),
                "boost_count": guild.get("premium_subscription_count"),
                "created_at": _snowflake_to_iso(guild.get("id")),
            }

        if tool_name == "member_insights":
            user_id = args.get("user_id")
            r = discord_api("GET", f"/guilds/{guild_id}/members/{user_id}")
            if not r.ok:
                return {"error": f"Could not fetch member — HTTP {r.status_code}"}
            m = r.json()
            user = m.get("user", {})

            warning_count = 0
            if db is not None:
                doc = db["warnings"].find_one({"guild_id": guild_id, "user_id": int(user_id)})
                warning_count = len(doc.get("warnings", [])) if doc else 0

            return {
                "id": user.get("id"),
                "username": user.get("username"),
                "display_name": m.get("nick") or user.get("global_name") or user.get("username"),
                "joined_at": m.get("joined_at"),
                "account_created_at": _snowflake_to_iso(user.get("id")),
                "roles": m.get("roles", []),
                "timed_out_until": m.get("communication_disabled_until"),
                "warning_count": warning_count,
            }

        if tool_name == "get_audit_log":
            limit = min(int(args.get("limit") or 10), 50)
            params = {"limit": limit}
            if args.get("user_id"):
                params["user_id"] = str(args["user_id"])
            r = discord_api("GET", f"/guilds/{guild_id}/audit-log", params=params)
            if not r.ok:
                return {"error": f"Could not fetch audit log — HTTP {r.status_code}"}
            entries = r.json().get("audit_log_entries", [])
            return {
                "entries": [
                    {
                        "id": e.get("id"),
                        "action_type": e.get("action_type"),
                        "user_id": e.get("user_id"),
                        "target_id": e.get("target_id"),
                        "reason": e.get("reason"),
                        "created_at": _snowflake_to_iso(e.get("id")),
                    }
                    for e in entries
                ]
            }

        if tool_name == "list_invites":
            r = discord_api("GET", f"/guilds/{guild_id}/invites")
            if not r.ok:
                return {"error": f"Could not fetch invites — HTTP {r.status_code}"}
            return {
                "invites": [
                    {
                        "code": i.get("code"),
                        "channel_id": (i.get("channel") or {}).get("id"),
                        "channel_name": (i.get("channel") or {}).get("name"),
                        "uses": i.get("uses"),
                        "max_uses": i.get("max_uses"),
                        "max_age_seconds": i.get("max_age"),
                        "created_by": (i.get("inviter") or {}).get("username"),
                    }
                    for i in r.json()
                ]
            }

        elif tool_name == "list_giveaways":
            cog = _get_giveaways_cog(bot)
            query = (args.get("query") or "").lower()
            giveaways = []
            
            # Get claim manager to check payment status
            from cogs.giveaway import ClaimManager
            claim_mgr = ClaimManager(db) if db is not None else None
            
            for msg_id, g in cog.giveaway_data.active_giveaways.items():
                if query and query not in g.title.lower() and query not in g.prize.lower():
                    continue
                
                # Get claims for this giveaway
                claims = []
                unpaid_count = 0
                paid_count = 0
                total_claims = 0
                
                if claim_mgr:
                    claims = claim_mgr.get_claims_for_giveaway(msg_id)
                    total_claims = len(claims)
                    for claim in claims:
                        if claim.get("paid", False):
                            paid_count += 1
                        else:
                            unpaid_count += 1
                
                giveaways.append({
                    "message_id": str(msg_id),
                    "display_id": g.display_id,
                    "title": g.title,
                    "prize": g.prize,
                    "channel_id": str(g.channel_id),
                    "status": "Ended" if g.ended else "Active",
                    "winners_count": g.winners_count,
                    "entries": len(g.entries),
                    "winners": [str(w) for w in g.winners] if g.ended else [],
                    "claimed_users": [str(u) for u in g.claimed_users],  # Users who opened claim tickets
                    "total_claims": total_claims,
                    "unpaid_claims": unpaid_count,
                    "paid_claims": paid_count,
                    "all_claims_paid": unpaid_count == 0 and total_claims > 0,
                })
            return {"giveaways": giveaways}

    except Exception as e:
        logger.error(f"Agent read-only tool '{tool_name}' failed: {e}")
        return {"error": str(e)[:300]}

    return {"error": f"Unknown read-only tool {tool_name}"}


# ---------------------------------------------------------------------------
# Gemini call
# ---------------------------------------------------------------------------

def _post_chat_completion(url, api_key, model, messages, tools=None, temperature=0.3, max_tokens=None):
    """One HTTP call to Gemini's OpenAI-compatible chat-completions endpoint."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "temperature": temperature}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if max_tokens:
        payload["max_tokens"] = max_tokens
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Could not reach {url}: {e}") from e
    if not resp.ok:
        # Quota errors, deprecated/retired model IDs, bad key, etc. all land
        # here — make them legible instead of a bare "500 Server Error"
        # from raise_for_status()
        raise RuntimeError(f"{url} returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _call_llm(messages, tools=None, temperature=0.3, max_tokens=None):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set.")
    return _post_chat_completion(GEMINI_API_URL, GEMINI_API_KEY, GEMINI_MODEL, messages, tools=tools, temperature=temperature, max_tokens=max_tokens)


def simple_chat(messages: list, temperature: float = 0.7, max_tokens: int = 600) -> str:
    """Plain conversational completion — no tools. Used by cogs/ai_chat.py for
    @mention / reply-to-bot conversations in Discord itself (not the dashboard
    agent, which uses run_agent_turn below)."""
    data = _call_llm(messages, temperature=temperature, max_tokens=max_tokens)
    return data["choices"][0]["message"]["content"] or "..."


PROMOTE_COLOR = 0x3BA55D  # green, matches cogs/promotion.py's PROMOTE_COLOR
DEMOTE_COLOR = 0xDD2E44   # red, matches cogs/promotion.py's DEMOTE_COLOR
DEFAULT_PROMOTION_REASON = "No reason provided"


def _get_role_name(discord_api, guild_id, role_id):
    if not role_id:
        return None
    r = discord_api("GET", f"/guilds/{guild_id}/roles")
    if not r.ok:
        return str(role_id)
    for ro in r.json():
        if str(ro.get("id")) == str(role_id):
            return ro.get("name")
    return str(role_id)


def _run_promotion_tool(tool_name, args, guild_id, discord_api, db, reason, actor_name):
    """Implements promote_member / demote_member for the AI agent: gives/removes
    a role via REST and posts the same announcement embed cogs/promotion.py's
    /promote and /demote slash commands post, using the guild's configured
    PROMOTE_ANNOUNCE_CHANNEL_ID from bot_config."""
    user_id = str(args.get("user_id", ""))
    is_promote = tool_name == "promote_member"
    reason = reason or DEFAULT_PROMOTION_REASON

    m = discord_api("GET", f"/guilds/{guild_id}/members/{user_id}")
    if not m.ok:
        return False, f"HTTP {m.status_code}: {m.text[:200]}", ""
    member = m.json()
    user = member.get("user", {}) or {}
    display_name = member.get("nick") or user.get("global_name") or user.get("username") or user_id

    if is_promote:
        new_role_id = args.get("new_role_id")
        old_role_id = args.get("old_role_id")
        current_role_ids = {str(rid) for rid in member.get("roles", [])}
        if str(new_role_id) in current_role_ids:
            new_role_name = _get_role_name(discord_api, guild_id, new_role_id)
            return False, f"{display_name} already has the **{new_role_name}** role.", ""

        r = discord_api("PUT", f"/guilds/{guild_id}/members/{user_id}/roles/{new_role_id}",
                         reason=f"Promoted by {actor_name}: {reason}")
        if not r.ok:
            return False, f"HTTP {r.status_code}: {r.text[:200]}", ""

        new_label = _get_role_name(discord_api, guild_id, new_role_id)
        old_label = _get_role_name(discord_api, guild_id, old_role_id) if old_role_id else None
        detail = f"gave role {new_label} to {display_name}" + (f" (was {old_label})" if old_label else "")
    else:
        old_role_id = args.get("old_role_id")
        new_role_id = args.get("new_role_id")
        current_role_ids = {str(rid) for rid in member.get("roles", [])}
        if str(old_role_id) not in current_role_ids:
            old_role_name = _get_role_name(discord_api, guild_id, old_role_id)
            return False, f"{display_name} doesn't have the **{old_role_name}** role.", ""

        r = discord_api("DELETE", f"/guilds/{guild_id}/members/{user_id}/roles/{old_role_id}",
                         reason=f"Demoted by {actor_name}: {reason}")
        if not r.ok:
            return False, f"HTTP {r.status_code}: {r.text[:200]}", ""

        if new_role_id and str(new_role_id) not in current_role_ids:
            r2 = discord_api("PUT", f"/guilds/{guild_id}/members/{user_id}/roles/{new_role_id}",
                              reason=f"Demoted by {actor_name}: {reason}")
            if not r2.ok:
                return False, f"HTTP {r2.status_code}: {r2.text[:200]}", ""

        old_label = _get_role_name(discord_api, guild_id, old_role_id)
        new_label = _get_role_name(discord_api, guild_id, new_role_id) if new_role_id else "Member"
        detail = f"removed role {old_label} from {display_name}, new rank {new_label}"

    # --- Build and post the announcement embed, same shape as build_rank_embed ---
    title = "⬆️ Promotion" if is_promote else "⬇️ Demotion"
    color = PROMOTE_COLOR if is_promote else DEMOTE_COLOR
    movement_arrow = "⬆️" if is_promote else "⬇️"
    fields = [
        {"name": "👤 Member", "value": f"<@{user_id}>\n`{user.get('username', display_name)}`", "inline": True},
        {"name": "⭐ By", "value": actor_name, "inline": True},
    ]
    if is_promote:
        if old_role_id:
            fields.append({"name": "Movement", "value": f"{movement_arrow} **{old_label}** → **{new_label}**", "inline": False})
        else:
            fields.append({"name": "Role Given", "value": f"{movement_arrow} **{new_label}**", "inline": False})
    else:
        fields.append({"name": "Movement", "value": f"{movement_arrow} **{old_label}** → **{new_label}**", "inline": False})
    fields.append({"name": "❓ Reason", "value": reason, "inline": False})

    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "thumbnail": {"url": user.get("avatar") and f"https://cdn.discordapp.com/avatars/{user_id}/{user['avatar']}.png" or None},
    }
    embed = {k: v for k, v in embed.items() if v is not None and v != {"url": None}}

    if db is not None:
        cfg = db["bot_config"].find_one({"guild_id": guild_id}) or {}
        channel_id = cfg.get("PROMOTE_ANNOUNCE_CHANNEL_ID")
        if channel_id:
            discord_api("POST", f"/channels/{channel_id}/messages", json={"embeds": [embed]})

    return True, None, detail


def _run_destructive_tool(tool_name: str, args: dict, guild_id: int, discord_api, db=None, actor_name: str = "AI", discord_id: str = None, bot=None):
    """Actually performs a destructive action via the Discord REST API.
    Shared by app.py's dashboard confirm-execute route and the trusted-staff
    auto-execute path used from Discord chat (cogs/ai_chat.py).

    `db` is only needed for the warning tools (they write to the `warnings`
    collection directly, same shape list_warnings reads). `actor_name` is
    recorded as the "mod" on new warning entries.

    `discord_id` is the Discord user ID of the person making the request,
    used for MC bot payment operations.

    `bot` is only needed for the giveaway tools — unlike everything else
    here, which talks to Discord purely over REST, giveaways rely on real
    discord.py View objects (persistent Claim buttons), so those three
    tools call into the live Giveaways cog (see _run_on_bot_loop) instead
    of re-implementing it over REST."""
    from datetime import datetime, timedelta  # local import to avoid a hard dep for read-only-only callers

    user_id = str(args.get("user_id", "") or "")
    reason = (args.get("reason") or "Requested via AI").strip()
    ok, error, detail = False, None, ""

    try:
        if tool_name == "kick_member":
            r = discord_api("DELETE", f"/guilds/{guild_id}/members/{user_id}", reason=reason)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = reason

        elif tool_name == "ban_member":
            delete_days = max(0, min(7, int(args.get("delete_days", 0) or 0)))
            r = discord_api(
                "PUT", f"/guilds/{guild_id}/bans/{user_id}",
                reason=reason, json={"delete_message_seconds": delete_days * 86400},
            )
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = reason

        elif tool_name == "unban_member":
            r = discord_api("DELETE", f"/guilds/{guild_id}/bans/{user_id}")
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"

        elif tool_name == "timeout_member":
            minutes = max(1, min(40320, int(args.get("minutes", 10) or 10)))
            until = (datetime.utcnow() + timedelta(minutes=minutes)).isoformat() + "Z"
            r = discord_api(
                "PATCH", f"/guilds/{guild_id}/members/{user_id}",
                reason=reason, json={"communication_disabled_until": until},
            )
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"{minutes}m — {reason}"

        elif tool_name == "remove_timeout":
            r = discord_api(
                "PATCH", f"/guilds/{guild_id}/members/{user_id}",
                json={"communication_disabled_until": None},
            )
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"

        elif tool_name in ("add_role", "remove_role"):
            role_id = args.get("role_id")
            method = "PUT" if tool_name == "add_role" else "DELETE"
            r = discord_api(method, f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}")
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"role {role_id}"

        elif tool_name in ("promote_member", "demote_member"):
            ok, error, detail = _run_promotion_tool(tool_name, args, guild_id, discord_api, db, reason, actor_name)

        elif tool_name == "send_message":
            channel_id = args.get("channel_id")
            content = (args.get("content") or "").strip()[:2000]
            embed = _parse_embed(args.get("embed"))
            if not content and not embed:
                ok, error = False, "Message needs content and/or an embed — both were empty."
            else:
                payload = {}
                if content:
                    payload["content"] = content
                if embed:
                    payload["embeds"] = [embed]
                r = discord_api("POST", f"/channels/{channel_id}/messages", json=payload)
                ok = r.ok
                error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
                detail = f"channel #{channel_id}: {(content or (embed or {}).get('title') or '(embed)')[:80]}"

        elif tool_name == "dm_user":
            content = (args.get("content") or "").strip()[:2000]
            embed = _parse_embed(args.get("embed"))
            if not content and not embed:
                ok, error = False, "Message needs content and/or an embed — both were empty."
            else:
                dm = discord_api("POST", "/users/@me/channels", json={"recipient_id": user_id})
                if not dm.ok:
                    ok, error = False, f"HTTP {dm.status_code}: {dm.text[:200]}"
                else:
                    dm_channel_id = dm.json()["id"]
                    payload = {}
                    if content:
                        payload["content"] = content
                    if embed:
                        payload["embeds"] = [embed]
                    r = discord_api("POST", f"/channels/{dm_channel_id}/messages", json=payload)
                    ok = r.ok
                    error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
                detail = (content or (embed or {}).get("title") or "(embed)")[:80]

        elif tool_name == "create_channel":
            name = (args.get("name") or "").strip()[:100]
            payload = {"name": name, "type": 0}
            if args.get("topic"):
                payload["topic"] = str(args["topic"])[:1024]
            if args.get("parent_id"):
                payload["parent_id"] = str(args["parent_id"])
            r = discord_api("POST", f"/guilds/{guild_id}/channels", reason=reason, json=payload)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"#{name}" if ok else ""

        elif tool_name == "rename_channel":
            channel_id = args.get("channel_id")
            name = (args.get("name") or "").strip()[:100]
            r = discord_api("PATCH", f"/channels/{channel_id}", reason=reason, json={"name": name})
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"channel {channel_id} -> #{name}"

        elif tool_name == "delete_channel":
            channel_id = args.get("channel_id")
            r = discord_api("DELETE", f"/channels/{channel_id}", reason=reason)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"channel {channel_id}"

        elif tool_name == "create_role":
            payload = {"name": (args.get("name") or "new role").strip()[:100]}
            if args.get("color") is not None:
                payload["color"] = int(args["color"])
            if args.get("hoist") is not None:
                payload["hoist"] = bool(args["hoist"])
            if args.get("mentionable") is not None:
                payload["mentionable"] = bool(args["mentionable"])
            r = discord_api("POST", f"/guilds/{guild_id}/roles", reason=reason, json=payload)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = payload["name"] if ok else ""

        elif tool_name == "edit_role":
            role_id = args.get("role_id")
            payload = {}
            if args.get("name") is not None:
                payload["name"] = str(args["name"]).strip()[:100]
            if args.get("color") is not None:
                payload["color"] = int(args["color"])
            if args.get("hoist") is not None:
                payload["hoist"] = bool(args["hoist"])
            if args.get("mentionable") is not None:
                payload["mentionable"] = bool(args["mentionable"])
            r = discord_api("PATCH", f"/guilds/{guild_id}/roles/{role_id}", reason=reason, json=payload)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"role {role_id}"

        elif tool_name == "delete_role":
            role_id = args.get("role_id")
            r = discord_api("DELETE", f"/guilds/{guild_id}/roles/{role_id}", reason=reason)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"role {role_id}"

        elif tool_name == "delete_message":
            channel_id = args.get("channel_id")
            message_id = args.get("message_id")
            r = discord_api("DELETE", f"/channels/{channel_id}/messages/{message_id}", reason=reason)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"message {message_id} in #{channel_id}"

        elif tool_name == "pin_message":
            channel_id = args.get("channel_id")
            message_id = args.get("message_id")
            r = discord_api("PUT", f"/channels/{channel_id}/pins/{message_id}", reason=reason)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"message {message_id} in #{channel_id}"

        elif tool_name == "unpin_message":
            channel_id = args.get("channel_id")
            message_id = args.get("message_id")
            r = discord_api("DELETE", f"/channels/{channel_id}/pins/{message_id}", reason=reason)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"message {message_id} in #{channel_id}"

        elif tool_name == "warn_member":
            if db is None:
                ok, error = False, "Database unavailable"
            else:
                entry = {
                    "reason": reason,
                    "mod": actor_name,
                    "ts": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                }
                # /warnings reads cogs.moderation's in-memory _warnings dict,
                # not the DB directly — writing only to the collection (as
                # this used to do) saves fine but never shows up there until
                # a restart. Write to both, same as the real /warn command.
                moderation_cog._warnings[guild_id][int(user_id)].append(entry)
                moderation_cog.save_user_warnings(db, guild_id, int(user_id), moderation_cog._warnings[guild_id][int(user_id)])
                ok = True
                detail = reason

        elif tool_name == "clear_warnings":
            if db is None:
                ok, error = False, "Database unavailable"
            else:
                moderation_cog._warnings[guild_id][int(user_id)] = []
                moderation_cog.save_user_warnings(db, guild_id, int(user_id), [])
                ok = True
                detail = "all warnings cleared"

        elif tool_name == "set_nickname":
            nick = args.get("nickname") or None  # empty string -> None resets to username
            r = discord_api("PATCH", f"/guilds/{guild_id}/members/{user_id}", reason=reason, json={"nick": nick})
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = nick or "(reset)"

        elif tool_name == "set_afk":
            m = discord_api("GET", f"/guilds/{guild_id}/members/{user_id}")
            if not m.ok:
                ok, error = False, f"HTTP {m.status_code}: {m.text[:200]}"
            else:
                mdata = m.json()
                user_data = mdata.get("user", {}) or {}
                current_nick = mdata.get("nick")
                display_name = current_nick or user_data.get("global_name") or user_data.get("username") or user_id
                afk_reason = (args.get("reason") or "No reason provided").strip()[:200]
                new_nick = afk_cog.afk_nickname(display_name)
                r = discord_api(
                    "PATCH", f"/guilds/{guild_id}/members/{user_id}",
                    reason=f"AFK: {afk_reason}"[:512], json={"nick": new_nick},
                )
                if not r.ok:
                    ok, error = False, f"HTTP {r.status_code}: {r.text[:200]}"
                else:
                    afk_cog.set_afk_entry(guild_id, int(user_id), afk_reason, current_nick, db=db)
                    ok = True
                    detail = afk_reason

        elif tool_name == "clear_afk":
            entry = afk_cog.clear_afk_entry(guild_id, int(user_id), db=db)
            if entry is None:
                ok, error = False, "That member isn't currently marked AFK."
            else:
                r = discord_api(
                    "PATCH", f"/guilds/{guild_id}/members/{user_id}",
                    reason="No longer AFK", json={"nick": entry["original_nick"]},
                )
                if not r.ok:
                    # Status is already cleared in _afk even if the nickname
                    # restore fails (e.g. bot's role dropped below theirs) —
                    # don't leave them stuck "AFK" over a cosmetic failure.
                    ok, error = True, None
                    detail = f"AFK cleared, but nickname restore failed: HTTP {r.status_code}"
                else:
                    ok = True
                    detail = "nickname restored"

        elif tool_name == "edit_channel_settings":
            channel_id = args.get("channel_id")
            payload = {}
            if args.get("topic") is not None:
                payload["topic"] = str(args["topic"])[:1024]
            if args.get("slowmode_seconds") is not None:
                payload["rate_limit_per_user"] = max(0, min(int(args["slowmode_seconds"]), 21600))
            if args.get("nsfw") is not None:
                payload["nsfw"] = bool(args["nsfw"])
            if args.get("parent_id") is not None:
                payload["parent_id"] = str(args["parent_id"])
            r = discord_api("PATCH", f"/channels/{channel_id}", reason=reason, json=payload)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"channel {channel_id}"

        elif tool_name == "set_voice_state":
            payload = {}
            if args.get("voice_channel_id") is not None:
                payload["channel_id"] = str(args["voice_channel_id"])
            if args.get("mute") is not None:
                payload["mute"] = bool(args["mute"])
            if args.get("deafen") is not None:
                payload["deaf"] = bool(args["deafen"])
            r = discord_api("PATCH", f"/guilds/{guild_id}/members/{user_id}", reason=reason, json=payload)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"user {user_id}"

        elif tool_name == "create_invite":
            channel_id = args.get("channel_id")
            payload = {
                "max_age": int(args.get("max_age_seconds", 86400)),
                "max_uses": int(args.get("max_uses", 0)),
                "temporary": bool(args.get("temporary", False)),
            }
            r = discord_api("POST", f"/channels/{channel_id}/invites", reason=reason, json=payload)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"discord.gg/{r.json().get('code')}" if ok else ""

        elif tool_name == "delete_invite":
            code = args.get("invite_code")
            r = discord_api("DELETE", f"/invites/{code}", reason=reason)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = code

        elif tool_name == "delete_emoji":
            emoji_id = args.get("emoji_id")
            r = discord_api("DELETE", f"/guilds/{guild_id}/emojis/{emoji_id}", reason=reason)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"emoji {emoji_id}"

        elif tool_name == "prune_members":
            days = max(1, int(args.get("days") or 7))
            r = discord_api("POST", f"/guilds/{guild_id}/prune", reason=reason, json={"days": days, "compute_prune_count": True})
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"{r.json().get('pruned')} member(s) removed" if ok else ""

        elif tool_name == "edit_server_settings":
            payload = {k: v for k, v in {
                "name": args.get("name"),
                "description": args.get("description"),
                "verification_level": args.get("verification_level"),
            }.items() if v is not None}
            r = discord_api("PATCH", f"/guilds/{guild_id}", reason=reason, json=payload)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = ", ".join(f"{k}={v}" for k, v in payload.items())

        elif tool_name == "purge_messages":
            channel_id = args.get("channel_id")
            count = max(2, min(int(args.get("count") or 2), 100))
            only_from = args.get("only_from_user_id")
            # Discord's bulk-delete endpoint needs message IDs up front, so
            # fetch recent messages first, optionally filter by author, then
            # bulk-delete. Bulk delete only works on messages <14 days old —
            # anything older gets skipped rather than failing the whole call.
            rl = discord_api("GET", f"/channels/{channel_id}/messages", params={"limit": count})
            if not rl.ok:
                ok, error = False, f"HTTP {rl.status_code}: {rl.text[:200]}"
            else:
                msgs = rl.json()
                if only_from:
                    msgs = [m for m in msgs if m.get("author", {}).get("id") == str(only_from)]
                cutoff_ms = 14 * 24 * 60 * 60 * 1000
                now_ms = int(time.time() * 1000)
                ids = [m["id"] for m in msgs if now_ms - ((int(m["id"]) >> 22) + DISCORD_EPOCH_MS) < cutoff_ms]
                if not ids:
                    ok, error = False, "No eligible messages found (either none matched, or all were older than 14 days)"
                elif len(ids) == 1:
                    rd = discord_api("DELETE", f"/channels/{channel_id}/messages/{ids[0]}", reason=reason)
                    ok = rd.ok
                    error = None if ok else f"HTTP {rd.status_code}: {rd.text[:200]}"
                    detail = "1 message deleted"
                else:
                    rd = discord_api("POST", f"/channels/{channel_id}/messages/bulk-delete", reason=reason, json={"messages": ids})
                    ok = rd.ok
                    error = None if ok else f"HTTP {rd.status_code}: {rd.text[:200]}"
                    detail = f"{len(ids)} message(s) deleted"

        elif tool_name == "close_ticket":
            channel_id = args.get("channel_id")
            cr = discord_api("GET", f"/channels/{channel_id}")
            if not cr.ok:
                ok, error = False, f"HTTP {cr.status_code}: {cr.text[:200]}"
            else:
                cdata = cr.json()
                topic = cdata.get("topic") or ""
                shim = _ChannelShim(cdata.get("name", ""), topic, _snowflake_to_datetime(channel_id))
                if not tickets_cog.has_ticket_topic(shim):
                    ok, error = False, "That doesn't look like a ticket channel (no ticket topic found) — refusing to close it as one. Use delete_channel for regular channels."
                else:
                    creator_name, category = _parse_ticket_topic(topic)
                    messages, fetch_error = _fetch_all_ticket_messages(discord_api, channel_id)
                    if fetch_error:
                        ok, error = False, fetch_error
                    else:
                        creator_id = None
                        for m in messages:
                            if not m["is_bot"] and m["author"].lower().startswith(creator_name.lower()):
                                creator_id = m["author_id"]
                                break

                        html_content = tickets_cog._generate_html(shim, messages, actor_name, creator_name, category)

                        transcript_doc = {
                            "_id": tickets_cog.ObjectId(),
                            "guild_id": guild_id,
                            "channel_id": int(channel_id),
                            "channel_name": shim.name,
                            "creator_name": creator_name,
                            "category": category,
                            "closed_by": actor_name,
                            "closed_by_id": None,
                            "created_at": shim.created_at,
                            "closed_at": datetime.utcnow(),
                            "message_count": len(messages),
                            "html_content": html_content,
                            "participants": list({m["author_id"] for m in messages if not m["is_bot"]}),
                        }
                        if db is not None:
                            try:
                                db["transcripts"].insert_one(transcript_doc)
                            except Exception as e:
                                logger.error(f"close_ticket: failed to save transcript: {e}")

                        dashboard_url = os.getenv("DASHBOARD_URL", "https://your-domain.com")
                        cfg = tickets_cog.get_guild_config(db, guild_id) if db is not None else {}
                        tc_id = cfg.get("TRANSCRIPT_CHANNEL_ID") if cfg else None
                        if tc_id:
                            log_embed = {
                                "title": f"📑 Ticket Closed: {shim.name}",
                                "description": (
                                    f"**Category:** {category}\n**Creator:** {creator_name}\n"
                                    f"**Closed By:** {actor_name}\n**Messages:** {len(messages)}"
                                ),
                                "color": 0x5865F2,
                                "fields": [{
                                    "name": "View Transcript",
                                    "value": f"[Click here]({dashboard_url}/transcripts/{transcript_doc['_id']})",
                                    "inline": False,
                                }],
                            }
                            discord_api("POST", f"/channels/{tc_id}/messages", json={"embeds": [log_embed]})

                        if creator_id:
                            dm = discord_api("POST", "/users/@me/channels", json={"recipient_id": creator_id})
                            if dm.ok:
                                dm_channel_id = dm.json()["id"]
                                dm_embed = {
                                    "title": f"📑 Ticket Closed: {shim.name}",
                                    "description": f"Your ticket has been closed by {actor_name}.",
                                    "color": 0x5865F2,
                                    "fields": [{
                                        "name": "View Full Transcript",
                                        "value": f"{dashboard_url}/transcripts/{transcript_doc['_id']}",
                                        "inline": False,
                                    }],
                                }
                                discord_api("POST", f"/channels/{dm_channel_id}/messages", json={"embeds": [dm_embed]})

                        rd = discord_api("DELETE", f"/channels/{channel_id}", reason=reason)
                        ok = rd.ok
                        error = None if ok else f"HTTP {rd.status_code}: {rd.text[:200]}"
                        detail = f"ticket {shim.name} ({len(messages)} messages) — transcript saved"

        elif tool_name == "rename_ticket":
            channel_id = args.get("channel_id")
            name = (args.get("name") or "").strip()[:100]
            cr = discord_api("GET", f"/channels/{channel_id}")
            if not cr.ok:
                ok, error = False, f"HTTP {cr.status_code}: {cr.text[:200]}"
            else:
                cdata = cr.json()
                shim = _ChannelShim(cdata.get("name", ""), cdata.get("topic") or "")
                if not tickets_cog.has_ticket_topic(shim):
                    ok, error = False, "That doesn't look like a ticket channel — use rename_channel for regular channels."
                else:
                    r = discord_api("PATCH", f"/channels/{channel_id}", reason=reason, json={"name": name})
                    ok = r.ok
                    error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
                    detail = f"ticket {channel_id} -> #{name}"

        elif tool_name in ("add_user_to_ticket", "remove_user_from_ticket"):
            channel_id = args.get("channel_id")
            cr = discord_api("GET", f"/channels/{channel_id}")
            if not cr.ok:
                ok, error = False, f"HTTP {cr.status_code}: {cr.text[:200]}"
            else:
                cdata = cr.json()
                shim = _ChannelShim(cdata.get("name", ""), cdata.get("topic") or "")
                if not tickets_cog.has_ticket_topic(shim):
                    ok, error = False, "That doesn't look like a ticket channel."
                elif tool_name == "add_user_to_ticket":
                    # VIEW_CHANNEL | SEND_MESSAGES | READ_MESSAGE_HISTORY | ATTACH_FILES
                    perms = 0x400 | 0x800 | 0x10000 | 0x8000
                    r = discord_api(
                        "PUT", f"/channels/{channel_id}/permissions/{user_id}",
                        reason=reason, json={"type": 1, "allow": str(perms), "deny": "0"},
                    )
                    ok = r.ok
                    error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
                    detail = f"user {user_id} added to ticket {channel_id}"
                else:
                    r = discord_api("DELETE", f"/channels/{channel_id}/permissions/{user_id}", reason=reason)
                    ok = r.ok
                    error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
                    detail = f"user {user_id} removed from ticket {channel_id}"

        elif tool_name == "end_giveaway":
            msg_id = int(args.get("message_id"))
            cog = _get_giveaways_cog(bot)
            ok, message = _run_on_bot_loop(bot, cog.end_giveaway_action(msg_id))
            error = None if ok else message
            detail = message if ok else ""

        elif tool_name == "reroll_giveaway":
            msg_id = int(args.get("message_id"))
            cog = _get_giveaways_cog(bot)
            ok, message, _new_winners = _run_on_bot_loop(bot, cog.reroll_giveaway_action(msg_id))
            error = None if ok else message
            detail = message if ok else ""

        elif tool_name == "delete_giveaway":
            msg_id = int(args.get("message_id"))
            cog = _get_giveaways_cog(bot)
            ok, message = _run_on_bot_loop(bot, cog.delete_giveaway_action(msg_id))
            error = None if ok else message
            detail = message if ok else ""

        elif tool_name == "pay_giveaway_claims":
            from cogs.giveaway import ClaimManager
            
            msg_id = args.get("giveaway_message_id")
            if not msg_id:
                ok, error = False, "Missing giveaway_message_id"
            elif db is None:
                ok, error = False, "Database unavailable"
            else:
                claim_mgr = ClaimManager(db)
                claims = claim_mgr.get_unpaid_claims_for_giveaway(int(msg_id))
                
                if not claims:
                    ok, error = False, "No unpaid claims found for this giveaway"
                else:
                    # Get the payment cog
                    payment_cog = bot.get_cog("GiveawayPayment") if bot else None
                    if not payment_cog:
                        ok, error = False, "Payment cog not available"
                    else:
                        try:
                            # Run the async method on the bot's event loop
                            if bot and bot.loop and bot.loop.is_running():
                                # Get the discord_id - use the passed parameter or try to parse from actor_name
                                user_id = None
                                if discord_id:
                                    try:
                                        user_id = int(discord_id)
                                    except ValueError:
                                        pass
                                
                                # If we still don't have a user_id, try to parse from actor_name
                                if not user_id and actor_name:
                                    # actor_name might be "username#1234" or just the username
                                    # Try to extract the ID if it's in the format "username#1234"
                                    match = re.search(r'#(\d+)', actor_name)
                                    if match:
                                        try:
                                            user_id = int(match.group(1))
                                        except ValueError:
                                            pass
                                
                                if not user_id:
                                    ok, error = False, "Could not determine Discord user ID for MC bot payment. Please link your MC account first."
                                else:
                                    future = asyncio.run_coroutine_threadsafe(
                                        payment_cog.pay_giveaway(
                                            guild_id=guild_id,
                                            discord_id=user_id,
                                            giveaway_message_id=msg_id,
                                            requester_name=actor_name
                                        ),
                                        bot.loop
                                    )
                                    success, message, paid_count, failed_count = future.result(timeout=120)
                                    ok = success
                                    error = None if success else message
                                    detail = f"Paid {paid_count} winners, {failed_count} failed"
                            else:
                                ok, error = False, "Bot event loop not available"
                        except asyncio.TimeoutError:
                            ok, error = False, "Payment processing timed out"
                        except Exception as e:
                            logger.error(f"pay_giveaway_claims failed: {e}")
                            ok, error = False, str(e)[:300]
                            detail = ""

        elif tool_name == "pay_all_claims":
            from cogs.giveaway import ClaimManager
            
            if db is None:
                ok, error = False, "Database unavailable"
            else:
                claim_mgr = ClaimManager(db)
                claims = claim_mgr.get_all_unpaid_claims()
                
                if not claims:
                    ok, error = False, "No unpaid claims found"
                else:
                    payment_cog = bot.get_cog("GiveawayPayment") if bot else None
                    if not payment_cog:
                        ok, error = False, "Payment cog not available"
                    else:
                        try:
                            if bot and bot.loop and bot.loop.is_running():
                                # Get the discord_id - use the passed parameter or try to parse from actor_name
                                user_id = None
                                if discord_id:
                                    try:
                                        user_id = int(discord_id)
                                    except ValueError:
                                        pass
                                
                                if not user_id and actor_name:
                                    match = re.search(r'#(\d+)', actor_name)
                                    if match:
                                        try:
                                            user_id = int(match.group(1))
                                        except ValueError:
                                            pass
                                
                                if not user_id:
                                    ok, error = False, "Could not determine Discord user ID for MC bot payment. Please link your MC account first."
                                else:
                                    future = asyncio.run_coroutine_threadsafe(
                                        payment_cog.pay_all_claims(
                                            guild_id=guild_id,
                                            discord_id=user_id,
                                            requester_name=actor_name
                                        ),
                                        bot.loop
                                    )
                                    success, message, paid_count, failed_count = future.result(timeout=120)
                                    ok = success
                                    error = None if success else message
                                    detail = f"Paid {paid_count} winners across all giveaways, {failed_count} failed"
                            else:
                                ok, error = False, "Bot event loop not available"
                        except asyncio.TimeoutError:
                            ok, error = False, "Payment processing timed out"
                        except Exception as e:
                            logger.error(f"pay_all_claims failed: {e}")
                            ok, error = False, str(e)[:300]
                            detail = ""

        else:
            error = "Unknown action."

    except Exception as e:
        logger.error(f"_run_destructive_tool '{tool_name}' failed: {e}")
        ok, error = False, str(e)[:300]

    return ok, error, detail


def run_agent_turn(
    guild_id: int,
    history: list,
    user_message: str,
    discord_api,
    db,
    auto_execute: bool = False,
    log_action=None,
    actor_name: str = "AI",
    discord_id: str = None,
    bot=None,
):
    """
    Runs one user turn to completion: repeatedly calling Gemini, executing any
    read-only tool calls automatically.

    - auto_execute=False (default; used by any confirm-first caller):
      stops and returns a "pending_action" the first time the model wants
      to call a destructive tool, without running it.
    - auto_execute=True (used by trusted-staff Discord chat, which already
      gated the whole request on the Trusted Staff role): destructive tools
      are executed immediately via discord_api, their result is fed back to
      the model, and the loop continues — no separate confirmation step.
      If log_action(tool_name, args, ok, error, detail) is given, it's
      called after each auto-executed action for audit logging.
      `actor_name` is recorded as the "mod" on any warn_member entries
      written during this turn (defaults to "AI" for the confirm-first
      dashboard path).
      
      `discord_id` is the Discord user ID of the person making the request,
      used for MC bot payment operations.

    Returns:
      {
        "reply": str,
        "history": list,
        "pending_action": dict | None   # only ever set when auto_execute=False
      }
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": user_message}
    ]
    completed_log = []  # human-readable record of auto-executed actions this
                         # turn, so a maxed-out loop can tell the user what
                         # actually happened instead of just "I gave up"

    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            data = _call_llm(messages, tools=TOOLS, temperature=0.3)
            choice = data["choices"][0]["message"]
        except Exception as e:
            # This is the one place a Gemini hiccup (rate limit, timeout,
            # unexpected response shape) used to bubble all the way up to
            # cogs/ai_chat.py's bare except and vanish silently. Turn it
            # into a real reply instead.
            logger.error(f"run_agent_turn: LLM call failed: {e}")
            return {
                "reply": "⚠️ I hit an error talking to the AI backend just now (likely rate-limited or briefly down). Try again in a few seconds.",
                "history": messages[1:],
                "pending_action": None,
            }
        messages.append(choice)

        tool_calls = choice.get("tool_calls")
        if not tool_calls:
            reply = choice.get("content") or "..."
            return {"reply": reply, "history": messages[1:], "pending_action": None}

        for call in tool_calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}

            if name in DESTRUCTIVE_TOOLS and not auto_execute:
                pending = {
                    "tool": name,
                    "args": args,
                    "description": _friendly_action_description(name, args),
                }
                reply = choice.get("content") or f"I'd like to: {pending['description']}"
                return {"reply": reply, "history": messages[1:], "pending_action": pending}

            if name in DESTRUCTIVE_TOOLS:  # auto_execute is True here
                ok, error, detail = _run_destructive_tool(
                    name, args, guild_id, discord_api, 
                    db=db, 
                    actor_name=actor_name, 
                    discord_id=discord_id,
                    bot=bot
                )
                if log_action:
                    try:
                        log_action(name, args, ok, error, detail)
                    except Exception as e:
                        logger.error(f"log_action callback failed: {e}")
                result = {"ok": ok, "error": error} if not ok else {"ok": True, "detail": detail}
                mark = "✅" if ok else "❌"
                line = f"{mark} {_friendly_action_description(name, args)}"
                if not ok:
                    line += f" — failed: {error}"
                completed_log.append(line)
            else:
                result = _run_read_only_tool(name, args, guild_id, discord_api, db, bot=bot)

            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": name,
                "content": json.dumps(result)[:4000],
            })

    if completed_log:
        # We ran out of turns, but real actions did happen — tell the user
        # exactly what, instead of a generic "I gave up" that hides whether
        # anything was actually done to their server.
        summary = "\n".join(completed_log[-20:])
        reply = (
            f"⏳ That request needed more steps than I could finish in one go. "
            f"Here's what I completed before running out:\n{summary}\n\n"
            f"Send another message (e.g. \"continue\") and I'll pick up the rest."
        )
    else:
        reply = "I wasn't able to finish that within a reasonable number of steps — try breaking it into a smaller request."

    return {
        "reply": reply,
        "history": messages[1:],
        "pending_action": None,
    }