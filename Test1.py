import discord
import os
import asyncio
import logging
import aiosqlite
from discord.ui import Select, View
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import random

# ------------------- Setup ------------------- #

# Load environment variables from .env file
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)

# Define bot intents
intents = discord.Intents.default()
intents.message_content = True  # Enable reading message content
intents.reactions = True        # Enable reaction tracking
intents.members = True          # Enable member intents

# Initialize the bot
bot = commands.Bot(command_prefix="!", intents=intents)

# Lock for concurrent access
data_lock = asyncio.Lock()

# ---------------- Helper Functions ---------------- #

# Function to get or create a database for each server
async def get_database_for_guild(guild_id):
    db_filename = f'player_values_{guild_id}.db'
    conn = await aiosqlite.connect(db_filename)
    async with conn.cursor() as cursor:
        # Create player_values table if it doesn't exist
        await cursor.execute('''CREATE TABLE IF NOT EXISTS player_values (
                                player_id INTEGER PRIMARY KEY, 
                                total_value REAL
                              )''')
        # Create settings table if it doesn't exist
        await cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
                                setting_name TEXT PRIMARY KEY,
                                role_id INTEGER
                              )''')
        await conn.commit()
    return conn

# Helper functions to interact with the database
async def get_player_value(conn, player_id):
    async with conn.cursor() as cursor:
        await cursor.execute(
            "SELECT total_value FROM player_values WHERE player_id = ?",
            (player_id,))
        result = await cursor.fetchone()
    return result[0] if result else 0

async def update_player_value(conn, player_id, new_value):
    async with conn.cursor() as cursor:
        await cursor.execute(
            "INSERT OR REPLACE INTO player_values (player_id, total_value) "
            "VALUES (?, ?)", (player_id, new_value))
        await conn.commit()

async def reset_player_value(conn, player_id):
    async with conn.cursor() as cursor:
        await cursor.execute(
            "UPDATE player_values SET total_value = 0 WHERE player_id = ?",
            (player_id,))
        await conn.commit()

async def add_to_player_value(conn, player_id, amount):
    current_value = await get_player_value(conn, player_id)
    await update_player_value(conn, player_id, current_value + amount)

async def remove_from_player_value(conn, player_id, amount):
    current_value = await get_player_value(conn, player_id)
    await update_player_value(conn, player_id,
                              max(current_value - amount, 0))

# Helper functions to interact with settings
async def set_role_setting(conn, setting_name, role_id):
    async with conn.cursor() as cursor:
        await cursor.execute(
            "INSERT OR REPLACE INTO settings (setting_name, role_id) VALUES (?, ?)",
            (setting_name, role_id))
        await conn.commit()

async def get_role_setting(conn, setting_name):
    async with conn.cursor() as cursor:
        await cursor.execute(
            "SELECT role_id FROM settings WHERE setting_name = ?",
            (setting_name,))
        result = await cursor.fetchone()
    return result[0] if result else None

# Function to format numbers
def format_number(value):
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    elif value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(int(value))

# Function to parse value with suffixes (k, m)
def parse_value(value_str):
    value_str = value_str.lower()
    try:
        if value_str.endswith('k'):
            return float(value_str[:-1]) * 1_000
        elif value_str.endswith('m'):
            return float(value_str[:-1]) * 1_000_000
        else:
            return float(value_str)
    except ValueError:
        return None
# Function to track event time
async def track_event_time(interaction, event_start_time):
    five_minute_reminder_sent = False

    while True:
        now = datetime.now(timezone.utc)
        remaining_time = event_start_time - now

        if remaining_time.total_seconds() <= 0:
            await interaction.followup.send("🚨 Time's up! The event is starting now! 🚨")
            break

        if remaining_time.total_seconds() <= 300 and not five_minute_reminder_sent:
            await interaction.followup.send("⚠️ Reminder: The event starts in 5 minutes! ⚠️")
            five_minute_reminder_sent = True

        await asyncio.sleep(60)

# ---------------- Bot Events ---------------- #

@bot.event
async def on_ready():
    print(f"Bot is online as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Slash commands synced: {len(synced)} commands")
    except Exception as e:
        print(f"Error syncing slash commands: {e}")

# ---------------- BotSettings Cog ---------------- #

class BotSettings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # Ensure only administrators can use the settings commands
    async def cog_check(self, interaction: discord.Interaction):
        if interaction.user.guild_permissions.administrator:
            return True
        else:
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return False

    settings_group = app_commands.Group(name="settings", description="Configure bot settings")

    @settings_group.command(name="set_role", description="Set a role for a specific permission")
    @app_commands.describe(role="Role to assign")
    @app_commands.choices(permission_name=[
        app_commands.Choice(name="Split-admin rights", value="Split-admin rights"),
        app_commands.Choice(name="Content Admin rights", value="Content Admin rights")
    ])
    async def set_role(self, interaction: discord.Interaction, permission_name: app_commands.Choice[str], role: discord.Role):
        guild_id = interaction.guild_id
        conn = await get_database_for_guild(guild_id)

        await set_role_setting(conn, permission_name.value, role.id)
        await interaction.response.send_message(f"Set '{permission_name.value}' to role '{role.name}'.", ephemeral=True)
        await conn.close()

    @settings_group.command(name="view_roles", description="View current role settings")
    async def view_roles(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        conn = await get_database_for_guild(guild_id)
        embed = discord.Embed(title="Current Role Settings", color=discord.Color.blue())
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT setting_name, role_id FROM settings")
            rows = await cursor.fetchall()
            if rows:
                for setting_name, role_id in rows:
                    role = interaction.guild.get_role(role_id)
                    role_name = role.name if role else "Role not found"
                    embed.add_field(name=setting_name, value=role_name, inline=False)
            else:
                embed.description = "No settings found."
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await conn.close()

# ---------------- RaidAnnouncement Cog ---------------- #

class RaidAnnouncement(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active_events = {}
        self.message_id_to_event_id = {}

    @app_commands.command(name="f2b", description="Create a raid announcement for Roads F2B Comp")
    @app_commands.describe(time="Time when the event starts (HH:MM UTC)", role="Mention a role to ping")
    async def f2b(self, interaction: discord.Interaction, time: str, role: discord.Role = None):
        await interaction.response.defer()

        try:
            event_start_time = datetime.strptime(time, "%H:%M").replace(tzinfo=timezone.utc)
            now_utc = datetime.now(timezone.utc)
            event_start_time = event_start_time.replace(year=now_utc.year, month=now_utc.month, day=now_utc.day)

            if event_start_time < now_utc:
                event_start_time += timedelta(days=1)
        except ValueError:
            await interaction.followup.send("Invalid time format. Please use HH:MM UTC (e.g., 14:00 UTC).", ephemeral=True)
            return

        # Ensure role pings work by formatting the role mention properly
        ping_text = f"<@&{role.id}>" if role else ""

        event_id = random.randint(1000, 9999)
        while event_id in self.active_events:
            event_id = random.randint(1000, 9999)

        # Enhanced roles for F2B with emojis, using 🔥 for Dawnsong (fire weapon)
        roles_list = [
            ("🛡️ Tank", "Frontline Defender"),
            ("🪓 Carrioncaller", "Axe DPS Specialist"),
            ("💀 Curseskull", "Curse Caster"),
            ("🐻 Bear", "Damage Absorber"),
            ("🌳 Ent", "Nature Support"),
            ("🔥 Dawnsong", "Fire Magic DPS"),
            ("💚 Hallowfall", "Healing Support")
        ]

        user_roles = {role_name: [] for role_name, _ in roles_list}
        active_users_roles = {}

        # Track cancellation state
        self.active_events[event_id] = {
            'message_id': None,
            'channel_id': interaction.channel_id,
            'user_roles': user_roles,
            'active_users_roles': active_users_roles,
            'start_time': event_start_time,
            'cancelled': False  # New key to track if the event is canceled
        }

        # Updated Embed for F2B with more visual appeal
        embed = discord.Embed(
            title=f"🛡️ Roads F2B Comp - {time} UTC",
            description="**Gear Requirements:**\n6.3+ | PVP Food 8.2 | PVE Food 6.0 (min 2 each)",
            color=discord.Color.green()
        )

        embed.add_field(name="⚔️ **Gear & Mounts**", value="• Overcharge (OC)\n• Fast Mount (130%+)", inline=False)

        roles_needed = "\n".join([f"{emoji} {role_name} - *{role_desc}*" for emoji, (role_name, role_desc) in zip(
            ["🛡️", "🪓", "💀", "🐻", "🌳", "🔥", "💚"], roles_list)])
        embed.add_field(name="🔍 **Roles Needed**", value=roles_needed, inline=False)

        embed.add_field(name="📢 **Important Notes**", value="Stay calm, follow calls, and avoid tilting. Let's have fun!", inline=False)

        view = View(timeout=None)

        def role_signup_callback(role_name):
            async def callback(button_interaction: discord.Interaction):
                message_id = button_interaction.message.id
                event_id_lookup = self.message_id_to_event_id.get(message_id)
                if event_id_lookup is None:
                    await button_interaction.response.send_message("Error: Event not found.", ephemeral=True)
                    return

                event_data = self.active_events[event_id_lookup]
                if event_data['cancelled']:
                    await button_interaction.response.send_message("Event has been canceled.", ephemeral=True)
                    return

                user_roles = event_data['user_roles']
                active_users_roles = event_data['active_users_roles']

                user_mention = button_interaction.user.mention
                async with data_lock:
                    if user_mention in user_roles[role_name]:
                        user_roles[role_name].remove(user_mention)
                        active_users_roles.pop(user_mention, None)
                    else:
                        if user_mention in active_users_roles:
                            await button_interaction.response.send_message(
                                "You have already signed up for a role. Unsign first.", ephemeral=True)
                            return
                        for other_role in user_roles:
                            if user_mention in user_roles[other_role]:
                                user_roles[other_role].remove(user_mention)
                        user_roles[role_name].append(user_mention)
                        active_users_roles[user_mention] = role_name

                updated_embed = button_interaction.message.embeds[0]
                roles_needed_updated = ""
                for role in roles_list:
                    users = user_roles[role[0]]
                    if users:
                        user_list = ', '.join(users)
                        roles_needed_updated += f"{role[0]}: {user_list}\n"
                    else:
                        roles_needed_updated += f"{role[0]}\n"

                updated_embed.set_field_at(
                    index=1,  # Index of the "🔍 Roles Needed" field
                    name="🔍 **Roles Needed**",
                    value=roles_needed_updated.strip(),
                    inline=False
                )
                await button_interaction.response.edit_message(embed=updated_embed)

            return callback

        # Add visually enhanced buttons for each role
        for emoji, (role, _) in zip(["🛡️", "🪓", "💀", "🐻", "🌳", "🔥", "💚"], roles_list):
            button = Button(label=role, style=discord.ButtonStyle.primary, emoji=emoji)
            button.callback = role_signup_callback(role)
            view.add_item(button)

        # Add cancel event button
        async def cancel_event_callback(button_interaction: discord.Interaction):
            message_id = button_interaction.message.id
            event_id_lookup = self.message_id_to_event_id.get(message_id)
            if event_id_lookup is None:
                await button_interaction.response.send_message("Error: Event not found.", ephemeral=True)
                return

            guild_id = button_interaction.guild_id
            conn = await get_database_for_guild(guild_id)
            role_id = await get_role_setting(conn, "Content Admin rights")
            await conn.close()

            if role_id:
                content_role = button_interaction.guild.get_role(role_id)
                if content_role in button_interaction.user.roles or button_interaction.user == interaction.user:
                    await button_interaction.response.send_message("🚫 Event cancelled by the event creator.", ephemeral=True)
                    await button_interaction.message.delete()
                    if event_id_lookup in self.active_events:
                        self.active_events[event_id_lookup]['cancelled'] = True
                        del self.active_events[event_id_lookup]
                        del self.message_id_to_event_id[message_id]
                else:
                    await button_interaction.response.send_message(
                        "You do not have permission to cancel the event.", ephemeral=True)
            else:
                await button_interaction.response.send_message(
                    "Content Admin role is not set. Please set it using /settings.", ephemeral=True)

        cancel_button = Button(label="🚫 Cancel Event", style=discord.ButtonStyle.danger)
        cancel_button.callback = cancel_event_callback
        view.add_item(cancel_button)

        message_response = await interaction.followup.send(content=ping_text, embed=embed, view=view)
        self.active_events[event_id]['message_id'] = message_response.id
        self.message_id_to_event_id[message_response.id] = event_id

        # Call the event timer tracking
        await self.track_event_time(event_id, event_start_time, interaction.channel)

    # Updated event tracking to stop if the event is canceled
    async def track_event_time(self, event_id, event_start_time, channel):
        five_minute_reminder_sent = False

        while True:
            now = datetime.now(timezone.utc)
            remaining_time = event_start_time - now

            if self.active_events.get(event_id, {}).get('cancelled', False):
                # If the event is canceled, exit the loop
                break

            if remaining_time.total_seconds() <= 0:
                await channel.send("🚨 Time's up! The event is starting now! 🚨")
                break

            if remaining_time.total_seconds() <= 300 and not five_minute_reminder_sent:
                await channel.send("⚠️ Reminder: The event starts in 5 minutes! ⚠️")
                five_minute_reminder_sent = True

            await asyncio.sleep(60)

# ---------------- FFAnnouncement Cog ---------------- #

class FFAnnouncement(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active_events = {}
        self.message_id_to_event_id = {}

    role_choices = {
        "Roads-Pve": [
            "🛡️ Incubus", "🔥 Blazing", "💀 Shadowcaller", "🌳 Ironroot", 
            "❄️ Frost", "🕊️ Holy Staff", "🏹 Longbow", "⚔️ Spirit-Hunter", "⚡ Realm-Breaker"
        ],
        "Static Run": [
            "🛡️ 1hand-Mace", "🛡️ Incubus", "⚖️ HoJ", "🌿 EarthRune", "🕊️ Holy Staff", 
            "💀 Shadowcaller", "⚡ Realm-Breaker", "🔥 Blazing", "⭐ Astral-staff"
        ],
        "Ava-Raid": [
            "🌟 Lightcaller1", "🌟 Lightcaller2", "🏹 Xbow1", "🏹 Xbow2", "🏹 Xbow3", 
            "🏹 Xbow4", "🔥 Blazing", "🔆 Dawnsong", "❄️ Chillhowl1", "❄️ Chillhowl2", 
            "❄️ Chillhowl3", "👻 Specterjacket", "💀 Curse", "⚔️ Carving", "⚔️ Spirit-Hunter", 
            "⚡ Realm-Breaker", "💀 Curse'supp", "🌳 Iron-root", "✨ 1hand-arcane", 
            "✨ Great-arcane", "🕊️ Mainheal", "🛡️ Offtank"
        ],
        "Ganking": [
            "🏃 Doubble-bladed1", "🏃 Doubble-bladed2", "🏃 Doubble-bladed3", 
            "💥 Oneshot-Xbow1", "💥 Oneshot-Xbow2", "💥 Oneshot-Xbow3", 
            "👹 Claws Fiend-robe Swap1", "👹 Claws Fiend-robe Swap2", 
            "👹 Claws Fiend-robe Swap3", "💀 1hand Curse Fiend-Robe Swap1", 
            "💀 1hand Curse Fiend-Robe Swap2", "💀 1hand Curse Fiend-Robe Swap3", 
            "✨ Staff of Balance", "🐾 Bearpaws Fiend-Robe Swap1", 
            "🐾 Bearpaws Fiend-Robe Swap2", "🐾 Bearpaws Fiend-Robe Swap3"
        ]
    }

    async def title_autocomplete(self, interaction: discord.Interaction, current: str):
        titles = ["Roads-Pve", "Static Run", "Ava-Raid", "Ganking"]
        return [
            app_commands.Choice(name=title, value=title) 
            for title in titles if current.lower() in title.lower()
        ]

    @app_commands.command(name="ff", description="Create a customizable raid announcement")
    @app_commands.autocomplete(title=title_autocomplete)
    @app_commands.describe(title="Title of the event", start_time="Time when the event starts (HH:MM UTC)", roles_to_ping="Mention roles to ping")
    async def ff(self, interaction: discord.Interaction, title: str, start_time: str, roles_to_ping: str = None):
        await interaction.response.defer()

        try:
            event_start_time = datetime.strptime(start_time, "%H:%M").replace(tzinfo=timezone.utc)
            now_utc = datetime.now(timezone.utc)
            event_start_time = event_start_time.replace(year=now_utc.year, month=now_utc.month, day=now_utc.day)
            
            if event_start_time < now_utc:
                event_start_time += timedelta(days=1)
        except ValueError:
            await interaction.followup.send("Invalid time format. Please use HH:MM UTC (e.g., 14:00 UTC).", ephemeral=True)
            return

        # Fix role ping: check if roles_to_ping is a string or a discord.Role object
        if isinstance(roles_to_ping, discord.Role):
            ping_text = f"<@&{roles_to_ping.id}>"
        else:
            ping_text = roles_to_ping if roles_to_ping else ""

        event_id = random.randint(1000, 9999)
        while event_id in self.active_events:
            event_id = random.randint(1000, 9999)

        if title in self.role_choices:
            roles_list = self.role_choices[title]
        else:
            await interaction.followup.send(f"Invalid title. Available titles are: {', '.join(self.role_choices.keys())}.", ephemeral=True)
            return

        user_roles = {role_name: [] for role_name in roles_list}
        active_users_roles = {}

        self.active_events[event_id] = {
            'message_id': None,
            'channel_id': interaction.channel_id,
            'user_roles': user_roles,
            'active_users_roles': active_users_roles,
            'start_time': event_start_time,
            'cancelled': False
        }

        embed = discord.Embed(
            title=f"⚔️ {title} Event - {start_time} UTC",
            description=f"The event starts at **{start_time} UTC**.\nSelect roles from the dropdown below.",
            color=discord.Color.blue()
        )
        embed.add_field(name="🔍 **Roles Needed**", value="No roles selected yet.", inline=False)

        class RoleSelect(Select):
            def __init__(self, roles_list, message_id_to_event_id, event_id, active_events):
                self.message_id_to_event_id = message_id_to_event_id
                self.event_id = event_id
                self.active_events = active_events
                options = [
                    discord.SelectOption(label=role, description=f"Select {role}")
                    for role in roles_list
                ]
                super().__init__(placeholder="Select roles", min_values=1, max_values=len(roles_list), options=options)

            async def callback(self, select_interaction: discord.Interaction):
                selected_roles = self.values

                if not selected_roles:
                    await select_interaction.response.send_message("Please select at least one role.", ephemeral=True)
                    return

                user_roles_str = "\n".join(selected_roles)
                embed.set_field_at(0, name="🔍 **Roles Needed**", value=user_roles_str)

                signup_buttons_view = View(timeout=None)
                for role in selected_roles:
                    button = Button(label=role, style=discord.ButtonStyle.primary)
                    
                    async def signup_callback(interaction: discord.Interaction, role=role):
                        message_id = interaction.message.id
                        event_id_lookup = self.message_id_to_event_id.get(message_id)
                        if event_id_lookup is None:
                            await interaction.response.send_message("Error: Event not found.", ephemeral=True)
                            return

                        event_data = self.active_events[event_id_lookup]
                        if event_data['cancelled']:
                            await interaction.response.send_message("Event has been canceled.", ephemeral=True)
                            return

                        user_roles = event_data['user_roles']
                        active_users_roles = event_data['active_users_roles']

                        user_mention = interaction.user.mention
                        async with data_lock:
                            if user_mention in user_roles[role]:
                                user_roles[role].remove(user_mention)
                                active_users_roles.pop(user_mention, None)
                            else:
                                if user_mention in active_users_roles:
                                    await interaction.response.send_message(
                                        "You have already signed up for a role. Unsign first.", ephemeral=True)
                                    return
                                for other_role in user_roles:
                                    if user_mention in user_roles[other_role]:
                                        user_roles[other_role].remove(user_mention)
                                user_roles[role].append(user_mention)
                                active_users_roles[user_mention] = role

                        updated_embed = interaction.message.embeds[0]
                        roles_needed_updated = ""
                        for r in selected_roles:
                            users = user_roles[r]
                            if users:
                                user_list = ', '.join(users)
                                roles_needed_updated += f"{r}: {user_list}\n"
                            else:
                                roles_needed_updated += f"{r}\n"

                        updated_embed.set_field_at(
                            index=0,
                            name="🔍 **Roles Needed**",
                            value=roles_needed_updated.strip(),
                            inline=False
                        )
                        await interaction.response.edit_message(embed=updated_embed)

                    button.callback = signup_callback
                    signup_buttons_view.add_item(button)

                cancel_button = Button(label="🚫 Cancel Event", style=discord.ButtonStyle.danger)
                cancel_button.callback = cancel_event_callback
                signup_buttons_view.add_item(cancel_button)

                await select_interaction.response.edit_message(embed=embed, view=signup_buttons_view)

        view = View(timeout=None)
        role_select = RoleSelect(roles_list=roles_list, message_id_to_event_id=self.message_id_to_event_id, event_id=event_id, active_events=self.active_events)
        view.add_item(role_select)

        async def cancel_event_callback(button_interaction: discord.Interaction):
            message_id = button_interaction.message.id
            event_id_lookup = self.message_id_to_event_id.get(message_id)
            if event_id_lookup is None:
                await button_interaction.response.send_message("Error: Event not found.", ephemeral=True)
                return

            guild_id = button_interaction.guild_id
            conn = await get_database_for_guild(guild_id)
            role_id = await get_role_setting(conn, "Content Admin rights")
            await conn.close()

            if role_id:
                content_role = button_interaction.guild.get_role(role_id)
                if content_role in button_interaction.user.roles or button_interaction.user == interaction.user:
                    event_data = self.active_events[event_id_lookup]
                    event_data['cancelled'] = True
                    await button_interaction.response.send_message("🚫 Event cancelled by the event creator.", ephemeral=False)
                    await button_interaction.message.delete()
                    if event_id_lookup in self.active_events:
                        del self.active_events[event_id_lookup]
                        del self.message_id_to_event_id[message_id]
                else:
                    await button_interaction.response.send_message(
                        "You do not have permission to cancel the event.", ephemeral=True)
            else:
                await button_interaction.response.send_message(
                    "Content Admin role is not set. Please set it using /settings.", ephemeral=True)

        cancel_button = Button(label="🚫 Cancel Event", style=discord.ButtonStyle.danger)
        cancel_button.callback = cancel_event_callback
        view.add_item(cancel_button)

        message_response = await interaction.followup.send(content=ping_text, embed=embed, view=view)
        self.active_events[event_id]['message_id'] = message_response.id
        self.message_id_to_event_id[message_response.id] = event_id

        await track_event_time(interaction, event_start_time, event_id)

# Update track_event_time to include event_id and stop when the event is canceled
async def track_event_time(interaction, event_start_time, event_id):
    five_minute_reminder_sent = False
    channel = interaction.channel

    while True:
        now = datetime.now(timezone.utc)
        remaining_time = event_start_time - now

        # Check if the event is canceled
        if interaction.cog.active_events[event_id]['cancelled']:
            break

        if remaining_time.total_seconds() <= 0:
            await channel.send("🚨 Time's up! The event is starting now! 🚨")
            break

        if remaining_time.total_seconds() <= 300 and not five_minute_reminder_sent:
            await channel.send("⚠️ Reminder: The event starts in 5 minutes! ⚠️")
            five_minute_reminder_sent = True

        await asyncio.sleep(60)

# ---------------- SplitTracker Cog with Enhanced Visuals and Verification for Non-Threads ---------------- #

class SplitTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.tagged_members = {}
        self.tracked_message_ids = {}

    @app_commands.command(name="split", description="Tag players and assign an optional value")
    @app_commands.describe(value="Optional value to split among tagged players", players="Mention players separated by spaces")
    async def split_members(self, interaction: discord.Interaction, players: str, value: str = None):
        async with data_lock:
            if not players:
                await interaction.response.send_message("Please provide player mentions separated by spaces.", ephemeral=True)
                return

            guild_id = interaction.guild_id
            conn = await get_database_for_guild(guild_id)

            player_mentions = players.split()
            members = []
            mentions = []

            for mention in player_mentions:
                if mention.startswith('<@') and mention.endswith('>'):
                    member_id = mention[2:-1].replace('!', '')
                    try:
                        member = await interaction.guild.fetch_member(int(member_id))
                        if member:
                            members.append(member)
                            mentions.append(member.mention)
                    except ValueError:
                        await interaction.response.send_message(f"Invalid mention: {mention}", ephemeral=True)
                        await conn.close()
                        return
                else:
                    await interaction.response.send_message(f"Invalid mention format: {mention}", ephemeral=True)
                    await conn.close()
                    return

            post_id = interaction.channel.id
            self.tagged_members[post_id] = {
                'players': {member: {'submitted': False, 'image_count': 0, 'value_added': False} for member in members},
                'worth': 0
            }

            # Adding loot split value
            if value is not None and len(members) > 0:
                worth = parse_value(value)
                if worth is None:
                    await interaction.response.send_message("Invalid value format. Use numbers with 'k' or 'm' as suffix.", ephemeral=True)
                    await conn.close()
                    return
                worth = worth / len(members)
                self.tagged_members[post_id]['worth'] = worth
                formatted_worth = format_number(worth)

                # **Enhanced Visuals for the Embed**
                embed = discord.Embed(
                    title="💰 **Loot Distribution in Progress** 💰",
                    description=f"**Each player's share:** `{formatted_worth}` 💸\n\nTracking loot submissions below:",
                    color=discord.Color.gold()
                )

                player_info = []
                for member in members:
                    checkmark = "❌"
                    current_total_value = await get_player_value(conn, member.id)
                    formatted_value = f"`{formatted_worth}`"
                    total_value = f"💰 `{format_number(current_total_value)}`"
                    player_info.append(f"{member.display_name} {checkmark} | Share: {formatted_value} | Total Loot: {total_value}")

                embed.add_field(
                    name="👑 **Loot Split Participants** 👑",
                    value="\n".join(player_info),
                    inline=False
                )

                embed.set_footer(text="📸 Submit loot screenshots to confirm participation")

                mentions_text = " ".join(mentions)
                await interaction.response.send_message(f"📢 Loot split starting! {mentions_text} 💸")
                message = await interaction.followup.send(embed=embed)
                self.tracked_message_ids[post_id] = message.id
                print(f"Embed created, message ID stored: {message.id}")

            else:
                embed = discord.Embed(
                    title="💰 **Loot Split** 💰",
                    description="No value provided for this loot split.\nTracking loot submissions below:",
                    color=discord.Color.gold()
                )

                player_info = []
                for member in members:
                    checkmark = "❌"
                    current_total_value = await get_player_value(conn, member.id)
                    formatted_value = "`0`"
                    total_value = f"💰 `{format_number(current_total_value)}`"
                    player_info.append(f"{member.display_name} {checkmark} | Share: {formatted_value} | Total Loot: {total_value}")

                embed.add_field(
                    name="👑 **Loot Split Participants** 👑",
                    value="\n".join(player_info),
                    inline=False
                )

                embed.set_footer(text="📸 Submit loot screenshots to confirm participation")

                mentions_text = " ".join(mentions)
                await interaction.response.send_message(f"📢 Loot split starting! {mentions_text} 💸")
                message = await interaction.followup.send(embed=embed)
                self.tracked_message_ids[post_id] = message.id
                print(f"Embed created, message ID stored: {message.id}")

            await conn.close()

    # Event listener to track image submissions and add checkmark reaction
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.attachments or any([url for url in message.content.split() if url.startswith('http')]):
            channel_id = message.channel.id
            if channel_id in self.tagged_members:
                player_info = self.tagged_members[channel_id]['players']
                if message.author in player_info:
                    # Update the player submission status
                    player_info[message.author]['submitted'] = True
                    player_info[message.author]['image_count'] += 1

                    # Add the checkmark reaction to the message
                    await message.add_reaction("✅")

                    # Fetch the player values from the database
                    guild_id = message.guild.id
                    conn = await get_database_for_guild(guild_id)

                    message_id = self.tracked_message_ids.get(channel_id)
                    if message_id:
                        try:
                            channel = message.channel
                            tracked_message = await channel.fetch_message(message_id)
                            embed = tracked_message.embeds[0]

                            updated_player_info = []
                            for player, info in player_info.items():
                                checkmark = "✅" if info['submitted'] else "❌"
                                worth = self.tagged_members[channel_id]['worth']
                                current_total_value = await get_player_value(conn, player.id)
                                formatted_value = f"`{format_number(worth)}`"
                                total_value = f"💰 `{format_number(current_total_value)}`"
                                updated_player_info.append(f"{player.display_name} {checkmark} | Share: {formatted_value} | Total Loot: {total_value}")

                            embed.set_field_at(0, name="👑 **Loot Split Participants** 👑", value="\n".join(updated_player_info), inline=False)
                            await tracked_message.edit(embed=embed)

                            # Check if all players have submitted and trigger loot-splitter verification
                            if all(info['submitted'] for info in player_info.values()):
                                await self.trigger_loot_splitter_verification(message.channel, channel_id)

                        except Exception as e:
                            print(f"Failed to update embed: {e}")
                    await conn.close()

    # Function to trigger loot-splitter verification
    async def trigger_loot_splitter_verification(self, channel, post_id):
        embed = discord.Embed(
            title="🛡️ **Loot-Splitter Verification Required**",
            description="All players have submitted their loot. A loot-splitter must verify and confirm the split.",
            color=discord.Color.gold()
        )
        message = await channel.send(embed=embed)

        # Add the confirm and cancel buttons for the loot-splitter
        view = View(timeout=None)

        async def confirm_split(interaction: discord.Interaction):
            await self.finalize_split(interaction, post_id)

        async def cancel_split(interaction: discord.Interaction):
            await self.cancel_split(interaction, post_id)

        confirm_button = Button(label="✅ Confirm Split", style=discord.ButtonStyle.success)
        confirm_button.callback = confirm_split
        view.add_item(confirm_button)

        cancel_button = Button(label="❌ Cancel Split", style=discord.ButtonStyle.danger)
        cancel_button.callback = cancel_split
        view.add_item(cancel_button)

        await message.edit(embed=embed, view=view)

    # Function to finalize the split, add values to each player, and close the thread if applicable
    async def finalize_split(self, interaction: discord.Interaction, post_id):
        guild_id = interaction.guild_id
        conn = await get_database_for_guild(guild_id)

        for player, info in self.tagged_members[post_id]['players'].items():
            if info['submitted'] and not info['value_added']:
                worth = self.tagged_members[post_id]['worth']
                await add_to_player_value(conn, player.id, worth)
                info['value_added'] = True

        await conn.close()

        # If in a thread, unarchive and close it, else confirm normally
        if isinstance(interaction.channel, discord.Thread):
            if interaction.channel.archived:
                await interaction.channel.edit(archived=False)  # Unarchive the thread if it's archived

            await interaction.response.send_message("✅ The loot split has been successfully confirmed, values have been updated, and the thread will now be closed.", ephemeral=True)

            # Archive/close the thread after confirmation
            await interaction.channel.edit(archived=True)

        else:
            await interaction.response.send_message("✅ The loot split has been successfully confirmed, values have been updated.", ephemeral=True)

        # Remove the split data from the tracked members
        del self.tagged_members[post_id]

    async def cancel_split(self, interaction: discord.Interaction, post_id):
        del self.tagged_members[post_id]
        await interaction.response.send_message("The loot split has been canceled.", ephemeral=True)


# ---------------- AdminCommands Cog ---------------- #

class AdminCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        conn = await get_database_for_guild(guild_id)
        role_id = await get_role_setting(conn, "Split-admin rights")
        await conn.close()

        if role_id:
            admin_role = discord.utils.get(interaction.guild.roles, id=role_id)
            if admin_role in interaction.user.roles:
                return True
            else:
                await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
                return False
        else:
            await interaction.response.send_message("Split-admin rights role is not set. Please set it using /settings.", ephemeral=True)
            return False

    @app_commands.command(name="addvalue", description="Add value to a player's total value")
    @app_commands.describe(player="Player to add value to", value="Value to add")
    async def add_value(self, interaction: discord.Interaction, player: discord.Member, value: str):
        guild_id = interaction.guild_id
        conn = await get_database_for_guild(guild_id)
        amount = parse_value(value)

        if amount is None:
            await interaction.response.send_message("Invalid value format. Use numbers with 'k' or 'm' as suffix.", ephemeral=True)
            await conn.close()
            return

        await add_to_player_value(conn, player.id, amount)
        await interaction.response.send_message(f"Added {format_number(amount)} to {player.display_name}'s total value.")
        await conn.close()

    @app_commands.command(name="removevalue", description="Remove value from a player's total value")
    @app_commands.describe(player="Player to remove value from", value="Value to remove")
    async def remove_value(self, interaction: discord.Interaction, player: discord.Member, value: str):
        guild_id = interaction.guild_id
        conn = await get_database_for_guild(guild_id)

        amount = parse_value(value)
        if amount is None:
            await interaction.response.send_message("Invalid value format. Use numbers with 'k' or 'm' as suffix.", ephemeral=True)
            await conn.close()
            return

        await remove_from_player_value(conn, player.id, amount)
        await interaction.response.send_message(f"Removed {format_number(amount)} from {player.display_name}'s total value.")
        await conn.close()

    @app_commands.command(name="resetvalue", description="Reset a player's total value")
    @app_commands.describe(player="Player to reset")
    async def reset_value(self, interaction: discord.Interaction, player: discord.Member):
        guild_id = interaction.guild_id
        conn = await get_database_for_guild(guild_id)

        await reset_player_value(conn, player.id)
        await interaction.response.send_message(f"Reset {player.display_name}'s total value to 0.")
        await conn.close()

# ---------------- Main Function ---------------- #

async def main():
    bot_token = os.getenv('DISCORD_BOT_TOKEN')
    if bot_token is None:
        print("Error: DISCORD_BOT_TOKEN environment variable not set.")
        return

    await bot.add_cog(BotSettings(bot))
    await bot.add_cog(RaidAnnouncement(bot))
    await bot.add_cog(FFAnnouncement(bot))
    await bot.add_cog(SplitTracker(bot))
    await bot.add_cog(AdminCommands(bot))

    try:
        await bot.start(bot_token)
    except Exception as e:
        print(f"An error occurred while running the bot: {e}")

if __name__ == "__main__":
    asyncio.run(main())
