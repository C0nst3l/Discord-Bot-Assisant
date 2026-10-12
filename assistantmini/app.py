import asyncio
from datetime import date, datetime, timedelta
import json
import math
import os
import random
import re
import time
import uuid
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN was not found. "
        "Make sure it is set in your .env file."
    )

SETTINGS_FILE = Path("server_settings.json")
GIVEAWAY_FILE = Path("active_giveaways.json")
ECONOMY_USERDATA_FILE = Path("economymini_userdata.json")
GUILD_LEVELING_USERDATA_FILE = Path("guildlevelling_userdata.json")

MAX_FIELD_VALUE_LENGTH = 1024
MAX_GIVEAWAY_DURATION = 30 * 24 * 60 * 60
COMPLETED_GIVEAWAY_RETENTION = 48 * 60 * 60
PREMIUM_SKU_NAME = "AssistantMini Premium"
COIN_CRATE_SKU_NAME = "MiniEconomy $1M Coins Crate"
COIN_CRATE_REWARD = 1_000_000
PREMIUM_SYNC_INTERVAL = 30 * 60
CURRENCY_NAME = "<:EMCoin:1558283161354506241> Coins"
CURRENCY_SYMBOL = "<:EMCoin:1558283161354506241>"
BANK_DAILY_INTEREST_RATE = 0.10
STARTING_BALANCE = 1000
DAILY_REWARD = 500
DAILY_COOLDOWN = 24 * 60 * 60
WEEKLY_REWARD = 5000
WEEKLY_COOLDOWN = 7 * 24 * 60 * 60
LEVEL_UP_EXPERIENCE = 100
LEVEL_REWARD_EXPERIENCE = 5
GUILD_LEVEL_XP_MIN = 15
GUILD_LEVEL_XP_MAX = 30
GUILD_LEVEL_XP_COOLDOWN = 60
PRIVATE_ECONOMY_EMBED_COMMANDS = {
    "economy-coin-flip",
    "economy-daily",
    "economy-weekly",
    "economy-wallet",
    "economy-bank",
    "economy-bank-deposit",
    "economy-bank-withdraw",
    "economy-job-apply",
    "economy-job-work",
    "economy-work-shift",
    "economy-job-quit",
    "economy-leaderboard",
}

# How often failed giveaway completions are retried.
RETRY_DELAY = 30
# ============================================================
# Job Configuration
# ============================================================
JOB_TITLES = [
    "Waiter",
    "Chef",
    "Teacher",
    "Doctor",
    "Vice President",
    "President",
]
JOB_SHIFTS = {
    "Waiter": 1,
    "Chef": 2,
    "Teacher": 3,
    "Doctor": 4,
    "Vice President": 5,
    "President": 6,
}
JOB_LEVEL_REQUIREMENTS = {
    "Doctor": 2,
    "Vice President": 5,
    "President": 10,
}
JOB_WAGES = {
    "Waiter": 1000,
    "Chef": 4000,
    "Teacher": 8000,
    "Doctor": 16000,
    "Vice President": 25000,
    "President": 60000,
}


def get_job_work_day(timestamp):
    local_now = datetime.fromtimestamp(timestamp)
    if local_now.hour < 1:
        local_now -= timedelta(days=1)
    return local_now.date().isoformat()


def get_missed_job_workday_count(user_data, job_title, from_day, through_day):
    elapsed_days = (through_day - from_day).days
    if elapsed_days <= 0:
        return 0

    shifts_worked = user_data.get("job_shifts_worked")
    completed_work_day = user_data.get("job_work_date")
    if (
        isinstance(shifts_worked, int)
        and not isinstance(shifts_worked, bool)
        and shifts_worked == JOB_SHIFTS[job_title]
        and user_data.get("job_work_job") == job_title
        and isinstance(completed_work_day, str)
    ):
        try:
            completed_date = date.fromisoformat(completed_work_day)
        except ValueError:
            completed_date = None

        if completed_date is not None and from_day < completed_date <= through_day:
            elapsed_days -= 1

    return elapsed_days


# ============================================================
# INTENTS
# ============================================================

intents = discord.Intents.all()


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path: Path, default):
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)

    except (json.JSONDecodeError, OSError) as error:
        print(f"[JSON] Could not load {path}: {error}")
        return default


def save_json(path: Path, data):
    """
    Atomically save JSON.

    The temporary file prevents the main JSON file from being
    left half-written if the process stops during saving.
    """

    temporary_path = path.with_suffix(path.suffix + ".tmp")

    try:
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(
                data,
                file,
                indent=4,
                ensure_ascii=False
            )

        os.replace(temporary_path, path)
        return True

    except OSError as error:
        print(f"[JSON] Could not save {path}: {error}")

        try:
            if temporary_path.exists():
                temporary_path.unlink()
        except OSError:
            pass
        return False


# ============================================================
# SERVER SETTINGS
# ============================================================

server_settings = load_json(
    SETTINGS_FILE,
    {}
)


def get_default_server_settings():
    return {
        "colour": 0xffffff,
        "streamer": None,
        "assistantmini_premium": False,
        "giveaway_role_entries": {}
    }


def get_server_settings(guild_id: int):
    guild_id_str = str(guild_id)

    if guild_id_str not in server_settings:
        server_settings[guild_id_str] = get_default_server_settings()
        save_json(
            SETTINGS_FILE,
            server_settings
        )

    return server_settings[guild_id_str]


def add_premium_footer(embed: discord.Embed, guild_id: int):
    settings = get_server_settings(guild_id)

    if settings.get("assistantmini_premium", False):
        return embed

    footer_text = embed.footer.text
    attribution = "⚡ Made by AssistantMini • Remove Watermark via Premium Subscription ⚡"

    if attribution not in (footer_text or ""):
        embed.set_footer(
            text=(
                f"{footer_text} • {attribution}"
                if footer_text
                else attribution
            )
        )

    return embed


# ============================================================
# GIVEAWAYS
# ============================================================

giveaways = load_json(
    GIVEAWAY_FILE,
    {}
)
economy_userdata = load_json(
    ECONOMY_USERDATA_FILE,
    {}
)
if not isinstance(economy_userdata, dict):
    raise ValueError(
        f"{ECONOMY_USERDATA_FILE} must contain a JSON object."
    )
economy_lock = asyncio.Lock()
guild_leveling_userdata = load_json(
    GUILD_LEVELING_USERDATA_FILE,
    {}
)
if not isinstance(guild_leveling_userdata, dict):
    raise ValueError(
        f"{GUILD_LEVELING_USERDATA_FILE} must contain a JSON object."
    )
guild_leveling_lock = asyncio.Lock()


def get_guild_level_xp_required(level):
    return 100 + level * 50


def get_guild_level_total_experience(level):
    return level * 100 + 25 * level * (level - 1)


def get_guild_level_progress_from_total_experience(experience):
    low_level = 0
    high_level = experience // 100 + 1

    while low_level + 1 < high_level:
        middle_level = (low_level + high_level) // 2
        if get_guild_level_total_experience(middle_level) <= experience:
            low_level = middle_level
        else:
            high_level = middle_level

    level_experience = (
        experience - get_guild_level_total_experience(low_level)
    )
    return low_level, level_experience


def get_guild_leveling_progress(guild_id, user_id):
    guild_data = guild_leveling_userdata.get(str(guild_id), {})
    if not isinstance(guild_data, dict):
        guild_data = {}

    members = guild_data.get("members", {})
    if not isinstance(members, dict):
        members = {}

    user_data = members.get(str(user_id), {})
    if not isinstance(user_data, dict):
        user_data = {}

    return guild_data, members, user_data


def get_account_created_at(user_data, now):
    created_at = user_data.get("created_at")
    if (
        isinstance(created_at, (int, float))
        and not isinstance(created_at, bool)
        and 0 <= created_at <= 253402300799
        and math.isfinite(created_at)
    ):
        return created_at

    return now


def get_bank_interest_day(timestamp):
    return get_job_work_day(timestamp)


def calculate_bank_interest(user_data, now):
    bank_balance = user_data.get("coins_banks", 0)
    if (
        not isinstance(bank_balance, int)
        or isinstance(bank_balance, bool)
        or bank_balance < 0
    ):
        return None

    current_day = get_bank_interest_day(now)
    last_interest_day = user_data.get("bank_interest_day")
    if last_interest_day is None:
        last_interest_day = get_bank_interest_day(
            get_account_created_at(user_data, now)
        )

    try:
        last_date = date.fromisoformat(last_interest_day)
        current_date = date.fromisoformat(current_day)
    except (TypeError, ValueError):
        return None

    elapsed_days = (current_date - last_date).days
    if elapsed_days < 0:
        return None

    updated_balance = bank_balance
    for _ in range(elapsed_days):
        updated_balance += int(
            updated_balance * BANK_DAILY_INTEREST_RATE
        )

    return {
        **user_data,
        "coins_banks": updated_balance,
        "bank_interest_day": current_day,
    }, updated_balance - bank_balance


def create_economy_user_data(now):
    return {
        "coins": STARTING_BALANCE,
        "coins_banks": 0,
        "bank_interest_day": get_bank_interest_day(now),
        "created_at": now,
        "last_daily": None,
        "level": 0,
        "level_exp": 0,
    }


def has_valid_level_progress(user_data):
    level = user_data.get("level", 0)
    experience = user_data.get("level_exp", 0)
    return (
        isinstance(level, int)
        and not isinstance(level, bool)
        and level >= 0
        and isinstance(experience, int)
        and not isinstance(experience, bool)
        and 0 <= experience < LEVEL_UP_EXPERIENCE
    )


def award_level_experience(user_data, amount=LEVEL_REWARD_EXPERIENCE):
    level = user_data.get("level", 0)
    experience = user_data.get("level_exp", 0) + amount
    level += experience // LEVEL_UP_EXPERIENCE
    experience %= LEVEL_UP_EXPERIENCE
    return {
        "level": level,
        "level_exp": experience,
    }


def initialize_economy_levels():
    changed = False
    for user_data in economy_userdata.values():
        if isinstance(user_data, dict):
            if "level" not in user_data:
                user_data["level"] = 0
                changed = True
            if "level_exp" not in user_data:
                user_data["level_exp"] = 0
                changed = True
            if "coins_banks" not in user_data:
                user_data["coins_banks"] = 0
                changed = True
            if "bank_interest_day" not in user_data:
                user_data["bank_interest_day"] = get_bank_interest_day(
                    time.time()
                )
                changed = True

    if changed and not save_json(ECONOMY_USERDATA_FILE, economy_userdata):
        raise RuntimeError(
            "Could not save existing EconomyMini account defaults."
        )


initialize_economy_levels()


async def apply_bank_interest_for_user(user_id, now=None):
    if now is None:
        now = time.time()

    async with economy_lock:
        user_data = economy_userdata.get(user_id)
        if user_data is None:
            return True
        if not isinstance(user_data, dict):
            print(
                f"[ECONOMY BANK] Invalid account for user {user_id}; "
                "interest was not applied."
            )
            return False

        result = calculate_bank_interest(user_data, now)
        if result is None:
            print(
                f"[ECONOMY BANK] Invalid bank balance or interest day "
                f"for user {user_id}; interest was not applied."
            )
            return False

        updated_user_data, interest_earned = result
        if updated_user_data == user_data:
            return True

        updated_economy_userdata = dict(economy_userdata)
        updated_economy_userdata[user_id] = updated_user_data
        if not save_json(
            ECONOMY_USERDATA_FILE,
            updated_economy_userdata
        ):
            print(
                f"[ECONOMY BANK] Could not save interest for user "
                f"{user_id}; interest was not applied."
            )
            return False

        economy_userdata[user_id] = updated_user_data
        if interest_earned:
            print(
                f"[ECONOMY BANK] Applied {interest_earned:,} coins of "
                f"interest to user {user_id}."
            )

    return True


async def apply_due_bank_interest(now=None):
    if now is None:
        now = time.time()

    async with economy_lock:
        updated_economy_userdata = dict(economy_userdata)
        changed = False
        for user_id, user_data in economy_userdata.items():
            if not isinstance(user_data, dict):
                print(
                    f"[ECONOMY BANK] Invalid account for user {user_id}; "
                    "daily interest was skipped."
                )
                continue

            result = calculate_bank_interest(user_data, now)
            if result is None:
                print(
                    f"[ECONOMY BANK] Invalid bank balance or interest day "
                    f"for user {user_id}; daily interest was skipped."
                )
                continue

            updated_user_data, interest_earned = result
            if updated_user_data != user_data:
                updated_economy_userdata[user_id] = updated_user_data
                changed = True
                if interest_earned:
                    print(
                        f"[ECONOMY BANK] Applied {interest_earned:,} coins "
                        f"of interest to user {user_id}."
                    )

        if not changed:
            return True

        if not save_json(
            ECONOMY_USERDATA_FILE,
            updated_economy_userdata
        ):
            print(
                "[ECONOMY BANK] Could not save daily interest; "
                "interest will be retried."
            )
            return False

        economy_userdata.clear()
        economy_userdata.update(updated_economy_userdata)

    return True


# giveaway_id -> asyncio.Task
giveaway_tasks = {}

# Prevent simultaneous entry modifications from racing.
giveaway_locks = {}

# Tracks persistent views that have already been registered.
registered_view_ids = set()


def award_coin_crate_purchase(user_id: int, entitlement_id: int):
    """Grant the configured reward for a single coin-crate entitlement."""

    user_key = str(user_id)
    user_data = economy_userdata.get(user_key, {})
    if not isinstance(user_data, dict):
        user_data = {}

    granted_entitlements = user_data.get("coin_crate_entitlements", [])
    if not isinstance(granted_entitlements, list):
        granted_entitlements = []

    entitlement_key = str(entitlement_id)
    if entitlement_key in granted_entitlements:
        return False

    now = time.time()
    balance = user_data.get("coins", STARTING_BALANCE)
    if not isinstance(balance, int) or isinstance(balance, bool) or balance < 0:
        balance = STARTING_BALANCE

    updated_user_data = {
        **user_data,
        "level": user_data.get("level", 0),
        "level_exp": user_data.get("level_exp", 0),
        "coins": balance + COIN_CRATE_REWARD,
        "created_at": get_account_created_at(user_data, now),
        "coin_crate_entitlements": [
            *granted_entitlements,
            entitlement_key,
        ],
    }

    updated_economy_userdata = dict(economy_userdata)
    updated_economy_userdata[user_key] = updated_user_data

    if not save_json(ECONOMY_USERDATA_FILE, updated_economy_userdata):
        return False

    economy_userdata[user_key] = updated_user_data
    print(
        f"[ECONOMY] Granted {COIN_CRATE_REWARD:,} coins to user "
        f"{user_id} for {COIN_CRATE_SKU_NAME} entitlement {entitlement_id}."
    )
    return True


def get_giveaway_lock(giveaway_id: str):
    if giveaway_id not in giveaway_locks:
        giveaway_locks[giveaway_id] = asyncio.Lock()

    return giveaway_locks[giveaway_id]


def prune_expired_giveaways(now=None):
    if now is None:
        now = time.time()

    retained_giveaways = {}
    for giveaway_id, giveaway in giveaways.items():
        completed_at = (
            giveaway.get("completed_at")
            if isinstance(giveaway, dict)
            else None
        )
        if (
            isinstance(completed_at, int)
            and not isinstance(completed_at, bool)
        ):
            is_expired = (
                0 <= completed_at <= now - COMPLETED_GIVEAWAY_RETENTION
            )
        elif (
            isinstance(completed_at, float)
            and math.isfinite(completed_at)
        ):
            is_expired = (
                0 <= completed_at
                and now - completed_at >= COMPLETED_GIVEAWAY_RETENTION
            )
        else:
            is_expired = False

        lock = giveaway_locks.get(giveaway_id)
        if lock is not None and lock.locked():
            is_expired = False

        if not is_expired:
            retained_giveaways[giveaway_id] = giveaway

    if len(retained_giveaways) == len(giveaways):
        return True

    if not save_json(GIVEAWAY_FILE, retained_giveaways):
        print("[GIVEAWAY] Could not save completed giveaway cleanup.")
        return False

    expired_ids = set(giveaways) - set(retained_giveaways)
    giveaways.clear()
    giveaways.update(retained_giveaways)
    for giveaway_id in expired_ids:
        giveaway_locks.pop(giveaway_id, None)

    return True


