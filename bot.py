"""
Auto AFK Bot + Dead by Daylight Control Panel
----------------------------------------------
AFK part (fully automatic, no commands):
  Watches voice channels. If a member mutes/deafens themselves and stays
  that way for AFK_TIMEOUT_SECONDS straight, the bot tags them (nickname +
  channel message). Also posts one group summary message whenever a voice
  channel empties out, showing total duration and who was in it.

Dead by Daylight part - one control panel with buttons for everything:
  Run /dbd_panel once in a channel and a permanent button panel appears
  there covering killer/survivor voice swap, killer rotation queue, ready
  checks, random builds, round result reporting, and the scoreboard. The
  panel automatically reposts itself to stay the last message in that
  channel no matter what else gets posted there.
"""

import asyncio
import datetime
import json
import logging
import os
import random
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
AFK_TIMEOUT_SECONDS = int(os.getenv("AFK_TIMEOUT_SECONDS", "120"))  # 2 minutes
AFK_PREFIX = "[AFK] "
MAX_NICKNAME_LENGTH = 32  # Discord's hard limit

# Optional: specific text channel ID to post AFK announcements in.
# If unset, the bot falls back to the server's system channel (if any).
AFK_ANNOUNCE_CHANNEL_ID = os.getenv("AFK_ANNOUNCE_CHANNEL_ID")
AFK_ANNOUNCE_CHANNEL_ID = int(AFK_ANNOUNCE_CHANNEL_ID) if AFK_ANNOUNCE_CHANNEL_ID else None

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guild_config.json")

# All displayed times (session summaries, etc.) are shown in Sri Lanka Time.
DISPLAY_TZ = ZoneInfo("Asia/Colombo")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("afk-bot")

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True  # privileged - must be enabled in the Dev Portal

bot = commands.Bot(command_prefix="!", intents=intents)

# key: (guild_id, member_id) -> asyncio.Task counting down to tagging
pending_tasks: dict[tuple[int, int], asyncio.Task] = {}

# key: (guild_id, member_id) -> True while we consider this member AFK-tagged
tagged_members: set[tuple[int, int]] = set()

# Per-guild config for the Dead by Daylight killer/survivor voice swap.
# { "guild_id": {"survivor_channel_id": int, "killer_channel_id": int} }
guild_config: dict[str, dict] = {}

# --- Custom status rotator ---
STATUS_ROTATE_MINUTES = int(os.getenv("STATUS_ROTATE_MINUTES", "10"))

STATUS_MESSAGES = [
    (discord.ActivityType.watching, "4 survivors run"),
    (discord.ActivityType.listening, "generator noises"),
    (discord.ActivityType.watching, "the killer's terror radius"),
    (discord.ActivityType.playing, "hide and seek (badly)"),
    (discord.ActivityType.watching, "someone get hooked"),
    (discord.ActivityType.listening, "a heartbeat get closer"),
    (discord.ActivityType.playing, "with the AFK timer"),
    (discord.ActivityType.watching, "for muted mics"),
]

# --- Random build lists (a handful of well-known perks per role, for the
#     "/random_build" fun-challenge feature - just a shuffled sample, not
#     tied to game data in any way) ---
SURVIVOR_PERKS = [
    "Dead Hard", "Iron Will", "Kindred", "Sprint Burst", "Borrowed Time",
    "Decisive Strike", "Adrenaline", "Self-Care", "Unbreakable", "We'll Make It",
    "Prove Thyself", "Lithe", "Balanced Landing", "Spine Chill", "Bond",
    "Empathy", "Deliverance", "Off the Record", "Windows of Opportunity", "Resilience",
]

KILLER_PERKS = [
    "Barbecue & Chili", "Ruin", "Nurse's Calling", "Corrupt Intervention",
    "Pop Goes the Weasel", "Sloppy Butcher", "Discordance", "Bitter Murmur",
    "Save the Best for Last", "Deadlock", "Franklin's Demise", "Hex: Devour Hope",
    "Thanatophobia", "Whispers", "Blood Warden", "Nowhere to Hide",
    "Scourge Hook: Pain Resonance", "Dead Man's Switch", "Lethal Pursuer", "Coup de Grace",
]


def load_config():
    global guild_config
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                guild_config = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Could not read {CONFIG_PATH}: {e}")
            guild_config = {}
    else:
        guild_config = {}


def save_config():
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(guild_config, f, indent=2)
    except OSError as e:
        log.warning(f"Could not save {CONFIG_PATH}: {e}")


def get_guild_config(guild_id: int) -> dict:
    return guild_config.get(str(guild_id), {})


