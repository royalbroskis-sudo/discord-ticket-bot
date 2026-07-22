"""
cogs/giveaway_payment.py

AI-assisted payment system for giveaway claims.
This is called by the AI agent, NOT by slash commands.
"""

import asyncio
import math
import re
import discord
from discord.ext import commands
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple
import logging

from cogs.config import get_guild_config

logger = logging.getLogger(__name__)


class PaymentManager:
    """Handles giveaway payment calculations and execution."""
    
    @staticmethod
    def calculate_payout(prize: str, winners_count: int) -> str:
        """
        Calculate each winner's payout.
        
        Prize examples: "50m", "100M", "1.2b", "500k", "10k"
        Winners: 5 -> each gets 10m
        Winners: 1 -> each gets 50m
        """
        # Parse the prize amount
        prize = str(prize).strip().lower()
        
        # Extract number and unit - handle decimal numbers too
        match = re.match(r"([\d.]+)\s*([kkmb]?)", prize)
        if not match:
            return prize  # Can't parse, return as-is
        
        amount_str, unit = match.groups()
        try:
            amount = float(amount_str)
        except ValueError:
            return prize
        
        # Convert to base unit (millions)
        multiplier = {"k": 0.001, "m": 1, "b": 1000}
        base_millions = amount * multiplier.get(unit, 1)
        
        # Calculate per winner
        per_winner = base_millions / winners_count
        
        # Round to 1 decimal place
        per_winner = round(per_winner, 1)
        
        # Format back with unit
        if per_winner >= 1000:
            return f"{per_winner/1000:.1f}b"
        elif per_winner >= 1:
            # For values between 1 and 999, show as millions
            # But if the original was in "k" and per_winner is small, show as k
            if unit == "k" and per_winner < 1:
                # Show as k
                return f"{per_winner * 1000:.1f}k"
            else:
                return f"{per_winner:.1f}m"
        else:
            # Show as k for small amounts
            return f"{per_winner * 1000:.1f}k"