# ============================================================
# DURATION
# ============================================================

def parse_duration(duration: str):
    if not duration:
        return None

    duration = duration.strip().lower()

    match = re.fullmatch(
        r"(\d+)\s*([smhd])",
        duration
    )

    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)

    if amount <= 0:
        return None

    multipliers = {
        "s": 1,
        "m": 60,
        "h": 60 * 60,
        "d": 24 * 60 * 60
    }

    seconds = amount * multipliers[unit]

    if seconds > MAX_GIVEAWAY_DURATION:
        return None

    return seconds


def format_duration(seconds: int):
    if seconds % 86400 == 0:
        value = seconds // 86400
        return f"{value} day{'s' if value != 1 else ''}"

    if seconds % 3600 == 0:
        value = seconds // 3600
        return f"{value} hour{'s' if value != 1 else ''}"

    if seconds % 60 == 0:
        value = seconds // 60
        return f"{value} minute{'s' if value != 1 else ''}"

    return f"{seconds} second{'s' if seconds != 1 else ''}"


# ============================================================
# GIVEAWAY VALIDATION
# ============================================================

def validate_giveaway(giveaway_id: str, giveaway: dict):
    """
    Validate persistent giveaway data before attempting to use it.
    """

    required = {
        "id",
        "guild_id",
        "channel_id",
        "prize",
        "host_name",
        "end_time",
        "entries"
    }

    missing = required - set(giveaway.keys())

    if missing:
        print(
            f"[GIVEAWAY {giveaway_id}] "
            f"Missing fields: {sorted(missing)}"
        )
        return False

    try:
        int(giveaway["guild_id"])
        int(giveaway["channel_id"])
        float(giveaway["end_time"])

    except (TypeError, ValueError):
        print(
            f"[GIVEAWAY {giveaway_id}] "
            "Invalid guild/channel/end_time data."
        )
        return False

    if not isinstance(giveaway["entries"], list):
        print(
            f"[GIVEAWAY {giveaway_id}] "
            "entries is not a list."
        )
        return False

    completed_at = giveaway.get("completed_at")
    valid_completed_at = (
        isinstance(completed_at, int)
        and not isinstance(completed_at, bool)
        and 0 <= completed_at <= 253402300799
    ) or (
        isinstance(completed_at, float)
        and math.isfinite(completed_at)
        and 0 <= completed_at <= 253402300799
    )
    if completed_at is not None and not valid_completed_at:
        print(
            f"[GIVEAWAY {giveaway_id}] "
            "Invalid completion timestamp."
        )
        return False

    entry_weights = giveaway.get(
        "entry_weights",
        {}
    )

    if not isinstance(entry_weights, dict):
        print(
            f"[GIVEAWAY {giveaway_id}] "
            "entry_weights is not an object."
        )
        return False

    for user_id, weight in entry_weights.items():
        try:
            int(user_id)
            valid_weight = int(weight) >= 1

        except (TypeError, ValueError):
            valid_weight = False

        if not valid_weight:
            print(
                f"[GIVEAWAY {giveaway_id}] "
                f"Invalid entry weight for user {user_id!r}."
            )
            return False

    return True


def get_giveaway_ticket_count(giveaway: dict):
    entry_weights = giveaway.get(
        "entry_weights",
        {}
    )

    return sum(
        int(entry_weights.get(str(user_id), 1))
        for user_id in giveaway.get("entries", [])
    )


# ============================================================
# EMBEDS
# ============================================================

def create_giveaway_embed(giveaway: dict):
    guild_id = int(giveaway["guild_id"])

    settings = get_server_settings(guild_id)

    colour = 0x44C2A1

    embed = discord.Embed(
        title="<:MAgiveawayprize:1557835856146989138>    Giveaway!",
        description=(
            "Click the button below to enter!"
        ),
        colour=discord.Colour(colour)
    )

    embed.add_field(
        name="Prize",
        value=giveaway["prize"][
            :MAX_FIELD_VALUE_LENGTH
        ],
        inline=False
    )

    embed.add_field(
        name="Host",
        value=giveaway["host_name"][
            :MAX_FIELD_VALUE_LENGTH
        ],
        inline=True
    )

    required_role_id = giveaway.get(
        "required_role_id"
    )

    if required_role_id:
        guild = client.get_guild(guild_id)

        role = None

        if guild:
            role = guild.get_role(
                int(required_role_id)
            )

        role_text = (
            role.mention
            if role
            else "Required role"
        )

        embed.add_field(
            name="Required Role",
            value=role_text,
            inline=True
        )

    embed.add_field(
        name="Entries",
        value=str(
            get_giveaway_ticket_count(giveaway)
        ),
        inline=True
    )

    end_timestamp = int(
        giveaway["end_time"]
    )

    embed.add_field(
        name="⏰ Ends",
        value=(
            f"<t:{end_timestamp}:R>\n"
            f"<t:{end_timestamp}:F>"
        ),
        inline=False
    )

    embed.set_footer(
        text=f"Giveaway ID: {giveaway['id']}"
    )

    return add_premium_footer(
        embed,
        guild_id
    )


def create_finished_embed(
    giveaway: dict,
    winner_id: int | None
):
    guild_id = int(giveaway["guild_id"])

    settings = get_server_settings(
        guild_id
    )

    embed = discord.Embed(
        title="<:MAgiveawayprize:1557835856146989138>    Giveaway Ended!",
        colour=discord.Colour(
            settings.get(
                "colour",
                0x44C2A1
            )
        )
    )

    embed.add_field(
        name="~ Prize ~",
        value=giveaway["prize"][
            :MAX_FIELD_VALUE_LENGTH
        ],
        inline=False
    )

    embed.add_field(
        name="Host",
        value=giveaway["host_name"][
            :MAX_FIELD_VALUE_LENGTH
        ],
        inline=True
    )

    embed.add_field(
        name="Entries",
        value=str(
            get_giveaway_ticket_count(giveaway)
        ),
        inline=True
    )

    if winner_id is None:
        winner_text = "No valid entries."

    else:
        winner_text = f"<@{winner_id}>"

    embed.add_field(
        name="Winner",
        value=winner_text,
        inline=False
    )

    embed.set_footer(
        text=f"Giveaway ID: {giveaway['id']}"
    )

    return add_premium_footer(
        embed,
        guild_id
    )


# ============================================================
# BOT
# ============================================================

class AssistantMini(discord.Client):

    def __init__(self):
        super().__init__(
            intents=intents
        )

        self.tree = app_commands.CommandTree(
            self
        )

        self.startup_complete = False
        self.premium_sync_task = None
        self.giveaway_cleanup_task = None
        self.bank_interest_task = None

    async def setup_hook(self):
        """
        setup_hook runs once when the bot is starting.

        Persistent views are registered here rather than relying
        on on_ready(), which can fire repeatedly after reconnects.
        """

        prune_expired_giveaways()
        self.giveaway_cleanup_task = asyncio.create_task(
            self.cleanup_giveaway_history()
        )
        self.bank_interest_task = asyncio.create_task(
            self.run_bank_interest_schedule()
        )

        for giveaway_id in list(giveaways.keys()):

            if not validate_giveaway(
                giveaway_id,
                giveaways[giveaway_id]
            ):
                continue

            if giveaways[giveaway_id].get("completed_at") is not None:
                continue

            try:
                self.add_view(
                    GiveawayView(giveaway_id)
                )

                registered_view_ids.add(
                    giveaway_id
                )

            except Exception as error:
                print(
                    f"[VIEW] Could not register "
                    f"{giveaway_id}: {error}"
                )

        await self.tree.sync()

        print(
            f"[COMMANDS] Synced application commands."
        )

        self.premium_sync_task = asyncio.create_task(
            self.sync_premium_entitlements()
        )

        await recover_giveaways()

    async def cleanup_giveaway_history(self):
        while not self.is_closed():
            await asyncio.sleep(60)
            prune_expired_giveaways()

    async def run_bank_interest_schedule(self):
        while not self.is_closed():
            await apply_due_bank_interest()

            now = time.time()
            local_now = datetime.fromtimestamp(now)
            next_interest_time = local_now.replace(
                hour=1,
                minute=0,
                second=0,
                microsecond=0
            )
            if next_interest_time.timestamp() <= now:
                next_interest_time += timedelta(days=1)

            await asyncio.sleep(
                max(1, next_interest_time.timestamp() - now)
            )

    async def sync_premium_entitlements(self):
        while not self.is_closed():
            await self.wait_until_ready()

            try:
                skus = await self.fetch_skus()
                premium_skus = [
                    sku
                    for sku in skus
                    if sku.name.casefold() == PREMIUM_SKU_NAME.casefold()
                ]

                if len(premium_skus) != 1:
                    print(
                        f"[PREMIUM] Expected one SKU named "
                        f"{PREMIUM_SKU_NAME!r}, found "
                        f"{len(premium_skus)}; server settings were not changed."
                    )
                else:
                    premium_sku = premium_skus[0]
                    entitled_guild_ids = set()
                    now = discord.utils.utcnow()

                    async for entitlement in self.entitlements(
                        skus=[discord.Object(id=premium_sku.id)],
                        exclude_ended=True,
                        exclude_deleted=True
                    ):
                        if (
                            entitlement.guild_id is not None
                            and (
                                entitlement.starts_at is None
                                or entitlement.starts_at <= now
                            )
                            and not entitlement.is_expired()
                        ):
                            entitled_guild_ids.add(
                                entitlement.guild_id
                            )

                    changed = False

                    for guild in self.guilds:
                        settings = get_server_settings(guild.id)
                        has_premium = guild.id in entitled_guild_ids

                        if settings.get("assistantmini_premium") != has_premium:
                            settings["assistantmini_premium"] = has_premium
                            changed = True

                    if changed:
                        save_json(
                            SETTINGS_FILE,
                            server_settings
                        )

                    print(
                        f"[PREMIUM] Checked {len(self.guilds)} server(s); "
                        f"{sum(guild.id in entitled_guild_ids for guild in self.guilds)} "
                        "have AssistantMini Premium."
                    )

                coin_crate_skus = [
                    sku
                    for sku in skus
                    if sku.name.casefold() == COIN_CRATE_SKU_NAME.casefold()
                ]

                if coin_crate_skus:
                    coin_crate_sku = coin_crate_skus[0]
                    now = discord.utils.utcnow()

                    async for entitlement in self.entitlements(
                        skus=[discord.Object(id=coin_crate_sku.id)],
                        exclude_ended=True,
                        exclude_deleted=True
                    ):
                        if entitlement.user_id is None:
                            continue
                        if entitlement.starts_at is not None and entitlement.starts_at > now:
                            continue
                        if entitlement.is_expired():
                            continue

                        async with economy_lock:
                            award_coin_crate_purchase(
                                entitlement.user_id,
                                entitlement.id
                            )

            except discord.DiscordException as error:
                print(
                    f"[PREMIUM] Could not check SKU entitlements: {error}"
                )

            await asyncio.sleep(PREMIUM_SYNC_INTERVAL)


client = AssistantMini()


# ============================================================
# GIVEAWAY VIEW
# ============================================================

class GiveawayEnterButton(discord.ui.Button):

    def __init__(self, giveaway_id: str):
        super().__init__(
            label="Enter Giveaway",
            emoji="🎉",
            style=discord.ButtonStyle.primary,
            custom_id=f"giveaway_enter:{giveaway_id}"
        )
        self.giveaway_id = giveaway_id

    async def callback(
        self,
        interaction: discord.Interaction
    ) -> None:
        await enter_giveaway(
            interaction,
            self.giveaway_id
        )


class GiveawayView(discord.ui.View):

    def __init__(
        self,
        giveaway_id: str
    ):
        super().__init__(
            timeout=None
        )

        self.add_item(
            GiveawayEnterButton(giveaway_id)
        )


# ============================================================
# GIVEAWAY RECOVERY
# ============================================================

async def recover_giveaways():
    """
    Recover every giveaway after a restart.

    Active giveaways get a timer.

    Expired giveaways are processed immediately.
    """

    if client.startup_complete:
        return

    client.startup_complete = True

    prune_expired_giveaways()

    print(
        f"[RECOVERY] Checking {len(giveaways)} giveaway(s)."
    )

    for giveaway_id in list(giveaways.keys()):

        giveaway = giveaways.get(
            giveaway_id
        )

        if giveaway is None:
            continue

        if not validate_giveaway(
            giveaway_id,
            giveaway
        ):
            continue

        if giveaway.get("completed_at") is not None:
            continue

        start_giveaway_task(
            giveaway_id
        )

    print("[RECOVERY] Giveaway recovery complete.")


# ============================================================
# GIVEAWAY TASK
# ============================================================

def start_giveaway_task(
    giveaway_id: str
):
    existing = giveaway_tasks.get(
        giveaway_id
    )

    if existing and not existing.done():
        return

    task = asyncio.create_task(
        giveaway_worker(giveaway_id)
    )

    giveaway_tasks[giveaway_id] = task


async def giveaway_worker(
    giveaway_id: str
):
    """
    Reliable giveaway worker.

    If something temporary fails, the worker retries instead
    of simply disappearing.
    """

    while giveaway_id in giveaways:

        giveaway = giveaways.get(
            giveaway_id
        )

        if giveaway is None:
            break

        if not validate_giveaway(
            giveaway_id,
            giveaway
        ):
            break

        remaining = (
            float(giveaway["end_time"])
            - time.time()
        )

        if remaining > 0:

            try:
                await asyncio.sleep(
                    min(remaining, 60)
                )

            except asyncio.CancelledError:
                raise

            continue

        # Giveaway has expired.
        success = await finish_giveaway(
            giveaway_id
        )

        if success:
            break

        # Something failed.
        # Keep the giveaway and retry.
        print(
            f"[GIVEAWAY {giveaway_id}] "
            f"Completion failed. Retrying in "
            f"{RETRY_DELAY}s."
        )

        try:
            await asyncio.sleep(
                RETRY_DELAY
            )

        except asyncio.CancelledError:
            raise

    giveaway_tasks.pop(
        giveaway_id,
        None
    )


# ============================================================
# ENTER GIVEAWAY
# ============================================================