# --- Voice session tracking (posts ONE group summary when a voice channel
#     goes from occupied back to empty - not one message per person) ---
# key: channel_id -> {"guild_id": int, "start": datetime, "participants": {member_id: name}}
group_sessions: dict[int, dict] = {}

# key: guild_id -> running count of sessions closed (just for the "#N" label)
session_counter: dict[int, int] = {}


def record_channel_join(member: discord.Member, channel: discord.VoiceChannel):
    session = group_sessions.get(channel.id)
    if session is None:
        session = {
            "guild_id": member.guild.id,
            "start": datetime.datetime.now(datetime.timezone.utc),
            "started_by": member.display_name,
            "participants": {},
        }
        group_sessions[channel.id] = session
    session["participants"][member.id] = member.display_name


async def record_channel_leave(member: discord.Member, channel: discord.VoiceChannel):
    session = group_sessions.get(channel.id)
    if session is None:
        return

    # Only close out and post the summary once the channel is fully empty
    if len(channel.members) > 0:
        return

    session = group_sessions.pop(channel.id)
    start = session["start"]
    end = datetime.datetime.now(datetime.timezone.utc)
    started_by = session["started_by"]
    participants = list(session["participants"].values())

    guild = member.guild
    session_counter[guild.id] = session_counter.get(guild.id, 0) + 1
    count = session_counter[guild.id]

    announce_channel = get_announce_channel(guild)
    if announce_channel is None:
        log.warning(f"No announce channel available for session summary in {guild.name}")
        return

    names = ", ".join(participants) if participants else "no one"
    msg = (
        f"📊 Voice session #{count} ended in **{channel.name}** — "
        f"{format_time_range(start, end)}.\n"
        f"🚀 Started by: **{started_by}**\n"
        f"👥 {len(participants)} participant(s): {names}"
    )

    try:
        await announce_channel.send(msg)
        log.info(f"Posted group session summary for #{channel.name} in {guild.name}")
        await bump_panel_after_message(guild.id, announce_channel)
    except discord.Forbidden:
        log.warning(f"No permission to send messages in #{announce_channel.name} in {guild.name}")
    except discord.HTTPException as e:
        log.warning(f"Failed to post session summary: {e}")


def strip_afk_prefix(name: str) -> str:
    if name.startswith(AFK_PREFIX):
        return name[len(AFK_PREFIX):]
    return name