class GiveawayPayment(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.claim_manager = None
        if bot.db is not None:
            from cogs.giveaway import ClaimManager
            self.claim_manager = ClaimManager(bot.db)
        
        # Track if we started the bot for a session
        self.bot_started_for_session = {}
    
    async def ensure_mc_bot_online(self, discord_id: int) -> Tuple[bool, str]:
        """
        Ensure the user's Minecraft bot is online.
        Returns (success, message)
        """
        from cogs.mc_link import MCLink
        
        # Check if the user has a linked MC account
        mc_cog = self.bot.get_cog("MCLink")
        if not mc_cog:
            return False, "MC Link cog not available."
        
        # Check current status via mc-bot
        try:
            status = await mc_cog._get(f"/status/{discord_id}")
            if status.get("status") == "ready":
                # Already online and ready
                self.bot_started_for_session[discord_id] = False
                return True, "Already online"
            
            # Start the bot
            await mc_cog._post(f"/start-login/{discord_id}")
            
            # Wait for it to connect (poll up to 30 seconds)
            for _ in range(30):
                await asyncio.sleep(1)
                status = await mc_cog._get(f"/status/{discord_id}")
                if status.get("status") == "ready":
                    self.bot_started_for_session[discord_id] = True
                    return True, "Bot connected"
            
            return False, "Bot failed to connect within 30 seconds"
            
        except Exception as e:
            logger.error(f"Failed to start MC bot: {e}")
            return False, f"Error: {e}"
    
    async def disconnect_mc_bot_if_started(self, discord_id: int):
        """Disconnect the MC bot only if we started it for this session."""
        if not self.bot_started_for_session.get(discord_id, False):
            return
        
        from cogs.mc_link import MCLink
        mc_cog = self.bot.get_cog("MCLink")
        if mc_cog:
            await mc_cog._post(f"/logout/{discord_id}")
            self.bot_started_for_session[discord_id] = False
    
    async def pay_winner(
        self, 
        discord_id: int, 
        mc_ign: str, 
        amount: str, 
        claim_id: str,
        channel_id: int
    ) -> Tuple[bool, str]:
        """
        Pay a single winner via the MC bot.
        The server requires the leading slash for /pay.
        Returns (success, message)
        """
        from cogs.mc_link import MCLink
        mc_cog = self.bot.get_cog("MCLink")
        if not mc_cog:
            return False, "MC Link cog not available."
        
        # The server needs the leading slash for /pay
        command = f"/pay {mc_ign} {amount}"
        result = await mc_cog._post(f"/run-command/{discord_id}", json={"command": command, "captureMs": 3000})
        
        if not result.get("ok"):
            return False, result.get("error", "Payment failed")
        
        # Mark claim as paid
        if self.claim_manager:
            self.claim_manager.mark_paid(claim_id, discord_id, amount)
        
        return True, f"Paid {mc_ign} {amount}"
    
    async def process_claims(
        self,
        discord_id: int,
        claims: List[dict],
        log_channel_id: int = None,
        requester_name: str = "AI",
        guild: discord.Guild = None
    ) -> Tuple[List[dict], List[dict]]:
        """
        Process a list of claims.
        Returns (successful_payments, failed_payments)
        """
        successful = []
        failed = []
        
        if not claims:
            return successful, failed
        
        # Ensure MC bot is online
        online, msg = await self.ensure_mc_bot_online(discord_id)
        if not online:
            for claim in claims:
                failed.append({
                    "claim": claim,
                    "reason": f"Bot not online: {msg}"
                })
            return successful, failed
        
        # Get log channel if ID provided
        log_channel = None
        if log_channel_id and guild:
            log_channel = guild.get_channel(log_channel_id)
        
        try:
            for claim in claims:
                claim_id = str(claim["_id"])
                mc_ign = claim.get("mc_ign", "")
                if not mc_ign or mc_ign == "N/A":
                    failed.append({
                        "claim": claim,
                        "reason": "No IGN saved for this winner"
                    })
                    continue
                
                # Calculate payout
                giveaway_data = claim.get("giveaway_data", {})
                prize = giveaway_data.get("prize", "")
                winners_count = giveaway_data.get("winners_count", 1)
                
                if not prize:
                    failed.append({
                        "claim": claim,
                        "reason": "No prize data available"
                    })
                    continue
                
                amount = PaymentManager.calculate_payout(prize, winners_count)
                
                # Execute payment
                success, message = await self.pay_winner(
                    discord_id, mc_ign, amount, claim_id, claim.get("claim_channel_id")
                )
                
                if success:
                    claim["_paid_amount"] = amount
                    successful.append(claim)
                else:
                    failed.append({
                        "claim": claim,
                        "reason": message
                    })
                
                # Log payment
                await self.log_payment(
                    log_channel,
                    requester_name,
                    discord_id,
                    claim,
                    amount,
                    success,
                    message
                )
                
                # Wait 2 seconds between payments
                await asyncio.sleep(2)
        
        finally:
            # Disconnect only if we started the bot
            await self.disconnect_mc_bot_if_started(discord_id)
        
        return successful, failed
    
    async def log_payment(
        self,
        channel: discord.TextChannel,
        requester_name: str,
        bot_discord_id: int,
        claim: dict,
        amount: str,
        success: bool,
        message: str
    ):
        """Log a payment to the configured channel."""
        if not channel:
            logger.warning(f"Payment log channel not found or disabled")
            return
        
        try:
            # Get the giveaway link
            giveaway_msg_id = claim.get("giveaway_message_id")
            guild = channel.guild
            giveaway_link = f"https://discord.com/channels/{guild.id}/{claim.get('giveaway_data', {}).get('channel_id', 'unknown')}/{giveaway_msg_id}" if giveaway_msg_id else "N/A"
            
            embed = discord.Embed(
                title="💰 Giveaway Payment",
                color=discord.Color.green() if success else discord.Color.red(),
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(name="Paid By", value=requester_name, inline=True)
            embed.add_field(name="Minecraft Account", value=f"<@{bot_discord_id}>" if bot_discord_id else "Unknown", inline=True)
            
            winner_id = claim.get("user_id")
            embed.add_field(name="Winner", value=f"<@{winner_id}>" if winner_id else "Unknown", inline=True)
            
            embed.add_field(name="IGN", value=claim.get("mc_ign", "N/A"), inline=True)
            embed.add_field(name="Amount", value=amount, inline=True)
            embed.add_field(name="Giveaway", value=f"[Link]({giveaway_link})" if giveaway_link != "N/A" else "N/A", inline=True)
            
            embed.add_field(name="Status", value="✅ Success" if success else "❌ Failed", inline=True)
            embed.add_field(name="Time", value=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), inline=True)
            
            if not success:
                embed.add_field(name="Reason", value=message, inline=False)
            
            embed.set_footer(text=f"Claim ID: {claim.get('_id', 'N/A')}")
            
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to log payment: {e}")
    
    async def pay_giveaway(self, guild_id: int, discord_id: int, giveaway_message_id: int, requester_name: str = "AI") -> Tuple[bool, str, int, int]:
        """
        Pay all unpaid winners for a specific giveaway.
        Returns (success, message, paid_count, failed_count)
        """
        if self.claim_manager is None:
            return False, "Database unavailable", 0, 0
        
        try:
            msg_id = int(giveaway_message_id)
        except ValueError:
            return False, "Invalid giveaway ID", 0, 0
        
        claims = self.claim_manager.get_unpaid_claims_for_giveaway(msg_id)
        
        if not claims:
            return False, "No unpaid claims found for this giveaway", 0, 0
        
        # Get log channel
        cfg = get_guild_config(self.bot.db, guild_id)
        log_channel_id = cfg.get("PAYMENT_LOG_CHANNEL_ID")
        
        guild = self.bot.get_guild(guild_id)
        
        successful, failed = await self.process_claims(
            discord_id=discord_id,
            claims=claims,
            log_channel_id=log_channel_id,
            requester_name=requester_name,
            guild=guild
        )
        
        return True, f"Paid {len(successful)} winners, {len(failed)} failed", len(successful), len(failed)
    
    async def pay_all_claims(self, guild_id: int, discord_id: int, requester_name: str = "AI") -> Tuple[bool, str, int, int]:
        """
        Pay all unpaid winners across ALL giveaways.
        Returns (success, message, paid_count, failed_count)
        """
        if self.claim_manager is None:
            return False, "Database unavailable", 0, 0
        
        claims = self.claim_manager.get_all_unpaid_claims()
        
        if not claims:
            return False, "No unpaid claims found", 0, 0
        
        # Get log channel
        cfg = get_guild_config(self.bot.db, guild_id)
        log_channel_id = cfg.get("PAYMENT_LOG_CHANNEL_ID")
        
        guild = self.bot.get_guild(guild_id)
        
        successful, failed = await self.process_claims(
            discord_id=discord_id,
            claims=claims,
            log_channel_id=log_channel_id,
            requester_name=requester_name,
            guild=guild
        )
        
        return True, f"Paid {len(successful)} winners across all giveaways, {len(failed)} failed", len(successful), len(failed)


async def setup(bot: commands.Bot):
    await bot.add_cog(GiveawayPayment(bot))