async def enter_giveaway(
    interaction: discord.Interaction,
    giveaway_id: str
):
    giveaway = giveaways.get(
        giveaway_id
    )

    if giveaway is None:
        await interaction.response.send_message(
            "❌ This giveaway is no longer active.",
            ephemeral=True
        )
        return

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Giveaways can only be entered inside a server.",
            ephemeral=True
        )
        return

    try:
        giveaway_guild_id = int(
            giveaway["guild_id"]
        )

    except (KeyError, TypeError, ValueError):
        await interaction.response.send_message(
            "❌ This giveaway's data is invalid.",
            ephemeral=True
        )
        return

    if giveaway_guild_id != interaction.guild.id:
        await interaction.response.send_message(
            "❌ This giveaway does not belong to this server.",
            ephemeral=True
        )
        return

    lock = get_giveaway_lock(
        giveaway_id
    )

    async with lock:

        # Reload it after acquiring the lock.
        giveaway = giveaways.get(
            giveaway_id
        )

        if giveaway is None:
            await interaction.response.send_message(
                "❌ This giveaway is no longer active.",
                ephemeral=True
            )
            return

        # ----------------------------------------------------
        # Expiry check
        # ----------------------------------------------------

        if time.time() >= float(
            giveaway["end_time"]
        ):
            start_giveaway_task(
                giveaway_id
            )

            await interaction.response.send_message(
                "❌ This giveaway has ended.",
                ephemeral=True
            )
            return

        # ----------------------------------------------------
        # Required role
        # ----------------------------------------------------

        required_role_id = giveaway.get(
            "required_role_id"
        )

        if required_role_id:

            role = interaction.guild.get_role(
                int(required_role_id)
            )

            if role is None:
                await interaction.response.send_message(
                    "❌ The required role no longer exists.",
                    ephemeral=True
                )
                return

            member = interaction.user

            if not isinstance(member, discord.Member):
                await interaction.response.send_message(
                    "❌ Unable to validate your server roles.",
                    ephemeral=True
                )
                return

            if role not in member.roles:
                await interaction.response.send_message(
                    f"❌ You need {role.mention} "
                    "to enter this giveaway.",
                    ephemeral=True
                )
                return

        # ----------------------------------------------------
        # Duplicate entry
        # ----------------------------------------------------

        user_id = interaction.user.id

        entries = giveaway.setdefault(
            "entries",
            []
        )

        if user_id in entries:
            await interaction.response.send_message(
                "⚠️ You are already entered.",
                ephemeral=True
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ Unable to validate your server roles.",
                ephemeral=True
            )
            return

        role_entry_counts = get_server_settings(
            interaction.guild.id
        ).get(
            "giveaway_role_entries",
            {}
        )

        if not isinstance(role_entry_counts, dict):
            print(
                f"[GIVEAWAY] Invalid role-entry settings "
                f"for guild {interaction.guild.id}."
            )
            await interaction.response.send_message(
                "❌ Giveaway role-entry settings are invalid. "
                "Please contact an administrator.",
                ephemeral=True
            )
            return

        member_role_ids = {
            role.id
            for role in interaction.user.roles
        }
        ticket_count = 1

        for role_id, configured_tickets in role_entry_counts.items():
            try:
                role_id_int = int(role_id)
                configured_tickets_int = int(configured_tickets)

            except (TypeError, ValueError):
                print(
                    f"[GIVEAWAY] Invalid role-entry setting "
                    f"{role_id!r}: {configured_tickets!r}."
                )
                await interaction.response.send_message(
                    "❌ Giveaway role-entry settings are invalid. "
                    "Please contact an administrator.",
                    ephemeral=True
                )
                return

            if configured_tickets_int < 2:
                print(
                    f"[GIVEAWAY] Invalid ticket count "
                    f"{configured_tickets_int} for role {role_id_int}."
                )
                await interaction.response.send_message(
                    "❌ Giveaway role-entry settings are invalid. "
                    "Please contact an administrator.",
                    ephemeral=True
                )
                return

            if role_id_int in member_role_ids:
                ticket_count = max(
                    ticket_count,
                    configured_tickets_int
                )

        # ----------------------------------------------------
        # Add entry
        # ----------------------------------------------------

        entries.append(
            user_id
        )

        entry_weights = giveaway.setdefault(
            "entry_weights",
            {}
        )
        entry_weights[str(user_id)] = ticket_count

        save_json(
            GIVEAWAY_FILE,
            giveaways
        )

    # --------------------------------------------------------
    # Respond
    # --------------------------------------------------------

    await interaction.response.send_message(
        f"✅ You have been entered into the giveaway "
        f"with **{ticket_count}** ticket"
        f"{'s' if ticket_count != 1 else ''}!",
        ephemeral=True
    )

    # --------------------------------------------------------
    # Update message
    # --------------------------------------------------------

    await update_giveaway_message(
        giveaway_id
    )


# ============================================================
# UPDATE GIVEAWAY MESSAGE
# ============================================================

async def update_giveaway_message(
    giveaway_id: str
):
    giveaway = giveaways.get(
        giveaway_id
    )

    if giveaway is None:
        return

    message_id = giveaway.get(
        "message_id"
    )

    if not message_id:
        return

    guild = client.get_guild(
        int(giveaway["guild_id"])
    )

    if guild is None:
        return

    channel = guild.get_channel(
        int(giveaway["channel_id"])
    )

    if channel is None:
        return

    if not isinstance(
        channel,
        discord.abc.Messageable
    ):
        return

    try:
        message = await channel.fetch_message(
            int(message_id)
        )

        await message.edit(
            embed=create_giveaway_embed(
                giveaway
            ),
            view=GiveawayView(
                giveaway_id
            )
        )

    except discord.NotFound:
        print(
            f"[GIVEAWAY {giveaway_id}] "
            "Original message no longer exists."
        )

    except discord.Forbidden:
        print(
            f"[GIVEAWAY {giveaway_id}] "
            "No permission to update message."
        )

    except discord.HTTPException as error:
        print(
            f"[GIVEAWAY {giveaway_id}] "
            f"Could not update message: {error}"
        )


# ============================================================
# FINISH GIVEAWAY
# ============================================================

async def finish_giveaway(
    giveaway_id: str
):
    """
    Returns:

        True  = giveaway completed and retained for rerolls
        False = temporary/permanent operation failed;
                keep it in storage and retry
    """

    giveaway = giveaways.get(
        giveaway_id
    )

    if giveaway is None:
        return True

    if giveaway.get("completed_at") is not None:
        return True

    lock = get_giveaway_lock(
        giveaway_id
    )

    async with lock:

        giveaway = giveaways.get(
            giveaway_id
        )

        if giveaway is None:
            return True

        if giveaway.get("completed_at") is not None:
            return True

        # ----------------------------------------------------
        # Already announced?
        # ----------------------------------------------------

        # This prevents duplicate announcements if the bot
        # restarts after the winner announcement.
        announcement_id = giveaway.get(
            "announcement_message_id"
        )

        # ----------------------------------------------------
        # Validate guild
        # ----------------------------------------------------

        try:
            guild_id = int(
                giveaway["guild_id"]
            )
            channel_id = int(
                giveaway["channel_id"]
            )

        except (KeyError, TypeError, ValueError):
            print(
                f"[GIVEAWAY {giveaway_id}] "
                "Invalid guild/channel data."
            )
            return False

        guild = client.get_guild(
            guild_id
        )

        if guild is None:
            try:
                guild = await client.fetch_guild(
                    guild_id
                )
            except discord.HTTPException as error:
                print(
                    f"[GIVEAWAY {giveaway_id}] "
                    f"Could not fetch guild: {error}"
                )
                return False

        # ----------------------------------------------------
        # Find channel
        # ----------------------------------------------------

        channel = guild.get_channel(
            channel_id
        )

        if channel is None:
            try:
                channel = await guild.fetch_channel(
                    channel_id
                )

            except discord.NotFound:
                print(
                    f"[GIVEAWAY {giveaway_id}] "
                    "Channel no longer exists."
                )
                return False

            except discord.Forbidden:
                print(
                    f"[GIVEAWAY {giveaway_id}] "
                    "No permission to access channel."
                )
                return False

            except discord.HTTPException as error:
                print(
                    f"[GIVEAWAY {giveaway_id}] "
                    f"Channel fetch failed: {error}"
                )
                return False

        if not isinstance(
            channel,
            discord.abc.Messageable
        ):
            print(
                f"[GIVEAWAY {giveaway_id}] "
                "Channel is not message-capable."
            )
            return False

        # ----------------------------------------------------
        # Choose winner
        # ----------------------------------------------------

        entries = list(
            dict.fromkeys(
                giveaway.get(
                    "entries",
                    []
                )
            )
        )

        entry_weights = giveaway.get(
            "entry_weights",
            {}
        )

        winner_id = giveaway.get(
            "winner_id"
        )

        if winner_id is None:

            if entries:
                winner_id = random.choices(
                    entries,
                    weights=[
                        int(
                            entry_weights.get(
                                str(user_id),
                                1
                            )
                        )
                        for user_id in entries
                    ],
                    k=1
                )[0]

            giveaway["winner_id"] = (
                winner_id
            )

            save_json(
                GIVEAWAY_FILE,
                giveaways
            )

        # ----------------------------------------------------
        # Finished embed
        # ----------------------------------------------------

        finished_embed = create_finished_embed(
            giveaway,
            winner_id
        )

        # ----------------------------------------------------
        # Edit original giveaway
        # ----------------------------------------------------

        message_id = giveaway.get(
            "message_id"
        )

        if message_id:

            try:
                message = await channel.fetch_message(
                    int(message_id)
                )

                await message.edit(
                    embed=finished_embed,
                    view=None
                )

            except discord.NotFound:
                print(
                    f"[GIVEAWAY {giveaway_id}] "
                    "Original giveaway message is gone."
                )

                # That's okay. We can still announce the result.

            except discord.Forbidden:
                print(
                    f"[GIVEAWAY {giveaway_id}] "
                    "Cannot edit original message."
                )

                return False

            except discord.HTTPException as error:
                print(
                    f"[GIVEAWAY {giveaway_id}] "
                    f"Could not edit giveaway: {error}"
                )

                return False

        # ----------------------------------------------------
        # Winner announcement
        # ----------------------------------------------------

        if announcement_id is None:

            try:

                if winner_id is not None:

                    announcement = await channel.send(
                        f"🎉 Congratulations <@{winner_id}>! "
                        f"You won **{giveaway['prize']}**!"
                    )

                else:

                    announcement = await channel.send(
                        f"🎉 The giveaway for "
                        f"**{giveaway['prize']}** has ended, "
                        "but nobody entered."
                    )

                # Save the announcement ID immediately.
                giveaway[
                    "announcement_message_id"
                ] = announcement.id

                save_json(
                    GIVEAWAY_FILE,
                    giveaways
                )

            except discord.Forbidden:
                print(
                    f"[GIVEAWAY {giveaway_id}] "
                    "Cannot announce winner."
                )
                return False

            except discord.HTTPException as error:
                print(
                    f"[GIVEAWAY {giveaway_id}] "
                    f"Announcement failed: {error}"
                )
                return False

        # ----------------------------------------------------
        # COMPLETED
        # ----------------------------------------------------

        giveaway["completed_at"] = time.time()
        if not save_json(GIVEAWAY_FILE, giveaways):
            giveaway.pop("completed_at", None)
            return False

        print(
            f"[GIVEAWAY {giveaway_id}] "
            "Completed successfully and retained for rerolls."
        )

        return True


# ============================================================
# BOT READY
# ============================================================

@client.event
async def on_ready():

    user = client.user
    if user is None:
        print("Logged in as unknown user.")
        return

    print(
        f"Logged in as {user} "
        f"({user.id})"
    )

    print(
        f"Connected to {len(client.guilds)} server(s)."
    )


@client.event
async def on_entitlement_create(entitlement: discord.Entitlement):
    if entitlement.user_id is None:
        return

    if entitlement.is_expired():
        return

    try:
        skus = await client.fetch_skus()
    except discord.DiscordException as error:
        print(
            f"[ECONOMY] Could not identify entitlement "
            f"{entitlement.id}: {error}"
        )
        return

    sku = next(
        (
            item
            for item in skus
            if item.id == entitlement.sku_id
        ),
        None
    )
    if sku is None or sku.name.casefold() != COIN_CRATE_SKU_NAME.casefold():
        return

    async with economy_lock:
        award_coin_crate_purchase(
            entitlement.user_id,
            entitlement.id
        )


# ============================================================
# GIVEAWAY CREATE
# ============================================================

@client.tree.command(
    name="giveaway-create",
    description="Create a new giveaway."
)
@app_commands.describe(
    prize="The prize being given away.",
    duration="Duration such as 30s, 10m, 2h or 7d.",
    host_name="The name of the giveaway host.",
    channel="The channel where the giveaway will be posted.",
    required_role="Optional role required to enter."
)
@app_commands.default_permissions(
    administrator=True
)
async def giveaway_create(
    interaction: discord.Interaction,
    prize: str,
    duration: str,
    host_name: str,
    channel: discord.TextChannel,
    required_role: discord.Role | None = None
):

    # --------------------------------------------------------
    # Server check
    # --------------------------------------------------------

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )
        return

    # --------------------------------------------------------
    # Administrator check
    # --------------------------------------------------------

    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "❌ This command can only be used by a server member.",
            ephemeral=True
        )
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ You need the **Administrator** permission "
            "to create giveaways.",
            ephemeral=True
        )
        return

    # --------------------------------------------------------
    # Prize
    # --------------------------------------------------------

    prize = prize.strip()

    if not prize:
        await interaction.response.send_message(
            "❌ The prize cannot be empty.",
            ephemeral=True
        )
        return

    if len(prize) > MAX_FIELD_VALUE_LENGTH:
        await interaction.response.send_message(
            f"❌ The prize must be "
            f"{MAX_FIELD_VALUE_LENGTH} characters or fewer.",
            ephemeral=True
        )
        return

    # --------------------------------------------------------
    # Host
    # --------------------------------------------------------

    host_name = host_name.strip()

    if not host_name:
        await interaction.response.send_message(
            "❌ The host name cannot be empty.",
            ephemeral=True
        )
        return

    if len(host_name) > MAX_FIELD_VALUE_LENGTH:
        await interaction.response.send_message(
            f"❌ The host name must be "
            f"{MAX_FIELD_VALUE_LENGTH} characters or fewer.",
            ephemeral=True
        )
        return

    # --------------------------------------------------------
    # Duration
    # --------------------------------------------------------

    duration_seconds = parse_duration(
        duration
    )

    if duration_seconds is None:
        await interaction.response.send_message(
            "❌ Invalid duration.\n\n"
            "Examples:\n"
            "`30s`\n"
            "`10m`\n"
            "`2h`\n"
            "`7d`\n\n"
            "Maximum duration is 30 days.",
            ephemeral=True
        )
        return

    # --------------------------------------------------------
    # Get bot member
    # --------------------------------------------------------

    bot_member = interaction.guild.me

    if bot_member is None:
        if client.user is None:
            await interaction.response.send_message(
                "❌ I couldn't determine my permissions "
                "in that channel.",
                ephemeral=True
            )
            return

        try:
            bot_member = await interaction.guild.fetch_member(
                client.user.id
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ I couldn't determine my permissions "
                "in that channel.",
                ephemeral=True
            )
            return

    # --------------------------------------------------------
    # Permissions
    # --------------------------------------------------------

    permissions = channel.permissions_for(
        bot_member
    )

    required_permissions = {
        "view_channel": "View Channel",
        "send_messages": "Send Messages",
        "embed_links": "Embed Links",
        "read_message_history": "Read Message History"
    }

    for attribute, display_name in required_permissions.items():

        if not getattr(
            permissions,
            attribute
        ):
            await interaction.response.send_message(
                f"❌ I need **{display_name}** "
                f"permission in {channel.mention}.",
                ephemeral=True
            )
            return

    # --------------------------------------------------------
    # Defer
    # --------------------------------------------------------

    await interaction.response.defer(
        ephemeral=True
    )

    # --------------------------------------------------------
    # Create ID
    # --------------------------------------------------------

    giveaway_id = uuid.uuid4().hex

    end_time = (
        time.time()
        + duration_seconds
    )

    giveaway = {
        "id": giveaway_id,
        "guild_id": interaction.guild.id,
        "channel_id": channel.id,

        # Set after Discord successfully creates the message.
        "message_id": None,

        "prize": prize,
        "host_name": host_name,

        "duration": duration,
        "duration_seconds": duration_seconds,

        "end_time": end_time,

        "required_role_id": (
            required_role.id
            if required_role
            else None
        ),

        "entries": [],
        "entry_weights": {},

        # These are filled in when the giveaway finishes.
        "winner_id": None,
        "announcement_message_id": None
    }

    # --------------------------------------------------------
    # Save giveaway
    # --------------------------------------------------------

    giveaways[giveaway_id] = giveaway

    save_json(
        GIVEAWAY_FILE,
        giveaways
    )

    # --------------------------------------------------------
    # Send giveaway message
    # --------------------------------------------------------

    try:

        message = await channel.send(
            embed=create_giveaway_embed(
                giveaway
            ),
            view=GiveawayView(
                giveaway_id
            )
        )

    except discord.Forbidden:

        giveaways.pop(
            giveaway_id,
            None
        )

        save_json(
            GIVEAWAY_FILE,
            giveaways
        )

        await interaction.followup.send(
            "❌ I couldn't send the giveaway message. "
            "Please check my channel permissions.",
            ephemeral=True
        )

        return

    except discord.HTTPException as error:

        giveaways.pop(
            giveaway_id,
            None
        )

        save_json(
            GIVEAWAY_FILE,
            giveaways
        )

        await interaction.followup.send(
            f"❌ Discord returned an error:\n"
            f"`{error}`",
            ephemeral=True
        )

        return

    # --------------------------------------------------------
    # Save message ID
    # --------------------------------------------------------

    giveaway["message_id"] = message.id

    save_json(
        GIVEAWAY_FILE,
        giveaways
    )

    # --------------------------------------------------------
    # Start worker
    # --------------------------------------------------------

    start_giveaway_task(
        giveaway_id
    )

    # --------------------------------------------------------
    # Confirmation
    # --------------------------------------------------------

    role_text = (
        required_role.mention
        if required_role
        else "None"
    )

    await interaction.followup.send(
        "✅ **Giveaway created!**\n\n"
        f"**Prize:** {prize}\n"
        f"**Duration:** "
        f"{format_duration(duration_seconds)}\n"
        f"**Host:** {host_name}\n"
        f"**Channel:** {channel.mention}\n"
        f"**Required role:** {role_text}",
        ephemeral=True
    )