def get_announce_channel(guild: discord.Guild) -> discord.TextChannel | None:
    if AFK_ANNOUNCE_CHANNEL_ID:
        channel = guild.get_channel(AFK_ANNOUNCE_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            return channel
        log.warning(f"AFK_ANNOUNCE_CHANNEL_ID set but not found/not a text channel in {guild.name}")
    return guild.system_channel


async def apply_afk_tag(member: discord.Member):
    key = (member.guild.id, member.id)

    # Re-check current state in case they unmuted/undeafened/left while we were waiting
    voice_state = member.voice
    if voice_state is None or not (voice_state.self_mute or voice_state.self_deaf):
        return

    if key in tagged_members:
        return  # already tagged

    current_name = member.nick or member.name

    # Best-effort nickname rename - silently skipped if Discord forbids it
    # (e.g. member is the server owner, or bot's role is too low).
    if not current_name.startswith(AFK_PREFIX):
        new_name = AFK_PREFIX + current_name
        if len(new_name) > MAX_NICKNAME_LENGTH:
            new_name = new_name[:MAX_NICKNAME_LENGTH]
        try:
            await member.edit(nick=new_name, reason="Auto-AFK: muted/deafened 2+ minutes")
            log.info(f"Renamed {member} as AFK in {member.guild.name}")
        except discord.Forbidden:
            log.warning(f"No permission to rename {member} in {member.guild.name} (continuing with announcement only)")
        except discord.HTTPException as e:
            log.warning(f"Failed to rename {member}: {e}")

    tagged_members.add(key)

    channel = get_announce_channel(member.guild)
    if channel is not None:
        try:
            await channel.send(f"🔴 **{member.display_name}** has gone AFK (muted/deafened).")
            log.info(f"Posted AFK announcement for {member} in #{channel.name}")
            await bump_panel_after_message(member.guild.id, channel)
        except discord.Forbidden:
            log.warning(f"No permission to send messages in #{channel.name} in {member.guild.name}")
        except discord.HTTPException as e:
            log.warning(f"Failed to post AFK announcement: {e}")
    else:
        log.warning(f"No announce channel available in {member.guild.name} - set AFK_ANNOUNCE_CHANNEL_ID")


async def remove_afk_tag(member: discord.Member):
    key = (member.guild.id, member.id)
    if key not in tagged_members:
        return

    current_name = member.nick or member.name
    restored_name = strip_afk_prefix(current_name)

    if restored_name != current_name:
        try:
            # If restored name equals their base username, clear the nickname instead
            if restored_name == member.name:
                await member.edit(nick=None, reason="Auto-AFK: no longer muted/deafened")
            else:
                await member.edit(nick=restored_name, reason="Auto-AFK: no longer muted/deafened")
            log.info(f"Removed AFK rename from {member} in {member.guild.name}")
        except discord.Forbidden:
            log.warning(f"No permission to rename {member} in {member.guild.name}")
        except discord.HTTPException as e:
            log.warning(f"Failed to rename {member}: {e}")

    channel = get_announce_channel(member.guild)
    if channel is not None:
        try:
            await channel.send(f"🟢 **{member.display_name}** is back.")
            log.info(f"Posted AFK-return announcement for {member} in #{channel.name}")
            await bump_panel_after_message(member.guild.id, channel)
        except discord.Forbidden:
            log.warning(f"No permission to send messages in #{channel.name} in {member.guild.name}")
        except discord.HTTPException as e:
            log.warning(f"Failed to post AFK-return announcement: {e}")

    tagged_members.discard(key)


def cancel_pending(key: tuple[int, int]):
    task = pending_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def start_afk_timer(member: discord.Member):
    key = (member.guild.id, member.id)
    cancel_pending(key)

    async def timer():
        try:
            await asyncio.sleep(AFK_TIMEOUT_SECONDS)
            await apply_afk_tag(member)
        except asyncio.CancelledError:
            pass
        finally:
            pending_tasks.pop(key, None)

    pending_tasks[key] = asyncio.create_task(timer())


def format_time_range(start: datetime.datetime, end: datetime.datetime) -> str:
    duration = end - start  # duration is timezone-independent, compute before converting
    total_minutes = int(duration.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        duration_str = f"{hours}h {minutes}m"
    elif hours:
        duration_str = f"{hours}h"
    else:
        duration_str = f"{minutes}m"

    start_local = start.astimezone(DISPLAY_TZ)
    end_local = end.astimezone(DISPLAY_TZ)
    return f"{start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')} SLT ({duration_str})"


_status_index = 0


@tasks.loop(minutes=STATUS_ROTATE_MINUTES)
async def rotate_status():
    global _status_index
    activity_type, text = STATUS_MESSAGES[_status_index % len(STATUS_MESSAGES)]
    _status_index += 1
    try:
        await bot.change_presence(activity=discord.Activity(type=activity_type, name=text))
    except discord.HTTPException as e:
        log.warning(f"Failed to update status: {e}")


@bot.event
async def on_ready():
    load_config()
    log.info(f"Logged in as {bot.user} (id: {bot.user.id})")
    log.info(f"AFK timeout set to {AFK_TIMEOUT_SECONDS} seconds")

    # Re-register the persistent Killer/Survivor button panel so buttons on
    # any previously-posted panel message keep working after a restart.
    bot.add_view(DBDControlPanelView())

    try:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash command(s)")
    except discord.HTTPException as e:
        log.warning(f"Failed to sync slash commands: {e}")

    if not rotate_status.is_running():
        rotate_status.start()
        log.info(f"Status rotator started (every {STATUS_ROTATE_MINUTES} min)")

    # Catch members who were already muted in voice before the bot connected.
    # Without this, on_voice_state_update never sees a "before -> after" edge
    # for them and they'd stay muted forever without getting tagged.
    for guild in bot.guilds:
        for channel in guild.voice_channels:
            for member in channel.members:
                if member.bot:
                    continue
                record_channel_join(member, channel)
                if member.voice and (member.voice.self_mute or member.voice.self_deaf):
                    log.info(f"Found already-muted member on startup: {member}")
                    await start_afk_timer(member)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if member.bot:
        return

    key = (member.guild.id, member.id)

    # Track per-channel group sessions - posts ONE summary when a channel
    # goes from occupied back to empty, not one message per person.
    if before.channel != after.channel:
        if after.channel is not None:
            record_channel_join(member, after.channel)
        if before.channel is not None:
            await record_channel_leave(member, before.channel)

    # Left voice entirely -> cancel timer and remove tag if present
    if after.channel is None:
        cancel_pending(key)
        if key in tagged_members:
            await remove_afk_tag(member)
        return

    was_afk_state = before.self_mute or before.self_deaf
    is_afk_state = after.self_mute or after.self_deaf

    # Just muted or deafened (wasn't in either state before, is now) -> start 2 min countdown
    if is_afk_state and not was_afk_state:
        await start_afk_timer(member)

    # Just unmuted AND undeafened -> cancel any pending countdown and remove tag if already applied
    if not is_afk_state and was_afk_state:
        cancel_pending(key)
        if key in tagged_members:
            await remove_afk_tag(member)


# ---------------------------------------------------------------------------
# Dead by Daylight: killer can't hear survivors, everyone stays in one VC
# ---------------------------------------------------------------------------
# How it works: Discord lets a bot server-deafen a member. A server-deafened
# member's mic still works fine (everyone still hears THEM), but audio
# coming IN is cut off for them (they can't hear anyone else). That's exactly
# "killer talks, killer can't hear survivors" - no channel swapping needed.

# key: guild_id -> member_id of whoever is currently marked as killer
current_killer: dict[int, int] = {}

# key: guild_id -> {"killer_id": int, "results": {member_id: "escaped"|"sacrificed"}}
# A "round" starts automatically the moment a killer is set, and ends
# automatically the moment that killer's hearing is restored - survivors
# just report their own outcome with /result or the panel buttons in between.
round_state: dict[int, dict] = {}


def get_scoreboard(guild_id: int) -> dict:
    return guild_config.setdefault(str(guild_id), {}).setdefault("scoreboard", {})


async def set_killer(guild: discord.Guild, player: discord.Member, set_by: str) -> tuple[bool, str]:
    """Returns (success, message)."""
    if player.voice is None or player.voice.channel is None:
        return False, f"{player.display_name}, you need to be in a voice channel first."

    prev_killer_id = current_killer.get(guild.id)
    prev_round_summary = None
    if prev_killer_id and prev_killer_id != player.id:
        prev_member = guild.get_member(prev_killer_id)
        if prev_member is not None:
            try:
                await prev_member.edit(deafen=False, reason="New killer selected")
            except (discord.Forbidden, discord.HTTPException):
                pass
        # This covers the "skip I'm Back during the break" workflow: if the
        # previous round's results were never tallied via /back, tally them
        # now so nothing is lost just because the group went straight to
        # picking the next killer instead of restoring hearing in between.
        prev_round_summary = tally_round(guild.id)

    try:
        await player.edit(deafen=True, reason=f"DBD killer round (set by {set_by})")
        current_killer[guild.id] = player.id
        round_state[guild.id] = {"killer_id": player.id, "results": {}}
        log.info(f"{set_by} set {player} as killer (server-deafened) in {guild.name}")
        msg = (
            f"🔪 **{player.display_name}** is now the killer — they can still be heard, "
            f"but can't hear the survivors anymore."
        )
        if prev_round_summary:
            msg = f"{prev_round_summary}\n{msg}"
        return True, msg
    except discord.Forbidden:
        return False, "I don't have permission to deafen that member. I need the **Deafen Members** permission."
    except discord.HTTPException as e:
        return False, f"Couldn't update {player.display_name}: {e}"


def tally_round(guild_id: int) -> str | None:
    """Closes out the active round (if any) and folds its results into the
    permanent scoreboard. Returns a short summary line, or None if no
    results were reported this round."""
    state = round_state.pop(guild_id, None)
    if state is None or not state["results"]:
        return None

    scoreboard = get_scoreboard(guild_id)
    killer_stats = scoreboard.setdefault(str(state["killer_id"]), {})

    for member_id, outcome in state["results"].items():
        member_stats = scoreboard.setdefault(str(member_id), {})
        if outcome == "escaped":
            member_stats["escapes"] = member_stats.get("escapes", 0) + 1
            killer_stats["escapes_against"] = killer_stats.get("escapes_against", 0) + 1
        elif outcome == "sacrificed":
            member_stats["deaths"] = member_stats.get("deaths", 0) + 1
            killer_stats["kills"] = killer_stats.get("kills", 0) + 1

    save_config()
    escapes = sum(1 for o in state["results"].values() if o == "escaped")
    deaths = sum(1 for o in state["results"].values() if o == "sacrificed")
    return f"📋 Round result: {deaths} sacrificed, {escapes} escaped ({len(state['results'])} reported)."


async def clear_killer(guild: discord.Guild, player: discord.Member, set_by: str) -> tuple[bool, str]:
    try:
        await player.edit(deafen=False, reason=f"DBD round over (restored by {set_by})")
        was_current_killer = current_killer.get(guild.id) == player.id
        if was_current_killer:
            current_killer.pop(guild.id, None)
        log.info(f"{set_by} restored hearing for {player} in {guild.name}")
        msg = f"🏃 **{player.display_name}** can hear the survivors again."
        if was_current_killer:
            round_summary = tally_round(guild.id)
            if round_summary:
                msg += f"\n{round_summary}"
        return True, msg
    except discord.Forbidden:
        return False, "I don't have permission to undeafen that member. I need the **Deafen Members** permission."
    except discord.HTTPException as e:
        return False, f"Couldn't update {player.display_name}: {e}"


def record_result(guild: discord.Guild, member: discord.Member, outcome: str) -> tuple[bool, str]:
    """outcome is 'escaped' or 'sacrificed'."""
    state = round_state.get(guild.id)
    if state is None:
        return False, "No killer round is currently active — results can only be reported while a round is running."
    if member.id == state["killer_id"]:
        return False, "The killer doesn't report an outcome — survivors report their own."

    state["results"][member.id] = outcome
    label = "escaped! 🏃💨" if outcome == "escaped" else "was sacrificed to the Entity... 💀"
    return True, f"**{member.display_name}** {label}"


def build_scoreboard_text(guild: discord.Guild) -> str:
    scoreboard = get_scoreboard(guild.id)
    if not scoreboard:
        return "No results recorded yet. Report outcomes with `/result` or the panel buttons after a round."

    killer_rows = []
    survivor_rows = []
    for member_id_str, stats in scoreboard.items():
        member = guild.get_member(int(member_id_str))
        name = member.display_name if member else f"(left server, id {member_id_str})"
        if "kills" in stats or "escapes_against" in stats:
            killer_rows.append((name, stats.get("kills", 0), stats.get("escapes_against", 0)))
        if "escapes" in stats or "deaths" in stats:
            survivor_rows.append((name, stats.get("escapes", 0), stats.get("deaths", 0)))

    killer_rows.sort(key=lambda r: r[1], reverse=True)
    survivor_rows.sort(key=lambda r: r[1], reverse=True)

    lines = ["📊 **Dead by Daylight Scoreboard**"]
    if killer_rows:
        lines.append("\n🔪 **As Killer** (kills / escapes against)")
        for name, kills, escapes_against in killer_rows:
            lines.append(f"• {name} — {kills} kills, {escapes_against} escaped")
    if survivor_rows:
        lines.append("\n🏃 **As Survivor** (escapes / deaths)")
        for name, escapes, deaths in survivor_rows:
            lines.append(f"• {name} — {escapes} escapes, {deaths} deaths")

    return "\n".join(lines)


def build_queue_text(guild: discord.Guild) -> str:
    cfg = get_guild_config(guild.id)
    queue_ids = cfg.get("killer_queue")
    if not queue_ids:
        return "No rotation set up yet. Run `/queue_setup` first."

    current_index = cfg.get("queue_index", -1)
    lines = ["🔁 **Killer rotation:**"]
    for i, member_id in enumerate(queue_ids):
        member = guild.get_member(member_id)
        name = member.display_name if member else f"(left server, id {member_id})"
        marker = " 👈 current" if i == current_index else ""
        lines.append(f"{i + 1}. {name}{marker}")
    return "\n".join(lines)


async def next_killer_logic(guild: discord.Guild, set_by: str) -> tuple[bool, str]:
    cfg = get_guild_config(guild.id)
    queue_ids = cfg.get("killer_queue")

    if not queue_ids:
        return False, "No rotation set up yet. Run `/queue_setup` first with your player order."

    current_index = cfg.get("queue_index", -1)
    next_index = (current_index + 1) % len(queue_ids)
    next_player = guild.get_member(queue_ids[next_index])

    if next_player is None:
        return False, "Couldn't find the next player in this server anymore. Run `/queue_setup` again."

    success, msg = await set_killer(guild, next_player, set_by=set_by)

    if success:
        guild_config.setdefault(str(guild.id), {})["queue_index"] = next_index
        guild_config[str(guild.id)]["killer_queue"] = queue_ids
        save_config()
        upcoming_index = (next_index + 1) % len(queue_ids)
        upcoming = guild.get_member(queue_ids[upcoming_index])
        upcoming_name = upcoming.display_name if upcoming else "someone no longer in the server"
        msg += f"\n⏭️ Up next: **{upcoming_name}**"

    return success, msg


def random_build_text(role: str, member_name: str) -> str:
    perks = KILLER_PERKS if role == "killer" else SURVIVOR_PERKS
    pick = random.sample(perks, 4)
    emoji = "🔪" if role == "killer" else "🏃"
    perk_lines = "\n".join(f"• {p}" for p in pick)
    return f"{emoji} **{member_name}'s random {role} build:**\n{perk_lines}"


class ReadyCheckView(discord.ui.View):
    """Short-lived (not persistent) - tracks who has clicked Ready out of a
    specific set of expected members for one particular ready check."""

    def __init__(self, expected_ids: set[int]):
        super().__init__(timeout=600)
        self.expected_ids = expected_ids
        self.ready_ids: set[int] = set()

    @discord.ui.button(label="Ready", style=discord.ButtonStyle.success, emoji="✅")
    async def ready_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.expected_ids:
            await interaction.response.send_message("You're not part of this ready check.", ephemeral=True)
            return

        self.ready_ids.add(interaction.user.id)
        remaining = self.expected_ids - self.ready_ids

        if not remaining:
            await interaction.response.send_message("🎮 **Everyone's ready — let's go!**")
            self.stop()
        else:
            names = ", ".join(f"<@{uid}>" for uid in remaining)
            await interaction.response.send_message(
                f"✅ **{interaction.user.display_name}** is ready. Waiting on: {names}"
            )
        await bump_panel_if_needed(interaction)


async def ready_check_logic(interaction: discord.Interaction) -> tuple[bool, str, discord.ui.View | None]:
    member = interaction.guild.get_member(interaction.user.id)
    if member is None or member.voice is None or member.voice.channel is None:
        return False, "You need to be in a voice channel to start a ready check.", None

    channel = member.voice.channel
    others = [m for m in channel.members if not m.bot]

    if len(others) < 2:
        return False, "Need at least 2 people in your voice channel to run a ready check.", None

    mentions = " ".join(m.mention for m in others)
    view = ReadyCheckView(expected_ids={m.id for m in others})
    return True, f"🎮 **Ready check!** {mentions}\nClick ✅ when you're ready to start.", view


PANEL_TITLE = "🔪 Dead by Daylight — Control Panel"
PANEL_DESCRIPTION = (
    "**I'm Killer** / **I'm Back** — swap who's deafened for the round.\n"
    "**Next Killer** — advance the rotation set up with `/queue_setup`.\n"
    "**Ready Check** — ping your voice channel and track who's ready.\n"
    "**Random Build** — get a random 4-perk survivor build.\n"
    "**Escaped** / **Sacrificed** — survivors report their own round outcome.\n"
    "**Scoreboard** / **Show Queue** — check standings and rotation order.\n\n"
    "This panel always stays at the bottom of the channel."
)


def build_panel_embed() -> discord.Embed:
    return discord.Embed(title=PANEL_TITLE, description=PANEL_DESCRIPTION, color=discord.Color.red())


class DBDControlPanelView(discord.ui.View):
    """The all-in-one persistent control panel. timeout=None plus fixed
    custom_ids on every button means it keeps working after bot restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _bump_panel(self, interaction: discord.Interaction):
        """Repositions the panel to the bottom after a button click. Needed
        in addition to on_message-based repositioning, because ephemeral
        interaction responses never fire a normal message event - so
        without this, the panel would drift upward on every ephemeral
        button reply."""
        await bump_panel_if_needed(interaction)

    # --- Row 0: killer swap + rotation + ready check + random build ---
    @discord.ui.button(label="I'm Killer", style=discord.ButtonStyle.danger, emoji="🔪", custom_id="dbdcp:killer", row=0)
    async def killer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.guild.get_member(interaction.user.id) or interaction.user
        success, msg = await set_killer(interaction.guild, member, set_by=str(interaction.user))
        await interaction.response.send_message(msg, ephemeral=not success)
        await self._bump_panel(interaction)

    @discord.ui.button(label="I'm Back", style=discord.ButtonStyle.success, emoji="🏃", custom_id="dbdcp:back", row=0)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.guild.get_member(interaction.user.id) or interaction.user
        success, msg = await clear_killer(interaction.guild, member, set_by=str(interaction.user))
        await interaction.response.send_message(msg, ephemeral=not success)
        await self._bump_panel(interaction)

    @discord.ui.button(label="Next Killer", style=discord.ButtonStyle.primary, emoji="⏭️", custom_id="dbdcp:next_killer", row=0)
    async def next_killer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        success, msg = await next_killer_logic(interaction.guild, set_by=str(interaction.user))
        await interaction.response.send_message(msg, ephemeral=not success)
        await self._bump_panel(interaction)

    @discord.ui.button(label="Ready Check", style=discord.ButtonStyle.primary, emoji="✅", custom_id="dbdcp:ready", row=0)
    async def ready_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        success, msg, view = await ready_check_logic(interaction)
        if success:
            await interaction.response.send_message(msg, view=view)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        await self._bump_panel(interaction)

    @discord.ui.button(label="Random Build", style=discord.ButtonStyle.secondary, emoji="🎲", custom_id="dbdcp:build", row=0)
    async def random_build_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.guild.get_member(interaction.user.id) or interaction.user
        role = "killer" if current_killer.get(interaction.guild_id) == member.id else "survivor"
        await interaction.response.send_message(random_build_text(role, member.display_name), ephemeral=True)
        await self._bump_panel(interaction)

    # --- Row 1: result reporting + scoreboard + queue display ---
    @discord.ui.button(label="Escaped", style=discord.ButtonStyle.success, emoji="🏃", custom_id="dbdcp:escaped", row=1)
    async def escaped_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.guild.get_member(interaction.user.id) or interaction.user
        success, msg = record_result(interaction.guild, member, "escaped")
        await interaction.response.send_message(msg, ephemeral=not success)
        await self._bump_panel(interaction)

    @discord.ui.button(label="Sacrificed", style=discord.ButtonStyle.danger, emoji="💀", custom_id="dbdcp:sacrificed", row=1)
    async def sacrificed_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.guild.get_member(interaction.user.id) or interaction.user
        success, msg = record_result(interaction.guild, member, "sacrificed")
        await interaction.response.send_message(msg, ephemeral=not success)
        await self._bump_panel(interaction)

    @discord.ui.button(label="Scoreboard", style=discord.ButtonStyle.secondary, emoji="📊", custom_id="dbdcp:scoreboard", row=1)
    async def scoreboard_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(build_scoreboard_text(interaction.guild), ephemeral=True)
        await self._bump_panel(interaction)

    @discord.ui.button(label="Show Queue", style=discord.ButtonStyle.secondary, emoji="📋", custom_id="dbdcp:queue_show", row=1)
    async def queue_show_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(build_queue_text(interaction.guild), ephemeral=True)
        await self._bump_panel(interaction)


async def repost_panel_at_bottom(channel: discord.TextChannel):
    """Deletes the old panel message (if any) and posts a fresh one, so the
    panel always ends up as the last message in its designated channel."""
    guild_id_str = str(channel.guild.id)
    cfg = guild_config.setdefault(guild_id_str, {})
    old_message_id = cfg.get("panel_message_id")

    if old_message_id:
        try:
            old_msg = await channel.fetch_message(old_message_id)
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass  # already gone or can't delete - just post a new one anyway

    try:
        new_msg = await channel.send(embed=build_panel_embed(), view=DBDControlPanelView())
        cfg["panel_message_id"] = new_msg.id
        save_config()
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"Failed to repost DBD control panel in #{channel.name}: {e}")


async def bump_panel_if_needed(interaction: discord.Interaction):
    """Repositions the panel to the bottom after a slash command reply, same
    idea as the button _bump_panel helper - covers the case where someone
    runs a command (rather than clicking a button) in the panel's channel."""
    cfg = get_guild_config(interaction.guild_id)
    if cfg.get("panel_channel_id") == interaction.channel_id:
        await repost_panel_at_bottom(interaction.channel)


async def bump_panel_after_message(guild_id: int, channel: discord.abc.Messageable):
    """Same idea as bump_panel_if_needed, but for messages sent outside any
    interaction - the AFK tag announcements and voice session summaries,
    which fire from voice-state events rather than a command or button."""
    cfg = get_guild_config(guild_id)
    if cfg.get("panel_channel_id") == getattr(channel, "id", None):
        await repost_panel_at_bottom(channel)


@bot.event
async def on_message(message: discord.Message):
    # Keep any future prefix (!) commands working - harmless no-op today
    # since there are none, but avoids silently breaking them later.
    await bot.process_commands(message)

    # Only genuine user-typed messages reposition the panel here. Bot-authored
    # messages (including the panel's own repost) are handled explicitly by
    # each command/button instead, to avoid any risk of a repost loop.
    if message.author.bot or message.guild is None:
        return

    cfg = get_guild_config(message.guild.id)
    panel_channel_id = cfg.get("panel_channel_id")

    if panel_channel_id and message.channel.id == panel_channel_id:
        await repost_panel_at_bottom(message.channel)


@bot.tree.command(name="dbd_panel", description="Post the Dead by Daylight control panel here - it will always stay at the bottom of this channel.")
async def dbd_panel_cmd(interaction: discord.Interaction):
    guild_id_str = str(interaction.guild_id)
    cfg = guild_config.setdefault(guild_id_str, {})
    cfg["panel_channel_id"] = interaction.channel_id
    save_config()

    embed = build_panel_embed()
    await interaction.response.send_message(embed=embed, view=DBDControlPanelView())
    sent = await interaction.original_response()
    cfg["panel_message_id"] = sent.id
    save_config()


@bot.tree.command(name="killer", description="Mark a player as killer - they keep talking but can't hear survivors.")
@app_commands.describe(player="The player whose turn it is to be killer")
async def killer_cmd(interaction: discord.Interaction, player: discord.Member):
    success, msg = await set_killer(interaction.guild, player, set_by=str(interaction.user))
    await interaction.response.send_message(msg, ephemeral=not success)
    await bump_panel_if_needed(interaction)


@bot.tree.command(name="back", description="Restore a player's hearing after their killer round ends.")
@app_commands.describe(player="The player to restore (defaults to the current killer)")
async def back_cmd(interaction: discord.Interaction, player: discord.Member = None):
    if player is None:
        killer_id = current_killer.get(interaction.guild_id)
        if killer_id is None:
            await interaction.response.send_message(
                "No one is currently marked as killer. Specify a player to restore manually.",
                ephemeral=True,
            )
            return
        player = interaction.guild.get_member(killer_id)
        if player is None:
            await interaction.response.send_message("Couldn't find that member anymore.", ephemeral=True)
            return

    success, msg = await clear_killer(interaction.guild, player, set_by=str(interaction.user))
    await interaction.response.send_message(msg, ephemeral=not success)
    await bump_panel_if_needed(interaction)


@bot.tree.command(name="result", description="Report your own round outcome (escaped or sacrificed).")
@app_commands.describe(outcome="What happened to you this round")
@app_commands.choices(outcome=[
    app_commands.Choice(name="Escaped", value="escaped"),
    app_commands.Choice(name="Sacrificed", value="sacrificed"),
])
async def result_cmd(interaction: discord.Interaction, outcome: app_commands.Choice[str]):
    member = interaction.guild.get_member(interaction.user.id) or interaction.user
    success, msg = record_result(interaction.guild, member, outcome.value)
    await interaction.response.send_message(msg, ephemeral=not success)
    await bump_panel_if_needed(interaction)


@bot.tree.command(name="scoreboard", description="Show the Dead by Daylight killer/survivor scoreboard.")
async def scoreboard_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(build_scoreboard_text(interaction.guild))
    await bump_panel_if_needed(interaction)


@bot.tree.command(name="random_build", description="Get a random 4-perk build.")
@app_commands.describe(role="Which role's perk pool to pull from")
@app_commands.choices(role=[
    app_commands.Choice(name="Survivor", value="survivor"),
    app_commands.Choice(name="Killer", value="killer"),
])
async def random_build_cmd(interaction: discord.Interaction, role: app_commands.Choice[str] = None):
    role_value = role.value if role else "survivor"
    await interaction.response.send_message(random_build_text(role_value, interaction.user.display_name))
    await bump_panel_if_needed(interaction)


@bot.tree.command(name="ready_check", description="Ping everyone in your voice channel and track who's ready.")
async def ready_check_cmd(interaction: discord.Interaction):
    success, msg, view = await ready_check_logic(interaction)
    if success:
        await interaction.response.send_message(msg, view=view)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    await bump_panel_if_needed(interaction)


# ---------------------------------------------------------------------------
# Killer rotation queue - set an order once, then /next_killer cycles through
# it automatically without anyone needing to remember or call out names.
# ---------------------------------------------------------------------------

@bot.tree.command(name="queue_setup", description="Set the killer rotation order (up to 6 players).")
@app_commands.describe(
    player1="1st in rotation", player2="2nd in rotation", player3="3rd in rotation",
    player4="4th in rotation", player5="5th in rotation (optional)", player6="6th in rotation (optional)",
)
async def queue_setup_cmd(
    interaction: discord.Interaction,
    player1: discord.Member,
    player2: discord.Member,
    player3: discord.Member,
    player4: discord.Member,
    player5: discord.Member = None,
    player6: discord.Member = None,
):
    players = [p for p in [player1, player2, player3, player4, player5, player6] if p is not None]

    cfg = guild_config.setdefault(str(interaction.guild_id), {})
    cfg["killer_queue"] = [p.id for p in players]
    cfg["queue_index"] = -1  # -1 means "hasn't started yet" - /next_killer will pick index 0 first
    save_config()

    order = " → ".join(p.display_name for p in players)
    await interaction.response.send_message(
        f"🔁 Killer rotation set: {order}\nRun `/next_killer` to start with **{players[0].display_name}**."
    )
    await bump_panel_if_needed(interaction)


@bot.tree.command(name="next_killer", description="Move to the next player in the killer rotation.")
async def next_killer_cmd(interaction: discord.Interaction):
    success, msg = await next_killer_logic(interaction.guild, set_by=str(interaction.user))
    await interaction.response.send_message(msg, ephemeral=not success)
    await bump_panel_if_needed(interaction)


@bot.tree.command(name="queue_show", description="Show the current killer rotation order.")
async def queue_show_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(build_queue_text(interaction.guild), ephemeral=True)
    await bump_panel_if_needed(interaction)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Create a .env file (see .env.example) "
            "or set the DISCORD_TOKEN environment variable on your host."
        )
    bot.run(TOKEN)