@client.tree.command(
    name="giveaway-reroll",
    description="Pick a replacement winner for a completed giveaway."
)
@app_commands.describe(
    giveaway_id="The Giveaway ID shown in the giveaway embed footer."
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def giveaway_reroll_command(
    interaction: discord.Interaction,
    giveaway_id: str
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return

    if (
        not isinstance(interaction.user, discord.Member)
        or not interaction.user.guild_permissions.administrator
    ):
        await interaction.response.send_message(
            "You need the Administrator permission to use this command.",
            ephemeral=True
        )
        return

    giveaway_id = giveaway_id.strip().lower()
    if not giveaway_id:
        await interaction.response.send_message(
            "Enter the Giveaway ID shown in the giveaway embed footer.",
            ephemeral=True
        )
        return

    if not prune_expired_giveaways():
        await interaction.response.send_message(
            "Completed giveaway history could not be updated. Please try again later.",
            ephemeral=True
        )
        return

    giveaway = giveaways.get(giveaway_id)
    if giveaway is None:
        await interaction.response.send_message(
            "That giveaway was not found or its 48-hour reroll period has expired.",
            ephemeral=True
        )
        return

    if not validate_giveaway(giveaway_id, giveaway):
        await interaction.response.send_message(
            "The saved giveaway data is invalid. Please contact an administrator.",
            ephemeral=True
        )
        return

    if int(giveaway["guild_id"]) != interaction.guild.id:
        await interaction.response.send_message(
            "That giveaway does not belong to this server.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    lock = get_giveaway_lock(giveaway_id)
    async with lock:
        giveaway = giveaways.get(giveaway_id)
        if giveaway is None or not validate_giveaway(giveaway_id, giveaway):
            await interaction.followup.send(
                "The saved giveaway data is unavailable or invalid.",
                ephemeral=True
            )
            return

        completed_at = giveaway.get("completed_at")
        if (
            not isinstance(completed_at, (int, float))
            or isinstance(completed_at, bool)
            or time.time() - completed_at >= COMPLETED_GIVEAWAY_RETENTION
        ):
            await interaction.followup.send(
                "That giveaway has not finished or its 48-hour reroll period has expired.",
                ephemeral=True
            )
            return

        if float(giveaway["end_time"]) > time.time():
            await interaction.followup.send(
                "That giveaway has not finished yet.",
                ephemeral=True
            )
            return

        entries = []
        for entry in giveaway["entries"]:
            if (
                not isinstance(entry, int)
                or isinstance(entry, bool)
                or entry <= 0
            ):
                await interaction.followup.send(
                    "The giveaway contains invalid entrant data. Please contact an administrator.",
                    ephemeral=True
                )
                return
            if entry not in entries:
                entries.append(entry)

        if not entries:
            await interaction.followup.send(
                "This giveaway has no entrants to reroll.",
                ephemeral=True
            )
            return

        try:
            old_winner_id = (
                int(giveaway["winner_id"])
                if giveaway.get("winner_id") is not None
                else None
            )
        except (TypeError, ValueError):
            await interaction.followup.send(
                "The saved winner data is invalid. Please contact an administrator.",
                ephemeral=True
            )
            return

        eligible_entries = [
            entry for entry in entries if entry != old_winner_id
        ]
        if eligible_entries:
            entries = eligible_entries

        entry_weights = giveaway.get("entry_weights", {})
        try:
            weights = [
                int(entry_weights.get(str(entry), 1))
                for entry in entries
            ]
        except (TypeError, ValueError):
            await interaction.followup.send(
                "The saved entrant weights are invalid. Please contact an administrator.",
                ephemeral=True
            )
            return

        if any(weight < 1 for weight in weights):
            await interaction.followup.send(
                "The saved entrant weights are invalid. Please contact an administrator.",
                ephemeral=True
            )
            return

        new_winner_id = random.choices(
            entries,
            weights=weights,
            k=1
        )[0]

        channel = interaction.guild.get_channel(
            int(giveaway["channel_id"])
        )
        if channel is None:
            try:
                channel = await interaction.guild.fetch_channel(
                    int(giveaway["channel_id"])
                )
            except discord.NotFound:
                await interaction.followup.send(
                    "The giveaway channel no longer exists.",
                    ephemeral=True
                )
                return
            except discord.Forbidden:
                await interaction.followup.send(
                    "I do not have permission to access the giveaway channel.",
                    ephemeral=True
                )
                return
            except discord.HTTPException as error:
                await interaction.followup.send(
                    f"Could not access the giveaway channel: {error}",
                    ephemeral=True
                )
                return

        if not isinstance(channel, discord.abc.Messageable):
            await interaction.followup.send(
                "The giveaway channel cannot receive messages.",
                ephemeral=True
            )
            return

        message_id = giveaway.get("message_id")
        if message_id is None:
            await interaction.followup.send(
                "The original giveaway message ID is missing.",
                ephemeral=True
            )
            return

        try:
            original_message = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            await interaction.followup.send(
                "The original giveaway message no longer exists, so its result cannot be updated.",
                ephemeral=True
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                "I do not have permission to update the original giveaway message.",
                ephemeral=True
            )
            return
        except discord.HTTPException as error:
            await interaction.followup.send(
                f"Could not retrieve the original giveaway message: {error}",
                ephemeral=True
            )
            return

        previous_winner_id = giveaway.get("winner_id")
        giveaway["winner_id"] = new_winner_id
        if not save_json(GIVEAWAY_FILE, giveaways):
            giveaway["winner_id"] = previous_winner_id
            await interaction.followup.send(
                "The new winner could not be saved. Please try again later.",
                ephemeral=True
            )
            return

        try:
            await original_message.edit(
                embed=create_finished_embed(giveaway, new_winner_id),
                view=None
            )
        except discord.Forbidden:
            giveaway["winner_id"] = previous_winner_id
            if not save_json(GIVEAWAY_FILE, giveaways):
                print(
                    f"[GIVEAWAY {giveaway_id}] Could not restore the previous "
                    "winner after an edit permission failure."
                )
            await interaction.followup.send(
                "I do not have permission to update the original giveaway result.",
                ephemeral=True
            )
            return
        except discord.HTTPException as error:
            giveaway["winner_id"] = previous_winner_id
            if not save_json(GIVEAWAY_FILE, giveaways):
                print(
                    f"[GIVEAWAY {giveaway_id}] Could not restore the previous "
                    "winner after an edit failure."
                )
            await interaction.followup.send(
                f"Could not update the giveaway result: {error}",
                ephemeral=True
            )
            return

        try:
            announcement = await channel.send(
                f"The giveaway has been rerolled! "
                f"Congratulations <@{new_winner_id}>! "
                f"You won **{giveaway['prize']}**!"
            )
        except discord.Forbidden:
            giveaway["winner_id"] = previous_winner_id
            if not save_json(GIVEAWAY_FILE, giveaways):
                print(
                    f"[GIVEAWAY {giveaway_id}] Could not restore the previous "
                    "winner after an announcement permission failure."
                )
            try:
                await original_message.edit(
                    embed=create_finished_embed(
                        giveaway,
                        previous_winner_id
                    ),
                    view=None
                )
            except discord.HTTPException as error:
                print(
                    f"[GIVEAWAY {giveaway_id}] Could not restore the original "
                    f"result embed: {error}"
                )
            await interaction.followup.send(
                "I do not have permission to announce the new winner.",
                ephemeral=True
            )
            return
        except discord.HTTPException as error:
            giveaway["winner_id"] = previous_winner_id
            if not save_json(GIVEAWAY_FILE, giveaways):
                print(
                    f"[GIVEAWAY {giveaway_id}] Could not restore the previous "
                    "winner after an announcement failure."
                )
            try:
                await original_message.edit(
                    embed=create_finished_embed(
                        giveaway,
                        previous_winner_id
                    ),
                    view=None
                )
            except discord.HTTPException as restore_error:
                print(
                    f"[GIVEAWAY {giveaway_id}] Could not restore the original "
                    f"result embed: {restore_error}"
                )
            await interaction.followup.send(
                f"Could not announce the new winner: {error}",
                ephemeral=True
            )
            return

        previous_announcement_id = giveaway.get("announcement_message_id")
        previous_announcement_warning = None
        if previous_announcement_id is not None:
            try:
                previous_announcement = await channel.fetch_message(
                    int(previous_announcement_id)
                )
                await previous_announcement.edit(
                    content=(
                        "This giveaway result was rerolled. "
                        "See the latest announcement for the replacement winner."
                    )
                )
            except discord.NotFound:
                print(
                    f"[GIVEAWAY {giveaway_id}] Previous winner announcement "
                    "no longer exists."
                )
            except discord.Forbidden:
                previous_announcement_warning = (
                    " I could not edit the previous winner announcement."
                )
                print(
                    f"[GIVEAWAY {giveaway_id}] Could not update the previous "
                    "winner announcement."
                )
            except discord.HTTPException as error:
                previous_announcement_warning = (
                    " I could not edit the previous winner announcement."
                )
                print(
                    f"[GIVEAWAY {giveaway_id}] Could not update the previous "
                    f"winner announcement: {error}"
                )

        giveaway["announcement_message_id"] = announcement.id
        if not save_json(GIVEAWAY_FILE, giveaways):
            giveaway["announcement_message_id"] = previous_announcement_id
            await interaction.followup.send(
                f"The new winner <@{new_winner_id}> was announced, but the "
                "announcement reference could not be saved.",
                ephemeral=True
            )
            return

    await interaction.followup.send(
        f"Giveaway rerolled. The new winner is <@{new_winner_id}>."
        f"{previous_announcement_warning or ''}",
        ephemeral=True
    )


# ============================================================
# GIVEAWAY ROLE ENTRIES
# ============================================================

@client.tree.command(
    name="giveaway-role-entry",
    description="Give a server role multiple tickets in giveaways."
)
@app_commands.describe(
    role="The role that receives multiple giveaway tickets.",
    tickets="The total number of tickets members with this role receive."
)
@app_commands.default_permissions(
    administrator=True
)
async def giveaway_role_entry(
    interaction: discord.Interaction,
    role: discord.Role,
    tickets: int
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )
        return

    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "❌ This command can only be used by a server member.",
            ephemeral=True
        )
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ You need the **Administrator** permission "
            "to configure giveaway role entries.",
            ephemeral=True
        )
        return

    if tickets < 2:
        await interaction.response.send_message(
            "❌ The ticket count must be at least 2.",
            ephemeral=True
        )
        return

    settings = get_server_settings(
        interaction.guild.id
    )
    role_entry_counts = settings.setdefault(
        "giveaway_role_entries",
        {}
    )

    if not isinstance(role_entry_counts, dict):
        print(
            f"[GIVEAWAY] Invalid role-entry settings "
            f"for guild {interaction.guild.id}."
        )
        await interaction.response.send_message(
            "❌ Giveaway role-entry settings are invalid. "
            "Please contact support.",
            ephemeral=True
        )
        return

    role_id = str(role.id)
    is_new_role = role_id not in role_entry_counts
    role_limit = (
        10
        if settings.get("assistantmini_premium", False)
        else 2
    )

    if is_new_role and len(role_entry_counts) >= role_limit:
        premium_status = (
            "Premium servers can configure up to 10 roles."
            if role_limit == 2
            else "This server has reached the 10-role limit."
        )
        await interaction.response.send_message(
            f"❌ This server has reached its {role_limit}-role limit. "
            f"{premium_status}",
            ephemeral=True
        )
        return

    role_entry_counts[role_id] = tickets
    save_json(
        SETTINGS_FILE,
        server_settings
    )

    await interaction.response.send_message(
        f"✅ Members with {role.mention} will receive "
        f"**{tickets} total entries** in giveaways. "
        f"This server has {len(role_entry_counts)}/{role_limit} "
        "extra role entries. Join Premium version to add up to 10 roles.",
        ephemeral=True
    )

# ============================================================
# Giveaway Role Entry Remove Command
# ============================================================


@client.tree.command(
    name="giveaway-role-entry-remove",
    description="Remove a role's extra giveaway tickets."
)
@app_commands.describe(
    role="The role to remove from giveaway role entries."
)
@app_commands.default_permissions(
    administrator=True
)
async def giveaway_role_entry_remove(
    interaction: discord.Interaction,
    role: discord.Role
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )
        return

    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "❌ This command can only be used by a server member.",
            ephemeral=True
        )
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ You need the **Administrator** permission "
            "to configure giveaway role entries.",
            ephemeral=True
        )
        return

    settings = get_server_settings(
        interaction.guild.id
    )
    role_entry_counts = settings.get(
        "giveaway_role_entries",
        {}
    )

    if not isinstance(role_entry_counts, dict):
        print(
            f"[GIVEAWAY] Invalid role-entry settings "
            f"for guild {interaction.guild.id}."
        )
        await interaction.response.send_message(
            "❌ Giveaway role-entry settings are invalid. "
            "Please contact support.",
            ephemeral=True
        )
        return

    removed_tickets = role_entry_counts.pop(
        str(role.id),
        None
    )

    if removed_tickets is None:
        await interaction.response.send_message(
            f"❌ {role.mention} does not have a giveaway role-entry bonus.",
            ephemeral=True
        )
        return

    save_json(
        SETTINGS_FILE,
        server_settings
    )

    role_limit = (
        10
        if settings.get("assistantmini_premium", False)
        else 2
    )

    await interaction.response.send_message(
        f"✅ Removed the giveaway role-entry bonus for {role.mention}. "
        f"This server now has {len(role_entry_counts)}/{role_limit} "
        "configured roles.",
        ephemeral=True
    )

# ============================================================
# Giveaway Role Entry List Command
# ============================================================


@client.tree.command(
    name="giveaway-role-entry-list",
    description="List the roles with additional giveaway entries."
)
@app_commands.default_permissions(
    administrator=True
)
async def giveaway_role_entry_list(
    interaction: discord.Interaction
):
    async def send_embed(embed: discord.Embed):
        if interaction.guild is not None:
            add_premium_footer(
                embed,
                interaction.guild.id
            )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    if interaction.guild is None:
        await send_embed(discord.Embed(
            title="Giveaway role entries unavailable",
            description="This command can only be used in a server.",
            colour=discord.Colour(0xffffff)
        ))
        return

    if (
        not isinstance(interaction.user, discord.Member)
        or not interaction.user.guild_permissions.administrator
    ):
        await send_embed(discord.Embed(
            title="Administrator permission required",
            description=(
                "You need the Administrator permission to view "
                "giveaway role entries."
            ),
            colour=discord.Colour(0xffffff)
        ))
        return

    settings = get_server_settings(interaction.guild.id)
    role_entry_counts = settings.get("giveaway_role_entries", {})
    if not isinstance(role_entry_counts, dict):
        print(
            f"[GIVEAWAY] Invalid role-entry settings "
            f"for guild {interaction.guild.id}."
        )
        await send_embed(discord.Embed(
            title="Giveaway role entries unavailable",
            description=(
                "The saved giveaway role-entry settings are invalid. "
                "Please contact support."
            ),
            colour=discord.Colour(0xffffff)
        ))
        return

    embed = discord.Embed(
        title="Giveaway Role Entries",
        colour=discord.Colour(0xffffff)
    )
    if not role_entry_counts:
        embed.description = "No roles have additional giveaway entries set."
    else:
        role_lines = []
        for role_id, tickets in role_entry_counts.items():
            try:
                role_id_int = int(role_id)
                tickets_int = int(tickets)
            except (TypeError, ValueError):
                print(
                    f"[GIVEAWAY] Invalid role-entry record "
                    f"for guild {interaction.guild.id}: "
                    f"{role_id!r}={tickets!r}."
                )
                continue

            role = interaction.guild.get_role(role_id_int)
            role_label = (
                role.mention
                if role is not None
                else f"Deleted role (ID: {role_id_int})"
            )
            role_lines.append(
                f"{role_label}: {tickets_int} total entries"
            )

        embed.description = (
            "\n".join(role_lines)
            if role_lines
            else "No valid role-entry settings were found."
        )

    await send_embed(embed)


# ============================================================
# Dashboard Command
# ============================================================

@client.tree.command(
    name="dashboard",
    description="Open the Mini Assistants Dashboard."
)
async def dashboard_command(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="Mini Assistants Dashboard",
        description=(
            "[Open the Bots Official Dashboard]"
            "(https://constelinbox.wixsite.com/miniassistants-dashb)"
        ),
        colour=discord.Colour(0xffffff)
    )

    if interaction.guild:
        add_premium_footer(
            embed,
            interaction.guild.id
        )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


async def send_economy_embed(
    interaction: discord.Interaction,
    embed: discord.Embed
):
    if interaction.guild is not None:
        add_premium_footer(
            embed,
            interaction.guild.id
        )

    command = interaction.command

    await interaction.response.send_message(
        embed=embed,
        ephemeral=(
            command is not None
            and command.name in PRIVATE_ECONOMY_EMBED_COMMANDS
        )
    )

# ============================================================
# Daily Command
# ============================================================


@client.tree.command(
    name="economy-daily",
    description="Claim your daily currency reward."
)
async def daily_command(
    interaction: discord.Interaction
):
    user_id = str(interaction.user.id)
    now = time.time()

    async with economy_lock:
        user_data = economy_userdata.get(user_id, {})
        if not isinstance(user_data, dict):
            embed = discord.Embed(
                title="Daily reward unavailable",
                description=(
                    "Your saved economy data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        balance = user_data.get("coins", STARTING_BALANCE)
        last_daily = user_data.get("last_daily")
        if (
            not isinstance(balance, int)
            or isinstance(balance, bool)
            or balance < 0
            or not has_valid_level_progress(user_data)
            or (
                last_daily is not None
                and (
                    not isinstance(last_daily, (int, float))
                    or isinstance(last_daily, bool)
                    or (
                        isinstance(last_daily, float)
                        and not math.isfinite(last_daily)
                    )
                    or last_daily < 0
                    or last_daily > 253402300799
                )
            )
        ):
            embed = discord.Embed(
                title="Daily reward unavailable",
                description=(
                    "Your saved economy data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        if last_daily is not None:
            remaining = DAILY_COOLDOWN - (now - last_daily)
            if remaining > 0:
                remaining_seconds = math.ceil(remaining)
                hours, remainder = divmod(remaining_seconds, 60 * 60)
                minutes, seconds = divmod(remainder, 60)
                time_parts = []
                if hours:
                    time_parts.append(
                        f"{hours} hour{'s' if hours != 1 else ''}"
                    )
                if minutes:
                    time_parts.append(
                        f"{minutes} minute{'s' if minutes != 1 else ''}"
                    )
                if seconds or not time_parts:
                    time_parts.append(
                        f"{seconds} second{'s' if seconds != 1 else ''}"
                    )

                embed = discord.Embed(
                    title="Daily reward already claimed",
                    description=(
                        f"You can claim again in {', '.join(time_parts)}."
                    ),
                    colour=discord.Colour(0xffffff)
                )
                await send_economy_embed(interaction, embed)
                return

        updated_user_data = {
            **user_data,
            **award_level_experience(user_data),
            "coins": balance + DAILY_REWARD,
            "created_at": get_account_created_at(user_data, now),
            "last_daily": now
        }
        updated_economy_userdata = dict(economy_userdata)
        updated_economy_userdata[user_id] = updated_user_data

        if not save_json(
            ECONOMY_USERDATA_FILE,
            updated_economy_userdata
        ):
            embed = discord.Embed(
                title="Daily reward unavailable",
                description=(
                    "Your reward could not be saved. "
                    "Please try again later."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        economy_userdata[user_id] = updated_user_data

    embed = discord.Embed(
        title="Daily reward claimed",
        description=(
            f"You received {CURRENCY_SYMBOL} {DAILY_REWARD:,}.\n"
            f"Your new balance is "
            f"{CURRENCY_SYMBOL} {updated_user_data['coins']:,}."
        ),
        colour=discord.Colour(0xffffff)
    )
    await send_economy_embed(interaction, embed)
# ============================================================
# Weekly Command
# ============================================================


@client.tree.command(
    name="economy-weekly",
    description="Claim your weekly currency reward."
)
async def weekly_command(
    interaction: discord.Interaction
):
    user_id = str(interaction.user.id)
    now = time.time()

    async with economy_lock:
        user_data = economy_userdata.get(user_id, {})
        if not isinstance(user_data, dict):
            embed = discord.Embed(
                title="Weekly reward unavailable",
                description=(
                    "Your saved economy data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        balance = user_data.get("coins", STARTING_BALANCE)
        last_weekly = user_data.get("last_weekly")
        if (
            not isinstance(balance, int)
            or isinstance(balance, bool)
            or balance < 0
            or not has_valid_level_progress(user_data)
            or (
                last_weekly is not None
                and (
                    not isinstance(last_weekly, (int, float))
                    or isinstance(last_weekly, bool)
                    or (
                        isinstance(last_weekly, float)
                        and not math.isfinite(last_weekly)
                    )
                    or last_weekly < 0
                    or last_weekly > 253402300799
                )
            )
        ):
            embed = discord.Embed(
                title="Weekly reward unavailable",
                description=(
                    "Your saved economy data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        if last_weekly is not None:
            remaining = WEEKLY_COOLDOWN - (now - last_weekly)
            if remaining > 0:
                remaining_seconds = math.ceil(remaining)
                days, remainder = divmod(remaining_seconds, 24 * 60 * 60)
                hours, remainder = divmod(remainder, 60 * 60)
                minutes, seconds = divmod(remainder, 60)
                time_parts = []
                if days:
                    time_parts.append(
                        f"{days} day{'s' if days != 1 else ''}"
                    )
                if hours:
                    time_parts.append(
                        f"{hours} hour{'s' if hours != 1 else ''}"
                    )
                if minutes:
                    time_parts.append(
                        f"{minutes} minute{'s' if minutes != 1 else ''}"
                    )
                if seconds or not time_parts:
                    time_parts.append(
                        f"{seconds} second{'s' if seconds != 1 else ''}"
                    )

                embed = discord.Embed(
                    title="Weekly reward already claimed",
                    description=(
                        f"You can claim again in {', '.join(time_parts)}."
                    ),
                    colour=discord.Colour(0xffffff)
                )
                await send_economy_embed(interaction, embed)
                return

        updated_user_data = {
            **user_data,
            **award_level_experience(user_data),
            "coins": balance + WEEKLY_REWARD,
            "created_at": get_account_created_at(user_data, now),
            "last_weekly": now
        }
        updated_economy_userdata = dict(economy_userdata)
        updated_economy_userdata[user_id] = updated_user_data

        if not save_json(
            ECONOMY_USERDATA_FILE,
            updated_economy_userdata
        ):
            embed = discord.Embed(
                title="Weekly reward unavailable",
                description=(
                    "Your reward could not be saved. "
                    "Please try again later."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        economy_userdata[user_id] = updated_user_data

    embed = discord.Embed(
        title="Weekly reward claimed",
        description=(
            f"You received {CURRENCY_SYMBOL} {WEEKLY_REWARD:,}.\n"
            f"Your new balance is "
            f"{CURRENCY_SYMBOL} {updated_user_data['coins']:,}."
        ),
        colour=discord.Colour(0xffffff)
    )
    await send_economy_embed(interaction, embed)


# ============================================================
# Wallet Command
# ============================================================


@client.tree.command(
    name="economy-wallet",
    description="Check your current currency balance."
)
async def wallet_command(
    interaction: discord.Interaction
):
    user_id = str(interaction.user.id)
    now = time.time()
    async with economy_lock:
        if user_id not in economy_userdata:
            user_data = create_economy_user_data(now)
            updated_economy_userdata = dict(economy_userdata)
            updated_economy_userdata[user_id] = user_data
            if not save_json(
                ECONOMY_USERDATA_FILE,
                updated_economy_userdata
            ):
                embed = discord.Embed(
                    title="Wallet unavailable",
                    description=(
                        "Your starting balance could not be saved. "
                        "Please try again later."
                    ),
                    colour=discord.Colour(0xffffff)
                )
                await send_economy_embed(interaction, embed)
                return

            economy_userdata[user_id] = user_data

        else:
            user_data = economy_userdata[user_id]
            if isinstance(user_data, dict):
                created_at = get_account_created_at(user_data, now)
                if user_data.get("created_at") != created_at:
                    user_data = {
                        **user_data,
                        "created_at": created_at
                    }
                    updated_economy_userdata = dict(economy_userdata)
                    updated_economy_userdata[user_id] = user_data
                    if not save_json(
                        ECONOMY_USERDATA_FILE,
                        updated_economy_userdata
                    ):
                        embed = discord.Embed(
                            title="Wallet unavailable",
                            description=(
                                "Your account date could not be saved. "
                                "Please try again later."
                            ),
                            colour=discord.Colour(0xffffff)
                        )
                        await send_economy_embed(interaction, embed)
                        return

                    economy_userdata[user_id] = user_data

        balance = (
            user_data.get("coins")
            if isinstance(user_data, dict)
            else None
        )

    if (
        not isinstance(balance, int)
        or isinstance(balance, bool)
        or balance < 0
    ):
        embed = discord.Embed(
            title="Wallet unavailable",
            description=(
                "Your saved economy data is invalid. "
                "Please contact a server administrator."
            ),
            colour=discord.Colour(0xffffff)
        )
    else:
        embed = discord.Embed(
            title=f"{interaction.user.display_name}'s Wallet",
            description=f"Your balance is {CURRENCY_SYMBOL} {balance:,}.",
            colour=discord.Colour(0xffffff)
        )

    await send_economy_embed(interaction, embed)


async def transfer_economy_bank(
    interaction: discord.Interaction,
    amount: int,
    deposit: bool
):
    user_id = str(interaction.user.id)
    now = time.time()

    if not await apply_bank_interest_for_user(user_id, now):
        embed = discord.Embed(
            title="Bank transaction unavailable",
            description=(
                "Your bank interest could not be processed. "
                "Please try again later."
            ),
            colour=discord.Colour(0xffffff)
        )
        await send_economy_embed(interaction, embed)
        return

    async with economy_lock:
        user_data = economy_userdata.get(user_id)
        if user_data is None:
            user_data = create_economy_user_data(now)
        elif not isinstance(user_data, dict):
            embed = discord.Embed(
                title="Bank transaction unavailable",
                description=(
                    "Your saved economy data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        wallet_balance = user_data.get("coins")
        bank_balance = user_data.get("coins_banks", 0)
        if (
            not isinstance(wallet_balance, int)
            or isinstance(wallet_balance, bool)
            or wallet_balance < 0
            or not isinstance(bank_balance, int)
            or isinstance(bank_balance, bool)
            or bank_balance < 0
        ):
            embed = discord.Embed(
                title="Bank transaction unavailable",
                description=(
                    "Your saved wallet or bank balance is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        if deposit and amount > wallet_balance:
            embed = discord.Embed(
                title="Deposit unavailable",
                description="You do not have enough coins in your wallet.",
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        if not deposit and amount > bank_balance:
            embed = discord.Embed(
                title="Withdrawal unavailable",
                description="You do not have enough coins in your bank.",
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        updated_wallet_balance = (
            wallet_balance - amount
            if deposit
            else wallet_balance + amount
        )
        updated_bank_balance = (
            bank_balance + amount
            if deposit
            else bank_balance - amount
        )
        updated_user_data = {
            **user_data,
            "coins": updated_wallet_balance,
            "coins_banks": updated_bank_balance,
            "created_at": get_account_created_at(user_data, now),
        }
        updated_economy_userdata = dict(economy_userdata)
        updated_economy_userdata[user_id] = updated_user_data
        if not save_json(
            ECONOMY_USERDATA_FILE,
            updated_economy_userdata
        ):
            embed = discord.Embed(
                title="Bank transaction unavailable",
                description=(
                    "Your transaction could not be saved. "
                    "Please try again later."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        economy_userdata[user_id] = updated_user_data

    action = "deposited" if deposit else "withdrew"
    embed = discord.Embed(
        title="Bank deposit complete" if deposit else "Bank withdrawal complete",
        description=(
            f"You {action} {CURRENCY_SYMBOL} {amount:,}.\n"
            f"Wallet balance: {CURRENCY_SYMBOL} {updated_wallet_balance:,}\n"
            f"Bank balance: {CURRENCY_SYMBOL} {updated_bank_balance:,}"
        ),
        colour=discord.Colour(0xffffff)
    )
    await send_economy_embed(interaction, embed)


@client.tree.command(
    name="economy-bank-deposit",
    description="Deposit coins from your wallet into your bank."
)
@app_commands.describe(coins="The number of coins to deposit.")
async def economy_bank_deposit_command(
    interaction: discord.Interaction,
    coins: app_commands.Range[int, 1, 9007199254740991]
):
    await transfer_economy_bank(interaction, coins, deposit=True)


@client.tree.command(
    name="economy-bank-withdraw",
    description="Withdraw coins from your bank into your wallet."
)
@app_commands.describe(coins="The number of coins to withdraw.")
async def economy_bank_withdraw_command(
    interaction: discord.Interaction,
    coins: app_commands.Range[int, 1, 9007199254740991]
):
    await transfer_economy_bank(interaction, coins, deposit=False)


@client.tree.command(
    name="economy-bank",
    description="Check how many coins you have stored in your bank."
)
async def economy_bank_command(
    interaction: discord.Interaction
):
    user_id = str(interaction.user.id)
    now = time.time()

    if not await apply_bank_interest_for_user(user_id, now):
        embed = discord.Embed(
            title="Bank unavailable",
            description=(
                "Your bank interest could not be processed. "
                "Please try again later."
            ),
            colour=discord.Colour(0xffffff)
        )
        await send_economy_embed(interaction, embed)
        return

    async with economy_lock:
        user_data = economy_userdata.get(user_id)
        if user_data is None:
            user_data = create_economy_user_data(now)
            updated_economy_userdata = dict(economy_userdata)
            updated_economy_userdata[user_id] = user_data
            if not save_json(
                ECONOMY_USERDATA_FILE,
                updated_economy_userdata
            ):
                embed = discord.Embed(
                    title="Bank unavailable",
                    description=(
                        "Your starting balance could not be saved. "
                        "Please try again later."
                    ),
                    colour=discord.Colour(0xffffff)
                )
                await send_economy_embed(interaction, embed)
                return
            economy_userdata[user_id] = user_data

        if not isinstance(user_data, dict):
            embed = discord.Embed(
                title="Bank unavailable",
                description=(
                    "Your saved economy data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        bank_balance = user_data.get("coins_banks", 0)
        if (
            not isinstance(bank_balance, int)
            or isinstance(bank_balance, bool)
            or bank_balance < 0
        ):
            embed = discord.Embed(
                title="Bank unavailable",
                description=(
                    "Your saved bank balance is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

    embed = discord.Embed(
        title=f"{interaction.user.display_name}'s Bank",
        description=(
            f"Bank balance: {CURRENCY_SYMBOL} {bank_balance:,}.\n"
            f"Daily interest rate: {BANK_DAILY_INTEREST_RATE:.0%}.\n"
            f"Potential interest at the next 1 AM: "
            f"{CURRENCY_SYMBOL} "
            f"{int(bank_balance * BANK_DAILY_INTEREST_RATE):,}."
        ),
        colour=discord.Colour(0xffffff)
    )
    await send_economy_embed(interaction, embed)


# ============================================================
# Coin Flip Command
# ============================================================


@client.tree.command(
    name="economy-coin-flip",
    description="Bet coins on a random coin flip."
)
@app_commands.describe(
    amount="The number of coins to bet.",
    side="Choose heads or tails."
)
@app_commands.choices(
    side=[
        app_commands.Choice(name="Heads", value="heads"),
        app_commands.Choice(name="Tails", value="tails")
    ]
)
async def coin_flip_command(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1],
    side: app_commands.Choice[str]
):
    user_id = str(interaction.user.id)
    now = time.time()

    async with economy_lock:
        user_data = economy_userdata.get(user_id, {})
        if not isinstance(user_data, dict):
            embed = discord.Embed(
                title="Coin flip unavailable",
                description=(
                    "Your saved economy data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        balance = user_data.get("coins", STARTING_BALANCE)
        if (
            not isinstance(balance, int)
            or isinstance(balance, bool)
            or balance < 0
        ):
            embed = discord.Embed(
                title="Coin flip unavailable",
                description=(
                    "Your saved economy data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        if amount > balance:
            embed = discord.Embed(
                title="Coin flip unavailable",
                description=(
                    f"You only have {CURRENCY_SYMBOL} {balance:,}, so you cannot "
                    f"bet {CURRENCY_SYMBOL} {amount:,}."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        result = random.choice(("heads", "tails"))
        won = side.value == result
        new_balance = (
            balance + amount
            if won
            else balance - amount
        )
        updated_user_data = {
            **user_data,
            "coins": new_balance,
            "created_at": get_account_created_at(user_data, now)
        }
        if user_id not in economy_userdata:
            updated_user_data.setdefault("last_daily", None)
            updated_user_data.setdefault("level", 0)
            updated_user_data.setdefault("level_exp", 0)

        updated_economy_userdata = dict(economy_userdata)
        updated_economy_userdata[user_id] = updated_user_data

        if not save_json(
            ECONOMY_USERDATA_FILE,
            updated_economy_userdata
        ):
            embed = discord.Embed(
                title="Coin flip unavailable",
                description=(
                    "Your bet could not be saved. Please try again later."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        economy_userdata[user_id] = updated_user_data

    outcome = "won" if won else "lost"
    embed = discord.Embed(
        title=f"The coin landed on {result.title()}",
        description=(
            f"You chose {side.name} and {outcome}.\n"
            f"**Bet:** {CURRENCY_SYMBOL} {amount:,}\n"
            f"{'**Payout**' if won else '**Lost**'}: {CURRENCY_SYMBOL} {amount * 2 if won else amount:,}\n"
            f"Your new balance is {CURRENCY_SYMBOL} {new_balance:,}."
        ),
        colour=discord.Colour(0xffffff)
    )
    await send_economy_embed(interaction, embed)


# ============================================================
# Economy Pay Command
# ============================================================


@client.tree.command(
    name="economy-pay",
    description="Transfer coins to another server member."
)
@app_commands.describe(
    user="The member to receive the coins.",
    amount="The number of coins to transfer."
)
async def economy_pay_command(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: app_commands.Range[int, 1]
):
    if interaction.guild is None:
        embed = discord.Embed(
            title="Transfer unavailable",
            description="This command can only be used in a server.",
            colour=discord.Colour(0xffffff)
        )
        await send_economy_embed(interaction, embed)
        return

    if user.id == interaction.user.id:
        embed = discord.Embed(
            title="Transfer unavailable",
            description="You cannot transfer coins to yourself.",
            colour=discord.Colour(0xffffff)
        )
        await send_economy_embed(interaction, embed)
        return

    sender_id = str(interaction.user.id)
    recipient_id = str(user.id)

    async with economy_lock:
        sender_data = economy_userdata.get(sender_id, {})
        recipient_data = economy_userdata.get(recipient_id, {})
        if (
            not isinstance(sender_data, dict)
            or not isinstance(recipient_data, dict)
        ):
            embed = discord.Embed(
                title="Transfer unavailable",
                description=(
                    "Saved economy data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        sender_balance = sender_data.get("coins", STARTING_BALANCE)
        recipient_balance = recipient_data.get("coins", STARTING_BALANCE)
        if (
            not isinstance(sender_balance, int)
            or isinstance(sender_balance, bool)
            or sender_balance < 0
            or not isinstance(recipient_balance, int)
            or isinstance(recipient_balance, bool)
            or recipient_balance < 0
        ):
            embed = discord.Embed(
                title="Transfer unavailable",
                description=(
                    "Saved economy data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        if amount > sender_balance:
            embed = discord.Embed(
                title="Transfer unavailable",
                description=(
                    f"You have {CURRENCY_SYMBOL}{sender_balance:,} and cannot "
                    f"transfer {CURRENCY_SYMBOL}{amount:,} coins."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        now = time.time()
        updated_economy_userdata = dict(economy_userdata)
        updated_economy_userdata[sender_id] = {
            **sender_data,
            "coins": sender_balance - amount,
            "created_at": get_account_created_at(sender_data, now),
        }
        updated_economy_userdata[recipient_id] = {
            **recipient_data,
            "coins": recipient_balance + amount,
            "created_at": get_account_created_at(recipient_data, now),
        }

        if sender_id not in economy_userdata:
            updated_economy_userdata[sender_id].setdefault(
                "last_daily",
                None
            )
            updated_economy_userdata[sender_id].setdefault("level", 0)
            updated_economy_userdata[sender_id].setdefault("level_exp", 0)
        if recipient_id not in economy_userdata:
            updated_economy_userdata[recipient_id].setdefault(
                "last_daily",
                None
            )
            updated_economy_userdata[recipient_id].setdefault("level", 0)
            updated_economy_userdata[recipient_id].setdefault("level_exp", 0)

        if not save_json(
            ECONOMY_USERDATA_FILE,
            updated_economy_userdata
        ):
            embed = discord.Embed(
                title="Transfer unavailable",
                description=(
                    "Your transfer could not be saved. "
                    "Please try again later."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        economy_userdata.update(updated_economy_userdata)

    embed = discord.Embed(
        title="Coin transfer complete",
        description=(
            f"{interaction.user.display_name} transferred {CURRENCY_SYMBOL}{amount:,}"
            f"to {user.display_name}.\n"
            f"Your new balance is "
            f"{CURRENCY_SYMBOL}{sender_balance - amount:,}."
        ),
        colour=discord.Colour(0xffffff)
    )
    await send_economy_embed(interaction, embed)

# ============================================================
# Economy Job Apply Command
# ============================================================


@client.tree.command(
    name="economy-job-apply",
    description="Apply for an economy job."
)
@app_commands.choices(
    job=[
        app_commands.Choice(
            name=(
                f"{job_title} (${JOB_WAGES[job_title]:,} /Day) "
                + (
                    f"({JOB_SHIFTS[job_title]} "
                    f"shift{'s' if JOB_SHIFTS[job_title] != 1 else ''} "
                    "Req. /Day)"
                )
                + (
                    f" [Lv{JOB_LEVEL_REQUIREMENTS[job_title]}+ Req.]"
                    if job_title in JOB_LEVEL_REQUIREMENTS
                    else ""
                )
            ),
            value=job_title
        )
        for job_title in JOB_TITLES
    ]
)
async def economy_job_apply_command(
    interaction: discord.Interaction,
    job: app_commands.Choice[str]
):
    user_id = str(interaction.user.id)
    now = time.time()

    async with economy_lock:
        user_data = economy_userdata.get(user_id, {})
        if not isinstance(user_data, dict):
            embed = discord.Embed(
                title="Job application unavailable",
                description=(
                    "Your saved economy data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        if user_data.get("job"):
            embed = discord.Embed(
                title="Already employed",
                description=(
                    f"You already work as a **{user_data['job']}**. "
                    "Use `/economy-job-quit` before applying for another job."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        level = user_data.get("level", 0)
        if (
            not isinstance(level, int)
            or isinstance(level, bool)
            or level < 0
        ):
            embed = discord.Embed(
                title="Job application unavailable",
                description=(
                    "Your saved level data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        required_level = JOB_LEVEL_REQUIREMENTS.get(job.value, 0)
        if level < required_level:
            embed = discord.Embed(
                title="Level requirement not met",
                description=(
                    f"You must be level {required_level} or higher to apply "
                    f"for **{job.value}**. Your current level is {level}."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        work_day = get_job_work_day(now)
        if user_data.get("job_work_date") == work_day:
            shifts_worked = user_data.get("job_shifts_worked", 0)
            if (
                not isinstance(shifts_worked, int)
                or isinstance(shifts_worked, bool)
                or shifts_worked < 0
            ):
                embed = discord.Embed(
                    title="Job application unavailable",
                    description=(
                        "Your saved shift progress is invalid. "
                        "Please contact a server administrator."
                    ),
                    colour=discord.Colour(0xffffff)
                )
                await send_economy_embed(interaction, embed)
                return

            if (
                shifts_worked > 0
                and user_data.get("job_work_job") != job.value
            ):
                embed = discord.Embed(
                    title="Job application unavailable",
                    description=(
                        "You have already worked another job during today's "
                        "shift period. You can apply for a different job after "
                        "the 1am reset."
                    ),
                    colour=discord.Colour(0xffffff)
                )
                await send_economy_embed(interaction, embed)
                return

        updated_user_data = {
            **user_data,
            "level": user_data.get("level", 0),
            "level_exp": user_data.get("level_exp", 0),
            "coins": user_data.get("coins", STARTING_BALANCE),
            "created_at": get_account_created_at(user_data, now),
            "job": job.value,
            "job_last_reviewed_work_day": (
                date.fromisoformat(work_day) - timedelta(days=1)
            ).isoformat(),
            "job_missed_shift_warnings": 0,
        }
        updated_economy_userdata = dict(economy_userdata)
        updated_economy_userdata[user_id] = updated_user_data

        if not save_json(
            ECONOMY_USERDATA_FILE,
            updated_economy_userdata
        ):
            embed = discord.Embed(
                title="Job application unavailable",
                description=(
                    "Your application could not be saved. "
                    "Please try again later."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        economy_userdata[user_id] = updated_user_data

    embed = discord.Embed(
        title="Job application successful",
        description=f"You are now employed as a **{job.value}**.",
        colour=discord.Colour(0xffffff)
    )
    await send_economy_embed(interaction, embed)
# ============================================================
# Economy Job Work Command
# ============================================================
# ============================================================
# Economy Work Shift Command
# ============================================================


async def economy_job_work_impl(
    interaction: discord.Interaction
):
    user_id = str(interaction.user.id)
    now = time.time()
    work_day = get_job_work_day(now)

    async with economy_lock:
        user_data = economy_userdata.get(user_id, {})
        if not isinstance(user_data, dict):
            embed = discord.Embed(
                title="Unable to work",
                description=(
                    "Your saved economy data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        job_title = user_data.get("job")
        if job_title not in JOB_TITLES:
            embed = discord.Embed(
                title="No current job",
                description=(
                    "Apply for an economy job before working a shift."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        balance = user_data.get("coins", STARTING_BALANCE)
        if (
            not isinstance(balance, int)
            or isinstance(balance, bool)
            or balance < 0
            or not has_valid_level_progress(user_data)
        ):
            embed = discord.Embed(
                title="Unable to work",
                description=(
                    "Your saved coin balance is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        if user_data.get("job_work_date") == work_day:
            shifts_worked = user_data.get("job_shifts_worked", 0)
            work_job = user_data.get("job_work_job")
            if (
                not isinstance(shifts_worked, int)
                or isinstance(shifts_worked, bool)
                or shifts_worked < 0
                or shifts_worked > JOB_SHIFTS[job_title]
                or (
                    shifts_worked > 0
                    and work_job != job_title
                )
            ):
                embed = discord.Embed(
                    title="Unable to work",
                    description=(
                        "Your saved shift progress is invalid. "
                        "Please contact a server administrator."
                    ),
                    colour=discord.Colour(0xffffff)
                )
                await send_economy_embed(interaction, embed)
                return
        else:
            shifts_worked = 0

        required_shifts = JOB_SHIFTS[job_title]
        through_date = date.fromisoformat(work_day) - timedelta(days=1)
        reviewed_work_day = user_data.get("job_last_reviewed_work_day")
        if reviewed_work_day is None:
            reviewed_date = through_date
        else:
            try:
                reviewed_date = date.fromisoformat(reviewed_work_day)
            except (TypeError, ValueError):
                embed = discord.Embed(
                    title="Unable to work",
                    description=(
                        "Your saved job attendance data is invalid. "
                        "Please contact a server administrator."
                    ),
                    colour=discord.Colour(0xffffff)
                )
                await send_economy_embed(interaction, embed)
                return

        missed_warnings = user_data.get("job_missed_shift_warnings", 0)
        if (
            not isinstance(missed_warnings, int)
            or isinstance(missed_warnings, bool)
            or missed_warnings < 0
        ):
            embed = discord.Embed(
                title="Unable to work",
                description=(
                    "Your saved job attendance data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        if reviewed_date > through_date:
            embed = discord.Embed(
                title="Unable to work",
                description=(
                    "Your saved job attendance data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        missed_days = get_missed_job_workday_count(
            user_data,
            job_title,
            reviewed_date,
            through_date
        )
        updated_missed_warnings = missed_warnings + missed_days
        warning_notice = ""

        if updated_missed_warnings >= 3:
            fired_user_data = {
                **user_data,
                "job_last_reviewed_work_day": through_date.isoformat(),
                "job_missed_shift_warnings": updated_missed_warnings,
            }
            del fired_user_data["job"]
            updated_economy_userdata = dict(economy_userdata)
            updated_economy_userdata[user_id] = fired_user_data

            if not save_json(
                ECONOMY_USERDATA_FILE,
                updated_economy_userdata
            ):
                embed = discord.Embed(
                    title="Unable to update job status",
                    description=(
                        "Your job attendance could not be saved. "
                        "Please try again later."
                    ),
                    colour=discord.Colour(0xffffff)
                )
                await send_economy_embed(interaction, embed)
                return

            economy_userdata[user_id] = fired_user_data
            embed = discord.Embed(
                title="Fired from your job",
                description=(
                    f"You have been fired from your **{job_title}** job "
                    "for missing the required shifts on three days. "
                    "You can apply for another job with "
                    "`/economy-job-apply`."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        if missed_days:
            warning_numbers = ", ".join(
                str(number)
                for number in range(
                    missed_warnings + 1,
                    updated_missed_warnings + 1
                )
            )
            remaining_warnings = 3 - updated_missed_warnings
            warning_notice = (
                f"Warning{'s' if missed_days != 1 else ''} "
                f"{warning_numbers} of 3: You missed the required shifts "
                f"on {missed_days} workday"
                f"{'s' if missed_days != 1 else ''}. Missing shifts on "
                f"{remaining_warnings} more day"
                f"{'s' if remaining_warnings != 1 else ''} will result "
                "in being fired."
            )

        if (
            missed_days
            or reviewed_date != through_date
            or "job_missed_shift_warnings" not in user_data
        ):
            reviewed_user_data = {
                **user_data,
                "job_last_reviewed_work_day": through_date.isoformat(),
                "job_missed_shift_warnings": updated_missed_warnings,
            }
            updated_economy_userdata = dict(economy_userdata)
            updated_economy_userdata[user_id] = reviewed_user_data

            if not save_json(
                ECONOMY_USERDATA_FILE,
                updated_economy_userdata
            ):
                embed = discord.Embed(
                    title="Unable to update job attendance",
                    description=(
                        "Your job attendance could not be saved. "
                        "Please try again later."
                    ),
                    colour=discord.Colour(0xffffff)
                )
                await send_economy_embed(interaction, embed)
                return

            economy_userdata[user_id] = reviewed_user_data
            user_data = reviewed_user_data

        if shifts_worked >= required_shifts:
            description = (
                f"You have completed all {required_shifts} shifts for "
                f"your **{job_title}** job. You can work again after 1am."
            )
            if warning_notice:
                description = f"{warning_notice}\n\n{description}"
            embed = discord.Embed(
                title="Daily shifts complete",
                description=description,
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        if required_shifts > 1 and shifts_worked > 0:
            last_shift_at = user_data.get("job_last_shift_at")
            if last_shift_at is not None and (
                not isinstance(last_shift_at, (int, float))
                or isinstance(last_shift_at, bool)
                or last_shift_at < 0
                or last_shift_at > 253402300799
                or not math.isfinite(last_shift_at)
            ):
                embed = discord.Embed(
                    title="Unable to work",
                    description=(
                        "Your saved shift timing is invalid. "
                        "Please contact a server administrator."
                    ),
                    colour=discord.Colour(0xffffff)
                )
                await send_economy_embed(interaction, embed)
                return

            if last_shift_at is not None:
                remaining = 60 * 60 - (now - last_shift_at)
                if remaining > 0:
                    remaining_seconds = math.ceil(remaining)
                    hours, remainder = divmod(remaining_seconds, 60 * 60)
                    minutes, seconds = divmod(remainder, 60)
                    wait_parts = []
                    if hours:
                        wait_parts.append(
                            f"{hours} hour{'s' if hours != 1 else ''}"
                        )
                    if minutes:
                        wait_parts.append(
                            f"{minutes} minute{'s' if minutes != 1 else ''}"
                        )
                    if seconds:
                        wait_parts.append(
                            f"{seconds} second{'s' if seconds != 1 else ''}"
                        )
                    description = (
                        "You can work your next shift in "
                        f"{' '.join(wait_parts)}."
                    )
                    if warning_notice:
                        description = f"{warning_notice}\n\n{description}"
                    embed = discord.Embed(
                        title="Next shift not ready",
                        description=description,
                        colour=discord.Colour(0xffffff)
                    )
                    await send_economy_embed(interaction, embed)
                    return

        shifts_worked += 1
        updated_balance = balance
        completed_day = shifts_worked == required_shifts
        if completed_day:
            updated_balance += JOB_WAGES[job_title]

        updated_user_data = {
            **user_data,
            **award_level_experience(user_data),
            "coins": updated_balance,
            "created_at": get_account_created_at(user_data, now),
            "job_work_date": work_day,
            "job_shifts_worked": shifts_worked,
            "job_work_job": job_title,
            "job_last_shift_at": now,
        }
        updated_economy_userdata = dict(economy_userdata)
        updated_economy_userdata[user_id] = updated_user_data

        if not save_json(
            ECONOMY_USERDATA_FILE,
            updated_economy_userdata
        ):
            embed = discord.Embed(
                title="Unable to work",
                description=(
                    "Your shift could not be saved. Please try again later."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        economy_userdata[user_id] = updated_user_data

    if completed_day:
        description = (
            f"You completed shift {shifts_worked} of {required_shifts} for "
            f"your **{job_title}** job and earned {JOB_WAGES[job_title]} "
            f"coins for the day. Your new balance is {updated_balance} coins. "
            "You can work again after 1am."
        )
    else:
        description = (
            f"You completed shift {shifts_worked} of {required_shifts} for "
            f"your **{job_title}** job. Your daily wage of "
            f"{JOB_WAGES[job_title]} coins is paid after your final shift."
        )
    if warning_notice:
        description = f"{warning_notice}\n\n{description}"

    embed = discord.Embed(
        title="Shift completed",
        description=description,
        colour=discord.Colour(0xffffff)
    )
    await send_economy_embed(interaction, embed)


@client.tree.command(
    name="economy-work-shift",
    description="Work a shift at your current economy job."
)
async def economy_work_shift_command(
    interaction: discord.Interaction
):
    await economy_job_work_impl(interaction)


# ============================================================
# Economy Job Quit Command
# ============================================================


@client.tree.command(
    name="economy-job-quit",
    description="Quit your current economy job."
)
async def economy_job_quit_command(
    interaction: discord.Interaction
):
    user_id = str(interaction.user.id)

    async with economy_lock:
        user_data = economy_userdata.get(user_id, {})
        if not isinstance(user_data, dict):
            embed = discord.Embed(
                title="Unable to quit job",
                description=(
                    "Your saved economy data is invalid. "
                    "Please contact a server administrator."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        if not user_data.get("job"):
            embed = discord.Embed(
                title="No current job",
                description="You do not currently have an economy job.",
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        updated_user_data = dict(user_data)
        del updated_user_data["job"]
        updated_economy_userdata = dict(economy_userdata)
        updated_economy_userdata[user_id] = updated_user_data

        if not save_json(
            ECONOMY_USERDATA_FILE,
            updated_economy_userdata
        ):
            embed = discord.Embed(
                title="Unable to quit job",
                description=(
                    "Your job status could not be saved. "
                    "Please try again later."
                ),
                colour=discord.Colour(0xffffff)
            )
            await send_economy_embed(interaction, embed)
            return

        economy_userdata[user_id] = updated_user_data

    embed = discord.Embed(
        title="Job quit",
        description="You have quit your economy job.",
        colour=discord.Colour(0xffffff)
    )
    await send_economy_embed(interaction, embed)

# ============================================================
# Guild Level Commands
# ============================================================


async def update_admin_guild_level(
    interaction: discord.Interaction,
    user: discord.Member,
    action: str,
    amount: int
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return

    if (
        not isinstance(interaction.user, discord.Member)
        or not interaction.user.guild_permissions.administrator
    ):
        await interaction.response.send_message(
            "You need the Administrator permission to use this command.",
            ephemeral=True
        )
        return

    guild_id = str(interaction.guild.id)
    user_id = str(user.id)

    async with guild_leveling_lock:
        guild_data = guild_leveling_userdata.get(guild_id, {})
        if not isinstance(guild_data, dict):
            await interaction.response.send_message(
                "Saved server leveling data is invalid. Please contact a server administrator.",
                ephemeral=True
            )
            return

        members = guild_data.get("members", {})
        excluded_channels = guild_data.get("excluded_channels", [])
        if (
            not isinstance(members, dict)
            or not isinstance(excluded_channels, list)
            or any(not isinstance(channel_id, str) for channel_id in excluded_channels)
        ):
            await interaction.response.send_message(
                "Saved server leveling data is invalid. Please contact a server administrator.",
                ephemeral=True
            )
            return

        user_data = members.get(user_id, {})
        if not isinstance(user_data, dict):
            await interaction.response.send_message(
                "Saved user level data is invalid. Please contact a server administrator.",
                ephemeral=True
            )
            return

        level = user_data.get("level", 0)
        experience = user_data.get("xp", 0)
        last_xp_at = user_data.get("last_xp_at")
        if (
            not isinstance(level, int)
            or isinstance(level, bool)
            or level < 0
            or not isinstance(experience, int)
            or isinstance(experience, bool)
            or not 0 <= experience < get_guild_level_xp_required(level)
            or (
                last_xp_at is not None
                and (
                    not isinstance(last_xp_at, (int, float))
                    or isinstance(last_xp_at, bool)
                    or not math.isfinite(last_xp_at)
                    or not 0 <= last_xp_at <= 253402300799
                )
            )
        ):
            await interaction.response.send_message(
                "Saved user level data is invalid. Please contact a server administrator.",
                ephemeral=True
            )
            return

        if action == "set":
            updated_level = amount
            updated_experience = 0
        else:
            total_experience = (
                get_guild_level_total_experience(level) + experience
            )
            if action == "add":
                total_experience += amount
            else:
                total_experience = max(0, total_experience - amount)
            updated_level, updated_experience = (
                get_guild_level_progress_from_total_experience(
                    total_experience
                )
            )

        updated_user_data = {
            **user_data,
            "level": updated_level,
            "xp": updated_experience,
            "last_xp_at": last_xp_at,
        }
        updated_guild_data = {
            **guild_data,
            "excluded_channels": excluded_channels,
            "members": {
                **members,
                user_id: updated_user_data,
            },
        }
        updated_guild_leveling_userdata = {
            **guild_leveling_userdata,
            guild_id: updated_guild_data,
        }
        if not save_json(
            GUILD_LEVELING_USERDATA_FILE,
            updated_guild_leveling_userdata
        ):
            await interaction.response.send_message(
                "The user's level data could not be saved. Please try again later.",
                ephemeral=True
            )
            return

        guild_leveling_userdata[guild_id] = updated_guild_data

    embed = discord.Embed(
        description=(
            f"{user.mention} is now level {updated_level} with "
            f"{updated_experience} XP."
        ),
        colour=discord.Colour(0xffffff)
    )
    embed.set_author(
        name=f"{user.display_name}'s Level",
        icon_url=user.display_avatar.url
    )
    add_premium_footer(embed, interaction.guild.id)
    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


@client.tree.command(
    name="level",
    description="Show your level and experience in this server."
)
@app_commands.guild_only()
async def guild_level_command(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return

    guild_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)

    async with guild_leveling_lock:
        guild_data, members, user_data = get_guild_leveling_progress(
            guild_id,
            user_id
        )
        level = user_data.get("level", 0)
        experience = user_data.get("xp", 0)
        last_xp_at = user_data.get("last_xp_at")
        if (
            not isinstance(level, int)
            or isinstance(level, bool)
            or level < 0
            or not isinstance(experience, int)
            or isinstance(experience, bool)
            or not 0 <= experience < get_guild_level_xp_required(level)
            or (
                last_xp_at is not None
                and (
                    not isinstance(last_xp_at, (int, float))
                    or isinstance(last_xp_at, bool)
                    or not math.isfinite(last_xp_at)
                    or not 0 <= last_xp_at <= 253402300799
                )
            )
        ):
            await interaction.response.send_message(
                "Your saved level data is invalid. Please contact a server administrator.",
                ephemeral=True
            )
            return

        if user_id not in members:
            updated_guild_data = {
                **guild_data,
                "excluded_channels": guild_data.get("excluded_channels", []),
                "members": {
                    **members,
                    user_id: {
                        "level": 0,
                        "xp": 0,
                        "last_xp_at": None,
                    },
                },
            }
            updated_guild_leveling_userdata = {
                **guild_leveling_userdata,
                guild_id: updated_guild_data,
            }
            if not save_json(
                GUILD_LEVELING_USERDATA_FILE,
                updated_guild_leveling_userdata
            ):
                await interaction.response.send_message(
                    "Your level data could not be saved. Please try again later.",
                    ephemeral=True
                )
                return
            guild_leveling_userdata[guild_id] = updated_guild_data

    required_experience = get_guild_level_xp_required(level)
    remaining_experience = required_experience - experience
    embed = discord.Embed(
        description=(
            f"**Level:** {level}\n"
            f"`Experience: {experience}/{required_experience}`\n"
            f"{remaining_experience} experience needed to reach level "
            f"{level + 1}."
        ),
        colour=discord.Colour(0xffffff)
    )
    embed.set_author(
        name=f"{interaction.user.display_name}'s Level",
        icon_url=interaction.user.display_avatar.url
    )
    add_premium_footer(embed, interaction.guild.id)
    await interaction.response.send_message(embed=embed)
# ============================================================
# Guild Level change Command
# ============================================================


@client.tree.command(
    name="level-set",
    description="Set a member's level and reset their current-level XP."
)
@app_commands.describe(
    user="The server member whose level to update.",
    level="The level to set."
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def level_set_command(
    interaction: discord.Interaction,
    user: discord.Member,
    level: app_commands.Range[int, 0, 9007199254740991]
):
    await update_admin_guild_level(interaction, user, "set", level)


@client.tree.command(
    name="level-xp-add",
    description="Add level XP to a server member."
)
@app_commands.describe(
    user="The server member whose XP to increase.",
    amount="The amount of XP to add."
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def level_xp_add_command(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: app_commands.Range[int, 1, 9007199254740991]
):
    await update_admin_guild_level(interaction, user, "add", amount)


@client.tree.command(
    name="level-xp-remove",
    description="Remove level XP from a server member."
)
@app_commands.describe(
    user="The server member whose XP to decrease.",
    amount="The amount of XP to remove."
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def level_xp_remove_command(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: app_commands.Range[int, 1, 9007199254740991]
):
    await update_admin_guild_level(interaction, user, "remove", amount)


# ============================================================
# Guild Level Channel Exclude Commands
# ============================================================


@client.tree.command(
    name="level-channel-exclude",
    description="Exclude a channel from level experience."
)
@app_commands.describe(
    channel="The channel where messages will not earn level experience."
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def level_channel_exclude_command(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return

    if (
        not isinstance(interaction.user, discord.Member)
        or not interaction.user.guild_permissions.administrator
    ):
        await interaction.response.send_message(
            "You need the Administrator permission to use this command.",
            ephemeral=True
        )
        return

    guild_id = str(interaction.guild.id)
    channel_id = str(channel.id)
    async with guild_leveling_lock:
        guild_data = guild_leveling_userdata.get(guild_id, {})
        if not isinstance(guild_data, dict):
            await interaction.response.send_message(
                "Saved server leveling data is invalid. Please contact a server administrator.",
                ephemeral=True
            )
            return

        excluded_channels = guild_data.get("excluded_channels", [])
        if (
            not isinstance(excluded_channels, list)
            or any(not isinstance(item, str) for item in excluded_channels)
        ):
            await interaction.response.send_message(
                "Saved excluded-channel data is invalid. Please contact a server administrator.",
                ephemeral=True
            )
            return

        if channel_id in excluded_channels:
            await interaction.response.send_message(
                f"{channel.mention} is already excluded from level experience.",
                ephemeral=True
            )
            return

        updated_guild_data = {
            **guild_data,
            "excluded_channels": [*excluded_channels, channel_id],
            "members": guild_data.get("members", {}),
        }
        updated_guild_leveling_userdata = {
            **guild_leveling_userdata,
            guild_id: updated_guild_data,
        }
        if not save_json(
            GUILD_LEVELING_USERDATA_FILE,
            updated_guild_leveling_userdata
        ):
            await interaction.response.send_message(
                "The channel exclusion could not be saved. Please try again later.",
                ephemeral=True
            )
            return

        guild_leveling_userdata[guild_id] = updated_guild_data

    await interaction.response.send_message(
        f"{channel.mention} is now excluded from level experience.",
        ephemeral=True
    )

# ============================================================
# Guild Level Channel Exclude Remove Commands
# ============================================================


@client.tree.command(
    name="level-channel-exclude-remove",
    description="Allow messages in a channel to earn level experience."
)
@app_commands.describe(
    channel="The channel to allow to earn level experience."
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def level_channel_exclude_remove_command(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return

    if (
        not isinstance(interaction.user, discord.Member)
        or not interaction.user.guild_permissions.administrator
    ):
        await interaction.response.send_message(
            "You need the Administrator permission to use this command.",
            ephemeral=True
        )
        return

    guild_id = str(interaction.guild.id)
    channel_id = str(channel.id)
    async with guild_leveling_lock:
        guild_data = guild_leveling_userdata.get(guild_id, {})
        if not isinstance(guild_data, dict):
            await interaction.response.send_message(
                "Saved server leveling data is invalid. Please contact a server administrator.",
                ephemeral=True
            )
            return

        excluded_channels = guild_data.get("excluded_channels", [])
        if (
            not isinstance(excluded_channels, list)
            or any(not isinstance(item, str) for item in excluded_channels)
        ):
            await interaction.response.send_message(
                "Saved excluded-channel data is invalid. Please contact a server administrator.",
                ephemeral=True
            )
            return

        if channel_id not in excluded_channels:
            await interaction.response.send_message(
                f"{channel.mention} is not excluded from level experience.",
                ephemeral=True
            )
            return

        updated_guild_data = {
            **guild_data,
            "excluded_channels": [
                item for item in excluded_channels if item != channel_id
            ],
            "members": guild_data.get("members", {}),
        }
        updated_guild_leveling_userdata = {
            **guild_leveling_userdata,
            guild_id: updated_guild_data,
        }
        if not save_json(
            GUILD_LEVELING_USERDATA_FILE,
            updated_guild_leveling_userdata
        ):
            await interaction.response.send_message(
                "The channel exclusion could not be removed. Please try again later.",
                ephemeral=True
            )
            return

        guild_leveling_userdata[guild_id] = updated_guild_data

    await interaction.response.send_message(
        f"{channel.mention} can now earn level experience from messages.",
        ephemeral=True
    )


# ============================================================
# Guild Level Channel Exclude List Commands
# ============================================================


@client.tree.command(
    name="level-channel-exclude-list",
    description="Show channels excluded from level experience."
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def level_channel_exclude_list_command(
    interaction: discord.Interaction
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return

    if (
        not isinstance(interaction.user, discord.Member)
        or not interaction.user.guild_permissions.administrator
    ):
        await interaction.response.send_message(
            "You need the Administrator permission to use this command.",
            ephemeral=True
        )
        return

    guild_id = str(interaction.guild.id)
    async with guild_leveling_lock:
        guild_data = guild_leveling_userdata.get(guild_id, {})
        if not isinstance(guild_data, dict):
            await interaction.response.send_message(
                "Saved server leveling data is invalid. Please contact a server administrator.",
                ephemeral=True
            )
            return

        excluded_channels = guild_data.get("excluded_channels", [])
        if (
            not isinstance(excluded_channels, list)
            or any(not isinstance(channel_id, str) for channel_id in excluded_channels)
        ):
            await interaction.response.send_message(
                "Saved excluded-channel data is invalid. Please contact a server administrator.",
                ephemeral=True
            )
            return

        channel_lines = [
            f"<#{channel_id}>"
            for channel_id in excluded_channels
        ]

    if not channel_lines:
        channel_lines = ["No channels are excluded from level experience."]

    page_descriptions = []
    current_lines = []
    current_length = 0
    for line in channel_lines:
        line_length = len(line) + (1 if current_lines else 0)
        if current_lines and current_length + line_length > 3900:
            page_descriptions.append("\n".join(current_lines))
            current_lines = []
            current_length = 0
            line_length = len(line)
        current_lines.append(line)
        current_length += line_length
    page_descriptions.append("\n".join(current_lines))

    embeds = []
    for page_number, description in enumerate(page_descriptions, start=1):
        title = "Channels Excluded from Level XP"
        if len(page_descriptions) > 1:
            title += f" ({page_number}/{len(page_descriptions)})"
        embed = discord.Embed(
            title=title,
            description=description,
            colour=discord.Colour(0xffffff)
        )
        add_premium_footer(embed, interaction.guild.id)
        embeds.append(embed)

    await interaction.response.send_message(
        embed=embeds[0],
        ephemeral=True
    )
    for embed in embeds[1:]:
        await interaction.followup.send(
            embed=embed,
            ephemeral=True
        )


# ============================================================
# Economy Profile Command
# ============================================================


@client.tree.command(
    name="economy-profile",
    description="Show an economy profile."
)
@app_commands.describe(
    user="The user whose EconomyMini profile you want to see."
)
async def economy_profile_command(
    interaction: discord.Interaction,
    user: discord.User
):
    user_data = economy_userdata.get(str(user.id))
    if not isinstance(user_data, dict) or not user_data:
        await interaction.response.send_message(
            "User does not have an EconomyMini Profile Yet"
        )
        return

    embed = discord.Embed(
        description=f"**Wallet Balance:** {CURRENCY_SYMBOL} {user_data.get('coins', 0):,}\n"
        f"**Bank Balance:** {CURRENCY_SYMBOL} {user_data.get('coins_banks', 0):,}\n"
        f"**Economy Member Since:** `{time.strftime('%B %Y', time.localtime(user_data.get('created_at', time.time())))}`\n\n"
        f"**Current Job Role:** `{user_data.get('job', 'Unemployed')}`\n"
        f"**EconomyMini Level:** `{user_data.get('level', 0)}` - "
        f"`{user_data.get('level_exp', 0)}/{LEVEL_UP_EXPERIENCE} (To Lvl {user_data.get('level', 0) + 1})` ",
        colour=discord.Colour(0xffffff)
    )
    embed.set_author(
        name=f"{user.display_name}'s EconomyMini Profile",
        icon_url=user.display_avatar.url
    )
    embed.add_field(
        name="",
        value=f"> **Items - Coming Soon!**",
        inline=False
    )

    await send_economy_embed(interaction, embed)


@client.tree.command(
    name="economy-leaderboard",
    description="Show the global EconomyMini coin leaderboard."
)
@app_commands.guild_only()
async def economy_leaderboard_command(
    interaction: discord.Interaction
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return

    ranked_users = []
    for user_id, user_data in economy_userdata.items():
        if not user_id.isdecimal() or not isinstance(user_data, dict):
            print(
                f"[ECONOMY] Skipping invalid leaderboard account "
                f"{user_id!r}."
            )
            continue

        wallet_coins = user_data.get("coins", STARTING_BALANCE)
        bank_coins = user_data.get("coins_banks", 0)
        if (
            not isinstance(wallet_coins, int)
            or isinstance(wallet_coins, bool)
            or wallet_coins < 0
            or not isinstance(bank_coins, int)
            or isinstance(bank_coins, bool)
            or bank_coins < 0
        ):
            print(
                f"[ECONOMY] Skipping invalid wallet or bank balance for user "
                f"{user_id}."
            )
            continue

        ranked_users.append((int(user_id), wallet_coins + bank_coins))

    ranked_users.sort(key=lambda entry: (-entry[1], entry[0]))
    ranked_users = ranked_users[:10]

    if not ranked_users:
        embed = discord.Embed(
            title="Economy Leaderboard",
            description="No EconomyMini players are available to rank.",
            colour=discord.Colour(0xffffff)
        )
        add_premium_footer(embed, interaction.guild.id)
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )
        return

    async def resolve_user(user_id: int):
        user = client.get_user(user_id)
        if user is None:
            try:
                user = await client.fetch_user(user_id)
            except discord.NotFound:
                print(
                    f"[ECONOMY] Could not resolve leaderboard user "
                    f"{user_id}: Discord user not found."
                )
            except discord.Forbidden:
                print(
                    f"[ECONOMY] Could not resolve leaderboard user "
                    f"{user_id}: request forbidden."
                )
            except discord.HTTPException as error:
                print(
                    f"[ECONOMY] Could not resolve leaderboard user "
                    f"{user_id}: {error}"
                )
        return user

    users = await asyncio.gather(
        *(resolve_user(user_id) for user_id, _ in ranked_users)
    )
    leaderboard_lines = []
    for rank, ((user_id, coins), user) in enumerate(
        zip(ranked_users, users),
        start=1
    ):
        display_name = (
            discord.utils.escape_markdown(user.display_name)
            if user is not None
            else f"User ID {user_id}"
        )
        leaderboard_lines.append(
            f"#{rank} {display_name} | {CURRENCY_SYMBOL} {coins:,}"
        )

    embed = discord.Embed(
        title="Economy Leaderboard",
        description="\n".join(leaderboard_lines),
        colour=discord.Colour(0xffffff)
    )
    add_premium_footer(embed, interaction.guild.id)

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# Help Command
# ============================================================


@client.tree.command(
    name="help",
    description="Show AssistantMini's commands."
)
async def help_command(
    interaction: discord.Interaction
):

    if interaction.guild:

        settings = get_server_settings(
            interaction.guild.id
        )

        colour = settings.get(
            "colour",
            0xffffff
        )

    else:
        colour = 0xffffff

    embed = discord.Embed(
        title="AssistantMini Help",
        description="Available commands:",
        colour=discord.Colour(
            colour
        )
    )

    embed.add_field(
        name="/dashboard",
        value="Open the Mini Assistants Dashboard.",
        inline=False
    )

    embed.add_field(
        name="/economy-wallet",
        value=f"Check your current {CURRENCY_SYMBOL} coin balance.",
        inline=False
    )

    embed.add_field(
        name="/economy-bank",
        value="Check the coins stored in your bank.",
        inline=False
    )

    embed.add_field(
        name="/economy-bank-deposit",
        value="Move coins from your wallet into your bank.",
        inline=False
    )

    embed.add_field(
        name="/economy-bank-withdraw",
        value="Move coins from your bank into your wallet.",
        inline=False
    )

    embed.add_field(
        name="/economy-daily",
        value=(
            f"Claim {CURRENCY_SYMBOL}{DAILY_REWARD:,} once every 24 hours."
        ),
        inline=False
    )

    embed.add_field(
        name="/economy-weekly",
        value=(
            f"Claim {CURRENCY_SYMBOL}{WEEKLY_REWARD:,} once every 7 days."
        ),
        inline=False
    )

    embed.add_field(
        name="/economy-coin-flip",
        value=f"Bet a positive amount of {CURRENCY_SYMBOL} coins on heads or tails. A correct guess pays back twice your bet.",
        inline=False
    )

    embed.add_field(
        name="/economy-pay",
        value=f"Transfer a chosen amount of {CURRENCY_SYMBOL} coins to another server member.",
        inline=False
    )

    embed.add_field(
        name="/economy-leaderboard",
        value="Show the top 10 EconomyMini players globally by wallet and bank coins.",
        inline=False
    )

    if interaction.guild:
        add_premium_footer(
            embed,
            interaction.guild.id
        )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

# ============================================================
# Admin Help Command
# ===========================================================


@client.tree.command(
    name="help-admin",
    description="Show AssistantMini's ADMIN commands."
)
async def admin_help_command(
    interaction: discord.Interaction
):

    if interaction.guild:

        settings = get_server_settings(
            interaction.guild.id
        )

        colour = settings.get(
            "colour",
            0xffffff
        )

    else:
        colour = 0xffffff

    embed = discord.Embed(
        title="AssistantMini Admin Help",
        description="Available commands:",
        colour=discord.Colour(
            colour
        )
    )

    embed.add_field(
        name="/giveaway-create",
        value=(
            "Create a giveaway.\n"
            "Administrators only."
        ),
        inline=False
    )

    embed.add_field(
        name="/giveaway-reroll",
        value="Pick a replacement winner for a completed giveaway using its Giveaway ID. Administrators only; available for 48 hours.",
        inline=False
    )

    embed.add_field(
        name="/giveaway-role-entry",
        value=(
            "Set the total number of giveaway tickets for a role. "
            "Members with several configured roles receive the highest "
            "matching ticket count. Administrators only; 2 roles per "
            "server, or 10 with Premium."
        ),
        inline=False
    )

    embed.add_field(
        name="/giveaway-role-entry-remove",
        value="Remove a role's giveaway ticket bonus. Administrators only.",
        inline=False
    )

    embed.add_field(
        name="/giveaway-entry-role-list",
        value=(
            "List roles with additional giveaway entries. "
            "Administrators only.\n"
        ),
        inline=False
    )
    embed.add_field(
        name="/level-channel-exclude",
        value="Add channels excluded from level experience. Administrators only.",
        inline=False
    )

    embed.add_field(
        name="/level-channel-exclude-list",
        value="Show channels excluded from level experience. Administrators only.",
        inline=False
    )

    embed.add_field(
        name="/level-channel-exclude-remove",
        value="Allow a channel to earn level experience again. Administrators only.\n",
        inline=False
    )

    embed.add_field(
        name="/level-set",
        value="Set a member's level and reset their current-level XP. Administrators only.",
        inline=False
    )

    embed.add_field(
        name="/level-xp-add",
        value="Add XP to a member, including automatic level-ups. Administrators only.",
        inline=False
    )

    embed.add_field(
        name="/level-xp-remove",
        value="Remove XP, rolling back levels as needed without going below level 0. Administrators only.",
        inline=False
    )

    embed.add_field(
        name="/dashboard",
        value="Open the Mini Assistants Dashboard.",
        inline=False
    )

    if interaction.guild:
        add_premium_footer(
            embed,
            interaction.guild.id
        )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# Pre Made Rules Chat Command
# ===========================================================
@client.event
async def on_message(message: discord.Message):
    if message.guild is not None and not message.author.bot:
        guild_id = str(message.guild.id)
        user_id = str(message.author.id)
        now = time.time()

        async with guild_leveling_lock:
            guild_data, members, user_data = get_guild_leveling_progress(
                guild_id,
                user_id
            )
            excluded_channels = guild_data.get("excluded_channels", [])
            if not isinstance(excluded_channels, list):
                print(
                    f"[LEVELING] Invalid excluded channel list for guild "
                    f"{guild_id}; message XP was not awarded."
                )
            else:
                level = user_data.get("level", 0)
                experience = user_data.get("xp", 0)
                last_xp_at = user_data.get("last_xp_at")
                if (
                    not isinstance(level, int)
                    or isinstance(level, bool)
                    or level < 0
                    or not isinstance(experience, int)
                    or isinstance(experience, bool)
                    or not 0 <= experience < get_guild_level_xp_required(level)
                    or (
                        last_xp_at is not None
                        and (
                            not isinstance(last_xp_at, (int, float))
                            or isinstance(last_xp_at, bool)
                            or not math.isfinite(last_xp_at)
                            or not 0 <= last_xp_at <= 253402300799
                        )
                    )
                ):
                    print(
                        f"[LEVELING] Invalid level data for user {user_id} "
                        f"in guild {guild_id}; message XP was not awarded."
                    )
                else:
                    updated_user_data = dict(user_data)
                    updated_user_data.setdefault("level", 0)
                    updated_user_data.setdefault("xp", 0)
                    updated_user_data.setdefault("last_xp_at", None)
                    should_save = user_id not in members

                    if str(message.channel.id) not in excluded_channels:
                        if (
                            last_xp_at is None
                            or now - last_xp_at >= GUILD_LEVEL_XP_COOLDOWN
                        ):
                            updated_user_data["xp"] += random.randint(
                                GUILD_LEVEL_XP_MIN,
                                GUILD_LEVEL_XP_MAX
                            )
                            while (
                                updated_user_data["xp"]
                                >= get_guild_level_xp_required(
                                    updated_user_data["level"]
                                )
                            ):
                                updated_user_data["xp"] -= (
                                    get_guild_level_xp_required(
                                        updated_user_data["level"]
                                    )
                                )
                                updated_user_data["level"] += 1
                            updated_user_data["last_xp_at"] = now
                            should_save = True

                    if should_save:
                        updated_guild_data = {
                            **guild_data,
                            "excluded_channels": excluded_channels,
                            "members": {
                                **members,
                                user_id: updated_user_data,
                            },
                        }
                        updated_guild_leveling_userdata = {
                            **guild_leveling_userdata,
                            guild_id: updated_guild_data,
                        }
                        if save_json(
                            GUILD_LEVELING_USERDATA_FILE,
                            updated_guild_leveling_userdata
                        ):
                            guild_leveling_userdata[guild_id] = (
                                updated_guild_data
                            )

    if message.content.lower().startswith("!prerules"):
        if message.guild is None or not isinstance(message.author, discord.Member):
            await message.channel.send(
                "❌ You need administrator permission to use this command."
            )
            return

        if not message.author.guild_permissions.administrator:
            await message.channel.send(
                "❌ You need administrator permission to use this command."
            )
            return

        embed = discord.Embed(
            title="⟡ ݁₊  Server Rules ₊ ݁⟡",
            description="01 • Respect our Community!\n"
            "We take hate speech or troll of any kind seriously and appropriate action will be taken. This includes but is not limited to Homophobia, Transphobia, or any form of Racial or Religious Discrimination.\n\n"
            "02 • Self Promotion\n"
            "We love our fellow content creators but try not to self promote in our channels without written permission from our Moderation Team of which can be contacted via our access ports.\n\n"
            "03 • Stick to Common Sense\n"
            "Many channels such as General and Media related channels will have specific rules which may not be listed in writing but still enforced by Mods such as No NSFW, Political or extremist conversations! These are enforced to the best of our ability and discretion.\n\n"
            "04 • Our Moderation Team\n"
            "Our Moderation Team is here to help and assist you with any issues you may have. If you have queries about their actions or decisions please contact them via our access ports. Please do not argue with them in public channels as this will be considered a violation of Rule 01.\n\n"
            "05 • Follow Discord ToS\n"
            "Whilst in our server please follow Discord Terms of Service which can be found [Here]",
            colour=discord.Colour(
                0xffffff
            )
        )
        add_premium_footer(
            embed,
            message.guild.id
        )
        await message.channel.send(
            embed=embed
        )


# ============================================================
# COMMAND ERROR HANDLER
# ============================================================


@client.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    print(
        f"[COMMAND ERROR] {error}"
    )

    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                "❌ An unexpected error occurred.",
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                "❌ An unexpected error occurred.",
                ephemeral=True
            )

    except discord.HTTPException:
        pass


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    client.run(TOKEN)
