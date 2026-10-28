# bot.py
import discord
from discord.ext import commands, tasks
import asyncio
import subprocess
import json
from datetime import datetime, timedelta
import shlex
import logging
import shutil
import os
import secrets
import string
import re
try:
    from playwright.async_api import async_playwright  # optional: panel screenshots
except ImportError:
    async_playwright = None
from typing import Optional, List, Dict, Any
import sqlite3
import random
import traceback
import aiohttp
import time
from collections import deque

# ============ CONFIGURATION ============
# Values are read from config.json (written by install.sh) or AXION_* environment variables.
# Things you change later with bot commands (.setlogo, .setalertchannel ...) are stored in vps.db.
# Rule: if you edit config.json again, the newest config.json value wins over the stored one.

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_config() -> Dict[str, Any]:
    path = os.environ.get('AXION_CONFIG', os.path.join(BASE_DIR, 'config.json'))
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        raise SystemExit(f"config.json is invalid ({path}): {e}")


CFG = _load_config()


def _cfg(key: str, default: str = '') -> str:
    value = os.environ.get('AXION_' + key.upper())
    if value is None or value == '':
        value = CFG.get(key, default)
    return str(value if value is not None else default).strip()


def _to_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


DISCORD_TOKEN = _cfg('token')
BOT_NAME = _cfg('brand_name', 'AxionHost')
PREFIX = _cfg('prefix', '.')
YOUR_SERVER_IP = _cfg('server_ip')
MAIN_ADMIN_ID = _cfg('main_admin_id', '1405866008127864852')
VPS_USER_ROLE_ID = ''
DEFAULT_STORAGE_POOL = _cfg('storage_pool', 'default')
NODE_NAME = _cfg('node_name', 'Node-1')
GUILD_ID = _to_int(_cfg('guild_id'))
ALERT_CHANNEL_ID = _to_int(_cfg('alert_channel_id'))
STATUS_CHANNEL_ID = _to_int(_cfg('status_channel_id'))

BRAND_ICON = "⚡"
PRODUCT_NAME = "AVM Pro"
PRODUCT_FULL_NAME = "Axion VPS Manager"
VERSION = "1.0.1"
CODENAME = "Atlas"
FOOTER_TEXT = f"{BOT_NAME} • AVM Pro"
CHANGELOG = {
    "1.0.1": ["SSH access now runs over Pinggy (tmate removed) with a saved root password",
              "SSHX rewritten: runs in the background, link read from a log, 30s limit — no more endless waiting",
              "Every step has a timeout and shows the real error (DNS, install, tunnel limit, DM closed)"],
    "1.0.0": ["Rebuilt from the v4 base as AVM (Axion VPS Manager)",
              "Invite + booster rewards with automatic VPS deploy (.claimfree)",
              "Mining Guard: detection, strikes, suspend, blacklist, alert buttons",
              "Live node status dashboard in its own channel",
              "Logo/banner branding via install.sh or Discord, working reinstall",
              "Fixed v4 bugs: last_daily reset, port dealloc on SQLite, duplicate plan-editor buttons"],
}

# Logo (embed thumbnail) and banner (embed image). Use a permanent direct image link
# (e.g. a GitHub raw URL) or set them from Discord with .setlogo / .setbanner + attachment.
THUMBNAIL = _cfg('logo_url')
BANNER = _cfg('banner_url')

# Free VPS Plans based on invites/boosts
FREE_VPS_PLANS = {
    'invites': [
        {'name': 'Free Tier I', 'invites': 10, 'ram': 12, 'cpu': 4, 'disk': 100},
        {'name': 'Free Tier II', 'invites': 20, 'ram': 24, 'cpu': 6, 'disk': 250},
        {'name': 'Free Tier III', 'invites': 28, 'ram': 32, 'cpu': 8, 'disk': 300}
    ],
    'boosts': [
        {'name': 'Boost Reward I', 'boosts': 1, 'ram': 24, 'cpu': 6, 'disk': 250},
        {'name': 'Boost Reward II', 'boosts': 2, 'ram': 32, 'cpu': 8, 'disk': 300}
    ]
}

# OS Options for VPS Creation and Reinstall
OS_OPTIONS = [
    {"label": "Ubuntu 20.04 LTS", "value": "ubuntu:20.04"},
    {"label": "Ubuntu 22.04 LTS", "value": "ubuntu:22.04"},
    {"label": "Ubuntu 24.04 LTS", "value": "ubuntu:24.04"},
    {"label": "Debian 10 (Buster)", "value": "images:debian/10"},
    {"label": "Debian 11 (Bullseye)", "value": "images:debian/11"},
    {"label": "Debian 12 (Bookworm)", "value": "images:debian/12"},
    {"label": "Debian 13 (Trixie)", "value": "images:debian/13"},
    {"label": "Rocky Linux 9", "value": "images:rockylinux/9"},
    {"label": "AlmaLinux 9", "value": "images:almalinux/9"},
    {"label": "Fedora 39", "value": "images:fedora/39"},
]

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(f'{BOT_NAME.lower()}_vps_bot')

# Check if lxc command is available
if not shutil.which("lxc"):
    logger.error("LXC command not found. Please ensure LXC is installed.")
    raise SystemExit("LXC command not found. Please ensure LXC is installed.")

# ============ DATABASE SETUP ============

def get_db():
    conn = sqlite3.connect('vps.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    
    # Admins table
    cur.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id TEXT PRIMARY KEY
    )''')
    cur.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (str(MAIN_ADMIN_ID),))
    
    # VPS table
    cur.execute('''CREATE TABLE IF NOT EXISTS vps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        container_name TEXT UNIQUE NOT NULL,
        plan_name TEXT DEFAULT 'Custom',
        ram TEXT NOT NULL,
        cpu TEXT NOT NULL,
        storage TEXT NOT NULL,
        config TEXT NOT NULL,
        os_version TEXT DEFAULT 'ubuntu:22.04',
        status TEXT DEFAULT 'stopped',
        suspended INTEGER DEFAULT 0,
        whitelisted INTEGER DEFAULT 0,
        suspended_reason TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        shared_with TEXT DEFAULT '[]',
        suspension_history TEXT DEFAULT '[]'
    )''')
    
    # Ensure columns exist
    cur.execute('PRAGMA table_info(vps)')
    info = cur.fetchall()
    columns = [col[1] for col in info]
    if 'os_version' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN os_version TEXT DEFAULT 'ubuntu:22.04'")
    if 'plan_name' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN plan_name TEXT DEFAULT 'Custom'")
    if 'suspended_reason' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN suspended_reason TEXT DEFAULT ''")
    
    # User stats for free VPS
    cur.execute('''CREATE TABLE IF NOT EXISTS user_stats (
        user_id TEXT PRIMARY KEY,
        invites INTEGER DEFAULT 0,
        boosts INTEGER DEFAULT 0,
        credits INTEGER DEFAULT 0,
        claimed_free_vps INTEGER DEFAULT 0,
        last_daily TEXT,
        last_updated TEXT
    )''')
    
    # --- AxionHost additions ---
    cur.execute('PRAGMA table_info(user_stats)')
    us_columns = [col[1] for col in cur.fetchall()]
    if 'claimed_types' not in us_columns:
        cur.execute("ALTER TABLE user_stats ADD COLUMN claimed_types TEXT DEFAULT ''")

    cur.execute('''CREATE TABLE IF NOT EXISTS invite_joins (
        invitee_id TEXT PRIMARY KEY,
        inviter_id TEXT NOT NULL,
        guild_id TEXT,
        invite_code TEXT,
        joined_at TEXT NOT NULL,
        counted INTEGER DEFAULT 1,
        left_at TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS mining_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        container_name TEXT NOT NULL,
        user_id TEXT NOT NULL,
        severity TEXT NOT NULL,
        evidence TEXT,
        action TEXT,
        created_at TEXT NOT NULL
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS strikes (
        user_id TEXT PRIMARY KEY,
        count INTEGER DEFAULT 0,
        last_strike TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS blacklist (
        user_id TEXT PRIMARY KEY,
        reason TEXT,
        created_at TEXT NOT NULL
    )''')

    # Settings table
    cur.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''')
    
    # Port allocations table
    cur.execute('''CREATE TABLE IF NOT EXISTS port_allocations (
        user_id TEXT PRIMARY KEY,
        allocated_ports INTEGER DEFAULT 0
    )''')
    
    # Port forwards table
    cur.execute('''CREATE TABLE IF NOT EXISTS port_forwards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        vps_container TEXT NOT NULL,
        vps_port INTEGER NOT NULL,
        host_port INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )''')
    
    # Suspension logs table
    cur.execute('''CREATE TABLE IF NOT EXISTS suspension_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        container_name TEXT NOT NULL,
        user_id TEXT NOT NULL,
        action TEXT NOT NULL,
        reason TEXT,
        admin_id TEXT,
        created_at TEXT NOT NULL
    )''')
    
    # Initialize settings
    settings_init = [
        ('cpu_threshold', '90'),
        ('ram_threshold', '90'),
        ('maintenance_mode', 'false'),
        ('maintenance_started_by', ''),
        ('maintenance_started_at', ''),
        ('bot_version', '4.1.0'),
        ('bot_status', 'online'),
        ('bot_activity', 'watching'),
        ('bot_activity_name', f'{BOT_NAME} VPS Manager'),
        ('daily_credit_amount', '10'),
        ('mining_guard', 'true'),
        ('mining_interval', '120'),
        ('status_interval', '60'),
        ('free_claims', 'true'),
        ('min_account_age_days', '7')
    ]
    for key, value in settings_init:
        cur.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
    
    conn.commit()
    conn.close()

def get_setting(key: str, default: Any = None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()

def get_vps_data() -> Dict[str, List[Dict[str, Any]]]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM vps')
    rows = cur.fetchall()
    conn.close()
    data = {}
    for row in rows:
        user_id = row['user_id']
        if user_id not in data:
            data[user_id] = []
        vps = dict(row)
        vps['shared_with'] = json.loads(vps['shared_with'])
        vps['suspension_history'] = json.loads(vps['suspension_history'])
        vps['suspended'] = bool(vps['suspended'])
        vps['whitelisted'] = bool(vps['whitelisted'])
        vps['os_version'] = vps.get('os_version', 'ubuntu:22.04')
        vps['plan_name'] = vps.get('plan_name', 'Custom')
        data[user_id].append(vps)
    return data

def get_admins() -> List[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM admins')
    rows = cur.fetchall()
    conn.close()
    return [row['user_id'] for row in rows]

def save_vps_data():
    conn = get_db()
    cur = conn.cursor()
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            shared_json = json.dumps(vps['shared_with'])
            history_json = json.dumps(vps['suspension_history'])
            suspended_int = 1 if vps['suspended'] else 0
            whitelisted_int = 1 if vps.get('whitelisted', False) else 0
            os_ver = vps.get('os_version', 'ubuntu:22.04')
            plan_name = vps.get('plan_name', 'Custom')
            created_at = vps.get('created_at', datetime.now().isoformat())
            suspended_reason = vps.get('suspended_reason', '')
            
            if 'id' not in vps or vps['id'] is None:
                cur.execute('''INSERT INTO vps (user_id, container_name, plan_name, ram, cpu, storage, config, os_version, status, suspended, whitelisted, suspended_reason, created_at, shared_with, suspension_history)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (user_id, vps['container_name'], plan_name, vps['ram'], vps['cpu'], vps['storage'], vps['config'],
                             os_ver, vps['status'], suspended_int, whitelisted_int, suspended_reason,
                             created_at, shared_json, history_json))
                vps['id'] = cur.lastrowid
            else:
                cur.execute('''UPDATE vps SET user_id = ?, plan_name = ?, ram = ?, cpu = ?, storage = ?, config = ?, os_version = ?, status = ?, suspended = ?, whitelisted = ?, suspended_reason = ?, shared_with = ?, suspension_history = ?
                               WHERE id = ?''',
                            (user_id, plan_name, vps['ram'], vps['cpu'], vps['storage'], vps['config'],
                             os_ver, vps['status'], suspended_int, whitelisted_int, suspended_reason,
                             shared_json, history_json, vps['id']))
    conn.commit()
    conn.close()

def save_admin_data():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM admins')
    for admin_id in admin_data['admins']:
        cur.execute('INSERT INTO admins (user_id) VALUES (?)', (admin_id,))
    conn.commit()
    conn.close()

def log_suspension(container_name: str, user_id: str, action: str, reason: str = "", admin_id: str = ""):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''INSERT INTO suspension_logs (container_name, user_id, action, reason, admin_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (container_name, user_id, action, reason, admin_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_suspension_logs(container_name: str = None) -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    if container_name:
        cur.execute('SELECT * FROM suspension_logs WHERE container_name = ? ORDER BY created_at DESC', (container_name,))
    else:
        cur.execute('SELECT * FROM suspension_logs ORDER BY created_at DESC LIMIT 50')
    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]

# User stats functions
def get_user_stats(user_id: str) -> Dict[str, Any]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM user_stats WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return dict(row)
    return {'user_id': user_id, 'invites': 0, 'boosts': 0, 'credits': 0, 'claimed_free_vps': 0, 'last_daily': None, 'last_updated': None, 'claimed_types': ''}

def update_user_stats(user_id: str, invites: int = 0, boosts: int = 0, credits: int = 0, claimed_free_vps: int = 0, last_daily: str = None):
    """Atomic upsert. Counters never go below 0 and unrelated columns (last_daily, claimed_types) are preserved."""
    conn = get_db()
    cur = conn.cursor()
    now = datetime.now().isoformat()
    cur.execute('''INSERT INTO user_stats (user_id, invites, boosts, credits, claimed_free_vps, last_daily, last_updated)
                   VALUES (?, MAX(0, ?), MAX(0, ?), MAX(0, ?), MAX(0, ?), ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       invites = MAX(0, user_stats.invites + ?),
                       boosts = MAX(0, user_stats.boosts + ?),
                       credits = MAX(0, user_stats.credits + ?),
                       claimed_free_vps = MAX(0, user_stats.claimed_free_vps + ?),
                       last_daily = COALESCE(?, user_stats.last_daily),
                       last_updated = ?''',
                (user_id, invites, boosts, credits, claimed_free_vps, last_daily, now,
                 invites, boosts, credits, claimed_free_vps, last_daily, now))
    conn.commit()
    conn.close()


# Port forwarding functions
def get_user_allocation(user_id: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT allocated_ports FROM port_allocations WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def get_user_used_ports(user_id: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM port_forwards WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0]

def allocate_ports(user_id: str, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO port_allocations (user_id, allocated_ports) VALUES (?, COALESCE((SELECT allocated_ports FROM port_allocations WHERE user_id = ?), 0) + ?)', (user_id, user_id, amount))
    conn.commit()
    conn.close()

def deallocate_ports(user_id: str, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE port_allocations SET allocated_ports = MAX(0, allocated_ports - ?) WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()

def get_available_host_port() -> Optional[int]:
    used_ports = set()
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT host_port FROM port_forwards')
    for row in cur.fetchall():
        used_ports.add(row[0])
    conn.close()
    for _ in range(100):
        port = random.randint(20000, 50000)
        if port not in used_ports:
            return port
    return None

async def create_port_forward(user_id: str, container: str, vps_port: int) -> Optional[int]:
    host_port = get_available_host_port()
    if not host_port:
        return None
    try:
        await execute_lxc(f"lxc config device add {container} tcp_proxy_{host_port} proxy listen=tcp:0.0.0.0:{host_port} connect=tcp:127.0.0.1:{vps_port}")
        await execute_lxc(f"lxc config device add {container} udp_proxy_{host_port} proxy listen=udp:0.0.0.0:{host_port} connect=udp:127.0.0.1:{vps_port}")
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO port_forwards (user_id, vps_container, vps_port, host_port, created_at) VALUES (?, ?, ?, ?, ?)',
                    (user_id, container, vps_port, host_port, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return host_port
    except Exception as e:
        logger.error(f"Failed to create port forward: {e}")
        return None

async def remove_port_forward(forward_id: int, is_admin: bool = False) -> tuple[bool, Optional[str]]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id, vps_container, host_port FROM port_forwards WHERE id = ?', (forward_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, None
    user_id, container, host_port = row
    try:
        await execute_lxc(f"lxc config device remove {container} tcp_proxy_{host_port}")
        await execute_lxc(f"lxc config device remove {container} udp_proxy_{host_port}")
        cur.execute('DELETE FROM port_forwards WHERE id = ?', (forward_id,))
        conn.commit()
        conn.close()
        return True, user_id
    except Exception as e:
        logger.error(f"Failed to remove port forward {forward_id}: {e}")
        conn.close()
        return False, None

def get_user_forwards(user_id: str) -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM port_forwards WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]

# Initialize database
init_db()

# Load data at startup
vps_data = get_vps_data()
admin_data = {'admins': get_admins()}

# Global settings from DB
CPU_THRESHOLD = int(get_setting('cpu_threshold', 90))
RAM_THRESHOLD = int(get_setting('ram_threshold', 90))
MAINTENANCE_MODE = get_setting('maintenance_mode', 'false').lower() == 'true'
MAINTENANCE_STARTED_BY = get_setting('maintenance_started_by', '')
MAINTENANCE_STARTED_AT = get_setting('maintenance_started_at', '')
BOT_STATUS = get_setting('bot_status', 'online')
BOT_ACTIVITY = get_setting('bot_activity', 'watching')
BOT_ACTIVITY_NAME = get_setting('bot_activity_name', f'{BOT_NAME} VPS Manager')
DAILY_CREDIT_AMOUNT = int(get_setting('daily_credit_amount', 10))

# ---- AxionHost runtime settings: config.json -> DB -> globals ----
def _sync_config_to_db():
    """When a value in config.json changed since the last start, it overrides the stored value."""
    for key in ('logo_url', 'banner_url', 'alert_channel_id', 'status_channel_id', 'node_name', 'guild_id'):
        value = _cfg(key)
        if value and value != get_setting('cfg_' + key, ''):
            set_setting(key, value)
            set_setting('cfg_' + key, value)


def _load_plans():
    raw = get_setting('plans_json', '')
    if not raw:
        return
    try:
        data = json.loads(raw)
        for cat in ('invites', 'boosts'):
            plans = data[cat]
            if len(plans) != len(FREE_VPS_PLANS[cat]):
                raise ValueError(f"{cat}: plan count changed")
            for p in plans:
                for k in ('name', 'ram', 'cpu', 'disk', cat):
                    if k not in p:
                        raise ValueError(f"{cat}: plan missing '{k}'")
                if min(int(p['ram']), int(p['cpu']), int(p['disk']), int(p[cat])) <= 0:
                    raise ValueError(f"{cat}: values must be positive")
        for cat in ('invites', 'boosts'):
            FREE_VPS_PLANS[cat] = data[cat]
    except Exception as e:
        logger.error(f"Stored plans_json is invalid, using built-in plans: {e}")


def save_plans():
    set_setting('plans_json', json.dumps(FREE_VPS_PLANS))


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


_sync_config_to_db()
THUMBNAIL = get_setting('logo_url', '') or ''
BANNER = get_setting('banner_url', '') or ''
ALERT_CHANNEL_ID = _to_int(get_setting('alert_channel_id', '0'))
STATUS_CHANNEL_ID = _to_int(get_setting('status_channel_id', '0'))
GUILD_ID = _to_int(get_setting('guild_id', '0'))
NODE_NAME = get_setting('node_name', NODE_NAME) or NODE_NAME
MINING_GUARD_ENABLED = get_setting('mining_guard', 'true').lower() == 'true'
MINING_INTERVAL = _clamp(_to_int(get_setting('mining_interval', '120'), 120), 30, 3600)
STATUS_INTERVAL = _clamp(_to_int(get_setting('status_interval', '60'), 60), 30, 3600)
FREE_CLAIMS_ENABLED = get_setting('free_claims', 'true').lower() == 'true'
MIN_ACCOUNT_AGE_DAYS = _clamp(_to_int(get_setting('min_account_age_days', '7'), 7), 0, 365)
_load_plans()

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# Dictionary to track active help menus to prevent duplicates
active_help_menus = {}

# Helper function to truncate text
def truncate_text(text, max_length=1024):
    if not text:
        return text
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."

# Embed creation functions with proper thumbnail and banner handling
def create_embed(title, description="", color=0x1a1a1a):
    embed = discord.Embed(
        title=f"{BRAND_ICON} {title}",
        description=truncate_text(description, 4096),
        color=color
    )
    
    # Set thumbnail if URL is provided and valid (small image at top right)
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    
    # Set banner/image if URL is provided and valid (small banner at top)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def add_field(embed, name, value, inline=False):
    embed.add_field(
        name=f"⌯⌲ {name}",
        value=truncate_text(value, 1024),
        inline=inline
    )
    return embed

def create_success_embed(title, description=""):
    return create_embed(title, description, color=0x00ff88)

def create_error_embed(title, description=""):
    embed = discord.Embed(
        title=f"{BRAND_ICON} {title}",
        description=f"───────────────\n{description}\n───────────────",
        color=0xff3366
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def create_info_embed(title, description=""):
    return create_embed(title, description, color=0x00ccff)

def create_warning_embed(title, description=""):
    return create_embed(title, description, color=0xffaa00)

# ============ MAINTENANCE MODE CHECK ============

async def maintenance_check(ctx):
    global MAINTENANCE_MODE, MAINTENANCE_STARTED_BY, MAINTENANCE_STARTED_AT
    
    if MAINTENANCE_MODE:
        user_id = str(ctx.author.id)
        if user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", []):
            return True
        
        try:
            started_by_user = await bot.fetch_user(int(MAINTENANCE_STARTED_BY)) if MAINTENANCE_STARTED_BY else None
            started_by_mention = started_by_user.mention if started_by_user else "Unknown"
        except:
            started_by_mention = "Unknown"
        
        try:
            started_at = datetime.fromisoformat(MAINTENANCE_STARTED_AT).strftime('%Y-%m-%d %H:%M:%S') if MAINTENANCE_STARTED_AT else "Unknown"
        except:
            started_at = "Unknown"
        
        embed = discord.Embed(
            title="🔧 Maintenance Mode Active",
            description="The bot is currently under maintenance. Only administrators can use commands at this time.",
            color=0xffaa00
        )
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.add_field(name="⌯⌲ Started By", value=started_by_mention, inline=True)
        embed.add_field(name="⌯⌲ Status", value="Commands disabled for non-admins", inline=True)
        embed.add_field(name="⌯⌲ Started At", value=started_at, inline=False)
        embed.set_footer(text=FOOTER_TEXT)
        
        await ctx.send(embed=embed)
        return False
    return True

# ============ ADMIN CHECKS ============

def is_admin():
    async def predicate(ctx):
        if not await maintenance_check(ctx):
            return False
        
        user_id = str(ctx.author.id)
        if user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", []):
            return True
        raise commands.CheckFailure("You need admin permissions to use this command.")
    return commands.check(predicate)

def is_main_admin():
    async def predicate(ctx):
        if not await maintenance_check(ctx):
            return False
        
        if str(ctx.author.id) == str(MAIN_ADMIN_ID):
            return True
        raise commands.CheckFailure("Only the main admin can use this command.")
    return commands.check(predicate)

# ============ LXC COMMAND EXECUTION ============

async def execute_lxc(command, timeout=120):
    try:
        cmd = shlex.split(command)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise asyncio.TimeoutError(f"Command timed out after {timeout} seconds")
        if proc.returncode != 0:
            error = stderr.decode().strip() if stderr else "Command failed with no error output"
            raise Exception(error)
        return stdout.decode().strip() if stdout else True
    except asyncio.TimeoutError as te:
        logger.error(f"LXC command timed out: {command} - {str(te)}")
        raise
    except Exception as e:
        logger.error(f"LXC Error: {command} - {str(e)}")
        raise

# ============ LXC CONFIGURATION ============

async def apply_lxc_config(container_name):
    try:
        await execute_lxc(f"lxc config set {container_name} security.nesting true")
        await execute_lxc(f"lxc config set {container_name} security.privileged true")
        await execute_lxc(f"lxc config set {container_name} security.syscalls.intercept.mknod true")
        await execute_lxc(f"lxc config set {container_name} security.syscalls.intercept.setxattr true")
        
        try:
            await execute_lxc(f"lxc config device add {container_name} fuse unix-char path=/dev/fuse")
        except Exception as e:
            if "already exists" not in str(e).lower():
                raise
        
        await execute_lxc(f"lxc config set {container_name} linux.kernel_modules overlay,loop,nf_nat,ip_tables,ip6_tables,netlink_diag,br_netfilter")
        
        raw_lxc_config = """
lxc.apparmor.profile = unconfined
lxc.cgroup.devices.allow = a
lxc.cap.drop =
lxc.mount.auto = proc:rw sys:rw cgroup:rw
"""
        await execute_lxc(f"lxc config set {container_name} raw.lxc '{raw_lxc_config}'")
        
        logger.info(f"Applied LXC config to {container_name}")
        return True
    except Exception as e:
        logger.error(f"Failed to apply LXC config to {container_name}: {e}")
        return False

async def apply_internal_permissions(container_name):
    try:
        await asyncio.sleep(5)
        
        commands = [
            "mkdir -p /etc/sysctl.d/",
            "echo 'net.ipv4.ip_unprivileged_port_start=0' > /etc/sysctl.d/99-custom.conf",
            "echo 'net.ipv4.ping_group_range=0 2147483647' >> /etc/sysctl.d/99-custom.conf",
            "echo 'fs.inotify.max_user_watches=524288' >> /etc/sysctl.d/99-custom.conf",
            "sysctl -p /etc/sysctl.d/99-custom.conf || true"
        ]
        
        for cmd in commands:
            try:
                await execute_lxc(f"lxc exec {container_name} -- bash -c \"{cmd}\"")
            except Exception:
                continue
        
        logger.info(f"Applied internal permissions to {container_name}")
    except Exception as e:
        logger.error(f"Failed to apply internal permissions to {container_name}: {e}")

# ============ VPS USER ROLE ============

async def get_or_create_vps_role(guild):
    global VPS_USER_ROLE_ID
    if VPS_USER_ROLE_ID:
        role = guild.get_role(VPS_USER_ROLE_ID)
        if role:
            return role
    role = discord.utils.get(guild.roles, name=f"{BOT_NAME} VPS User")
    if role:
        VPS_USER_ROLE_ID = role.id
        return role
    try:
        role = await guild.create_role(
            name=f"{BOT_NAME} VPS User",
            color=discord.Color.dark_purple(),
            reason=f"{BOT_NAME} VPS User role for bot management",
            permissions=discord.Permissions.none()
        )
        VPS_USER_ROLE_ID = role.id
        logger.info(f"Created {BOT_NAME} VPS User role: {role.name} (ID: {role.id})")
        return role
    except Exception as e:
        logger.error(f"Failed to create {BOT_NAME} VPS User role: {e}")
        return None

# ============ CONTAINER STATS FUNCTIONS ============

async def get_container_status(container_name):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "info", container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
        for line in output.splitlines():
            if line.startswith("Status: "):
                return line.split(": ", 1)[1].strip().lower()
        return "unknown"
    except Exception:
        return "unknown"

async def get_container_cpu(container_name):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "top", "-bn1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
        for line in output.splitlines():
            if '%Cpu(s):' in line:
                parts = line.split()
                us = float(parts[1])
                sy = float(parts[3])
                ni = float(parts[5])
                id_ = float(parts[7])
                wa = float(parts[9])
                hi = float(parts[11])
                si = float(parts[13])
                st = float(parts[15])
                usage = us + sy + ni + wa + hi + si + st
                return f"{usage:.1f}%"
        return "0.0%"
    except Exception:
        return "N/A"

async def get_container_memory(container_name):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "free", "-m",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().splitlines()
        if len(lines) > 1:
            parts = lines[1].split()
            total = int(parts[1])
            used = int(parts[2])
            usage_pct = (used / total * 100) if total > 0 else 0
            return f"{used}/{total} MB ({usage_pct:.1f}%)"
        return "Unknown"
    except Exception:
        return "N/A"

async def get_container_disk(container_name):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "df", "-h", "/",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().splitlines()
        for line in lines:
            if '/dev/' in line and ' /' in line:
                parts = line.split()
                if len(parts) >= 5:
                    used = parts[2]
                    size = parts[1]
                    perc = parts[4]
                    return f"{used}/{size} ({perc})"
        return "Unknown"
    except Exception:
        return "N/A"

async def get_container_uptime(container_name):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "uptime",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        uptime_str = stdout.decode().strip()
        if ',' in uptime_str:
            uptime_parts = uptime_str.split('up ')[1].split(',')[0]
            return uptime_parts.strip()
        return uptime_str
    except Exception:
        return "Unknown"

async def get_container_logs(container_name, lines=50):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "journalctl", "-n", str(lines), "--no-pager",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip() if stdout else "No logs available"
    except Exception:
        return "Unable to fetch logs"

def get_uptime():
    try:
        result = subprocess.run(['uptime'], capture_output=True, text=True)
        return result.stdout.strip()
    except Exception:
        return "Unknown"

# ============ BOT EVENTS ============

@bot.event
async def on_ready():
    logger.info(f'{bot.user} has connected to Discord!')
    
    if MAINTENANCE_MODE:
        await bot.change_presence(status=discord.Status.idle, activity=discord.Game(name="🔧 Maintenance Mode"))
    else:
        activity_types = {
            'playing': discord.ActivityType.playing,
            'watching': discord.ActivityType.watching,
            'listening': discord.ActivityType.listening,
        }
        
        status_types = {
            'online': discord.Status.online,
            'idle': discord.Status.idle,
            'dnd': discord.Status.dnd,
        }
        
        activity_type = activity_types.get(BOT_ACTIVITY, discord.ActivityType.watching)
        status = status_types.get(BOT_STATUS, discord.Status.online)
        
        await bot.change_presence(
            status=status,
            activity=discord.Activity(type=activity_type, name=BOT_ACTIVITY_NAME)
        )
    
    logger.info(f"{BOT_NAME} Bot is ready!")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=create_error_embed("Missing Argument", f"Please check command usage with `{PREFIX}help`."))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=create_error_embed("Invalid Argument", "Please check your input and try again."))
    elif isinstance(error, commands.CheckFailure):
        error_msg = str(error) if str(error) else "You need admin permissions for this command."
        await ctx.send(embed=create_error_embed("Access Denied", error_msg))
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(embed=create_warning_embed("Command on Cooldown", f"Please wait {error.retry_after:.2f} seconds before using this command again."))
    else:
        logger.error("Command error in %s", getattr(ctx.command, "name", "?"), exc_info=error)
        original = getattr(error, "original", error)
        detail = "An unexpected error occurred."
        if is_staff(str(ctx.author.id)):
            detail += f"\n```{type(original).__name__}: {str(original)[:500]}```"
        await ctx.send(embed=create_error_embed("System Error", detail))

# ============ USER COMMANDS ============

@bot.command(name='ping')
@commands.cooldown(1, 3, commands.BucketType.user)
async def ping(ctx):
    """Check bot latency"""
    if not await maintenance_check(ctx):
        return
    
    latency = round(bot.latency * 1000)
    
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"{BOT_NAME} Bot latency: `{latency}ms`",
        color=0x00ff88
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    
    await ctx.send(embed=embed)

@bot.command(name='uptime')
@commands.cooldown(1, 5, commands.BucketType.user)
async def uptime(ctx):
    """Show host uptime"""
    if not await maintenance_check(ctx):
        return
    
    up = get_uptime()
    embed = discord.Embed(
        title="⏱️ Host Uptime",
        description=f"```\n{up}\n```",
        color=0x00ccff
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)



@bot.command(name='myvps')
@commands.cooldown(1, 5, commands.BucketType.user)
async def my_vps(ctx):
    """List your VPS"""
    if not await maintenance_check(ctx):
        return
    
    user_id = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    
    if not vps_list:
        embed = discord.Embed(
            title=f"{BRAND_ICON} No VPS Found",
            description="You don't have any VPS. Do invites or boosts to get one.",
            color=0xff3366
        )
        embed.add_field(
            name="⌯⌲ Quick Actions",
            value=f"`.plans`",
            inline=False
        )
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)
        return
    
    embed = discord.Embed(
        title="🖥️ My VPS",
        description=f"You have `{len(vps_list)}` VPS",
        color=0x00ccff
    )
    
    for i, vps in enumerate(vps_list, 1):
        status = vps.get('status', 'unknown').upper()
        if vps.get('suspended', False):
            status += " (SUSPENDED)"
        
        status_emoji = "🟢" if vps.get('status') == 'running' else "🔴" if vps.get('status') == 'stopped' else "🟡"
        
        vps_info = f"{status_emoji} **VPS #{i}:** `{vps['container_name']}`\n"
        vps_info += f"• **Status:** {status}\n"
        vps_info += f"• **Plan:** {vps.get('plan_name', 'Custom')}\n"
        vps_info += f"• **Resources:** {vps.get('config', 'Custom')}\n"
        
        embed.add_field(name="", value=vps_info, inline=False)
    
    embed.add_field(name="⌯⌲ Management", value=f"Use `{PREFIX}manage` to control your VPS\nUse `{PREFIX}list` for detailed information", inline=False)
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)


# =========== FIXED: LIVE PROCESSING INSTALL PANEL ===========
# ─────────────────────────────────────────────
# 1. CREDENTIAL GENERATOR (unchanged, works fine)
# ─────────────────────────────────────────────
def generate_random_creds(panel_type: str):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    password = ''.join(secrets.choice(alphabet) for _ in range(18))
    rand = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))

    if "puffer" in panel_type.lower():
        username = f"puffer_{rand}"
        email    = f"{username}@pufferpanel.local"
    elif "ptero" in panel_type.lower():
        username = "admin"
        email    = f"ptero_{rand}@pterodactyl.local"
    else:
        username = f"user_{rand}"
        email    = f"{username}@panel.local"

    return username, email, password


# ─────────────────────────────────────────────
# 2. PROCESSING MESSAGE UPDATER
# ─────────────────────────────────────────────
async def update_processing(msg, text: str):
    """Embed update karta hai real-time status ke liye"""
    embed = discord.Embed(
        title="⚙️ Real-time Installation",
        description=text,
        color=0x00ccff
    )
    embed.set_footer(text=FOOTER_TEXT)
    try:
        await msg.edit(embed=embed)
    except Exception:
        pass


# ─────────────────────────────────────────────
# 3. FIX: CLOUDFLARED LINK CAPTURE
#    Problem: pehle wala re.findall sirf output string check karta tha
#    Fix: ab hum container ke andar se live link capture karte hain
# ─────────────────────────────────────────────
async def get_cloudflared_link(container_name: str, timeout: int = 60) -> str:
    """
    Container ke andar cloudflared run karke live link capture karta hai.
    Returns: trycloudflare.com link ya fallback message
    """
    try:
        # Pehle check karo cloudflared installed hai ya nahi
        check = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--",
            "bash", "-c", "which cloudflared || echo NOT_FOUND",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        cout, _ = await check.communicate()
        
        if b"NOT_FOUND" in cout:
            # Install cloudflared
            install_cmd = (
                "curl -L https://github.com/cloudflare/cloudflared/releases/latest"
                "/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared "
                "&& chmod +x /usr/local/bin/cloudflared"
            )
            await asyncio.create_subprocess_exec(
                "lxc", "exec", container_name, "--", "bash", "-c", install_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.sleep(5)

        # Log file path
        log_file = f"/tmp/cf_{container_name}.log"
        
        # Pehle purana log clean karo
        await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--",
            "bash", "-c", f"rm -f {log_file}"
        )

        # Cloudflared start karo background mein, log file mein output save karo
        run_cmd = f"nohup cloudflared tunnel --url http://localhost:80 > {log_file} 2>&1 &"
        await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "bash", "-c", run_cmd
        )

        # Poll karo jab tak link mile ya timeout ho
        link_pattern = re.compile(r'https://[a-zA-Z0-9\-]+\.trycloudflare\.com')
        for _ in range(timeout // 3):
            await asyncio.sleep(3)
            proc = await asyncio.create_subprocess_exec(
                "lxc", "exec", container_name, "--",
                "bash", "-c", f"cat {log_file} 2>/dev/null || echo ''",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            out, _ = await proc.communicate()
            text = out.decode(errors="ignore")
            match = link_pattern.search(text)
            if match:
                return match.group(0)

        return None  # timeout ho gaya

    except Exception as e:
        return None


# ─────────────────────────────────────────────
# 4. FIX: PANEL INSTALLER
#    Problem: bash -c 'echo 1 | bash <(curl ...)' LXC ke andar hang karta tha
#    Fix: script ko pehle download karo, phir run karo with proper env
# ─────────────────────────────────────────────
async def run_panel_installer(container_name: str, choice: str,
                               username: str, email: str, password: str,
                               timeout: int = 300) -> tuple[bool, str]:
    """
    Panel installer run karta hai container ke andar.
    Returns: (success: bool, output: str)
    """
    try:
        # Step 1: Dependencies install karo
        dep_cmd = "apt-get update -qq && apt-get install -y curl wget sudo 2>&1 | tail -5"
        dep_proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "bash", "-c", dep_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await asyncio.wait_for(dep_proc.communicate(), timeout=120)

        # Step 2: Script download karo
        dl_cmd = "curl -fsSL https://ptero.jishnu.fun/ -o /tmp/panel_install.sh && chmod +x /tmp/panel_install.sh"
        dl_proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "bash", "-c", dl_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            await asyncio.wait_for(dl_proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            return False, "Script download timeout ho gaya. Network check karo."

        # Step 3: Script run karo with env variables (credentials pass karo)
        # PANEL_USER, PANEL_EMAIL, PANEL_PASS env vars through script
        run_cmd = (
            f"export PANEL_CHOICE='{choice}' "
            f"PANEL_USER='{username}' "
            f"PANEL_EMAIL='{email}' "
            f"PANEL_PASS='{password}'; "
            f"echo '{choice}' | bash /tmp/panel_install.sh 2>&1"
        )
        
        run_proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "bash", "-c", run_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(run_proc.communicate(), timeout=timeout)
            output = stdout.decode(errors="ignore") + stderr.decode(errors="ignore")
            
            if run_proc.returncode == 0 or len(output) > 100:
                return True, output
            else:
                return False, output or "Installer ne kuch output nahi diya."
        except asyncio.TimeoutError:
            return False, f"Installation timeout ({timeout}s). VPS console mein check karo."

    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────
# 5. FIX: PLAYWRIGHT SCREENSHOT
# ─────────────────────────────────────────────
async def take_panel_screenshot(url: str, vps_name: str):
    """Panel ka real screenshot leta hai"""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            page = await browser.new_page()
            await page.goto(url, timeout=20000, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)
            screenshot_path = f"/tmp/{vps_name}_panel.png"
            await page.screenshot(path=screenshot_path, full_page=False)
            await browser.close()
            return screenshot_path
    except Exception:
        return None


# ─────────────────────────────────────────────
# 6. VPS SELECT VIEW (unchanged logic, small fixes)
# ─────────────────────────────────────────────
class VPSSelectView(discord.ui.View):
    def __init__(self, ctx, vps_list):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.vps_list = vps_list
        options = [
            discord.SelectOption(
                label=f"VPS {i+1}: {v['container_name']}",
                value=v['container_name']
            )
            for i, v in enumerate(vps_list)
        ]
        self.select = discord.ui.Select(
            placeholder="Select your VPS...",
            options=options
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message(
                "❌ This is not for you!", ephemeral=True
            )
            return
        await interaction.response.defer()
        await start_panel_install(self.ctx, self.select.values[0], interaction)


async def start_panel_install(ctx, container_name: str, interaction=None):
    embed = discord.Embed(
        title="🛠 Choose Panel",
        description=(
            f"**VPS:** `{container_name}`\n\n"
            "✅ Auto credentials generate honge\n"
            "✅ Real Cloudflared tunnel link milega\n"
            "✅ Screenshot DM mein aayega"
        ),
        color=0x00ccff
    )
    embed.set_footer(text=FOOTER_TEXT)
    view = PanelChoiceView(ctx, container_name)
    if interaction:
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await ctx.send(embed=embed, view=view)


# ─────────────────────────────────────────────
# 7. FIX: PANEL CHOICE VIEW - MAIN FIXED CLASS
# ─────────────────────────────────────────────
class PanelChoiceView(discord.ui.View):
    def __init__(self, ctx, container_name):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.container_name = container_name

    @discord.ui.button(label="1️⃣ PufferPanel", style=discord.ButtonStyle.primary, emoji="🐡")
    async def puffer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._install_panel(interaction, "PufferPanel", "1")

    @discord.ui.button(label="2️⃣ Pterodactyl", style=discord.ButtonStyle.primary, emoji="🦖")
    async def ptero_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._install_panel(interaction, "Pterodactyl", "2")

    @discord.ui.button(label="3️⃣ Cloudflared Only", style=discord.ButtonStyle.success, emoji="🌩️")
    async def cloud_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._install_cloudflared_only(interaction)

    # ── FIXED: Main install function ──────────────────────────────
    async def _install_panel(self, interaction: discord.Interaction,
                              panel_name: str, choice: str):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message(
                "❌ This is not for you!", ephemeral=True
            )
            return

        await interaction.response.defer()

        # Step 1: Credentials generate karo
        username, email, password = generate_random_creds(panel_name)

        # FIX: followup.send se pehle message bhejo
        msg = await interaction.followup.send(
            embed=discord.Embed(
                title="⚙️ Installation Starting...",
                description=(
                    f"✅ **Credentials Generated**\n"
                    f"👤 Username: `{username}`\n"
                    f"📧 Email: `{email}`\n"
                    f"🔑 Password: `{password}`\n\n"
                    f"⏳ Panel install ho raha hai... (~3-5 min)"
                ),
                color=0x00ccff
            )
        )

        # Step 2: Panel install karo (FIXED function)
        await update_processing(
            msg,
            f"📦 **{panel_name} Installing...**\n"
            f"VPS: `{self.container_name}`\n"
            f"⏳ Please wait, yeh 3-5 minutes le sakta hai..."
        )

        success, output = await run_panel_installer(
            self.container_name, choice,
            username, email, password,
            timeout=300
        )

        if not success:
            await msg.edit(
                embed=discord.Embed(
                    title="❌ Installation Failed",
                    description=f"**Error:**\n```\n{output[:1500]}\n```",
                    color=0xff3366
                )
            )
            return

        # Step 3: Cloudflared link capture karo (FIXED)
        await update_processing(
            msg,
            f"✅ **{panel_name} Installed!**\n\n"
            f"🌐 **Cloudflared tunnel setup ho raha hai...**\n"
            f"⏳ Link aane mein 30-60 seconds lagte hain..."
        )

        panel_link = await get_cloudflared_link(self.container_name, timeout=90)

        if not panel_link:
            panel_link = "❌ Auto-link nahi mila — VPS console mein `cloudflared tunnel --url http://localhost:80` run karo"

        # Step 4: Screenshot lo (agar link mila)
        screenshot_path = None
        if panel_link.startswith("https://"):
            await update_processing(
                msg,
                f"✅ **Cloudflared Link:** `{panel_link}`\n\n"
                f"📸 Panel screenshot le raha hai..."
            )
            screenshot_path = await take_panel_screenshot(panel_link, self.container_name)

        # Step 5: DM bhejo with all details
        user = self.ctx.author

        dm_embed = discord.Embed(
            title=f"🎉 {panel_name} Successfully Installed!",
            description=f"VPS `{self.container_name}` pe panel ready hai!",
            color=0x00ff88
        )
        dm_embed.add_field(
            name="🖥️ VPS",
            value=f"`{self.container_name}`",
            inline=False
        )
        dm_embed.add_field(
            name="👤 Login Credentials",
            value=(
                f"**Username:** `{username}`\n"
                f"**Email:** `{email}`\n"
                f"**Password:** `{password}`"
            ),
            inline=False
        )
        dm_embed.add_field(
            name="🔗 Panel Link",
            value=panel_link,
            inline=False
        )
        dm_embed.add_field(
            name="⚠️ Security",
            value="Password immediately change karo!",
            inline=False
        )
        dm_embed.set_footer(text=f"{BOT_NAME} • Change password immediately!")

        dm_status = "✅ Full details + screenshot DM mein bhej diye!"
        try:
            if screenshot_path and os.path.exists(screenshot_path):
                file = discord.File(screenshot_path, filename="panel_screenshot.png")
                dm_embed.set_image(url="attachment://panel_screenshot.png")
                await user.send(embed=dm_embed, file=file)
                os.remove(screenshot_path)
            else:
                await user.send(embed=dm_embed)
        except discord.Forbidden:
            dm_status = "❌ DM blocked hai! Discord mein DMs enable karo."
        except Exception as e:
            dm_status = f"❌ DM error: {str(e)[:100]}"

        # Step 6: Final success message
        final_embed = discord.Embed(
            title="✅ Installation Complete!",
            description=(
                f"**{panel_name}** VPS `{self.container_name}` pe install ho gaya!\n\n"
                f"🔗 **Link:** {panel_link}\n\n"
                f"{dm_status}"
            ),
            color=0x00ff88
        )
        final_embed.add_field(
            name="👤 Quick Credentials",
            value=f"**User:** `{username}` | **Pass:** `{password}`",
            inline=False
        )
        final_embed.set_footer(text=FOOTER_TEXT)
        await msg.edit(embed=final_embed)

    # ── Cloudflared Only (Button 3) ───────────────────────────────
    async def _install_cloudflared_only(self, interaction: discord.Interaction):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message(
                "❌ This is not for you!", ephemeral=True
            )
            return

        await interaction.response.defer()
        msg = await interaction.followup.send(
            embed=discord.Embed(
                title="🌩️ Cloudflared Tunnel Setup",
                description=f"VPS `{self.container_name}` pe Cloudflared tunnel start ho raha hai...",
                color=0x00ccff
            )
        )

        link = await get_cloudflared_link(self.container_name, timeout=90)

        if link:
            final = discord.Embed(
                title="✅ Cloudflared Tunnel Ready!",
                description=f"🔗 **Tunnel Link:** {link}",
                color=0x00ff88
            )
            final.add_field(
                name="ℹ️ Note",
                value="Yeh free tunnel hai — link restart ke baad change ho jata hai.",
                inline=False
            )
        else:
            final = discord.Embed(
                title="❌ Tunnel Link Nahi Mila",
                description=(
                    "Auto-capture failed. Manually run karo VPS console mein:\n"
                    "```bash\ncloudflared tunnel --url http://localhost:80\n```"
                ),
                color=0xff3366
            )
        final.set_footer(text=FOOTER_TEXT)
        await msg.edit(embed=final)


# ─────────────────────────────────────────────
# 8. MAIN COMMANDS (unchanged interface)
# ─────────────────────────────────────────────

@bot.command(name='installpanel')
@commands.cooldown(1, 30, commands.BucketType.user)
async def install_panel(ctx):
    if not await maintenance_check(ctx):
        return
    
    user_id = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    
    if not vps_list:
        await ctx.send(embed=create_error_embed("No VPS", "Pehle VPS banao!"))
        return
    
    if len(vps_list) == 1:
        await start_panel_install(ctx, vps_list[0]['container_name'])
    else:
        embed = create_info_embed("🛠 Install Panel",
            f"You have **{len(vps_list)}** VPS.\nSelect one:")
        view = VPSSelectView(ctx, vps_list)
        await ctx.send(embed=embed, view=view)

@bot.command(name='panel')
async def panel_alias(ctx):
    await install_panel(ctx)

@bot.command(name='list')
@commands.cooldown(1, 5, commands.BucketType.user)
async def list_user_vps(ctx):
    """Detailed VPS list"""
    if not await maintenance_check(ctx):
        return
    
    user_id = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    
    if not vps_list:
        embed = discord.Embed(
            title=f"{BRAND_ICON} No VPS Found",
            description="You don't have any VPS. Do invites or boosts to get one.",
            color=0xff3366
        )
        embed.add_field(
            name="⌯⌲ Quick Actions",
            value=f"`.plans`",
            inline=False
        )
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)
        return
    
    embed = discord.Embed(
        title="📋 Your VPS List",
        description=f"Showing `{len(vps_list)}` VPS for {ctx.author.mention}",
        color=0x00ccff
    )
    
    for i, vps in enumerate(vps_list, 1):
        container_name = vps['container_name']
        
        status = await get_container_status(container_name)
        cpu_usage = await get_container_cpu(container_name)
        memory_usage = await get_container_memory(container_name)
        disk_usage = await get_container_disk(container_name)
        uptime_info = await get_container_uptime(container_name)
        
        status_emoji = "🟢" if status == 'running' else "🔴" if status == 'stopped' else "🟡"
        suspended_text = " (SUSPENDED)" if vps.get('suspended', False) else ""
        
        vps_info = f"**#{i} | {status_emoji} {status.upper()}{suspended_text}**\n"
        vps_info += f"**Container:** `{container_name}`\n"
        vps_info += f"**Plan:** {vps.get('plan_name', 'Custom')}\n"
        vps_info += f"**Resources:** {vps['ram']} RAM | {vps['cpu']} CPU | {vps['storage']} Storage\n"
        vps_info += f"**OS:** {vps.get('os_version', 'ubuntu:22.04')}\n"
        vps_info += f"**Uptime:** {uptime_info}\n"
        vps_info += f"**CPU Usage:** {cpu_usage}\n"
        vps_info += f"**Memory:** {memory_usage}\n"
        vps_info += f"**Disk:** {disk_usage}\n"
        vps_info += f"**Created:** {vps.get('created_at', 'Unknown')[:10]}\n"
        
        embed.add_field(name=f"VPS #{i}", value=vps_info, inline=False)
    
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

# ============ VPS MANAGEMENT COMMANDS ============

class ManageView(discord.ui.View):
    def __init__(self, user_id, vps_list, is_shared=False, owner_id=None, is_admin=False, actual_index: Optional[int] = None):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.vps_list = vps_list[:]
        self.selected_index = 0 if vps_list else None
        self.is_shared = is_shared
        self.owner_id = owner_id or user_id
        self.is_admin = is_admin
        self.actual_index = actual_index
        self.indices = list(range(len(vps_list)))
        
        if self.is_shared and self.actual_index is None:
            raise ValueError("actual_index required for shared views")
        
        if len(vps_list) > 1:
            options = [
                discord.SelectOption(
                    label=f"VPS {i+1} ({v.get('plan_name', 'Custom')})",
                    description=f"Status: {v.get('status', 'unknown')}",
                    value=str(i)
                ) for i, v in enumerate(vps_list)
            ]
            self.select = discord.ui.Select(placeholder="Select a VPS to manage", options=options)
            self.select.callback = self.select_vps
            self.add_item(self.select)
        else:
            self.add_action_buttons()
    
    async def create_vps_embed(self, index):
        vps = self.vps_list[index]
        status = vps.get('status', 'unknown')
        suspended = vps.get('suspended', False)
        whitelisted = vps.get('whitelisted', False)
        status_color = 0x00ff88 if status == 'running' and not suspended else 0xffaa00 if suspended else 0xff3366
        container_name = vps['container_name']
        
        lxc_status = await get_container_status(container_name)
        cpu_usage = await get_container_cpu(container_name)
        memory_usage = await get_container_memory(container_name)
        disk_usage = await get_container_disk(container_name)
        uptime = await get_container_uptime(container_name)
        
        status_text = f"{lxc_status.upper()}"
        if suspended:
            status_text += " (SUSPENDED)"
        if whitelisted:
            status_text += " (WHITELISTED)"
        
        owner_text = ""
        if self.is_admin and self.owner_id != self.user_id:
            try:
                owner_user = await bot.fetch_user(int(self.owner_id))
                owner_text = f"\n**Owner:** {owner_user.mention}"
            except:
                owner_text = f"\n**Owner ID:** {self.owner_id}"
        
        embed = discord.Embed(
            title=f"🖥️ VPS Management",
            description=f"Managing VPS #{index + 1}: `{container_name}`{owner_text}",
            color=status_color
        )
        
        resource_info = f"**Plan:** {vps.get('plan_name', 'Custom')}\n"
        resource_info += f"**Status:** {status_text}\n"
        resource_info += f"**RAM:** {vps['ram']}\n"
        resource_info += f"**CPU:** {vps['cpu']} Cores\n"
        resource_info += f"**Storage:** {vps['storage']}\n"
        resource_info += f"**OS:** {vps.get('os_version', 'ubuntu:22.04')}\n"
        resource_info += f"**Uptime:** {uptime}"
        
        embed.add_field(name="⌯⌲ Resources", value=resource_info, inline=False)
        
        if suspended:
            embed.add_field(name="⌯⌲ Suspended", value="This VPS is suspended. Contact an admin to unsuspend.", inline=False)
        if whitelisted:
            embed.add_field(name="⌯⌲ Whitelisted", value="This VPS is exempt from auto-suspension.", inline=False)
        
        live_stats = f"**CPU Usage:** {cpu_usage}\n**Memory:** {memory_usage}\n**Disk:** {disk_usage}"
        embed.add_field(name="⌯⌲ Live Usage", value=live_stats, inline=False)
        embed.add_field(name="⌯⌲ Controls", value="Use the buttons below to manage your VPS", inline=False)
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        
        return embed
    
    def add_action_buttons(self):
        if not self.is_shared and not self.is_admin:
            reinstall_button = discord.ui.Button(label="🔄 Reinstall", style=discord.ButtonStyle.danger)
            reinstall_button.callback = lambda inter: self.action_callback(inter, 'reinstall')
            self.add_item(reinstall_button)
        
        start_button = discord.ui.Button(label="▶ Start", style=discord.ButtonStyle.success)
        start_button.callback = lambda inter: self.action_callback(inter, 'start')
        
        stop_button = discord.ui.Button(label="⏹ Stop", style=discord.ButtonStyle.secondary)
        stop_button.callback = lambda inter: self.action_callback(inter, 'stop')
        
        ssh_button = discord.ui.Button(label="🔑 SSH", style=discord.ButtonStyle.primary)
        ssh_button.callback = lambda inter: self.action_callback(inter, 'ssh')
        
        sshx_button = discord.ui.Button(label="🔗 SSHX", style=discord.ButtonStyle.success)
        sshx_button.callback = lambda inter: self.action_callback(inter, 'sshx')
        
        stats_button = discord.ui.Button(label="📊 Stats", style=discord.ButtonStyle.secondary)
        stats_button.callback = lambda inter: self.action_callback(inter, 'stats')
        
        self.add_item(start_button)
        self.add_item(stop_button)
        self.add_item(ssh_button)
        self.add_item(sshx_button)
        self.add_item(stats_button)
    
    async def select_vps(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "This is not your VPS!"), ephemeral=True)
            return
        
        self.selected_index = int(self.select.values[0])
        new_embed = await self.create_vps_embed(self.selected_index)
        self.clear_items()
        self.add_action_buttons()
        await interaction.response.edit_message(embed=new_embed, view=self)
    
    async def action_callback(self, interaction: discord.Interaction, action: str):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "This is not your VPS!"), ephemeral=True)
            return
        
        if self.selected_index is None and len(self.vps_list) == 1:
            self.selected_index = 0
        
        if self.selected_index is None:
            await interaction.response.send_message(embed=create_error_embed("No VPS Selected", "Please select a VPS first."), ephemeral=True)
            return
        
        actual_idx = self.actual_index if self.is_shared else self.indices[self.selected_index]
        target_vps = vps_data[self.owner_id][actual_idx]
        suspended = target_vps.get('suspended', False)
        
        if suspended and not self.is_admin and action not in ['stats']:
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "This VPS is suspended. Contact an admin to unsuspend."), ephemeral=True)
            return
        
        container_name = target_vps["container_name"]
        
        if action == 'stats':
            status = await get_container_status(container_name)
            cpu_usage = await get_container_cpu(container_name)
            memory_usage = await get_container_memory(container_name)
            disk_usage = await get_container_disk(container_name)
            uptime = await get_container_uptime(container_name)
            
            stats_embed = create_info_embed("Live Statistics", f"Real-time stats for `{container_name}`")
            add_field(stats_embed, "Status", f"`{status.upper()}`", True)
            add_field(stats_embed, "CPU", cpu_usage, True)
            add_field(stats_embed, "Memory", memory_usage, True)
            add_field(stats_embed, "Disk", disk_usage, True)
            add_field(stats_embed, "Uptime", uptime, True)
            
            await interaction.response.send_message(embed=stats_embed, ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        if action == 'start':
            try:
                await execute_lxc(f"lxc start {container_name}")
                target_vps["status"] = "running"
                target_vps["suspended"] = False
                save_vps_data()
                await apply_internal_permissions(container_name)
                await interaction.followup.send(embed=create_success_embed("VPS Started", f"VPS `{container_name}` is now running!"), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Start Failed", str(e)), ephemeral=True)
        
        elif action == 'stop':
            try:
                await execute_lxc(f"lxc stop {container_name}", timeout=120)
                target_vps["status"] = "stopped"
                save_vps_data()
                await interaction.followup.send(embed=create_success_embed("VPS Stopped", f"VPS `{container_name}` has been stopped!"), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Stop Failed", str(e)), ephemeral=True)
        
        elif action == 'ssh':
            await handle_ssh_action(interaction, container_name, suspended)

        elif action == 'sshx':
            await handle_sshx_action(interaction, container_name, suspended)

        elif action == 'reinstall':
            if self.is_shared or self.is_admin:
                await interaction.followup.send(embed=create_error_embed("Access Denied", "Only the VPS owner can reinstall!"), ephemeral=True)
                return
            
            if suspended:
                await interaction.followup.send(embed=create_error_embed("Cannot Reinstall", "Unsuspend the VPS first."), ephemeral=True)
                return
            
            await start_reinstall_flow(interaction, target_vps, self.owner_id)
        
        if self.selected_index is not None:
            new_embed = await self.create_vps_embed(self.selected_index)
            await interaction.edit_original_response(embed=new_embed, view=self)

@bot.command(name='manage')
@commands.cooldown(1, 5, commands.BucketType.user)
async def manage_vps(ctx, user: discord.Member = None):
    """Manage your VPS"""
    if not await maintenance_check(ctx):
        return
    
    if user:
        user_id_check = str(ctx.author.id)
        if user_id_check != str(MAIN_ADMIN_ID) and user_id_check not in admin_data.get("admins", []):
            await ctx.send(embed=create_error_embed("Access Denied", "Only admins can manage other users' VPS."))
            return
        
        user_id = str(user.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            embed = discord.Embed(
                title=f"{BRAND_ICON} No VPS Found",
                description=f"{user.mention} doesn't have any VPS.",
                color=0xff3366
            )
            if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
                embed.set_thumbnail(url=THUMBNAIL)
            if BANNER and BANNER.startswith(('http://', 'https://')):
                embed.set_image(url=BANNER)
            embed.set_footer(text=FOOTER_TEXT)
            await ctx.send(embed=embed)
            return
        
        view = ManageView(str(ctx.author.id), vps_list, is_admin=True, owner_id=user_id)
        embed = await view.create_vps_embed(0)
        await ctx.send(embed=embed, view=view)
    
    else:
        user_id = str(ctx.author.id)
        vps_list = vps_data.get(user_id, [])
        
        if not vps_list:
            embed = discord.Embed(
                title=f"{BRAND_ICON} No VPS Found",
                description="You don't have any VPS. Do invites or boosts to get one.",
                color=0xff3366
            )
            embed.add_field(
                name="⌯⌲ Quick Actions",
                value=f"`.plans`",
                inline=False
            )
            if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
                embed.set_thumbnail(url=THUMBNAIL)
            if BANNER and BANNER.startswith(('http://', 'https://')):
                embed.set_image(url=BANNER)
            embed.set_footer(text=FOOTER_TEXT)
            await ctx.send(embed=embed)
            return
        
        view = ManageView(user_id, vps_list)
        embed = await view.create_vps_embed(0)
        await ctx.send(embed=embed, view=view)

@bot.command(name='share-user')
@commands.cooldown(1, 3, commands.BucketType.user)
async def share_user(ctx, shared_user: discord.Member, vps_number: int):
    """Share VPS access with another user"""
    if not await maintenance_check(ctx):
        return
    
    user_id = str(ctx.author.id)
    shared_user_id = str(shared_user.id)
    
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or you don't have a VPS."))
        return
    
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps:
        vps["shared_with"] = []
    
    if shared_user_id in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Already Shared", f"{shared_user.mention} already has access to this VPS!"))
        return
    
    vps["shared_with"].append(shared_user_id)
    save_vps_data()
    
    embed = discord.Embed(
        title="✅ VPS Shared",
        description=f"VPS #{vps_number} shared with {shared_user.mention}!",
        color=0x00ff88
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

@bot.command(name='share-ruser')
@commands.cooldown(1, 3, commands.BucketType.user)
async def revoke_share(ctx, shared_user: discord.Member, vps_number: int):
    """Revoke VPS access from another user"""
    if not await maintenance_check(ctx):
        return
    
    user_id = str(ctx.author.id)
    shared_user_id = str(shared_user.id)
    
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or you don't have a VPS."))
        return
    
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps:
        vps["shared_with"] = []
    
    if shared_user_id not in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Not Shared", f"{shared_user.mention} doesn't have access to this VPS!"))
        return
    
    vps["shared_with"].remove(shared_user_id)
    save_vps_data()
    
    embed = discord.Embed(
        title="✅ Access Revoked",
        description=f"Access to VPS #{vps_number} revoked from {shared_user.mention}!",
        color=0x00ff88
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

@bot.command(name='manage-shared')
@commands.cooldown(1, 3, commands.BucketType.user)
async def manage_shared_vps(ctx, owner: discord.Member, vps_number: int):
    """Manage a VPS that has been shared with you"""
    if not await maintenance_check(ctx):
        return
    
    owner_id = str(owner.id)
    user_id = str(ctx.author.id)
    
    if owner_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[owner_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or owner doesn't have a VPS."))
        return
    
    vps = vps_data[owner_id][vps_number - 1]
    if user_id not in vps.get("shared_with", []):
        await ctx.send(embed=create_error_embed("Access Denied", "You do not have access to this VPS."))
        return
    
    view = ManageView(user_id, [vps], is_shared=True, owner_id=owner_id, actual_index=vps_number - 1)
    embed = await view.create_vps_embed(0)
    await ctx.send(embed=embed, view=view)

@bot.command(name='vpsinfo')
@is_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def vps_info(ctx, container_name: str):
    """VPS information"""
    if not container_name:
        await ctx.send(embed=create_error_embed("Usage", f"Usage: {PREFIX}vpsinfo <container_name>"))
        return
    
    found_vps = None
    found_user = None
    user_id = None
    
    for uid, vps_list in vps_data.items():
        for vps in vps_list:
            if vps['container_name'] == container_name:
                found_vps = vps
                user_id = uid
                try:
                    found_user = await bot.fetch_user(int(uid))
                except:
                    found_user = None
                break
        if found_vps:
            break
    
    if not found_vps:
        await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS found with container name: `{container_name}`"))
        return
    
    status = await get_container_status(container_name)
    cpu = await get_container_cpu(container_name)
    memory = await get_container_memory(container_name)
    disk = await get_container_disk(container_name)
    uptime = await get_container_uptime(container_name)
    
    embed = discord.Embed(
        title=f"🖥️ VPS Information - {container_name}",
        description=f"Details for VPS",
        color=0x1a1a1a
    )
    
    embed.add_field(name="⌯⌲ Owner", value=found_user.mention if found_user else f"ID: {user_id}", inline=True)
    embed.add_field(name="⌯⌲ Status", value=status.upper(), inline=True)
    embed.add_field(name="⌯⌲ Plan", value=found_vps.get('plan_name', 'Custom'), inline=True)
    
    resources = f"**RAM:** {found_vps['ram']}\n"
    resources += f"**CPU:** {found_vps['cpu']} Cores\n"
    resources += f"**Storage:** {found_vps['storage']}\n"
    resources += f"**OS:** {found_vps.get('os_version', 'ubuntu:22.04')}"
    embed.add_field(name="⌯⌲ Allocated Resources", value=resources, inline=False)
    
    live_stats = f"**CPU Usage:** {cpu}\n"
    live_stats += f"**Memory:** {memory}\n"
    live_stats += f"**Disk:** {disk}\n"
    live_stats += f"**Uptime:** {uptime}"
    embed.add_field(name="⌯⌲ Live Statistics", value=live_stats, inline=False)
    
    if found_vps.get('suspended', False):
        embed.add_field(name="⌯⌲ Suspended", value=f"Reason: {found_vps.get('suspended_reason', 'No reason')}", inline=False)
    
    if found_vps.get('whitelisted', False):
        embed.add_field(name="⌯⌲ Whitelisted", value="Exempt from auto-suspension", inline=False)
    
    created = found_vps.get('created_at', 'Unknown')[:19].replace('T', ' ')
    embed.add_field(name="⌯⌲ Created", value=created, inline=False)
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    
    await ctx.send(embed=embed)

@bot.command(name='vps-stats')
@is_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def vps_stats(ctx, container_name: str):
    """VPS stats"""
    cpu = await get_container_cpu(container_name)
    memory = await get_container_memory(container_name)
    disk = await get_container_disk(container_name)
    uptime = await get_container_uptime(container_name)
    
    embed = discord.Embed(
        title=f"📊 VPS Stats - {container_name}",
        description=f"**CPU Usage:** {cpu}\n**Memory:** {memory}\n**Disk:** {disk}\n**Uptime:** {uptime}",
        color=0x00ccff
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

@bot.command(name='restart-vps')
@is_admin()
@commands.cooldown(1, 10, commands.BucketType.user)
async def restart_vps(ctx, container_name: str):
    """Restart VPS"""
    await ctx.send(embed=create_info_embed("Restarting VPS", f"Restarting VPS `{container_name}`..."))
    
    try:
        await execute_lxc(f"lxc restart {container_name}")
        
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    vps['status'] = 'running'
                    vps['suspended'] = False
                    save_vps_data()
                    break
        
        await apply_internal_permissions(container_name)
        
        embed = discord.Embed(
            title="✅ VPS Restarted",
            description=f"VPS `{container_name}` has been restarted successfully!",
            color=0x00ff88
        )
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)
    
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restart Failed", f"Error: {str(e)}"))

@bot.command(name='clone-vps')
@is_admin()
@commands.cooldown(1, 30, commands.BucketType.user)
async def clone_vps(ctx, container_name: str, new_name: str = None):
    """Clone VPS"""
    if not new_name:
        new_name = f"{container_name}-clone-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    await ctx.send(embed=create_info_embed("Cloning VPS", f"Cloning `{container_name}` to `{new_name}`..."))
    
    try:
        await execute_lxc(f"lxc copy {container_name} {new_name}")
        embed = discord.Embed(
            title="✅ VPS Cloned",
            description=f"VPS `{container_name}` cloned to `{new_name}`",
            color=0x00ff88
        )
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Clone Failed", f"Error: {str(e)}"))

@bot.command(name='snapshot')
@is_admin()
@commands.cooldown(1, 30, commands.BucketType.user)
async def create_snapshot(ctx, container_name: str, snap_name: str = None):
    """Create snapshot"""
    if not snap_name:
        snap_name = f"snap-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    
    await ctx.send(embed=create_info_embed("Creating Snapshot", f"Creating snapshot `{snap_name}` for `{container_name}`..."))
    
    try:
        await execute_lxc(f"lxc snapshot {container_name} {snap_name}")
        embed = discord.Embed(
            title="✅ Snapshot Created",
            description=f"Snapshot `{snap_name}` created for `{container_name}`",
            color=0x00ff88
        )
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Snapshot Failed", f"Error: {str(e)}"))

@bot.command(name='restore-backup')
@is_admin()
@commands.cooldown(1, 30, commands.BucketType.user)
async def restore_backup(ctx, container_name: str, snap_name: str):
    """Restore VPS Data"""
    await ctx.send(embed=create_info_embed("Restoring Backup", f"Restoring `{container_name}` from snapshot `{snap_name}`..."))
    
    try:
        await execute_lxc(f"lxc restore {container_name} {snap_name}")
        embed = discord.Embed(
            title="✅ Backup Restored",
            description=f"VPS `{container_name}` restored from snapshot `{snap_name}`",
            color=0x00ff88
        )
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restore Failed", f"Error: {str(e)}"))

# ============ BOT SYSTEM COMMANDS ============








class PlanEditView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=300)
        self.ctx = ctx
        
        self.select = discord.ui.Select(
            placeholder="Select a plan to edit...",
            options=[
                *plan_select_options(),
            ]
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)
        
    
    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu is not for you!", ephemeral=True)
            return
        
        value = self.select.values[0]
        category, index = value.split('_')
        index = int(index)
        
        plan = FREE_VPS_PLANS[category][index]
        
        embed = discord.Embed(
            title=f"✏️ Editing Plan: {plan['name']}",
            description=f"Current configuration:",
            color=0x00ccff
        )
        embed.add_field(name="RAM", value=f"{plan['ram']} GB", inline=True)
        embed.add_field(name="CPU", value=f"{plan['cpu']} Cores", inline=True)
        embed.add_field(name="Disk", value=f"{plan['disk']} GB", inline=True)
        embed.add_field(name="Requirement", value=f"{plan.get('invites', plan.get('boosts'))} {'Invites' if 'invites' in plan else 'Boosts'}", inline=False)
        
        view = PlanEditActionView(self.ctx, category, index, plan)
        await interaction.response.edit_message(embed=embed, view=view)
    
    @discord.ui.button(label="❌ Close", style=discord.ButtonStyle.danger, row=1)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu is not for you!", ephemeral=True)
            return
        
        embed = create_info_embed("Plan Editor Closed", "No changes were made.")
        await interaction.response.edit_message(embed=embed, view=None)

class PlanEditActionView(discord.ui.View):
    def __init__(self, ctx, category, index, plan):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.category = category
        self.index = index
        self.plan = dict(plan)  # edit a copy; only Save writes it back
        
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu is not for you!", ephemeral=True)
            return False
        return True
    
    @discord.ui.button(label="📝 Edit RAM", style=discord.ButtonStyle.primary, custom_id="edit_ram", row=0)
    async def edit_ram(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = EditPlanModal(self, "ram", f"Enter new RAM value (current: {self.plan['ram']} GB)")
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="📝 Edit CPU", style=discord.ButtonStyle.primary, custom_id="edit_cpu", row=0)
    async def edit_cpu(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = EditPlanModal(self, "cpu", f"Enter new CPU core count (current: {self.plan['cpu']})")
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="📝 Edit Disk", style=discord.ButtonStyle.primary, custom_id="edit_disk", row=0)
    async def edit_disk(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = EditPlanModal(self, "disk", f"Enter new Disk size in GB (current: {self.plan['disk']} GB)")
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="📝 Edit Requirement", style=discord.ButtonStyle.primary, custom_id="edit_req", row=1)
    async def edit_req(self, interaction: discord.Interaction, button: discord.ui.Button):
        req_type = "invites" if 'invites' in self.plan else "boosts"
        current = self.plan.get('invites', self.plan.get('boosts'))
        modal = EditPlanModal(self, "req", f"Enter new {req_type} requirement (current: {current})")
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="💾 Save Changes", style=discord.ButtonStyle.success, custom_id="save", row=2)
    async def save_changes(self, interaction: discord.Interaction, button: discord.ui.Button):
        FREE_VPS_PLANS[self.category][self.index] = self.plan
        save_plans()
        
        embed = discord.Embed(
            title="✅ Plan Updated",
            description=f"**{self.plan['name']}** has been updated successfully!",
            color=0x00ff88
        )
        embed.add_field(name="⌯⌲ New Configuration", 
                       value=f"**RAM:** {self.plan['ram']} GB\n**CPU:** {self.plan['cpu']} Cores\n**Disk:** {self.plan['disk']} GB\n**Requirement:** {self.plan.get('invites', self.plan.get('boosts'))} {'Invites' if 'invites' in self.plan else 'Boosts'}", 
                       inline=False)
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await interaction.response.edit_message(embed=embed, view=None)
    
    @discord.ui.button(label="⬅️ Back", style=discord.ButtonStyle.secondary, custom_id="back", row=2)
    async def go_back(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = PlanEditView(self.ctx)
        embed = create_info_embed("📋 Plan Editor", "Select a plan to edit from the dropdown below.")
        await interaction.response.edit_message(embed=embed, view=view)
    
    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, custom_id="cancel", row=2)
    async def cancel_edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = create_info_embed("Plan Editor Closed", "No changes were saved.")
        await interaction.response.edit_message(embed=embed, view=None)

class EditPlanModal(discord.ui.Modal, title="Edit Plan Value"):
    def __init__(self, parent_view, field, prompt):
        super().__init__()
        self.parent_view = parent_view
        self.field = field
        
        self.value_input = discord.ui.TextInput(
            label=prompt,
            placeholder="Enter new value...",
            required=True,
            min_length=1,
            max_length=3
        )
        self.add_item(self.value_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_value = int(self.value_input.value)
            if new_value <= 0:
                await interaction.response.send_message("Value must be positive!", ephemeral=True)
                return
            
            if self.field == "ram":
                self.parent_view.plan['ram'] = new_value
            elif self.field == "cpu":
                self.parent_view.plan['cpu'] = new_value
            elif self.field == "disk":
                self.parent_view.plan['disk'] = new_value
            elif self.field == "req":
                if 'invites' in self.parent_view.plan:
                    self.parent_view.plan['invites'] = new_value
                else:
                    self.parent_view.plan['boosts'] = new_value
            
            embed = discord.Embed(
                title=f"✏️ Editing Plan: {self.parent_view.plan['name']}",
                description=f"Updated configuration:",
                color=0x00ccff
            )
            embed.add_field(name="RAM", value=f"{self.parent_view.plan['ram']} GB", inline=True)
            embed.add_field(name="CPU", value=f"{self.parent_view.plan['cpu']} Cores", inline=True)
            embed.add_field(name="Disk", value=f"{self.parent_view.plan['disk']} GB", inline=True)
            embed.add_field(name="Requirement", value=f"{self.parent_view.plan.get('invites', self.parent_view.plan.get('boosts'))} {'Invites' if 'invites' in self.parent_view.plan else 'Boosts'}", inline=False)
            if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
                embed.set_thumbnail(url=THUMBNAIL)
            if BANNER and BANNER.startswith(('http://', 'https://')):
                embed.set_image(url=BANNER)
            embed.set_footer(text=FOOTER_TEXT)
            
            await interaction.response.edit_message(embed=embed, view=self.parent_view)
            
        except ValueError:
            await interaction.response.send_message("Please enter a valid number!", ephemeral=True)

# ============ ADMIN COMMANDS ============

@bot.command(name='admin-add')
@is_main_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def admin_add(ctx, user: discord.Member):
    """Add admin"""
    user_id = str(user.id)
    
    if user_id == str(MAIN_ADMIN_ID):
        await ctx.send(embed=create_error_embed("Already Admin", "This user is already the main admin!"))
        return
    
    if user_id in admin_data.get("admins", []):
        await ctx.send(embed=create_error_embed("Already Admin", f"{user.mention} is already an admin!"))
        return
    
    admin_data["admins"].append(user_id)
    save_admin_data()
    
    embed = discord.Embed(
        title="✅ Admin Added",
        description=f"{user.mention} has been added as an admin!",
        color=0x00ff88
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

@bot.command(name='admin-remove')
@is_main_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def admin_remove(ctx, user: discord.Member):
    """Remove admin"""
    user_id = str(user.id)
    
    if user_id == str(MAIN_ADMIN_ID):
        await ctx.send(embed=create_error_embed("Cannot Remove", "You cannot remove the main admin!"))
        return
    
    if user_id not in admin_data.get("admins", []):
        await ctx.send(embed=create_error_embed("Not Admin", f"{user.mention} is not an admin!"))
        return
    
    admin_data["admins"].remove(user_id)
    save_admin_data()
    
    embed = discord.Embed(
        title="✅ Admin Removed",
        description=f"{user.mention} has been removed as an admin!",
        color=0x00ff88
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

@bot.command(name='admin-list')
@is_main_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def admin_list(ctx):
    """List admins"""
    embed = discord.Embed(
        title="🛡️ Admin List",
        description="Current administrators of the system",
        color=0x00ccff
    )
    
    try:
        main_admin = await bot.fetch_user(int(MAIN_ADMIN_ID))
        embed.add_field(name="⌯⌲ Main Admin", value=f"{main_admin.mention}", inline=False)
    except:
        embed.add_field(name="⌯⌲ Main Admin", value=f"User ID: {MAIN_ADMIN_ID}", inline=False)
    
    if admin_data['admins']:
        admin_text = []
        for admin_id in admin_data['admins']:
            try:
                admin_user = await bot.fetch_user(int(admin_id))
                admin_text.append(f"• {admin_user.mention}")
            except:
                admin_text.append(f"• User ID: {admin_id}")
        
        embed.add_field(name="⌯⌲ Admins", value="\n".join(admin_text), inline=False)
    else:
        embed.add_field(name="⌯⌲ Admins", value="No additional admins", inline=False)
    
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

@bot.command(name='create')
@is_admin()
@commands.cooldown(1, 10, commands.BucketType.user)
async def create_vps(ctx, ram: int, cpu: int, disk: int, user: discord.Member):
    """Create VPS with OS selection"""
    if ram <= 0 or cpu <= 0 or disk <= 0:
        await ctx.send(embed=create_error_embed("Invalid Specs", "RAM, CPU, and Disk must be positive integers."))
        return
    
    embed = create_info_embed("VPS Creation", 
        f"Creating VPS for {user.mention}\n"
        f"**RAM:** {ram}GB\n"
        f"**CPU:** {cpu} Cores\n"
        f"**Disk:** {disk}GB\n\n"
        f"Select OS below.")
    
    view = OSSelectView(ram, cpu, disk, user, ctx)
    await ctx.send(embed=embed, view=view)

class OSSelectView(discord.ui.View):
    def __init__(self, ram: int, cpu: int, disk: int, user: discord.Member, ctx):
        super().__init__(timeout=300)
        self.ram = ram
        self.cpu = cpu
        self.disk = disk
        self.user = user
        self.ctx = ctx
        self.selected_os = None
        self.select = discord.ui.Select(
            placeholder="Select an OS for the VPS",
            options=[discord.SelectOption(label=o["label"], value=o["value"]) for o in OS_OPTIONS]
        )
        self.select.callback = self.select_os
        self.add_item(self.select)
        cancel_btn = discord.ui.Button(label="❌ Cancel", style=discord.ButtonStyle.danger, row=1)

        async def _cancel(interaction: discord.Interaction):
            if str(interaction.user.id) != str(ctx.author.id):
                await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the command author can cancel."), ephemeral=True)
                return
            await interaction.response.edit_message(embed=create_info_embed("Cancelled", "VPS creation cancelled."), view=None)

        cancel_btn.callback = _cancel
        self.add_item(cancel_btn)
    
    async def select_os(self, interaction: discord.Interaction):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the command author can select."), ephemeral=True)
            return
        
        self.selected_os = self.select.values[0]
        await interaction.response.defer()
        
        confirm_view = discord.ui.View()
        confirm_button = discord.ui.Button(label="✅ Confirm", style=discord.ButtonStyle.success, custom_id="confirm")
        cancel_button = discord.ui.Button(label="❌ Cancel", style=discord.ButtonStyle.danger, custom_id="cancel")
        
        async def confirm_callback(confirm_interaction):
            await self.create_vps(confirm_interaction, self.selected_os)
        
        async def cancel_callback(cancel_interaction):
            if str(cancel_interaction.user.id) != str(self.ctx.author.id):
                await cancel_interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the command author can cancel."), ephemeral=True)
                return
            await cancel_interaction.response.edit_message(embed=create_info_embed("Cancelled", "VPS creation cancelled."), view=None)
        
        confirm_button.callback = confirm_callback
        cancel_button.callback = cancel_callback
        
        confirm_view.add_item(confirm_button)
        confirm_view.add_item(cancel_button)
        
        embed = create_info_embed("Confirm VPS Creation", 
            f"**User:** {self.user.mention}\n"
            f"**OS:** {self.selected_os}\n"
            f"**RAM:** {self.ram}GB\n"
            f"**CPU:** {self.cpu} Cores\n"
            f"**Disk:** {self.disk}GB\n\n"
            f"Please confirm to proceed.")
        
        await interaction.edit_original_response(embed=embed, view=confirm_view)
    
    async def create_vps(self, interaction: discord.Interaction, os_version: str):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the command author can confirm."), ephemeral=True)
            return
        await interaction.response.defer()

        creating_embed = create_info_embed("Creating VPS", f"Deploying {os_version} VPS for {self.user.mention}...\nA fresh image can take a few minutes.")
        await interaction.edit_original_response(embed=creating_embed, view=None)

        try:
            vps_info = await deploy_vps_container(str(self.user.id), self.ram, self.cpu, self.disk, os_version, "Custom")
        except Exception as e:
            logger.error("VPS creation failed", exc_info=True)
            await interaction.followup.send(embed=create_error_embed("Creation Failed", f"Error: {str(e)[:1500]}"))
            return

        await grant_vps_role(self.ctx.guild, self.user)

        success_embed = discord.Embed(title="✅ VPS Created Successfully", color=0x00ff88)
        add_field(success_embed, "Owner", self.user.mention, True)
        add_field(success_embed, "VPS ID", f"#{vps_info['_number']}", True)
        add_field(success_embed, "Container", f"`{vps_info['container_name']}`", True)
        add_field(success_embed, "Resources", f"**RAM:** {self.ram}GB\n**CPU:** {self.cpu} Cores\n**Storage:** {self.disk}GB", False)
        add_field(success_embed, "OS", os_version, True)
        if not vps_info.get('_config_ok'):
            add_field(success_embed, "⚠️ Warning", "Docker-ready LXC config did not fully apply — check bot.log.", False)
        await interaction.followup.send(embed=success_embed)

        if not await send_vps_welcome_dm(self.user, vps_info):
            await self.ctx.send(embed=create_info_embed("Notification Failed", f"Couldn't send DM to {self.user.mention}. Please ensure DMs are enabled."))

@bot.command(name='delete-vps')
@is_admin()
@commands.cooldown(1, 10, commands.BucketType.user)
async def delete_vps(ctx, user: discord.Member, vps_number: int, *, reason: str = "No reason"):
    """Delete user's VPS"""
    user_id = str(user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or user doesn't have a VPS."))
        return
    
    vps = vps_data[user_id][vps_number - 1]
    container_name = vps["container_name"]
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM port_forwards WHERE vps_container = ?', (container_name,))
    conn.commit()
    conn.close()
    
    await ctx.send(embed=create_info_embed("Deleting VPS", f"Removing VPS #{vps_number}..."))
    
    try:
        await execute_lxc(f"lxc delete {container_name} --force")
        
        del vps_data[user_id][vps_number - 1]
        if not vps_data[user_id]:
            del vps_data[user_id]
            if ctx.guild:
                vps_role = await get_or_create_vps_role(ctx.guild)
                if vps_role and vps_role in user.roles:
                    try:
                        await user.remove_roles(vps_role, reason="No VPS ownership")
                    except discord.Forbidden:
                        logger.warning(f"Failed to remove VPS role from {user.name}")
        
        save_vps_data()
        
        embed = discord.Embed(
            title="✅ VPS Deleted Successfully",
            color=0x00ff88
        )
        embed.add_field(name="⌯⌲ Owner", value=user.mention, inline=True)
        embed.add_field(name="⌯⌲ VPS ID", value=f"#{vps_number}", inline=True)
        embed.add_field(name="⌯⌲ Container", value=f"`{container_name}`", inline=True)
        embed.add_field(name="⌯⌲ Reason", value=reason, inline=False)
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        
        await ctx.send(embed=embed)
    
    except Exception as e:
        await ctx.send(embed=create_error_embed("Deletion Failed", f"Error: {str(e)}"))

@bot.command(name='add-resources')
@is_admin()
@commands.cooldown(1, 10, commands.BucketType.user)
async def add_resources(ctx, container_name: str, ram: int = None, cpu: int = None, disk: int = None):
    """Add resources to VPS"""
    if ram is None and cpu is None and disk is None:
        await ctx.send(embed=create_error_embed("Missing Parameters", "Please specify at least one resource to add"))
        return
    
    found_vps = None
    user_id = None
    vps_index = None
    
    for uid, vps_list in vps_data.items():
        for i, vps in enumerate(vps_list):
            if vps['container_name'] == container_name:
                found_vps = vps
                user_id = uid
                vps_index = i
                break
        if found_vps:
            break
    
    if not found_vps:
        await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS found with ID: `{container_name}`"))
        return
    
    was_running = found_vps.get('status') == 'running' and not found_vps.get('suspended', False)
    disk_changed = disk is not None
    
    if was_running:
        await ctx.send(embed=create_info_embed("Stopping VPS", f"Stopping VPS `{container_name}` to apply resource changes..."))
        try:
            await execute_lxc(f"lxc stop {container_name}")
            found_vps['status'] = 'stopped'
            save_vps_data()
        except Exception as e:
            await ctx.send(embed=create_error_embed("Stop Failed", f"Error stopping VPS: {str(e)}"))
            return
    
    changes = []
    try:
        current_ram_gb = int(found_vps['ram'].replace('GB', ''))
        current_cpu = int(found_vps['cpu'])
        current_disk_gb = int(found_vps['storage'].replace('GB', ''))
        
        new_ram_gb = current_ram_gb
        new_cpu = current_cpu
        new_disk_gb = current_disk_gb
        
        if ram is not None and ram > 0:
            new_ram_gb += ram
            ram_mb = new_ram_gb * 1024
            await execute_lxc(f"lxc config set {container_name} limits.memory {ram_mb}MB")
            changes.append(f"RAM: +{ram}GB (New total: {new_ram_gb}GB)")
        
        if cpu is not None and cpu > 0:
            new_cpu += cpu
            await execute_lxc(f"lxc config set {container_name} limits.cpu {new_cpu}")
            changes.append(f"CPU: +{cpu} cores (New total: {new_cpu} cores)")
        
        if disk is not None and disk > 0:
            new_disk_gb += disk
            await execute_lxc(f"lxc config device set {container_name} root size={new_disk_gb}GB")
            changes.append(f"Disk: +{disk}GB (New total: {new_disk_gb}GB)")
        
        found_vps['ram'] = f"{new_ram_gb}GB"
        found_vps['cpu'] = str(new_cpu)
        found_vps['storage'] = f"{new_disk_gb}GB"
        found_vps['config'] = f"{new_ram_gb}GB RAM / {new_cpu} CPU / {new_disk_gb}GB Disk"
        vps_data[user_id][vps_index] = found_vps
        save_vps_data()
        
        if was_running:
            await execute_lxc(f"lxc start {container_name}")
            found_vps['status'] = 'running'
            save_vps_data()
            await apply_internal_permissions(container_name)
        
        embed = discord.Embed(
            title="✅ Resources Added",
            description=f"Successfully added resources to VPS `{container_name}`",
            color=0x00ff88
        )
        embed.add_field(name="⌯⌲ Changes Applied", value="\n".join(changes), inline=False)
        if disk_changed:
            embed.add_field(name="⌯⌲ Disk Note", value="Run `sudo resize2fs /` inside the VPS to expand the filesystem.", inline=False)
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        
        await ctx.send(embed=embed)
    
    except Exception as e:
        await ctx.send(embed=create_error_embed("Resource Addition Failed", f"Error: {str(e)}"))

@bot.command(name='resize-vps')
@is_admin()
@commands.cooldown(1, 10, commands.BucketType.user)
async def resize_vps(ctx, container_name: str, ram: int = None, cpu: int = None, disk: int = None):
    """Resize VPS resources"""
    await add_resources(ctx, container_name, ram, cpu, disk)

@bot.command(name='suspend-vps')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def suspend_vps(ctx, container_name: str, *, reason: str = "Admin action"):
    """Suspend VPS"""
    found = False
    for uid, lst in vps_data.items():
        for vps in lst:
            if vps['container_name'] == container_name:
                if vps.get('status') != 'running':
                    await ctx.send(embed=create_error_embed("Cannot Suspend", "VPS must be running to suspend."))
                    return
                try:
                    await execute_lxc(f"lxc stop {container_name}")
                    vps['status'] = 'stopped'
                    vps['suspended'] = True
                    vps['suspended_reason'] = reason
                    if 'suspension_history' not in vps:
                        vps['suspension_history'] = []
                    vps['suspension_history'].append({
                        'time': datetime.now().isoformat(),
                        'reason': reason,
                        'by': f"{ctx.author.name}"
                    })
                    save_vps_data()
                    log_suspension(container_name, uid, 'suspend', reason, str(ctx.author.id))
                except Exception as e:
                    await ctx.send(embed=create_error_embed("Suspend Failed", str(e)))
                    return
                
                embed = discord.Embed(
                    title="⏸ VPS Suspended",
                    description=f"VPS `{container_name}` suspended.",
                    color=0xffaa00
                )
                embed.add_field(name="⌯⌲ Reason", value=reason, inline=False)
                if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
                    embed.set_thumbnail(url=THUMBNAIL)
                if BANNER and BANNER.startswith(('http://', 'https://')):
                    embed.set_image(url=BANNER)
                embed.set_footer(text=FOOTER_TEXT)
                await ctx.send(embed=embed)
                found = True
                break
        if found:
            break
    
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))

@bot.command(name='unsuspend-vps')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def unsuspend_vps(ctx, container_name: str):
    """Unsuspend VPS"""
    found = False
    for uid, lst in vps_data.items():
        for vps in lst:
            if vps['container_name'] == container_name:
                if not vps.get('suspended', False):
                    await ctx.send(embed=create_error_embed("Not Suspended", "VPS is not suspended."))
                    return
                try:
                    vps['suspended'] = False
                    vps['suspended_reason'] = ''
                    vps['status'] = 'running'
                    await execute_lxc(f"lxc start {container_name}")
                    await apply_internal_permissions(container_name)
                    save_vps_data()
                    log_suspension(container_name, uid, 'unsuspend', '', str(ctx.author.id))
                    
                    embed = discord.Embed(
                        title="▶ VPS Unsuspended",
                        description=f"VPS `{container_name}` unsuspended and started.",
                        color=0x00ff88
                    )
                    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
                        embed.set_thumbnail(url=THUMBNAIL)
                    if BANNER and BANNER.startswith(('http://', 'https://')):
                        embed.set_image(url=BANNER)
                    embed.set_footer(text=FOOTER_TEXT)
                    await ctx.send(embed=embed)
                    found = True
                except Exception as e:
                    await ctx.send(embed=create_error_embed("Start Failed", str(e)))
                break
        if found:
            break
    
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))

@bot.command(name='suspension-logs')
@is_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def suspension_logs(ctx, container_name: str = None):
    """View suspension logs"""
    logs = get_suspension_logs(container_name)
    
    if not logs:
        await ctx.send(embed=create_info_embed("Suspension Logs", "No logs found."))
        return
    
    log_text = ""
    for log in logs[:10]:
        log_text += f"**{log['action']}** - {log['container_name']}\n"
        log_text += f"Time: {log['created_at'][:19]}\n"
        if log['reason']:
            log_text += f"Reason: {log['reason']}\n"
        log_text += "\n"
    
    embed = discord.Embed(
        title="📋 Suspension Logs",
        description=log_text,
        color=0x00ccff
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

@bot.command(name='whitelist-vps')
@is_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def whitelist_vps(ctx, container_name: str, action: str):
    """Whitelist VPS from auto-suspend"""
    action = action.lower()
    if action not in ['add', 'remove']:
        await ctx.send(embed=create_error_embed("Invalid Action", "Use `add` or `remove`."))
        return
    
    found = False
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            if vps['container_name'] == container_name:
                if action == 'add':
                    vps['whitelisted'] = True
                    msg = "added to whitelist (exempt from auto-suspension)"
                else:
                    vps['whitelisted'] = False
                    msg = "removed from whitelist"
                save_vps_data()
                
                embed = discord.Embed(
                    title="✅ Whitelist Updated",
                    description=f"VPS `{container_name}` {msg}.",
                    color=0x00ff88
                )
                if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
                    embed.set_thumbnail(url=THUMBNAIL)
                if BANNER and BANNER.startswith(('http://', 'https://')):
                    embed.set_image(url=BANNER)
                embed.set_footer(text=FOOTER_TEXT)
                await ctx.send(embed=embed)
                found = True
                break
        if found:
            break
    
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))

@bot.command(name='userinfo')
@is_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def user_info(ctx, user: discord.Member):
    """User information"""
    user_id = str(user.id)
    vps_list = vps_data.get(user_id, [])
    
    embed = discord.Embed(
        title=f"User Information - {user.name}",
        description=f"Detailed information for {user.mention}",
        color=0x1a1a1a
    )
    
    user_details = f"**Name:** {user.name}\n"
    user_details += f"**ID:** {user.id}\n"
    user_details += f"**Joined:** {user.joined_at.strftime('%Y-%m-%d %H:%M:%S') if user.joined_at else 'Unknown'}"
    
    embed.add_field(name="⌯⌲ User Details", value=user_details, inline=False)
    
    if vps_list:
        vps_info = []
        for i, vps in enumerate(vps_list):
            status_emoji = "🟢" if vps.get('status') == 'running' and not vps.get('suspended', False) else "🟡" if vps.get('suspended', False) else "🔴"
            vps_info.append(f"{status_emoji} VPS {i+1}: `{vps['container_name']}` - {vps.get('status', 'unknown').upper()}")
        
        embed.add_field(name="⌯⌲ VPS List", value="\n".join(vps_info), inline=False)
    else:
        embed.add_field(name="⌯⌲ VPS Information", value="**No VPS owned**", inline=False)
    
    port_quota = get_user_allocation(user_id)
    port_used = get_user_used_ports(user_id)
    embed.add_field(name="⌯⌲ Port Quota", value=f"Allocated: {port_quota}, Used: {port_used}", inline=False)
    
    is_admin_user = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    embed.add_field(name="⌯⌲ Admin Status", value=f"**{'Yes' if is_admin_user else 'No'}**", inline=False)
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    
    await ctx.send(embed=embed)

@bot.command(name='list-all')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def list_all_vps(ctx):
    """List all VPS"""
    total_vps = sum(len(v) for v in vps_data.values())
    total_users = len(vps_data)
    
    embed = discord.Embed(
        title="All VPS Information",
        description=f"**Total Users:** {total_users}\n**Total VPS:** {total_vps}",
        color=0x1a1a1a
    )
    
    vps_info = []
    for user_id, vps_list in vps_data.items():
        try:
            user = await bot.fetch_user(int(user_id))
            for i, vps in enumerate(vps_list):
                status_emoji = "🟢" if vps.get('status') == 'running' and not vps.get('suspended', False) else "🟡" if vps.get('suspended', False) else "🔴"
                status_text = vps.get('status', 'unknown').upper()
                if vps.get('suspended', False):
                    status_text += " (SUSPENDED)"
                if vps.get('whitelisted', False):
                    status_text += " (WHITELISTED)"
                vps_info.append(f"{status_emoji} **{user.name}** - VPS {i+1}: `{vps['container_name']}` - {status_text}")
        except:
            vps_info.append(f"❓ Unknown User ({user_id}) - {len(vps_list)} VPS")
    
    if vps_info:
        vps_text = "\n".join(vps_info[:15])
        embed.add_field(name="⌯⌲ VPS List", value=vps_text, inline=False)
        if len(vps_info) > 15:
            embed.add_field(name="⌯⌲ Note", value=f"Showing 15 of {len(vps_info)} VPS", inline=False)
    
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

@bot.command(name='exec')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def execute_command(ctx, container_name: str, *, command: str):
    """Execute command in VPS"""
    await ctx.send(embed=create_info_embed("Executing Command", f"Running command in VPS `{container_name}`..."))
    
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "bash", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode() if stdout else "No output"
        error = stderr.decode() if stderr else ""
        
        embed = discord.Embed(
            title=f"Command Output - {container_name}",
            description=f"Command: `{command}`",
            color=0x1a1a1a
        )
        
        if output.strip():
            if len(output) > 1000:
                output = output[:1000] + "\n... (truncated)"
            embed.add_field(name="⌯⌲ Output", value=f"```\n{output}\n```", inline=False)
        
        if error.strip():
            if len(error) > 1000:
                error = error[:1000] + "\n... (truncated)"
            embed.add_field(name="⌯⌲ Error", value=f"```\n{error}\n```", inline=False)
        
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)
    
    except Exception as e:
        await ctx.send(embed=create_error_embed("Execution Failed", f"Error: {str(e)}"))

@bot.command(name='stop-vps-all')
@is_admin()
@commands.cooldown(1, 30, commands.BucketType.user)
async def stop_all_vps(ctx):
    """Stop all VPS"""
    embed = discord.Embed(
        title="⚠️ Stopping All VPS",
        description="This will stop ALL running VPS on the server.\n\nThis action cannot be undone. Continue?",
        color=0xffaa00
    )
    
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
        
        @discord.ui.button(label="Stop All VPS", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.defer()
            try:
                await execute_lxc("lxc stop --all --force")
                
                stopped_count = 0
                for user_id, vps_list in vps_data.items():
                    for vps in vps_list:
                        if vps.get('status') == 'running':
                            vps['status'] = 'stopped'
                            stopped_count += 1
                
                save_vps_data()
                embed = discord.Embed(
                    title="✅ All VPS Stopped",
                    description=f"Successfully stopped {stopped_count} VPS",
                    color=0x00ff88
                )
                if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
                    embed.set_thumbnail(url=THUMBNAIL)
                if BANNER and BANNER.startswith(('http://', 'https://')):
                    embed.set_image(url=BANNER)
                embed.set_footer(text=FOOTER_TEXT)
                await interaction.followup.send(embed=embed)
            except Exception as e:
                embed = create_error_embed("Stop Failed", str(e))
                await interaction.followup.send(embed=embed)
        
        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, item: discord.ui.Button):
            embed = discord.Embed(
                title="Operation Cancelled",
                description="The stop all VPS operation has been cancelled.",
                color=0x00ccff
            )
            if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
                embed.set_thumbnail(url=THUMBNAIL)
            if BANNER and BANNER.startswith(('http://', 'https://')):
                embed.set_image(url=BANNER)
            embed.set_footer(text=FOOTER_TEXT)
            await interaction.response.edit_message(embed=embed, view=None)
    
    await ctx.send(embed=embed, view=ConfirmView())

@bot.command(name='migrate-vps')
@is_admin()
@commands.cooldown(1, 30, commands.BucketType.user)
async def migrate_vps(ctx, container_name: str, pool: str):
    """Migrate VPS to another storage pool"""
    await ctx.send(embed=create_info_embed("Migrating VPS", f"Migrating `{container_name}` to pool `{pool}`..."))
    
    try:
        await execute_lxc(f"lxc move {container_name} -s {pool}")
        embed = discord.Embed(
            title="✅ VPS Migrated",
            description=f"VPS `{container_name}` migrated to pool `{pool}`",
            color=0x00ff88
        )
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Migration Failed", f"Error: {str(e)}"))

@bot.command(name='vps-network')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def vps_network(ctx, container_name: str, action: str, value: str = None):
    """Network management for VPS"""
    actions = ['list', 'limit', 'add', 'remove']
    
    if action not in actions:
        await ctx.send(embed=create_error_embed("Invalid Action", f"Use: {', '.join(actions)}"))
        return
    
    try:
        if action == 'list':
            proc = await asyncio.create_subprocess_exec(
                "lxc", "exec", container_name, "--", "ip", "addr",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                output = stdout.decode()
                if len(output) > 1900:
                    output = output[:1900] + "..."
                embed = discord.Embed(
                    title=f"Network - {container_name}",
                    description=f"```\n{output}\n```",
                    color=0x00ccff
                )
            else:
                embed = create_error_embed("Error", f"Failed to list network interfaces")
        elif action == 'limit' and value:
            await execute_lxc(f"lxc config device set {container_name} eth0 limits.egress {value}")
            await execute_lxc(f"lxc config device set {container_name} eth0 limits.ingress {value}")
            embed = discord.Embed(
                title="✅ Network Limit Set",
                description=f"Set network limit to {value} for `{container_name}`",
                color=0x00ff88
            )
        else:
            embed = create_error_embed("Invalid Command", "Usage: .vps-network <container> <list|limit> [value]")
        
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Network Operation Failed", str(e)))

@bot.command(name='apply-permissions')
@is_admin()
@commands.cooldown(1, 10, commands.BucketType.user)
async def apply_permissions(ctx, container_name: str):
    """Apply Docker-ready permissions to VPS"""
    await ctx.send(embed=create_info_embed("Applying Permissions", f"Applying Docker-ready permissions to `{container_name}`..."))
    
    try:
        await apply_lxc_config(container_name)
        await apply_internal_permissions(container_name)
        embed = discord.Embed(
            title="✅ Permissions Applied",
            description=f"Docker-ready permissions applied to `{container_name}`",
            color=0x00ff88
        )
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Failed", f"Error: {str(e)}"))

# ============ SYSTEM COMMANDS ============

@bot.command(name='thresholds')
@is_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def thresholds(ctx):
    """Show current resource thresholds"""
    embed = discord.Embed(
        title="📊 Resource Thresholds",
        description=f"**CPU:** {CPU_THRESHOLD}%\n**RAM:** {RAM_THRESHOLD}%",
        color=0x00ccff
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

@bot.command(name='set-threshold')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_threshold(ctx, cpu: int, ram: int):
    """Set resource thresholds"""
    global CPU_THRESHOLD, RAM_THRESHOLD
    
    if cpu < 0 or ram < 0:
        await ctx.send(embed=create_error_embed("Invalid Thresholds", "Thresholds must be non-negative."))
        return
    
    CPU_THRESHOLD = cpu
    RAM_THRESHOLD = ram
    set_setting('cpu_threshold', str(cpu))
    set_setting('ram_threshold', str(ram))
    
    embed = discord.Embed(
        title="✅ Thresholds Updated",
        description=f"**CPU:** {cpu}%\n**RAM:** {ram}%",
        color=0x00ff88
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

@bot.command(name='serverstats')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def server_stats(ctx):
    """Server statistics"""
    total_containers = sum(len(v) for v in vps_data.values())
    running_containers = 0
    suspended_containers = 0
    
    for vps_list in vps_data.values():
        for vps in vps_list:
            if vps.get('status') == 'running':
                running_containers += 1
            if vps.get('suspended', False):
                suspended_containers += 1
    
    latency = round(bot.latency * 1000)
    
    embed = discord.Embed(
        title="📊 Server Statistics",
        color=0x00ccff
    )
    
    host_info = f"**Bot Latency:** {latency}ms\n"
    host_info += f"**Prefix:** `{PREFIX}`"
    
    vps_info = f"**Total VPS:** {total_containers}\n"
    vps_info += f"**Running:** {running_containers}\n"
    vps_info += f"**Suspended:** {suspended_containers}\n"
    vps_info += f"**Total Users:** {len(vps_data)}"
    
    embed.add_field(name="⌯⌲ Host Information", value=host_info, inline=True)
    embed.add_field(name="⌯⌲ VPS Overview", value=vps_info, inline=True)
    
    uptime = get_uptime()
    embed.add_field(name="⌯⌲ System Uptime", value=uptime, inline=False)
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    
    await ctx.send(embed=embed)

@bot.command(name='lxc-list')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def lxc_list(ctx):
    """List all LXC containers"""
    try:
        result = await execute_lxc("lxc list")
        embed = discord.Embed(
            title="📋 LXC Containers List",
            description=f"```\n{result}\n```",
            color=0x00ccff
        )
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Error", str(e)))

@bot.command(name='set-status')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_status(ctx, activity_type: str, *, name: str):
    """Set bot status"""
    types = {
        'playing': discord.ActivityType.playing,
        'watching': discord.ActivityType.watching,
        'listening': discord.ActivityType.listening,
    }
    
    if activity_type.lower() not in types:
        await ctx.send(embed=create_error_embed("Invalid Type", "Valid types: playing, watching, listening"))
        return
    
    await bot.change_presence(activity=discord.Activity(type=types[activity_type.lower()], name=name))
    set_setting('bot_activity', activity_type.lower())
    set_setting('bot_activity_name', name)
    
    embed = discord.Embed(
        title="✅ Status Updated",
        description=f"Set to {activity_type}: {name}",
        color=0x00ff88
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

@bot.command(name='change-mode')
@is_main_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def change_mode(ctx, mode: str):
    """Change bot mode"""
    modes = {
        'online': discord.Status.online,
        'idle': discord.Status.idle,
        'dnd': discord.Status.dnd,
    }
    
    if mode.lower() not in modes:
        await ctx.send(embed=create_error_embed("Invalid Mode", "Valid modes: online, idle, dnd"))
        return
    
    await bot.change_presence(status=modes[mode.lower()])
    set_setting('bot_status', mode.lower())
    
    embed = discord.Embed(
        title="✅ Mode Changed",
        description=f"Bot mode set to {mode}",
        color=0x00ff88
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

@bot.command(name='maintenance')
@is_main_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def maintenance_mode(ctx, mode: str):
    """Toggle maintenance mode"""
    global MAINTENANCE_MODE, MAINTENANCE_STARTED_BY, MAINTENANCE_STARTED_AT
    
    mode = mode.lower()
    if mode not in ['on', 'off']:
        await ctx.send(embed=create_error_embed("Invalid Mode", "Please use `on` or `off`."))
        return
    
    if mode == 'on':
        MAINTENANCE_MODE = True
        MAINTENANCE_STARTED_BY = str(ctx.author.id)
        MAINTENANCE_STARTED_AT = datetime.now().isoformat()
        
        set_setting('maintenance_mode', 'true')
        set_setting('maintenance_started_by', str(ctx.author.id))
        set_setting('maintenance_started_at', MAINTENANCE_STARTED_AT)
        
        await bot.change_presence(status=discord.Status.idle, activity=discord.Game(name="🔧 Maintenance Mode"))
        
        embed = discord.Embed(
            title="🔧 Maintenance Mode Active",
            description="The bot is now in maintenance mode. Only administrators can use commands.",
            color=0xffaa00
        )
        embed.add_field(name="⌯⌲ Started By", value=ctx.author.mention, inline=True)
        embed.add_field(name="⌯⌲ Status", value="Commands disabled for non-admins", inline=True)
        
    else:
        MAINTENANCE_MODE = False
        MAINTENANCE_STARTED_BY = ''
        MAINTENANCE_STARTED_AT = ''
        
        set_setting('maintenance_mode', 'false')
        set_setting('maintenance_started_by', '')
        set_setting('maintenance_started_at', '')
        
        activity_types = {
            'playing': discord.ActivityType.playing,
            'watching': discord.ActivityType.watching,
            'listening': discord.ActivityType.listening,
        }
        activity_type = activity_types.get(BOT_ACTIVITY, discord.ActivityType.watching)
        status_types = {
            'online': discord.Status.online,
            'idle': discord.Status.idle,
            'dnd': discord.Status.dnd,
        }
        status = status_types.get(BOT_STATUS, discord.Status.online)
        
        await bot.change_presence(status=status, activity=discord.Activity(type=activity_type, name=BOT_ACTIVITY_NAME))
        
        embed = discord.Embed(
            title="✅ Maintenance Mode Deactivated",
            description="All commands are now available.",
            color=0x00ff88
        )
    
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

@bot.command(name='purge-data')
@is_main_admin()
@commands.cooldown(1, 60, commands.BucketType.user)
async def purge_data(ctx, user: discord.Member):
    """Purge all data for a user"""
    user_id = str(user.id)
    
    if user_id not in vps_data:
        await ctx.send(embed=create_error_embed("No Data", f"{user.mention} has no VPS data."))
        return
    
    embed = discord.Embed(
        title="⚠️ Purge Data",
        description=f"This will permanently delete ALL VPS data for {user.mention}.\n"
                    f"This action CANNOT be undone!\n\n"
                    f"**VPS Count:** {len(vps_data[user_id])}\n\n"
                    f"Type `{PREFIX}confirm-purge {user.id}` to proceed.",
        color=0xffaa00
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

@bot.command(name='confirm-purge')
@is_main_admin()
@commands.cooldown(1, 60, commands.BucketType.user)
async def confirm_purge(ctx, user_id: str):
    """Confirm purge of user data"""
    try:
        user = await bot.fetch_user(int(user_id))
    except:
        user = None
    
    if user_id not in vps_data:
        await ctx.send(embed=create_error_embed("No Data", f"User ID {user_id} has no VPS data."))
        return
    
    deleted_count = 0
    for vps in vps_data[user_id][:]:
        try:
            await execute_lxc(f"lxc delete {vps['container_name']} --force")
            deleted_count += 1
        except:
            pass
    
    del vps_data[user_id]
    save_vps_data()
    
    embed = discord.Embed(
        title="✅ Data Purged",
        description=f"Successfully purged data for {user.mention if user else f'User {user_id}'}\n"
                    f"Deleted {deleted_count} VPS containers.",
        color=0x00ff88
    )
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

# ============ PORT FORWARDING COMMANDS ============

@bot.command(name='ports')
@commands.cooldown(1, 2, commands.BucketType.user)
async def ports_command(ctx, subcmd: str = None, *args):
    """Manage port forwarding"""
    if not await maintenance_check(ctx):
        return
    
    user_id = str(ctx.author.id)
    allocated = get_user_allocation(user_id)
    used = get_user_used_ports(user_id)
    available = allocated - used
    
    if subcmd is None:
        embed = discord.Embed(
            title="🔌 Port Forwarding Help",
            description=f"**Your Quota:** Allocated: {allocated}, Used: {used}, Available: {available}",
            color=0x00ccff
        )
        embed.add_field(name="⌯⌲ Commands", 
                       value=f"{PREFIX}ports add <vps_num> <vps_port>\n{PREFIX}ports list\n{PREFIX}ports remove <id>", 
                       inline=False)
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)
        return
    
    if subcmd == 'add':
        if len(args) < 2:
            await ctx.send(embed=create_error_embed("Usage", f"Usage: {PREFIX}ports add <vps_number> <vps_port>"))
            return
        
        try:
            vps_num = int(args[0])
            vps_port = int(args[1])
            if vps_port < 1 or vps_port > 65535:
                raise ValueError
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid Input", "VPS number and port must be positive integers (port: 1-65535)."))
            return
        
        vps_list = vps_data.get(user_id, [])
        if vps_num < 1 or vps_num > len(vps_list):
            await ctx.send(embed=create_error_embed("Invalid VPS", f"Invalid VPS number (1-{len(vps_list)}). Use {PREFIX}myvps to list."))
            return
        
        vps = vps_list[vps_num - 1]
        container = vps['container_name']
        
        if used >= allocated:
            await ctx.send(embed=create_error_embed("Quota Exceeded", f"No available slots. Allocated: {allocated}, Used: {used}. Contact admin for more."))
            return
        
        host_port = await create_port_forward(user_id, container, vps_port)
        if host_port:
            embed = discord.Embed(
                title="✅ Port Forward Created",
                description=f"VPS #{vps_num} port {vps_port} (TCP/UDP) forwarded to host port {host_port}.",
                color=0x00ff88
            )
            embed.add_field(name="⌯⌲ Access", value=f"External: {YOUR_SERVER_IP}:{host_port} → VPS:{vps_port} (TCP & UDP)", inline=False)
            embed.add_field(name="⌯⌲ Quota Update", value=f"Used: {used + 1}/{allocated}", inline=False)
            if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
                embed.set_thumbnail(url=THUMBNAIL)
            if BANNER and BANNER.startswith(('http://', 'https://')):
                embed.set_image(url=BANNER)
            embed.set_footer(text=FOOTER_TEXT)
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=create_error_embed("Failed", "Could not assign host port. Try again later."))
    
    elif subcmd == 'list':
        forwards = get_user_forwards(user_id)
        embed = discord.Embed(
            title="Your Port Forwards",
            description=f"**Quota:** Allocated: {allocated}, Used: {used}, Available: {available}",
            color=0x00ccff
        )
        
        if not forwards:
            embed.add_field(name="⌯⌲ Forwards", value="No active port forwards.", inline=False)
        else:
            text = []
            for f in forwards:
                vps_num = next((i+1 for i, v in enumerate(vps_data.get(user_id, [])) if v['container_name'] == f['vps_container']), 'Unknown')
                created = datetime.fromisoformat(f['created_at']).strftime('%Y-%m-%d %H:%M')
                text.append(f"**ID {f['id']}** - VPS #{vps_num}: {f['vps_port']} (TCP/UDP) → {f['host_port']} (Created: {created})")
            
            embed.add_field(name="⌯⌲ Active Forwards", value="\n".join(text[:10]), inline=False)
            if len(forwards) > 10:
                embed.add_field(name="⌯⌲ Note", value=f"Showing 10 of {len(forwards)}. Remove unused with {PREFIX}ports remove <id>.", inline=False)
        
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)
    
    elif subcmd == 'remove':
        if len(args) < 1:
            await ctx.send(embed=create_error_embed("Usage", f"Usage: {PREFIX}ports remove <forward_id>"))
            return
        
        try:
            fid = int(args[0])
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid ID", "Forward ID must be an integer."))
            return
        
        success, _ = await remove_port_forward(fid)
        if success:
            embed = discord.Embed(
                title="✅ Removed",
                description=f"Port forward {fid} removed (TCP & UDP).",
                color=0x00ff88
            )
            embed.add_field(name="⌯⌲ Quota Update", value=f"Used: {used - 1}/{allocated}", inline=False)
            if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
                embed.set_thumbnail(url=THUMBNAIL)
            if BANNER and BANNER.startswith(('http://', 'https://')):
                embed.set_image(url=BANNER)
            embed.set_footer(text=FOOTER_TEXT)
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=create_error_embed("Not Found", "Forward ID not found. Use .ports list."))
    
    else:
        await ctx.send(embed=create_error_embed("Invalid Subcommand", f"Use: add <vps_num> <port>, list, remove <id>"))

@bot.command(name='ports-add-user')
@is_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def ports_add_user(ctx, amount: int, user: discord.Member):
    """Allocate port slots to a user"""
    if amount <= 0:
        await ctx.send(embed=create_error_embed("Invalid Amount", "Amount must be a positive integer."))
        return
    
    user_id = str(user.id)
    allocate_ports(user_id, amount)
    
    embed = discord.Embed(
        title="✅ Ports Allocated",
        description=f"Allocated {amount} port slots to {user.mention}.",
        color=0x00ff88
    )
    embed.add_field(name="⌯⌲ Quota", value=f"Total: {get_user_allocation(user_id)} slots", inline=False)
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)
    
    try:
        dm_embed = discord.Embed(
            title="Port Slots Allocated",
            description=f"You have been granted {amount} additional port forwarding slots by an admin.\nUse `{PREFIX}ports list` to view your quota and active forwards.",
            color=0x00ccff
        )
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            dm_embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            dm_embed.set_image(url=BANNER)
        dm_embed.set_footer(text=FOOTER_TEXT)
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("DM Failed", f"Could not notify {user.mention} via DM."))

@bot.command(name='ports-remove-user')
@is_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def ports_remove_user(ctx, amount: int, user: discord.Member):
    """Deallocate port slots from a user"""
    if amount <= 0:
        await ctx.send(embed=create_error_embed("Invalid Amount", "Amount must be a positive integer."))
        return
    
    user_id = str(user.id)
    current = get_user_allocation(user_id)
    if amount > current:
        amount = current
    
    deallocate_ports(user_id, amount)
    remaining = get_user_allocation(user_id)
    
    embed = discord.Embed(
        title="✅ Ports Deallocated",
        description=f"Removed {amount} port slots from {user.mention}.",
        color=0x00ff88
    )
    embed.add_field(name="⌯⌲ Remaining Quota", value=f"{remaining} slots", inline=False)
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=THUMBNAIL)
    if BANNER and BANNER.startswith(('http://', 'https://')):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)
    
    try:
        dm_embed = discord.Embed(
            title="Port Slots Reduced",
            description=f"Your port forwarding quota has been reduced by {amount} slots by an admin.\nRemaining: {remaining} slots.",
            color=0xffaa00
        )
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            dm_embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            dm_embed.set_image(url=BANNER)
        dm_embed.set_footer(text=FOOTER_TEXT)
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("DM Failed", f"Could not notify {user.mention} via DM."))

@bot.command(name='ports-revoke')
@is_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def ports_revoke(ctx, forward_id: int):
    """Revoke a port forward"""
    success, user_id = await remove_port_forward(forward_id, is_admin=True)
    if success and user_id:
        try:
            user = await bot.fetch_user(int(user_id))
            dm_embed = discord.Embed(
                title="Port Forward Revoked",
                description=f"One of your port forwards (ID: {forward_id}) has been revoked by an admin.",
                color=0xffaa00
            )
            if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
                dm_embed.set_thumbnail(url=THUMBNAIL)
            if BANNER and BANNER.startswith(('http://', 'https://')):
                dm_embed.set_image(url=BANNER)
            dm_embed.set_footer(text=FOOTER_TEXT)
            await user.send(embed=dm_embed)
        except:
            pass
        embed = discord.Embed(
            title="✅ Revoked",
            description=f"Port forward ID {forward_id} revoked.",
            color=0x00ff88
        )
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)
    else:
        await ctx.send(embed=create_error_embed("Failed", "Port forward ID not found or removal failed."))

# ============ HELP SYSTEM ============

class HelpView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.current_category = "user"
        self.message = None
        
        self.select = discord.ui.Select(
            placeholder="Select a category...",
            options=[
                discord.SelectOption(label="👤 User Commands", value="user", description="Basic commands for all users"),
                discord.SelectOption(label="🖥️ VPS Management", value="vps", description="Manage your VPS containers"),
                discord.SelectOption(label="🔌 Port Forwarding", value="ports", description="Manage port forwards"),
                discord.SelectOption(label="🤖 Bot System Commands", value="bot_system", description="Bot economy and stats"),
                discord.SelectOption(label="⚙️ System Commands", value="system", description="Bot and system commands"),
                discord.SelectOption(label="🛡️ Admin Commands", value="admin", description="Administrator commands"),
                discord.SelectOption(label="👑 Main Admin Commands", value="main_admin", description="Main administrator commands"),
                discord.SelectOption(label="🎁 Rewards & Invites", value="rewards", description="Invite / boost free VPS plans"),
                discord.SelectOption(label="⛏️ Mining Guard", value="mining", description="Anti-mining detection & punishments"),
                discord.SelectOption(label="📡 Branding & Status", value="branding", description="Logo, channels, node dashboard"),
            ]
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)
        
        self.update_embed()
    
    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This help menu is not for you!", ephemeral=True)
            return
        
        self.current_category = interaction.data["values"][0]
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)
    
    def update_embed(self):
        colors = {
            "user": 0x3498db,
            "vps": 0x2ecc71,
            "ports": 0xe74c3c,
            "bot_system": 0x9b59b6,
            "system": 0xf39c12,
            "admin": 0xe67e22,
            "main_admin": 0xf1c40f
        }
        
        color = colors.get(self.current_category, 0x5865F2)
        
        if self.current_category == "user":
            embed = discord.Embed(
                title=f"📚 {BOT_NAME} Help - 👤 User Commands",
                description="Basic commands available to all users",
                color=color
            )
            commands = [
                f"**`{PREFIX}ping`** - Check bot latency",
                f"**`{PREFIX}uptime`** - Show host uptime",
                f"**`{PREFIX}plans`** - View free VPS plans",
                f"**`{PREFIX}freeplans`** - Free plans list",
                f"**`{PREFIX}myvps`** - List your VPS",
                f"**`{PREFIX}list`** - Detailed VPS list",
                f"**`{PREFIX}manage`** - Manage your VPS",
                f"**`{PREFIX}manage @user`** - Manage another user's VPS (Admin only)",
                f"**`{PREFIX}share-user @user <vps_number>`** - Share VPS access",
                f"**`{PREFIX}share-ruser @user <vps_number>`** - Revoke VPS access",
                f"**`{PREFIX}manage-shared @owner <vps_number>`** - Manage shared VPS"
            ]
            total = 11
            
        elif self.current_category == "vps":
            embed = discord.Embed(
                title=f"📚 {BOT_NAME} Help - 🖥️ VPS Management",
                description="Commands for managing your VPS",
                color=color
            )
            commands = [
                f"**`{PREFIX}myvps`** - List your VPS",
                f"**`{PREFIX}list`** - Detailed VPS list",
                f"**`{PREFIX}vpsinfo [container]`** - VPS information",
                f"**`{PREFIX}vps-stats <container>`** - VPS stats",
                f"**`{PREFIX}restart-vps <container>`** - Restart VPS (Admin)",
                f"**`{PREFIX}clone-vps <container> [new_name]`** - Clone VPS (Admin)",
                f"**`{PREFIX}snapshot <container> [snap_name]`** - Create snapshot (Admin)",
                f"**`{PREFIX}restore-backup <container> <snap_name>`** - Restore VPS (Admin)"
            ]
            total = 8
            
        elif self.current_category == "ports":
            embed = discord.Embed(
                title=f"📚 {BOT_NAME} Help - 🔌 Port Forwarding",
                description="Manage port forwarding for your VPS",
                color=color
            )
            commands = [
                f"**`{PREFIX}ports`** - Port forwarding help",
                f"**`{PREFIX}ports add <vps_num> <port>`** - Add port forward",
                f"**`{PREFIX}ports list`** - List your port forwards",
                f"**`{PREFIX}ports remove <id>`** - Remove port forward",
                f"**`{PREFIX}ports-add-user <amount> @user`** - Allocate ports (Admin)",
                f"**`{PREFIX}ports-remove-user <amount> @user`** - Deallocate ports (Admin)",
                f"**`{PREFIX}ports-revoke <id>`** - Revoke port forward (Admin)"
            ]
            total = 7
            
        elif self.current_category == "bot_system":
            embed = discord.Embed(
                title=f"📚 {BOT_NAME} Help - 🤖 Bot System Commands",
                description="Bot economy and statistics commands",
                color=color
            )
            commands = [
                f"**`{PREFIX}plans`** - View free VPS plans",
                f"**`{PREFIX}addinv @user <amount>`** - Add invites (Admin)",
                f"**`{PREFIX}removeinv @user <amount>`** - Remove invites (Admin)",
                f"**`{PREFIX}addboost @user <amount>`** - Add boosts (Admin)",
                f"**`{PREFIX}removeboost @user <amount>`** - Remove boosts (Admin)",
                f"**`{PREFIX}credits @user <amount>`** - Add credits (Admin)",
                f"**`{PREFIX}dailycredit`** - Claim daily credits",
                f"**`{PREFIX}edit-plans`** - Edit VPS plans (Main Admin)"
            ]
            total = 8
            
        elif self.current_category == "system":
            embed = discord.Embed(
                title=f"📚 {BOT_NAME} Help - ⚙️ System Commands",
                description="Bot and system management commands",
                color=color
            )
            commands = [
                f"**`{PREFIX}ping`** - Check bot latency",
                f"**`{PREFIX}uptime`** - Show host uptime",
                f"**`{PREFIX}serverstats`** - Server statistics (Admin)",
                f"**`{PREFIX}thresholds`** - View resource thresholds",
                f"**`{PREFIX}set-threshold <cpu> <ram>`** - Set thresholds (Admin)",
                f"**`{PREFIX}set-status <type> <name>`** - Set bot status (Admin)",
                f"**`{PREFIX}change-mode <mode>`** - Change bot mode (Main Admin)",
                f"**`{PREFIX}maintenance <on/off>`** - Maintenance mode (Main Admin)",
                f"**`{PREFIX}lxc-list`** - List all LXC containers (Admin)"
            ]
            total = 9
            
        elif self.current_category == "admin":
            embed = discord.Embed(
                title=f"📚 {BOT_NAME} Help - 🛡️ Admin Commands",
                description="Commands for server administrators",
                color=color
            )
            commands = [
                f"**`{PREFIX}create <ram> <cpu> <disk> @user`** - Create VPS",
                f"**`{PREFIX}delete-vps @user <vps_number> [reason]`** - Delete VPS",
                f"**`{PREFIX}add-resources <container> [ram] [cpu] [disk]`** - Add resources",
                f"**`{PREFIX}resize-vps <container> [ram] [cpu] [disk]`** - Resize VPS",
                f"**`{PREFIX}suspend-vps <container> [reason]`** - Suspend VPS",
                f"**`{PREFIX}unsuspend-vps <container>`** - Unsuspend VPS",
                f"**`{PREFIX}suspension-logs [container]`** - View suspension logs",
                f"**`{PREFIX}whitelist-vps <container> <add|remove>`** - Whitelist VPS",
                f"**`{PREFIX}userinfo @user`** - User information",
                f"**`{PREFIX}list-all`** - List all VPS",
                f"**`{PREFIX}exec <container> <command>`** - Execute command",
                f"**`{PREFIX}stop-vps-all`** - Stop all VPS",
                f"**`{PREFIX}restart-vps <container>`** - Restart VPS",
                f"**`{PREFIX}clone-vps <container> [new_name]`** - Clone VPS",
                f"**`{PREFIX}snapshot <container> [snap_name]`** - Create snapshot",
                f"**`{PREFIX}restore-backup <container> <snap_name>`** - Restore backup",
                f"**`{PREFIX}vpsinfo [container]`** - VPS information",
                f"**`{PREFIX}vps-stats <container>`** - VPS stats"
            ]
            total = 18
            
        elif self.current_category == "main_admin":
            embed = discord.Embed(
                title=f"📚 {BOT_NAME} Help - 👑 Main Admin Commands",
                description="Commands for the main administrator",
                color=color
            )
            commands = [
                f"**`{PREFIX}admin-add @user`** - Add admin",
                f"**`{PREFIX}admin-remove @user`** - Remove admin",
                f"**`{PREFIX}admin-list`** - List admins",
                f"**`{PREFIX}maintenance <on/off>`** - Maintenance mode",
                f"**`{PREFIX}set-status <type> <name>`** - Set bot status",
                f"**`{PREFIX}change-mode <mode>`** - Change bot mode",
                f"**`{PREFIX}edit-plans`** - Edit VPS plans",
                f"**`{PREFIX}purge-data @user`** - Purge user data",
                f"**`{PREFIX}confirm-purge <user_id>`** - Confirm purge"
            ]
            total = 9
            
        elif self.current_category == "rewards":
            embed = discord.Embed(title=f"📚 {BOT_NAME} Help - 🎁 Rewards & Invites",
                                  description="Earn a free VPS by inviting friends or boosting the server", color=0x1abc9c)
            commands = [
                f"**`{PREFIX}invites [@user]`** - Invite / boost progress",
                f"**`{PREFIX}plans`** - View free VPS plans",
                f"**`{PREFIX}claimfree`** - Claim & auto-deploy your VPS",
                f"**`{PREFIX}dailycredit`** - Claim daily credits",
                f"**`{PREFIX}syncboosts`** - Credit current boosters (Admin)",
                f"**`{PREFIX}resetclaim @user [type]`** - Reset a claim (Admin)",
                f"**`{PREFIX}freeclaims <on/off>`** - Pause free claims (Main Admin)"
            ]
            total = 7

        elif self.current_category == "mining":
            embed = discord.Embed(title=f"📚 {BOT_NAME} Help - ⛏️ Mining Guard",
                                  description="Automatic crypto-mining detection with strikes: kill → suspend → blacklist", color=0xe74c3c)
            commands = [
                f"**`{PREFIX}miningguard [on|off|status]`** - Control the guard (Admin)",
                f"**`{PREFIX}minerscan <container>`** - Manual scan, report only (Admin)",
                f"**`{PREFIX}minerlog [container]`** - Recent detections (Admin)",
                f"**`{PREFIX}strikes @user`** - View strikes (Admin)",
                f"**`{PREFIX}clearstrikes @user`** - Clear strikes + blacklist (Admin)",
                f"**`{PREFIX}blacklist <add|remove|list> [@user] [reason]`** - Free-claim blacklist (Admin)",
                f"**`{PREFIX}setalertchannel #channel`** - Alert channel (Admin)"
            ]
            total = 7

        elif self.current_category == "branding":
            embed = discord.Embed(title=f"📚 {BOT_NAME} Help - 📡 Branding & Status",
                                  description="Logo, banner and the live node dashboard", color=0x3498db)
            commands = [
                f"**`{PREFIX}setlogo <url>`** - Set logo (or attach an image) (Admin)",
                f"**`{PREFIX}setbanner <url|clear>`** - Set banner (Admin)",
                f"**`{PREFIX}setstatuschannel #channel`** - Live node dashboard (Admin)",
                f"**`{PREFIX}setnode <name>`** - Node display name (Admin)",
                f"**`{PREFIX}refreshstatus`** - Force dashboard refresh (Admin)",
                f"**`{PREFIX}branding`** - Show configuration & health (Admin)",
                f"**`{PREFIX}version`** - AVM version & changelog"
            ]
            total = 7

        else:
            embed = discord.Embed(
                title=f"📚 {BOT_NAME} Help",
                description="Select a category from the dropdown",
                color=color
            )
            commands = []
            total = 0
        
        embed.add_field(name="⌯⌲ Commands", value="\n".join(commands) if commands else "No commands available", inline=False)
        embed.add_field(name="⌯⌲ Navigation", 
                       value=f"• Use dropdown to switch categories\n• Total commands: {total}\n• Prefix: `{PREFIX}`", 
                       inline=False)
        if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=THUMBNAIL)
        if BANNER and BANNER.startswith(('http://', 'https://')):
            embed.set_image(url=BANNER)
        embed.set_footer(text=FOOTER_TEXT)
        
        self.embed = embed



# ============================================================
#              AXIONHOST PREMIUM MODULES (v5)
#   branding • invite/boost rewards • auto deploy • reinstall
#   mining guard • live node status dashboard
# ============================================================

BOT_START_TS = time.time()
deploy_lock = asyncio.Lock()


def is_staff(user_id) -> bool:
    uid = str(user_id)
    return uid == str(MAIN_ADMIN_ID) or uid in admin_data.get("admins", [])


def find_vps(container_name: str):
    """Return (owner_id, vps_dict) for a container name, or None."""
    for uid, lst in vps_data.items():
        for vps in lst:
            if vps['container_name'] == container_name:
                return uid, vps
    return None


def fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def make_bar(pct: Optional[float], length: int = 10) -> str:
    if pct is None:
        return "▱" * length
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round(pct / 100 * length))
    return "▰" * filled + "▱" * (length - filled)


def make_spark(values) -> str:
    chars = "▁▂▃▄▅▆▇█"
    vals = [v for v in values if v is not None]
    if not vals:
        return "▁" * 12
    return "".join(chars[min(len(chars) - 1, int(max(0.0, min(100.0, v)) / 100 * (len(chars) - 1) + 0.5))] for v in vals)


# ------------------------------------------------------------
# BRANDING  (.setlogo / .setbanner / .branding)
# ------------------------------------------------------------

branding_errors: Dict[str, str] = {}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def apply_branding(kind: str, url: str, ref: str = ''):
    global THUMBNAIL, BANNER
    if kind == 'logo':
        THUMBNAIL = url
        set_setting('logo_url', url)
        set_setting('logo_ref', ref)
    else:
        BANNER = url
        set_setting('banner_url', url)
        set_setting('banner_ref', ref)
    branding_errors.pop(kind, None)


async def validate_image_url(url: str) -> tuple[bool, str]:
    """Explicitly verify that a URL is reachable and really an image."""
    if not url.startswith(('http://', 'https://')):
        return False, "URL must start with http:// or https://"
    lowered = url.lower()
    if 'cdn.discordapp.com' in lowered or 'media.discordapp.net' in lowered:
        return False, ("Discord CDN links expire after ~24h. Attach the image to the command "
                       "instead (the bot will host it and keep it fresh), or use a permanent link "
                       "such as a GitHub raw URL.")
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return False, f"URL returned HTTP {resp.status}"
                ctype = resp.headers.get('Content-Type', '').lower()
                if not ctype.startswith('image/'):
                    return False, f"URL is not an image (Content-Type: {ctype or 'unknown'})"
                if resp.content_length and resp.content_length > MAX_IMAGE_BYTES:
                    return False, "Image is larger than 8 MB"
                return True, ctype
    except Exception as e:
        return False, f"Could not fetch URL: {type(e).__name__}: {e}"


async def refresh_branding_assets():
    """Re-fetch attachment URLs of bot-hosted logo/banner so they never expire."""
    for kind, key in (('logo', 'logo_ref'), ('banner', 'banner_ref')):
        ref = get_setting(key, '')
        if not ref:
            continue
        try:
            ch_id, msg_id = (int(x) for x in ref.split(':'))
            channel = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
            msg = await channel.fetch_message(msg_id)
            if not msg.attachments:
                raise RuntimeError("hosting message has no attachment")
            apply_branding(kind, msg.attachments[0].url, ref)
            logger.info(f"Refreshed {kind} URL from hosting message")
        except discord.NotFound:
            branding_errors[kind] = f"Hosting message deleted — re-run `{PREFIX}set{kind}` with the image attached."
            logger.error(f"{kind} hosting message was deleted")
        except Exception as e:
            branding_errors[kind] = f"Refresh failed: {type(e).__name__}: {e}"
            logger.error(f"Failed to refresh {kind}: {e}")


async def _handle_branding(ctx, kind: str, url: Optional[str]):
    label = "Logo" if kind == 'logo' else "Banner"
    cmd = f"{PREFIX}set{kind}"

    if url and url.lower() in ('clear', 'remove', 'none', 'off'):
        apply_branding(kind, '', '')
        await ctx.send(embed=create_success_embed(f"{label} Removed", f"{label} will no longer appear on embeds."))
        return

    new_url, ref = None, ''
    if ctx.message.attachments:
        att = ctx.message.attachments[0]
        if not (att.content_type or '').startswith('image/'):
            await ctx.send(embed=create_error_embed("Not An Image", "The attachment must be an image (png/jpg/webp/gif)."))
            return
        if att.size > MAX_IMAGE_BYTES:
            await ctx.send(embed=create_error_embed("Too Large", "Image must be 8 MB or smaller."))
            return
        try:
            host = await ctx.send(content=f"🖼️ {label} asset — **do not delete this message**, it keeps the {label.lower()} alive.",
                                  file=await att.to_file())
        except discord.Forbidden:
            await ctx.send(embed=create_error_embed("Missing Permission", "I need **Attach Files** in this channel to host the image."))
            return
        except Exception as e:
            await ctx.send(embed=create_error_embed("Upload Failed", f"{type(e).__name__}: {e}"))
            return
        new_url = host.attachments[0].url
        ref = f"{host.channel.id}:{host.id}"
    elif url:
        ok, info = await validate_image_url(url)
        if not ok:
            await ctx.send(embed=create_error_embed("Invalid Image URL", info))
            return
        new_url = url
    else:
        await ctx.send(embed=create_error_embed("Usage", f"`{cmd} <image url>` or attach an image to the message.\n`{cmd} clear` removes it."))
        return

    apply_branding(kind, new_url, ref)
    note = "Hosted by the bot — don't delete the image message above." if ref else "Using your direct link."
    await ctx.send(embed=create_success_embed(f"{label} Updated", f"New {label.lower()} is live on all embeds.\n{note}"))


@bot.command(name='setlogo')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_logo(ctx, url: str = None):
    """Set the brand logo (URL or attachment)"""
    await _handle_branding(ctx, 'logo', url)


@bot.command(name='setbanner')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_banner(ctx, url: str = None):
    """Set the embed banner (URL or attachment)"""
    await _handle_branding(ctx, 'banner', url)




@bot.command(name='setnode')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_node_name(ctx, *, name: str):
    """Set the node display name"""
    global NODE_NAME
    name = name.strip()[:40]
    if not name:
        await ctx.send(embed=create_error_embed("Invalid Name", "Node name cannot be empty."))
        return
    NODE_NAME = name
    set_setting('node_name', name)
    await ctx.send(embed=create_success_embed("Node Renamed", f"Node is now **{name}**. Dashboard updates on the next refresh."))


async def _channel_permission_gaps(channel, need_attach: bool = False) -> List[str]:
    me = channel.guild.me
    perms = channel.permissions_for(me)
    checks = [('View Channel', perms.view_channel), ('Send Messages', perms.send_messages),
              ('Embed Links', perms.embed_links), ('Read Message History', perms.read_message_history)]
    if need_attach:
        checks.append(('Attach Files', perms.attach_files))
    return [name for name, ok in checks if not ok]


@bot.command(name='setalertchannel')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_alert_channel(ctx, channel: discord.TextChannel):
    """Set the channel for mining alerts / punishments"""
    global ALERT_CHANNEL_ID
    gaps = await _channel_permission_gaps(channel)
    if gaps:
        await ctx.send(embed=create_error_embed("Missing Permissions", f"I'm missing in {channel.mention}: **{', '.join(gaps)}**"))
        return
    ALERT_CHANNEL_ID = channel.id
    set_setting('alert_channel_id', str(channel.id))
    try:
        await channel.send(embed=create_success_embed("Alert Channel Linked",
                           "Mining Guard alerts and punishments will be posted here."))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Test Message Failed", f"{type(e).__name__}: {e}"))
        return
    await ctx.send(embed=create_success_embed("Alert Channel Set", f"Alerts will go to {channel.mention}."))


@bot.command(name='setstatuschannel')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_status_channel(ctx, channel: discord.TextChannel):
    """Set the live node/status channel"""
    global STATUS_CHANNEL_ID
    gaps = await _channel_permission_gaps(channel)
    if gaps:
        await ctx.send(embed=create_error_embed("Missing Permissions", f"I'm missing in {channel.mention}: **{', '.join(gaps)}**"))
        return
    STATUS_CHANNEL_ID = channel.id
    set_setting('status_channel_id', str(channel.id))
    set_setting('status_message_id', '0')
    ok, info = await update_status_dashboard(force_new=True)
    if ok:
        await ctx.send(embed=create_success_embed("Status Channel Set", f"Live dashboard posted in {channel.mention}."))
    else:
        await ctx.send(embed=create_error_embed("Dashboard Failed", info))


@bot.command(name='refreshstatus')
@is_admin()
@commands.cooldown(1, 10, commands.BucketType.user)
async def refresh_status_cmd(ctx):
    """Force-refresh the status dashboard"""
    ok, info = await update_status_dashboard()
    if ok:
        await ctx.send(embed=create_success_embed("Dashboard Refreshed", "Status message updated."))
    else:
        await ctx.send(embed=create_error_embed("Dashboard Failed", info))


@bot.command(name='branding')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def branding_cmd(ctx):
    """Show branding / channel / feature configuration"""
    def ch(cid):
        return f"<#{cid}>" if cid else "❌ not set"

    embed = create_info_embed(f"{BOT_NAME} Configuration", "Current runtime configuration and health.")
    add_field(embed, "Logo", (THUMBNAIL if valid_url(THUMBNAIL) else "❌ not set") + (f"\n⚠️ {branding_errors['logo']}" if 'logo' in branding_errors else ""), False)
    add_field(embed, "Banner", (BANNER if valid_url(BANNER) else "not set") + (f"\n⚠️ {branding_errors['banner']}" if 'banner' in branding_errors else ""), False)
    add_field(embed, "Alert Channel", ch(ALERT_CHANNEL_ID), True)
    add_field(embed, "Status Channel", ch(STATUS_CHANNEL_ID), True)
    add_field(embed, "Node", NODE_NAME, True)
    add_field(embed, "Mining Guard", f"{'🟢 ON' if MINING_GUARD_ENABLED else '🔴 OFF'} • every {MINING_INTERVAL}s", True)
    add_field(embed, "Free Claims", "🟢 Enabled" if FREE_CLAIMS_ENABLED else "🔴 Disabled", True)
    add_field(embed, "Min Invite Account Age", f"{MIN_ACCOUNT_AGE_DAYS} days", True)
    inv_state = []
    for g in bot.guilds:
        if not tracked_guild(g):
            continue
        err = invite_errors.get(g.id)
        inv_state.append(f"{g.name}: " + (f"❌ {err}" if err else "✅ tracking"))
    add_field(embed, "Invite Tracking", "\n".join(inv_state) or "No guild", False)
    if status_state.get('last_error'):
        add_field(embed, "Dashboard Error", f"⚠️ {status_state['last_error']}", False)
    await ctx.send(embed=embed)


def valid_url(u: Optional[str]) -> bool:
    return bool(u) and str(u).startswith(('http://', 'https://'))


# ------------------------------------------------------------
# INVITE TRACKING + BOOST REWARDS
# ------------------------------------------------------------

invite_cache: Dict[int, Dict[str, Dict[str, Any]]] = {}
invite_errors: Dict[int, str] = {}
invite_locks: Dict[int, asyncio.Lock] = {}


def tracked_guild(guild) -> bool:
    return not GUILD_ID or guild.id == GUILD_ID


def _invite_snapshot(invites) -> Dict[str, Dict[str, Any]]:
    return {i.code: {'uses': i.uses or 0,
                     'inviter': i.inviter.id if i.inviter else None,
                     'max_uses': i.max_uses or 0} for i in invites}


async def cache_guild_invites(guild) -> bool:
    try:
        invite_cache[guild.id] = _invite_snapshot(await guild.invites())
        invite_errors.pop(guild.id, None)
        return True
    except discord.Forbidden:
        invite_errors[guild.id] = "Missing **Manage Server** permission"
        logger.error(f"Invite tracking disabled in {guild.name}: missing Manage Server permission")
    except Exception as e:
        invite_errors[guild.id] = f"{type(e).__name__}: {e}"
        logger.error(f"Could not read invites for {guild.name}: {e}")
    return False


def resolve_used_invite(old: Dict[str, Dict[str, Any]], new: Dict[str, Dict[str, Any]]) -> tuple[Optional[str], str]:
    """Work out which invite code a new member used. Returns (code, reason)."""
    increased = [c for c, d in new.items() if d['uses'] > old.get(c, {}).get('uses', 0)]
    if len(increased) == 1:
        return increased[0], 'ok'
    if len(increased) > 1:
        return None, 'ambiguous (several invites changed at once)'
    # Invite hit max uses and was deleted by Discord
    gone = [c for c, d in old.items() if c not in new and d.get('max_uses') and d['uses'] + 1 >= d['max_uses']]
    if len(gone) == 1:
        return gone[0], 'ok'
    if len(gone) > 1:
        return None, 'ambiguous (several single-use invites vanished)'
    return None, 'unknown (vanity URL, widget or discovery join)'


def get_join_row(invitee_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM invite_joins WHERE invitee_id = ?', (invitee_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def record_join(invitee_id: str, inviter_id: str, guild_id: str, code: str, counted: bool):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''INSERT INTO invite_joins (invitee_id, inviter_id, guild_id, invite_code, joined_at, counted, left_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(invitee_id) DO UPDATE SET inviter_id = excluded.inviter_id, guild_id = excluded.guild_id,
                       invite_code = excluded.invite_code, joined_at = excluded.joined_at,
                       counted = excluded.counted, left_at = NULL''',
                (invitee_id, inviter_id, guild_id, code, datetime.now().isoformat(), 1 if counted else 0))
    conn.commit()
    conn.close()


def mark_left(invitee_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE invite_joins SET left_at = ? WHERE invitee_id = ?', (datetime.now().isoformat(), invitee_id))
    conn.commit()
    conn.close()


def invite_breakdown(inviter_id: str) -> Dict[str, int]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''SELECT
        COALESCE(SUM(CASE WHEN counted = 1 AND left_at IS NULL THEN 1 ELSE 0 END), 0),
        COALESCE(SUM(CASE WHEN counted = 1 AND left_at IS NOT NULL THEN 1 ELSE 0 END), 0),
        COALESCE(SUM(CASE WHEN counted = 0 THEN 1 ELSE 0 END), 0)
        FROM invite_joins WHERE inviter_id = ?''', (inviter_id,))
    row = cur.fetchone()
    conn.close()
    return {'active': row[0], 'left': row[1], 'fake': row[2]}


def next_goal(stats: Dict[str, Any]):
    """Return the nearest unreached plan across invites/boosts as (plan, type, current, required) or None."""
    best = None
    for ptype in ('invites', 'boosts'):
        for plan in FREE_VPS_PLANS[ptype]:
            cur = stats.get(ptype, 0)
            if cur < plan[ptype]:
                gap = plan[ptype] - cur
                if best is None or gap < best[0]:
                    best = (gap, plan, ptype, cur, plan[ptype])
                break
    return best[1:] if best else None


async def _dm(user_id, embed) -> bool:
    try:
        user = await bot.fetch_user(int(user_id))
        await user.send(embed=embed)
        return True
    except Exception as e:
        logger.info(f"DM to {user_id} not delivered: {type(e).__name__}")
        return False












# ------------------------------------------------------------
# VPS DEPLOYMENT CORE (shared by .create, .claimfree and reinstall)
# ------------------------------------------------------------

def _next_container_name(user_id: str) -> tuple[str, int]:
    existing = {v['container_name'] for lst in vps_data.values() for v in lst}
    n = len(vps_data.get(user_id, [])) + 1
    while True:
        name = f"{BOT_NAME.lower()}-vps-{user_id}-{n}"
        if name not in existing:
            return name, n
        n += 1


async def _set_root_size(container: str, disk_gb: int):
    try:
        await execute_lxc(f"lxc config device set {container} root size={disk_gb}GB")
    except Exception as e:
        if "doesn't exist" in str(e).lower() or "does not exist" in str(e).lower():
            await execute_lxc(f"lxc config device override {container} root size={disk_gb}GB")
        else:
            raise


async def _provision_container(name: str, os_version: str, ram: int, cpu: int, disk: int) -> bool:
    """Create + configure + start a container. Returns whether the Docker-ready config applied cleanly."""
    await execute_lxc(f"lxc init {os_version} {name} -s {DEFAULT_STORAGE_POOL}", timeout=900)
    try:
        await execute_lxc(f"lxc config set {name} limits.memory {ram * 1024}MB")
        await execute_lxc(f"lxc config set {name} limits.cpu {cpu}")
        await _set_root_size(name, disk)
        config_ok = await apply_lxc_config(name)
        await execute_lxc(f"lxc start {name}", timeout=180)
    except Exception:
        try:
            await execute_lxc(f"lxc delete {name} --force", timeout=120)
        except Exception as cleanup_err:
            logger.error(f"Cleanup of half-created {name} failed: {cleanup_err}")
        raise
    await apply_internal_permissions(name)
    return config_ok


async def deploy_vps_container(owner_id: str, ram: int, cpu: int, disk: int, os_version: str,
                               plan_name: str = "Custom") -> Dict[str, Any]:
    async with deploy_lock:
        container_name, number = _next_container_name(owner_id)
        config_ok = await _provision_container(container_name, os_version, ram, cpu, disk)
        vps_info = {
            "container_name": container_name,
            "plan_name": plan_name,
            "ram": f"{ram}GB",
            "cpu": str(cpu),
            "storage": f"{disk}GB",
            "config": f"{ram}GB RAM / {cpu} CPU / {disk}GB Disk",
            "os_version": os_version,
            "status": "running",
            "suspended": False,
            "whitelisted": False,
            "suspended_reason": "",
            "suspension_history": [],
            "created_at": datetime.now().isoformat(),
            "shared_with": [],
            "id": None,
        }
        vps_data.setdefault(owner_id, []).append(vps_info)
        save_vps_data()
        vps_info['_number'] = number
        vps_info['_config_ok'] = config_ok
        return vps_info


async def grant_vps_role(guild, member):
    if not guild or not member:
        return
    role = await get_or_create_vps_role(guild)
    if role:
        try:
            await member.add_roles(role, reason=f"{BOT_NAME} VPS ownership granted")
        except discord.Forbidden:
            logger.warning(f"Could not assign VPS role to {member}: missing permission / role hierarchy")


async def send_vps_welcome_dm(user, vps_info: Dict[str, Any]) -> bool:
    embed = discord.Embed(title=f"{BRAND_ICON} {BOT_NAME} — Your VPS Is Ready", color=0x00ff88,
                          description="Your VPS has been deployed and is running.")
    details = (f"**VPS:** #{vps_info.get('_number', '?')} • `{vps_info['container_name']}`\n"
               f"**Plan:** {vps_info['plan_name']}\n"
               f"**Specs:** {vps_info['config']}\n"
               f"**OS:** {vps_info['os_version']}\n"
               f"**Created:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    embed.add_field(name="⌯⌲ Details", value=details, inline=False)
    embed.add_field(name="⌯⌲ Manage", value=f"Use `{PREFIX}manage` to start / stop / reinstall and get SSH access.", inline=False)
    embed.add_field(name="⌯⌲ Fair Use", value="Crypto-mining is **strictly prohibited** and is detected automatically. Violations are punished with kills, suspension and blacklisting.", inline=False)
    if valid_url(THUMBNAIL):
        embed.set_thumbnail(url=THUMBNAIL)
    if valid_url(BANNER):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    try:
        await user.send(embed=embed)
        return True
    except discord.Forbidden:
        return False
    except Exception as e:
        logger.error(f"Welcome DM failed for {user}: {e}")
        return False


# ------------------------------------------------------------
# FREE VPS CLAIM FLOW (invites / boosts) — auto deploy
# ------------------------------------------------------------

def get_claimed_types(stats: Dict[str, Any]) -> set:
    return set(filter(None, (stats.get('claimed_types') or '').split(',')))


def set_claimed_types(user_id: str, types: set):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO user_stats (user_id, last_updated) VALUES (?, ?)', (user_id, datetime.now().isoformat()))
    cur.execute('UPDATE user_stats SET claimed_types = ?, claimed_free_vps = ?, last_updated = ? WHERE user_id = ?',
                (",".join(sorted(types)), len(types), datetime.now().isoformat(), user_id))
    conn.commit()
    conn.close()


def eligible_options(stats: Dict[str, Any]):
    """Best unclaimed plan per reward type the user currently qualifies for."""
    claimed = get_claimed_types(stats)
    options = []
    for ptype in ('invites', 'boosts'):
        if ptype in claimed:
            continue
        best = None
        for plan in FREE_VPS_PLANS[ptype]:
            if stats.get(ptype, 0) >= plan[ptype] and (best is None or plan['ram'] > best['ram']):
                best = plan
        if best:
            options.append({'type': ptype, 'plan': best, 'current': stats.get(ptype, 0), 'required': best[ptype]})
    return options, claimed


def is_blacklisted(user_id: str) -> Optional[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT reason FROM blacklist WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return (row[0] or "No reason given") if row else None


class ClaimFreeView(discord.ui.View):
    def __init__(self, ctx, options):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.options = options
        self.selected = options[0] if len(options) == 1 else None
        self.selected_os = None
        self.busy = False
        self.plan_select = None

        if len(options) > 1:
            self.plan_select = discord.ui.Select(
                placeholder="Choose your reward",
                options=[discord.SelectOption(
                    label=f"{o['plan']['name']} — {o['type'].title()}", value=o['type'],
                    description=f"{o['plan']['ram']}GB RAM • {o['plan']['cpu']} CPU • {o['plan']['disk']}GB Disk") for o in options])
            self.plan_select.callback = self.on_plan
            self.add_item(self.plan_select)

        self.os_select = discord.ui.Select(
            placeholder="Choose your operating system",
            options=[discord.SelectOption(label=o["label"], value=o["value"]) for o in OS_OPTIONS])
        self.os_select.callback = self.on_os
        self.add_item(self.os_select)

        self.deploy_btn = discord.ui.Button(label="Deploy My VPS", style=discord.ButtonStyle.success, emoji="🚀", disabled=True)
        self.deploy_btn.callback = self.on_deploy
        self.add_item(self.deploy_btn)

    def _refresh_button(self):
        self.deploy_btn.disabled = not (self.selected and self.selected_os)

    async def _owner_only(self, interaction) -> bool:
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message("This menu is not for you!", ephemeral=True)
            return False
        return True

    async def on_plan(self, interaction: discord.Interaction):
        if not await self._owner_only(interaction):
            return
        chosen = self.plan_select.values[0]
        self.selected = next(o for o in self.options if o['type'] == chosen)
        for opt in self.plan_select.options:
            opt.default = opt.value == chosen
        self._refresh_button()
        await interaction.response.edit_message(view=self)

    async def on_os(self, interaction: discord.Interaction):
        if not await self._owner_only(interaction):
            return
        self.selected_os = self.os_select.values[0]
        for opt in self.os_select.options:
            opt.default = opt.value == self.selected_os
        self._refresh_button()
        await interaction.response.edit_message(view=self)

    async def on_deploy(self, interaction: discord.Interaction):
        if not await self._owner_only(interaction):
            return
        if self.busy:
            await interaction.response.send_message("Already deploying — please wait.", ephemeral=True)
            return
        uid = str(interaction.user.id)

        # Re-validate at click time (stats, blacklist, kill-switch, double-claim)
        if not FREE_CLAIMS_ENABLED:
            await interaction.response.send_message(embed=create_error_embed("Claims Paused", "Free VPS claims are currently disabled by staff."), ephemeral=True)
            return
        banned = is_blacklisted(uid)
        if banned:
            await interaction.response.send_message(embed=create_error_embed("Blacklisted", f"You cannot claim free VPS plans.\nReason: {banned}"), ephemeral=True)
            return
        stats = get_user_stats(uid)
        opts, claimed = eligible_options(stats)
        chosen = next((o for o in opts if o['type'] == self.selected['type']), None)
        if not chosen:
            await interaction.response.send_message(embed=create_error_embed("Not Eligible", "You no longer qualify for this reward or already claimed it."), ephemeral=True)
            return

        self.busy = True
        for child in self.children:
            child.disabled = True
        plan = chosen['plan']
        os_label = next((o['label'] for o in OS_OPTIONS if o['value'] == self.selected_os), self.selected_os)
        await interaction.response.edit_message(
            embed=create_info_embed("Deploying Your VPS", f"**{plan['name']}** on **{os_label}**\n\nThis can take a few minutes while the image is prepared. You'll get a DM when it's ready."),
            view=self)

        set_claimed_types(uid, claimed | {chosen['type']})  # reserve before the slow part
        try:
            vps_info = await deploy_vps_container(uid, plan['ram'], plan['cpu'], plan['disk'], self.selected_os, plan['name'])
        except Exception as e:
            set_claimed_types(uid, claimed)  # release the reservation
            logger.error("Free VPS deployment failed", exc_info=True)
            fail = create_error_embed("Deployment Failed", f"Your claim was **not** used — try again or contact staff.\n```{str(e)[:900]}```")
            try:
                await interaction.message.edit(embed=fail, view=None)
            except Exception:
                await self.ctx.send(embed=fail)
            await send_staff_alert(create_error_embed("Free VPS Deploy Failed", f"User <@{uid}> • plan **{plan['name']}**\n```{str(e)[:900]}```"))
            return

        await grant_vps_role(self.ctx.guild, interaction.user)
        dm_ok = await send_vps_welcome_dm(interaction.user, vps_info)
        done = create_success_embed("VPS Deployed", f"**{plan['name']}** is live!")
        add_field(done, "Container", f"`{vps_info['container_name']}`", True)
        add_field(done, "Specs", vps_info['config'], True)
        add_field(done, "OS", os_label, True)
        add_field(done, "Next Step", f"Run `{PREFIX}manage` to start SSH." + ("" if dm_ok else "\n⚠️ Your DMs are closed — enable them to receive details."), False)
        if not vps_info.get('_config_ok'):
            await send_staff_alert(create_warning_embed("Docker Config Incomplete", f"`{vps_info['container_name']}` deployed but the Docker-ready LXC config did not fully apply. Check `bot.log`."))
        try:
            await interaction.message.edit(embed=done, view=None)
        except Exception:
            await self.ctx.send(embed=done)

    async def on_timeout(self):
        if self.busy:
            return
        for child in self.children:
            child.disabled = True








# ------------------------------------------------------------
# REINSTALL (replaces the old "coming soon" placeholder)
# ------------------------------------------------------------

async def reinstall_container(vps: Dict[str, Any], os_version: str):
    name = vps['container_name']
    ram = int(re.sub(r'\D', '', str(vps['ram'])) or 0)
    cpu = int(re.sub(r'\D', '', str(vps['cpu'])) or 0)
    disk = int(re.sub(r'\D', '', str(vps['storage'])) or 0)
    if not (ram and cpu and disk):
        raise ValueError(f"Stored specs look invalid (ram={vps['ram']}, cpu={vps['cpu']}, storage={vps['storage']})")
    async with deploy_lock:
        conn = get_db()
        conn.execute('DELETE FROM port_forwards WHERE vps_container = ?', (name,))
        conn.commit()
        conn.close()
        await execute_lxc(f"lxc delete {name} --force", timeout=300)
        try:
            await _provision_container(name, os_version, ram, cpu, disk)
        except Exception:
            vps['status'] = 'stopped'
            save_vps_data()
            raise
        vps['os_version'] = os_version
        vps['status'] = 'running'
        save_vps_data()


class ReinstallView(discord.ui.View):
    def __init__(self, user_id, vps):
        super().__init__(timeout=180)
        self.user_id = str(user_id)
        self.vps = vps
        self.selected_os = None
        self.os_select = discord.ui.Select(placeholder="Choose the new operating system",
                                           options=[discord.SelectOption(label=o["label"], value=o["value"]) for o in OS_OPTIONS])
        self.os_select.callback = self.on_os
        self.add_item(self.os_select)
        self.confirm_btn = discord.ui.Button(label="Erase & Reinstall", style=discord.ButtonStyle.danger, emoji="🔄", disabled=True)
        self.confirm_btn.callback = self.on_confirm
        self.add_item(self.confirm_btn)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self.on_cancel
        self.add_item(cancel)

    async def _check(self, interaction) -> bool:
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return False
        return True

    async def on_os(self, interaction: discord.Interaction):
        if not await self._check(interaction):
            return
        self.selected_os = self.os_select.values[0]
        for opt in self.os_select.options:
            opt.default = opt.value == self.selected_os
        self.confirm_btn.disabled = False
        await interaction.response.edit_message(view=self)

    async def on_cancel(self, interaction: discord.Interaction):
        if not await self._check(interaction):
            return
        await interaction.response.edit_message(embed=create_info_embed("Cancelled", "Your VPS was not touched."), view=None)

    async def on_confirm(self, interaction: discord.Interaction):
        if not await self._check(interaction):
            return
        name = self.vps['container_name']
        await interaction.response.edit_message(
            embed=create_info_embed("Reinstalling", f"Rebuilding `{name}` with **{self.selected_os}** — this can take a few minutes."), view=None)
        try:
            await reinstall_container(self.vps, self.selected_os)
        except Exception as e:
            logger.error(f"Reinstall of {name} failed", exc_info=True)
            await interaction.edit_original_response(embed=create_error_embed("Reinstall Failed", f"```{str(e)[:1200]}```\nThe VPS may be stopped — contact staff if it doesn't recover."))
            await send_staff_alert(create_error_embed("Reinstall Failed", f"`{name}`\n```{str(e)[:900]}```"))
            return
        await interaction.edit_original_response(embed=create_success_embed("Reinstall Complete", f"`{name}` is running **{self.selected_os}** again. Use `{PREFIX}manage` for SSH."))


async def start_reinstall_flow(interaction: discord.Interaction, target_vps: Dict[str, Any], owner_id: str):
    embed = create_warning_embed("Reinstall VPS",
                                 f"⚠️ This **permanently erases all data** on `{target_vps['container_name']}` and rebuilds it with a fresh OS.\n"
                                 "Port forwards on this VPS are removed. Choose an OS, then confirm.")
    await interaction.followup.send(embed=embed, view=ReinstallView(interaction.user.id, target_vps), ephemeral=True)

# ------------------------------------------------------------
# STAFF ALERTS
# ------------------------------------------------------------

async def send_staff_alert(embed, view=None, content: Optional[str] = None) -> bool:
    """Post to the alert channel; fall back to a DM to the main admin so alerts are never silently lost."""
    channel = None
    if ALERT_CHANNEL_ID:
        try:
            channel = bot.get_channel(ALERT_CHANNEL_ID) or await bot.fetch_channel(ALERT_CHANNEL_ID)
        except Exception as e:
            logger.error(f"Alert channel {ALERT_CHANNEL_ID} unavailable: {type(e).__name__}: {e}")
    else:
        logger.error(f"No alert channel configured — use {PREFIX}setalertchannel. Falling back to DM.")
    if channel:
        try:
            await channel.send(content=content, embed=embed, view=view,
                               allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            return True
        except Exception as e:
            logger.error(f"Alert send failed: {type(e).__name__}: {e}")
    try:
        admin = await bot.fetch_user(int(MAIN_ADMIN_ID))
        await admin.send(content="⚠️ Alert channel unavailable — delivering via DM.", embed=embed)
        return True
    except Exception as e:
        logger.error(f"Alert DM fallback failed: {type(e).__name__}: {e}")
        return False


# ------------------------------------------------------------
# MINING GUARD
# ------------------------------------------------------------

MINER_EXACT = {'xmrig', 'minerd', 'ccminer', 'cgminer', 'bfgminer', 'ethminer', 'cpuminer', 'kdevtmpfsi', 'kinsing',
               'gminer', 'lolminer', 'nbminer', 't-rex', 'trex-miner', 'srbminer', 'bzminer', 'wildrig'}
MINER_PREFIXES = ('xmrig', 'xmr-stak', 'xmrstak', 'cpuminer', 'nbminer', 'lolminer', 't-rex', 'phoenixminer',
                  'teamredminer', 'srbminer', 'nanominer', 'kawpowminer', 'bzminer', 'wildrig', 'minergate')
BENIGN_BINARIES = {'grep', 'egrep', 'fgrep', 'rg', 'nano', 'vim', 'vi', 'less', 'more', 'cat', 'tail', 'head', 'man',
                   'git', 'curl', 'wget', 'apt', 'apt-get', 'dpkg', 'pip', 'pip3', 'ssh', 'scp', 'sshd', 'tmate',
                   'sh', 'bash', 'dash', 'zsh', 'fish', 'sudo', 'su', 'ps', 'top', 'htop', 'watch', 'sed', 'awk',
                   'tmux', 'screen', 'journalctl', 'cron', 'crond', 'sshx'}
MINER_CMD_PATTERNS = [
    (re.compile(r'stratum\+(?:tcp|ssl|tls)s?://', re.I), "stratum mining-pool URL"),
    (re.compile(r'--donate-level', re.I), "xmrig --donate-level flag"),
    (re.compile(r'--algo[= ]+(?:rx/0|randomx|cryptonight\S*|ethash|etchash|kawpow|autolykos2|kheavyhash|verushash|yespower\S*)', re.I), "mining algorithm flag"),
    (re.compile(r'(?:^|\s)-a\s+(?:rx/0|randomx|cn/\S+|cryptonight\S*|scrypt|x16r\S*|yespower\S*)', re.I), "mining algorithm flag"),
    (re.compile(r'\b(?:minexmr|supportxmr|nanopool|2miners|f2pool|ethermine|hashvault|moneroocean|nicehash|unmineable|herominers|c3pool|miningpoolhub|antpool|viabtc|poolin|mining-dutch|zpool|prohashing|crazypool|woolypooly|flexpool|k1pool|minergate)\.(?:com|org|net|io|ru|to|cc|fun|xyz|pw|ws)\b', re.I), "known mining-pool domain"),
    (re.compile(r'--cpu-max-threads-hint|--randomx-|--cpu-priority', re.I), "xmrig tuning flag"),
    (re.compile(r'\b[48][0-9AB][1-9A-HJ-NP-Za-km-z]{93}\b'), "Monero wallet address in arguments"),
]
MINING_PORTS = {3333, 4444, 5555, 7777, 8888, 9999, 14433, 14444, 45560, 45700, 10128, 3357}
STRIKE_SUSPEND = 2      # strike number at which the VPS is suspended
STRIKE_BLACKLIST = 3    # strike number at which the user is also blacklisted
MEDIUM_SUSTAINED_CHECKS = 3
MEDIUM_ALERT_COOLDOWN = 1800

mining_state: Dict[str, Any] = {
    'cpu_prev': {}, 'cpu_high': {}, 'last_alert': {},
    'last_scan': None, 'scans': 0, 'last_error': None,
}

_EXEC_SCRIPT = (
    "echo '##PS'; "
    "if command -v ps >/dev/null 2>&1; then ps -eo pid=,pcpu=,args= 2>/dev/null; "
    "else for d in /proc/[0-9]*; do p=${d#/proc/}; c=$(tr '\\0' ' ' < \"$d/cmdline\" 2>/dev/null); "
    "[ -n \"$c\" ] && echo \"$p 0.0 $c\"; done; fi; "
    "echo '##NET'; cat /proc/net/tcp /proc/net/tcp6 2>/dev/null; echo '##END'"
)


def analyze_process_list(procs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pure function: flag processes that look like crypto miners."""
    findings = []
    for p in procs:
        cmd = p.get('cmd', '')
        if not cmd:
            continue
        argv0 = cmd.split(None, 1)[0]
        base = os.path.basename(argv0).lower().lstrip('-').rstrip(':')
        if base.endswith('.exe'):
            base = base[:-4]
        reason = None
        if base in MINER_EXACT or base.startswith(MINER_PREFIXES):
            reason = f"known miner binary `{base}`"
        elif base not in BENIGN_BINARIES:
            for rx, label in MINER_CMD_PATTERNS:
                if rx.search(cmd):
                    reason = label
                    break
        if reason:
            findings.append({'pid': p['pid'], 'cpu': p.get('cpu', 0.0), 'cmd': cmd, 'reason': reason})
    return findings


def parse_exec_output(text: str) -> tuple[List[Dict[str, Any]], set]:
    procs, ports = [], set()
    section = None
    for line in text.splitlines():
        if line.startswith('##'):
            section = line[2:].strip()
            continue
        if section == 'PS':
            m = re.match(r'^\s*(\d+)\s+([\d.]+)\s+(.*)$', line)
            if m:
                procs.append({'pid': int(m.group(1)), 'cpu': float(m.group(2)), 'cmd': m.group(3)})
        elif section == 'NET':
            parts = line.split()
            if len(parts) > 3 and parts[3] == '01' and ':' in parts[2]:
                try:
                    ports.add(int(parts[2].split(':')[1], 16))
                except ValueError:
                    pass
    return procs, ports


async def run_lxc_exec(container: str, argv: List[str], timeout: int = 25) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec("lxc", "exec", container, "--", *argv,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise asyncio.TimeoutError(f"exec in {container} timed out after {timeout}s")
    return proc.returncode, out.decode(errors='ignore'), err.decode(errors='ignore')


async def container_state(container: str) -> Optional[Dict[str, Any]]:
    try:
        out = await execute_lxc(f"lxc query /1.0/instances/{container}/state", timeout=20)
        return json.loads(out) if isinstance(out, str) and out else None
    except Exception as e:
        logger.warning(f"lxc query state failed for {container}: {e}")
        return None


def log_mining_event(container: str, user_id: str, severity: str, evidence: str, action: str):
    conn = get_db()
    conn.execute('INSERT INTO mining_events (container_name, user_id, severity, evidence, action, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                 (container, user_id, severity, evidence[:1500], action[:300], datetime.now().isoformat()))
    conn.commit()
    conn.close()


def count_mining_events(hours: int = 24) -> int:
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM mining_events WHERE created_at >= ?', (since,))
    n = cur.fetchone()[0]
    conn.close()
    return n


def get_strikes(user_id: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT count FROM strikes WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0


def add_strike(user_id: str) -> int:
    conn = get_db()
    conn.execute('''INSERT INTO strikes (user_id, count, last_strike) VALUES (?, 1, ?)
                    ON CONFLICT(user_id) DO UPDATE SET count = count + 1, last_strike = excluded.last_strike''',
                 (user_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return get_strikes(user_id)


def clear_strikes(user_id: str):
    conn = get_db()
    conn.execute('DELETE FROM strikes WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()


def add_blacklist(user_id: str, reason: str):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO blacklist (user_id, reason, created_at) VALUES (?, ?, ?)',
                 (user_id, reason[:300], datetime.now().isoformat()))
    conn.commit()
    conn.close()


def remove_blacklist(user_id: str) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM blacklist WHERE user_id = ?', (user_id,))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


async def scan_container(uid: str, vps: Dict[str, Any], punish: bool = True) -> Dict[str, Any]:
    """Inspect one container. With punish=False (manual scan) nothing is killed or suspended."""
    container = vps['container_name']
    result: Dict[str, Any] = {'container': container, 'owner': uid, 'severity': None, 'findings': [],
                              'cpu': None, 'ports': [], 'skipped': None, 'processes': 0}

    state = await container_state(container)
    if state:
        running = str(state.get('status', '')).lower() == 'running'
        usage = (state.get('cpu') or {}).get('usage')
        if running and usage is not None:
            now = time.monotonic()
            prev = mining_state['cpu_prev'].get(container)
            mining_state['cpu_prev'][container] = (now, usage)
            cores = max(1, _to_int(re.sub(r'\D', '', str(vps.get('cpu', '1'))), 1))
            if prev and now > prev[0] and usage >= prev[1]:
                result['cpu'] = max(0.0, min(100.0, (usage - prev[1]) / ((now - prev[0]) * 1e9 * cores) * 100))
    else:
        running = (await get_container_status(container)) == 'running'
    if not running:
        mining_state['cpu_high'].pop(container, None)
        mining_state['cpu_prev'].pop(container, None)
        result['skipped'] = 'not running'
        return result

    if result['cpu'] is not None:
        if result['cpu'] >= CPU_THRESHOLD:
            mining_state['cpu_high'][container] = mining_state['cpu_high'].get(container, 0) + 1
        else:
            mining_state['cpu_high'][container] = 0

    rc, out, err = await run_lxc_exec(container, ['sh', '-c', _EXEC_SCRIPT])
    if rc != 0 and not out:
        raise RuntimeError(f"exec failed in {container}: {err.strip()[:200] or 'no output'}")
    procs, ports = parse_exec_output(out)
    result['processes'] = len(procs)
    result['findings'] = analyze_process_list(procs)
    result['ports'] = sorted(ports & MINING_PORTS)

    if result['findings']:
        result['severity'] = 'high'
    elif mining_state['cpu_high'].get(container, 0) >= MEDIUM_SUSTAINED_CHECKS and result['ports']:
        result['severity'] = 'medium'

    if punish and result['severity']:
        await handle_detection(uid, vps, result)
    return result


def evidence_text(result: Dict[str, Any]) -> str:
    lines = [f"PID {f['pid']} — {f['reason']}: {f['cmd'][:90]}" for f in result['findings'][:5]]
    if len(result['findings']) > 5:
        lines.append(f"… and {len(result['findings']) - 5} more")
    if result['ports']:
        lines.append(f"Outbound connections on pool-style ports: {', '.join(map(str, result['ports']))}")
    if result['cpu'] is not None:
        lines.append(f"Container CPU: {result['cpu']:.0f}% (limit {CPU_THRESHOLD}%)")
    return "\n".join(lines) or "No evidence recorded"


def build_mining_alert_embed(uid: str, container: str, severity: str, result: Dict[str, Any],
                             action: str, strikes: Optional[int] = None) -> discord.Embed:
    high = severity == 'high'
    embed = discord.Embed(
        title="🚨 Mining Activity Detected" if high else "⚠️ Suspicious Activity",
        description=(f"{BOT_NAME} Mining Guard flagged a VPS.\n" if high else "Sustained high CPU plus pool-style connections — **review recommended**.\n"),
        color=0xff3366 if high else 0xffaa00,
        timestamp=discord.utils.utcnow())
    embed.add_field(name="⌯⌲ Owner", value=f"<@{uid}> (`{uid}`)", inline=True)
    embed.add_field(name="⌯⌲ VPS", value=f"`{container}`", inline=True)
    embed.add_field(name="⌯⌲ Severity", value="🔴 HIGH" if high else "🟡 MEDIUM", inline=True)
    embed.add_field(name="⌯⌲ Evidence", value=f"```\n{truncate_text(evidence_text(result), 900)}\n```", inline=False)
    embed.add_field(name="⌯⌲ Action Taken", value=truncate_text(action, 1000), inline=False)
    if strikes is not None:
        embed.add_field(name="⌯⌲ Strikes", value=f"{strikes}/{STRIKE_BLACKLIST}", inline=True)
    if valid_url(THUMBNAIL):
        embed.set_thumbnail(url=THUMBNAIL)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


async def kill_pids(container: str, pids: List[int]) -> bool:
    if not pids:
        return True
    try:
        rc, _, err = await run_lxc_exec(container, ['kill', '-9', *[str(p) for p in pids]])
        if rc != 0:
            logger.warning(f"kill in {container} returned {rc}: {err.strip()[:200]}")
        return rc == 0
    except Exception as e:
        logger.error(f"kill failed in {container}: {e}")
        return False


async def suspend_for_mining(uid: str, vps: Dict[str, Any], reason: str) -> Optional[str]:
    """Stop and suspend a VPS. Returns an error string on failure."""
    container = vps['container_name']
    error = None
    try:
        await execute_lxc(f"lxc stop {container} --force", timeout=120)
    except Exception as e:
        error = f"stop failed: {str(e)[:200]}"
        logger.error(f"Could not stop {container}: {e}")
    vps['status'] = 'stopped' if error is None else vps.get('status', 'running')
    vps['suspended'] = True
    vps['suspended_reason'] = reason
    vps.setdefault('suspension_history', []).append({'time': datetime.now().isoformat(), 'reason': reason, 'by': 'MiningGuard'})
    save_vps_data()
    log_suspension(container, uid, 'suspend', reason, 'MININGGUARD')
    return error


async def handle_detection(uid: str, vps: Dict[str, Any], result: Dict[str, Any]):
    container = vps['container_name']
    severity = result['severity']

    if severity == 'medium':
        key = f"medium:{container}"
        if time.time() - mining_state['last_alert'].get(key, 0) < MEDIUM_ALERT_COOLDOWN:
            return
        mining_state['last_alert'][key] = time.time()
        action = "Alert only — no automatic action for medium severity."
        log_mining_event(container, uid, severity, evidence_text(result), action)
        await send_staff_alert(build_mining_alert_embed(uid, container, severity, result, action),
                               view=MiningAlertView(container, uid), content=f"<@{MAIN_ADMIN_ID}>")
        return

    # HIGH severity
    exempt = bool(vps.get('whitelisted')) or is_staff(uid)
    if exempt:
        key = f"exempt:{container}"
        if time.time() - mining_state['last_alert'].get(key, 0) < MEDIUM_ALERT_COOLDOWN:
            return  # already reported recently; don't spam the alert channel every scan
        mining_state['last_alert'][key] = time.time()
        action = "Alert only — VPS is whitelisted or owned by staff, no punishment applied."
        log_mining_event(container, uid, severity, evidence_text(result), action)
        await send_staff_alert(build_mining_alert_embed(uid, container, severity, result, action),
                               view=MiningAlertView(container, uid), content=f"<@{MAIN_ADMIN_ID}>")
        return

    killed = await kill_pids(container, [f['pid'] for f in result['findings']])
    strikes = add_strike(uid)
    steps = ["Miner processes killed" if killed else "⚠️ Could not kill all miner processes"]
    reason = f"Crypto-mining detected ({result['findings'][0]['reason']})"
    user_note = "Your VPS is still running. **Mining is prohibited** — the next violation suspends it."

    if strikes >= STRIKE_SUSPEND:
        err = await suspend_for_mining(uid, vps, reason)
        steps.append("VPS suspended" if err is None else f"⚠️ Suspend problem: {err}")
        user_note = "Your VPS has been **suspended** for crypto-mining. Contact staff to appeal."
    if strikes >= STRIKE_BLACKLIST:
        add_blacklist(uid, reason)
        steps.append("User blacklisted from free claims")
        steps.append(f"Recommended: `{PREFIX}delete-vps <@{uid}> <number> mining`")
        user_note = "Your VPS has been **suspended** and you are **blacklisted** for repeated crypto-mining."
    steps.append(f"Strike {strikes}/{STRIKE_BLACKLIST} recorded")
    action = "\n".join(f"• {s}" for s in steps)

    log_mining_event(container, uid, severity, evidence_text(result), " | ".join(steps))
    logger.warning(f"MINING detected on {container} (owner {uid}) — strike {strikes}: {' | '.join(steps)}")
    await send_staff_alert(build_mining_alert_embed(uid, container, severity, result, action, strikes),
                           view=MiningAlertView(container, uid), content=f"<@{MAIN_ADMIN_ID}>")

    dm = create_error_embed("Crypto-Mining Detected",
                            f"Our system found mining software on your VPS `{container}`.\n\n{user_note}\n\n**Strike {strikes}/{STRIKE_BLACKLIST}**")
    if not await _dm(uid, dm):
        logger.info(f"Could not DM {uid} about mining strike (DMs closed)")


async def admin_unsuspend(container: str, admin_id: str):
    found = find_vps(container)
    if not found:
        raise ValueError("VPS not found")
    uid, vps = found
    if not vps.get('suspended'):
        raise ValueError("VPS is not suspended")
    await execute_lxc(f"lxc start {container}")
    vps['suspended'] = False
    vps['suspended_reason'] = ''
    vps['status'] = 'running'
    save_vps_data()
    log_suspension(container, uid, 'unsuspend', 'via alert panel', str(admin_id))
    await apply_internal_permissions(container)


class MiningAlertView(discord.ui.View):
    def __init__(self, container: str, owner_id: str):
        super().__init__(timeout=86400)
        self.container = container
        self.owner_id = str(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_staff(str(interaction.user.id)):
            return True
        await interaction.response.send_message(embed=create_error_embed("Access Denied", "Staff only."), ephemeral=True)
        return False

    @discord.ui.button(label="Unsuspend", style=discord.ButtonStyle.success, emoji="▶️")
    async def unsuspend_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            await admin_unsuspend(self.container, str(interaction.user.id))
            await interaction.followup.send(embed=create_success_embed("Unsuspended", f"`{self.container}` started."), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(embed=create_error_embed("Unsuspend Failed", str(e)[:900]), ephemeral=True)

    @discord.ui.button(label="Whitelist VPS", style=discord.ButtonStyle.primary, emoji="🛡️")
    async def whitelist_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        found = find_vps(self.container)
        if not found:
            await interaction.response.send_message(embed=create_error_embed("Not Found", "VPS no longer exists."), ephemeral=True)
            return
        found[1]['whitelisted'] = True
        save_vps_data()
        await interaction.response.send_message(embed=create_success_embed("Whitelisted", f"`{self.container}` is now exempt from punishment."), ephemeral=True)

    @discord.ui.button(label="Clear Strikes", style=discord.ButtonStyle.secondary, emoji="🧹")
    async def clear_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        clear_strikes(self.owner_id)
        removed = remove_blacklist(self.owner_id)
        await interaction.response.send_message(embed=create_success_embed("Cleared", f"Strikes cleared for <@{self.owner_id}>" + (" and blacklist removed." if removed else ".")), ephemeral=True)


@tasks.loop(seconds=120)
async def mining_guard_loop():
    if not MINING_GUARD_ENABLED:
        return
    try:
        targets = [(uid, vps) for uid, lst in list(vps_data.items()) for vps in list(lst) if not vps.get('suspended')]
        sem = asyncio.Semaphore(3)

        async def run(uid, vps):
            async with sem:
                try:
                    await scan_container(uid, vps)
                except Exception as e:
                    logger.error(f"Mining scan failed for {vps['container_name']}: {type(e).__name__}: {e}")
                    mining_state['last_error'] = f"{vps['container_name']}: {type(e).__name__}: {e}"

        await asyncio.gather(*(run(u, v) for u, v in targets))
        mining_state['last_scan'] = time.time()
        mining_state['scans'] += 1
    except Exception:
        logger.exception("Mining guard loop crashed this cycle")
        mining_state['last_error'] = "loop error — see bot.log"


@bot.command(name='miningguard')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def miningguard_cmd(ctx, mode: str = "status"):
    """Mining guard on/off/status"""
    global MINING_GUARD_ENABLED
    mode = mode.lower()
    if mode in ('on', 'off'):
        MINING_GUARD_ENABLED = mode == 'on'
        set_setting('mining_guard', 'true' if MINING_GUARD_ENABLED else 'false')
    elif mode != 'status':
        await ctx.send(embed=create_error_embed("Invalid Mode", "Use `on`, `off` or `status`."))
        return
    last = mining_state['last_scan']
    embed = create_info_embed("Mining Guard", f"Status: {'🟢 **ON**' if MINING_GUARD_ENABLED else '🔴 **OFF**'}")
    add_field(embed, "Scan Interval", f"{MINING_INTERVAL}s", True)
    add_field(embed, "Last Scan", f"<t:{int(last)}:R>" if last else "not yet", True)
    add_field(embed, "Scans Run", str(mining_state['scans']), True)
    add_field(embed, "Detections (24h)", str(count_mining_events(24)), True)
    add_field(embed, "Alert Channel", f"<#{ALERT_CHANNEL_ID}>" if ALERT_CHANNEL_ID else f"❌ not set — `{PREFIX}setalertchannel`", True)
    add_field(embed, "Punishment Ladder", f"1: kill + warn • {STRIKE_SUSPEND}: suspend • {STRIKE_BLACKLIST}: suspend + blacklist", False)
    if mining_state['last_error']:
        add_field(embed, "Last Scan Error", f"⚠️ {mining_state['last_error']}", False)
    await ctx.send(embed=embed)


@bot.command(name='minerscan')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def minerscan_cmd(ctx, container_name: str):
    """Manually scan a VPS (report only, no punishment)"""
    found = find_vps(container_name)
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"No VPS named `{container_name}`."))
        return
    uid, vps = found
    try:
        result = await scan_container(uid, vps, punish=False)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Scan Failed", f"{type(e).__name__}: {str(e)[:900]}"))
        return
    if result['skipped']:
        await ctx.send(embed=create_info_embed("Scan Skipped", f"`{container_name}` is {result['skipped']}."))
        return
    if result['severity']:
        embed = build_mining_alert_embed(uid, container_name, result['severity'], result, "Manual scan — no action taken.")
    else:
        embed = create_success_embed("Scan Clean", f"`{container_name}` — {result['processes']} processes checked, nothing suspicious.")
        if result['cpu'] is not None:
            add_field(embed, "Container CPU", f"{result['cpu']:.0f}%", True)
    await ctx.send(embed=embed)


@bot.command(name='minerlog')
@is_admin()
@commands.cooldown(1, 5, commands.BucketType.user)
async def minerlog_cmd(ctx, container_name: str = None):
    """Recent mining detections"""
    conn = get_db()
    cur = conn.cursor()
    if container_name:
        cur.execute('SELECT * FROM mining_events WHERE container_name = ? ORDER BY id DESC LIMIT 8', (container_name,))
    else:
        cur.execute('SELECT * FROM mining_events ORDER BY id DESC LIMIT 8')
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await ctx.send(embed=create_info_embed("Mining Log", "No detections recorded."))
        return
    lines = [f"**{r['severity'].upper()}** • `{r['container_name']}` • <@{r['user_id']}> • {r['created_at'][:16].replace('T', ' ')}\n└ {truncate_text(r['action'], 120)}" for r in rows]
    await ctx.send(embed=create_info_embed("Mining Log", truncate_text("\n".join(lines), 4000)))


@bot.command(name='strikes')
@is_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def strikes_cmd(ctx, user: discord.Member):
    """Show a user's mining strikes"""
    uid = str(user.id)
    banned = is_blacklisted(uid)
    await ctx.send(embed=create_info_embed("Mining Strikes", f"{user.mention}: **{get_strikes(uid)}/{STRIKE_BLACKLIST}** strikes\nBlacklist: {('🔴 ' + banned) if banned else '🟢 none'}"))


@bot.command(name='clearstrikes')
@is_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def clearstrikes_cmd(ctx, user: discord.Member):
    """Clear strikes and blacklist for a user"""
    clear_strikes(str(user.id))
    removed = remove_blacklist(str(user.id))
    await ctx.send(embed=create_success_embed("Strikes Cleared", f"{user.mention} has 0 strikes" + (" and is no longer blacklisted." if removed else ".")))


@bot.command(name='blacklist')
@is_admin()
@commands.cooldown(1, 3, commands.BucketType.user)
async def blacklist_cmd(ctx, action: str, user: discord.Member = None, *, reason: str = "Staff decision"):
    """Blacklist from free claims: add/remove/list"""
    action = action.lower()
    if action == 'list':
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT * FROM blacklist ORDER BY created_at DESC LIMIT 20')
        rows = cur.fetchall()
        conn.close()
        text = "\n".join(f"<@{r['user_id']}> — {r['reason']}" for r in rows) or "Nobody is blacklisted."
        await ctx.send(embed=create_info_embed("Blacklist", truncate_text(text, 4000)))
    elif action in ('add', 'remove') and user:
        if action == 'add':
            add_blacklist(str(user.id), reason)
            await ctx.send(embed=create_success_embed("Blacklisted", f"{user.mention} can no longer claim free VPS plans."))
        else:
            ok = remove_blacklist(str(user.id))
            await ctx.send(embed=create_success_embed("Blacklist Updated", f"{user.mention} " + ("removed from the blacklist." if ok else "was not blacklisted.")))
    else:
        await ctx.send(embed=create_error_embed("Usage", f"`{PREFIX}blacklist add @user [reason]` • `remove @user` • `list`"))


# ------------------------------------------------------------
# LIVE NODE / STATUS DASHBOARD
# ------------------------------------------------------------

status_state: Dict[str, Any] = {
    'cpu_hist': deque(maxlen=14), 'ram_hist': deque(maxlen=14), 'prev_net': None,
    'overload_count': 0, 'last_overload_alert': 0.0, 'last_error': None, 'last_stats': None,
}


def _read_proc_stat() -> tuple[int, int]:
    with open('/proc/stat') as f:
        vals = [int(x) for x in f.readline().split()[1:9]]
    idle = vals[3] + vals[4]
    total = sum(vals)
    return total - idle, total


async def host_cpu_percent() -> float:
    a = _read_proc_stat()
    await asyncio.sleep(0.5)
    b = _read_proc_stat()
    dt = b[1] - a[1]
    return max(0.0, min(100.0, 100.0 * (b[0] - a[0]) / dt)) if dt > 0 else 0.0


def host_memory() -> tuple[int, int]:
    info = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, v = line.split(':', 1)
            info[k] = int(v.split()[0]) * 1024
    total = info['MemTotal']
    return total - info['MemAvailable'], total


def host_net_bytes() -> tuple[int, int]:
    rx = tx = 0
    skip = ('lo', 'veth', 'lxd', 'incus', 'docker', 'br-', 'virbr', 'tap')
    with open('/proc/net/dev') as f:
        for line in list(f)[2:]:
            name, data = line.split(':', 1)
            name = name.strip()
            if name.startswith(skip):
                continue
            fields = data.split()
            rx += int(fields[0])
            tx += int(fields[8])
    return rx, tx


async def get_lxc_counts() -> Optional[tuple[int, int]]:
    try:
        out = await execute_lxc("lxc list --format json", timeout=30)
        data = json.loads(out) if isinstance(out, str) and out else []
        return len(data), sum(1 for c in data if str(c.get('status', '')).lower() == 'running')
    except Exception as e:
        logger.warning(f"lxc list failed: {e}")
        return None


async def collect_node_stats() -> Dict[str, Any]:
    s: Dict[str, Any] = {'errors': []}

    async def safe(name, fn):
        try:
            res = fn()
            if asyncio.iscoroutine(res):
                res = await res
            return res
        except Exception as e:
            s['errors'].append(f"{name}: {type(e).__name__}: {e}")
            return None

    s['cpu'] = await safe('cpu', host_cpu_percent)
    mem = await safe('memory', host_memory)
    s['mem_used'], s['mem_total'] = mem if mem else (None, None)
    s['ram_pct'] = (s['mem_used'] / s['mem_total'] * 100) if mem else None
    disk = await safe('disk', lambda: shutil.disk_usage('/'))
    s['disk_used'], s['disk_total'] = (disk.used, disk.total) if disk else (None, None)
    s['disk_pct'] = (disk.used / disk.total * 100) if disk else None
    s['load'] = await safe('load', os.getloadavg)
    s['cores'] = os.cpu_count() or 1
    s['uptime'] = await safe('uptime', lambda: float(open('/proc/uptime').read().split()[0]))

    net = await safe('network', host_net_bytes)
    s['net_rate'] = None
    if net:
        now = time.time()
        prev = status_state['prev_net']
        if prev and now > prev[0]:
            dt = now - prev[0]
            s['net_rate'] = (max(0, net[0] - prev[1]) / dt, max(0, net[1] - prev[2]) / dt)
        status_state['prev_net'] = (now, net[0], net[1])

    counts = await get_lxc_counts()
    s['lxc_total'], s['lxc_running'] = counts if counts else (None, None)
    s['db_total'] = sum(len(v) for v in vps_data.values())
    s['suspended'] = sum(1 for lst in vps_data.values() for v in lst if v.get('suspended'))
    s['users'] = len(vps_data)

    if s['cpu'] is not None:
        status_state['cpu_hist'].append(s['cpu'])
    if s['ram_pct'] is not None:
        status_state['ram_hist'].append(s['ram_pct'])
    status_state['last_stats'] = s
    return s


def node_health(s: Dict[str, Any]) -> tuple[str, str, int]:
    checks = [(s.get('cpu'), CPU_THRESHOLD), (s.get('ram_pct'), RAM_THRESHOLD), (s.get('disk_pct'), 90)]
    known = [(v, limit) for v, limit in checks if v is not None]
    if not known:
        return "⚪", "Unknown", 0x95a5a6
    if any(v >= 95 for v, _ in known):
        return "🔴", "Overloaded", 0xff3366
    if any(v >= limit for v, limit in known):
        return "🟡", "High Load", 0xffaa00
    return "🟢", "Operational", 0x00ff88


def build_status_embed(s: Dict[str, Any]) -> discord.Embed:
    emoji, label, color = node_health(s)
    embed = discord.Embed(title=f"{BRAND_ICON} {BOT_NAME} — Node Status", color=color, timestamp=discord.utils.utcnow(),
                          description=f"**{NODE_NAME}** • {emoji} **{label}**\nUpdated <t:{int(time.time())}:R> • refreshes every {STATUS_INTERVAL}s")

    cpu, ram, disk = s.get('cpu'), s.get('ram_pct'), s.get('disk_pct')
    cpu_txt = f"`{make_bar(cpu)}` **{cpu:.1f}%**" if cpu is not None else "`N/A`"
    if s.get('load'):
        cpu_txt += f"\n{s['cores']} cores • load {s['load'][0]:.2f}"
    cpu_txt += f"\n`{make_spark(status_state['cpu_hist'])}`"
    embed.add_field(name="🧠 CPU", value=cpu_txt, inline=True)

    ram_txt = f"`{make_bar(ram)}` **{ram:.1f}%**\n{fmt_bytes(s['mem_used'])} / {fmt_bytes(s['mem_total'])}" if ram is not None else "`N/A`"
    if ram is not None:
        ram_txt += f"\n`{make_spark(status_state['ram_hist'])}`"
    embed.add_field(name="🧩 Memory", value=ram_txt, inline=True)

    disk_txt = f"`{make_bar(disk)}` **{disk:.1f}%**\n{fmt_bytes(s['disk_used'])} / {fmt_bytes(s['disk_total'])}" if disk is not None else "`N/A`"
    embed.add_field(name="💽 Disk", value=disk_txt, inline=True)

    if s.get('net_rate'):
        net_txt = f"⬇️ {s['net_rate'][0] * 8 / 1e6:.2f} Mbps\n⬆️ {s['net_rate'][1] * 8 / 1e6:.2f} Mbps"
    else:
        net_txt = "measuring…"
    embed.add_field(name="🌐 Network", value=net_txt, inline=True)
    embed.add_field(name="⏱️ Host Uptime", value=fmt_duration(s['uptime']) if s.get('uptime') is not None else "N/A", inline=True)

    lat = bot.latency
    lat_txt = f"{round(lat * 1000)}ms" if lat == lat and lat != float('inf') else "connecting"
    embed.add_field(name="🤖 Bot", value=f"{lat_txt} • up {fmt_duration(time.time() - BOT_START_TS)}", inline=True)

    total = s['lxc_total'] if s.get('lxc_total') is not None else s['db_total']
    running = s['lxc_running'] if s.get('lxc_running') is not None else "?"
    stopped = (total - running) if isinstance(running, int) else "?"
    embed.add_field(name="🖥️ VPS Fleet",
                    value=f"**{total}** total • 🟢 {running} running • 🔴 {stopped} stopped\n⏸️ {s['suspended']} suspended • 👥 {s['users']} users", inline=False)

    last = mining_state['last_scan']
    guard = (f"{'🟢 Active' if MINING_GUARD_ENABLED else '🔴 Disabled'} • last scan " + (f"<t:{int(last)}:R>" if last else "pending")
             + f" • {count_mining_events(24)} detection(s) / 24h")
    embed.add_field(name="🛡️ Mining Guard", value=guard, inline=False)

    if s['errors']:
        embed.add_field(name="⚠️ Collector Issues", value=truncate_text("\n".join(s['errors']), 900), inline=False)
    if valid_url(THUMBNAIL):
        embed.set_thumbnail(url=THUMBNAIL)
    if valid_url(BANNER):
        embed.set_image(url=BANNER)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


async def update_status_dashboard(force_new: bool = False) -> tuple[bool, str]:
    if not STATUS_CHANNEL_ID:
        return False, f"No status channel configured — use `{PREFIX}setstatuschannel #channel`."
    try:
        channel = bot.get_channel(STATUS_CHANNEL_ID) or await bot.fetch_channel(STATUS_CHANNEL_ID)
    except discord.NotFound:
        return False, f"Status channel {STATUS_CHANNEL_ID} no longer exists."
    except discord.Forbidden:
        return False, "I can't access the status channel (missing View Channel)."
    embed = build_status_embed(await collect_node_stats())
    msg_id = _to_int(get_setting('status_message_id', '0'))
    try:
        if msg_id and not force_new:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=embed)
                return True, 'edited'
            except discord.NotFound:
                pass
        msg = await channel.send(embed=embed)
        set_setting('status_message_id', str(msg.id))
        return True, 'sent'
    except discord.Forbidden:
        return False, "Missing permission in the status channel (need Send Messages, Embed Links, Read History)."
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def check_node_overload():
    s = status_state.get('last_stats')
    if not s:
        return
    emoji, label, _ = node_health(s)
    if label in ('High Load', 'Overloaded'):
        status_state['overload_count'] += 1
    else:
        status_state['overload_count'] = 0
    if status_state['overload_count'] >= 3 and time.time() - status_state['last_overload_alert'] > 1800:
        status_state['last_overload_alert'] = time.time()
        await send_staff_alert(create_warning_embed(f"{NODE_NAME} {label}",
                               f"CPU {s['cpu'] if s['cpu'] is not None else 'N/A'}% • RAM {round(s['ram_pct']) if s['ram_pct'] is not None else 'N/A'}% • "
                               f"Disk {round(s['disk_pct']) if s['disk_pct'] is not None else 'N/A'}%\nSustained for {status_state['overload_count']} consecutive refreshes."))


@tasks.loop(seconds=60)
async def status_dashboard_loop():
    try:
        ok, info = await update_status_dashboard()
        if not ok and info != status_state['last_error']:
            logger.error(f"Status dashboard: {info}")
        status_state['last_error'] = None if ok else info
        await check_node_overload()
    except Exception as e:
        logger.exception("Status dashboard loop error")
        status_state['last_error'] = f"{type(e).__name__}: {e}"


@tasks.loop(hours=6)
async def branding_refresh_loop():
    try:
        await refresh_branding_assets()
    except Exception:
        logger.exception("Branding refresh loop error")


# ------------------------------------------------------------
# STARTUP
# ------------------------------------------------------------

async def _axion_setup_hook():
    mining_guard_loop.change_interval(seconds=MINING_INTERVAL)
    status_dashboard_loop.change_interval(seconds=STATUS_INTERVAL)
    for loop_task in (mining_guard_loop, status_dashboard_loop, branding_refresh_loop):
        if not loop_task.is_running():
            loop_task.start()

bot.setup_hook = _axion_setup_hook


@bot.listen('on_ready')
async def axion_on_ready():
    await refresh_branding_assets()
    for guild in bot.guilds:
        if tracked_guild(guild):
            await cache_guild_invites(guild)
    logger.info(f"{BOT_NAME} premium modules ready • guilds={len(bot.guilds)} • mining_guard={MINING_GUARD_ENABLED} • "
                f"alert_channel={ALERT_CHANNEL_ID or 'unset'} • status_channel={STATUS_CHANNEL_ID or 'unset'}")
    if not ALERT_CHANNEL_ID:
        logger.warning(f"Alert channel not set — mining alerts will DM the main admin. Use {PREFIX}setalertchannel.")


def plan_select_options():
    opts = []
    for cat in ('invites', 'boosts'):
        icon = '📋' if cat == 'invites' else '⚡'
        for i, p in enumerate(FREE_VPS_PLANS[cat]):
            opts.append(discord.SelectOption(
                label=f"{icon} {p['name']} ({p[cat]} {cat.title()})", value=f"{cat}_{i}",
                description=f"{p['ram']}GB RAM, {p['cpu']} CPU, {p['disk']}GB Disk"))
    return opts


# ============================================================
#   VPS ACCESS: SSH over Pinggy  +  SSHX web terminal
#   (rewritten: background processes, log-file parsing,
#    hard timeouts, explicit errors — no more infinite waits)
# ============================================================
PINGGY_TOKEN = _cfg('pinggy_token')
PINGGY_HOSTS = ['pro.pinggy.io'] if PINGGY_TOKEN else ['free.pinggy.io', 'a.pinggy.io']
PINGGY_USER = f"{PINGGY_TOKEN}+tcp" if PINGGY_TOKEN else "tcp"
PINGGY_LOG = '/tmp/avm_pinggy.log'
PINGGY_PID = '/root/.avm_pinggy.pid'
SSH_PASS_FILE = '/root/.avm_ssh_pass'
SSHX_LOG = '/tmp/avm_sshx.log'
SSHX_PID = '/root/.avm_sshx.pid'

ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07')
PINGGY_TCP_RE = re.compile(r'tcp://([A-Za-z0-9.-]+):(\d{2,5})')
PINGGY_HOST_RE = re.compile(r'\b((?:[a-z0-9-]+\.)+pinggy\.(?:online|link|io)):(\d{2,5})\b', re.I)
SSHX_URL_RE = re.compile(r'https://(?:[A-Za-z0-9-]+\.)*sshx\.io/s/[A-Za-z0-9_-]+(?:#[A-Za-z0-9_-]+)?')
access_locks: Dict[str, asyncio.Lock] = {}


class AccessError(Exception):
    """Problem with a clear, user-safe message."""


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub('', text or '').replace('\r', '')


def parse_pinggy_log(text: str) -> Optional[tuple]:
    clean = strip_ansi(text)
    m = PINGGY_TCP_RE.search(clean) or PINGGY_HOST_RE.search(clean)
    return (m.group(1), int(m.group(2))) if m else None


def parse_sshx_log(text: str) -> Optional[str]:
    m = SSHX_URL_RE.search(strip_ansi(text))
    return m.group(0) if m else None


def log_tail(text: str, lines: int = 6) -> str:
    rows = [r.strip() for r in strip_ansi(text).splitlines() if r.strip()]
    return "\n".join(rows[-lines:])[:600] or "(no output)"


# ---- shell snippets (kept separate so they can be syntax-checked and tested) ----
def sh_install_missing(names: List[str]) -> str:
    """Install only what's missing. names ⊆ {server, client, curl}. Supports apt / dnf / yum / apk."""
    checks = {
        'server': '[ -x /usr/sbin/sshd ] || command -v sshd >/dev/null 2>&1',
        'client': 'command -v ssh >/dev/null 2>&1',
        'curl': 'command -v curl >/dev/null 2>&1',
    }
    miss = "\n".join(f'{checks[n]} || miss="$miss {n}"' for n in names)
    return f'''miss=""
{miss}
[ -z "$miss" ] && exit 0
PM=""
for p in apt-get dnf yum apk; do command -v $p >/dev/null 2>&1 && PM=$p && break; done
pk=""
for n in $miss; do
  case "$PM:$n" in
    apt-get:server) pk="$pk openssh-server";;
    apt-get:client) pk="$pk openssh-client";;
    apt-get:curl) pk="$pk curl ca-certificates";;
    dnf:server|yum:server) pk="$pk openssh-server";;
    dnf:client|yum:client) pk="$pk openssh-clients";;
    dnf:curl|yum:curl) pk="$pk curl";;
    apk:server) pk="$pk openssh-server";;
    apk:client) pk="$pk openssh-client";;
    apk:curl) pk="$pk curl ca-certificates";;
  esac
done
case "$PM" in
  apt-get) export DEBIAN_FRONTEND=noninteractive; apt-get update -y >/tmp/avm_pkg.log 2>&1; apt-get install -y --no-install-recommends $pk >>/tmp/avm_pkg.log 2>&1;;
  dnf|yum) $PM install -y $pk >/tmp/avm_pkg.log 2>&1;;
  apk) apk add --no-cache $pk >/tmp/avm_pkg.log 2>&1;;
  *) echo "no supported package manager (apt/dnf/yum/apk) found" >&2; exit 90;;
esac
rc=$?
[ $rc -ne 0 ] && tail -n 6 /tmp/avm_pkg.log >&2
exit $rc
'''


def sh_dns_check(host: str) -> str:
    return (f'if command -v getent >/dev/null 2>&1; then getent hosts {host} >/dev/null 2>&1 || exit 7; '
            f'elif command -v nslookup >/dev/null 2>&1; then nslookup {host} >/dev/null 2>&1 || exit 7; fi; exit 0')


SH_CONFIGURE_SSHD = '''mkdir -p /run/sshd /etc/ssh/sshd_config.d
printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n' > /etc/ssh/sshd_config.d/00-avm.conf
if ! grep -qE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config.d' /etc/ssh/sshd_config 2>/dev/null; then
  sed -i -E 's/^[[:space:]]*#?[[:space:]]*PermitRootLogin.*/PermitRootLogin yes/; s/^[[:space:]]*#?[[:space:]]*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
  grep -q '^PermitRootLogin yes' /etc/ssh/sshd_config || echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config
  grep -q '^PasswordAuthentication yes' /etc/ssh/sshd_config || echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config
fi
ssh-keygen -A >/dev/null 2>&1
SSHD=$(command -v sshd 2>/dev/null || echo /usr/sbin/sshd)
$SSHD -t 2>/tmp/avm_sshd_test.log || { cat /tmp/avm_sshd_test.log >&2; exit 11; }
started=0
if command -v systemctl >/dev/null 2>&1; then
  for u in ssh sshd; do
    if systemctl cat $u >/dev/null 2>&1; then
      systemctl enable $u >/dev/null 2>&1
      systemctl restart $u >/dev/null 2>&1 && started=1 && break
    fi
  done
fi
if [ $started -eq 0 ]; then
  for p in /proc/[0-9]*; do
    [ "$(cat $p/comm 2>/dev/null)" = "sshd" ] && kill "${p#/proc/}" 2>/dev/null
  done
  $SSHD >/tmp/avm_sshd_start.log 2>&1 || { cat /tmp/avm_sshd_start.log >&2; exit 12; }
fi
i=0
while [ $i -lt 15 ]; do
  grep -qE ':0016 [0-9A-F:]+ 0A' /proc/net/tcp /proc/net/tcp6 2>/dev/null && exit 0
  i=$((i+1)); sleep 1
done
echo "sshd is not listening on port 22" >&2
exit 13
'''


def sh_alive(pidf: str) -> str:
    """Shell condition: pid file points at a live, non-zombie process."""
    return (f'[ -f {pidf} ] && kill -0 "$(cat {pidf})" 2>/dev/null '
            f'&& ! grep -q "^State:.*Z" "/proc/$(cat {pidf})/status" 2>/dev/null')


def sh_start_tunnel(target: str, log: str = PINGGY_LOG, pidf: str = PINGGY_PID) -> str:
    return f'''if {sh_alive(pidf)} && grep -qE 'tcp://|pinggy' {log} 2>/dev/null; then
  echo REUSE; exit 0
fi
[ -f {pidf} ] && kill "$(cat {pidf})" 2>/dev/null
rm -f {log}
nohup ssh -p 443 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o ConnectTimeout=15 -o BatchMode=yes -R0:localhost:22 {target} >{log} 2>&1 </dev/null &
echo $! > {pidf}
echo STARTED
'''


def sh_read_state(log: str, pidf: str) -> str:
    return (f'if {sh_alive(pidf)}; then echo "##ALIVE"; else echo "##DEAD"; fi; '
            f'cat {log} 2>/dev/null; exit 0')


def sh_find_sshx() -> str:
    return ('for p in "$(command -v sshx 2>/dev/null)" /usr/local/bin/sshx /root/.local/bin/sshx /root/.cargo/bin/sshx /usr/bin/sshx; do '
            '[ -n "$p" ] && [ -x "$p" ] && { echo "$p"; exit 0; }; done; exit 1')


def sh_install_sshx() -> str:
    return ('curl -sSf --max-time 100 -o /tmp/sshx-get.sh https://sshx.io/get || exit 21; '
            'sh /tmp/sshx-get.sh >/tmp/sshx-install.log 2>&1 || { tail -n 6 /tmp/sshx-install.log >&2; exit 22; }; exit 0')


def sh_start_sshx(binary: str, log: str = SSHX_LOG, pidf: str = SSHX_PID) -> str:
    return f'''if {sh_alive(pidf)} && grep -q 'sshx.io/s/' {log} 2>/dev/null; then
  echo REUSE; exit 0
fi
[ -f {pidf} ] && kill "$(cat {pidf})" 2>/dev/null
rm -f {log}
nohup {binary} >{log} 2>&1 </dev/null &
echo $! > {pidf}
echo STARTED
'''


# ---- helpers ----
async def cexec(container: str, script: str, timeout: int, what: str) -> tuple:
    try:
        return await run_lxc_exec(container, ['sh', '-c', script], timeout=timeout)
    except asyncio.TimeoutError:
        raise AccessError(f"{what} timed out after {timeout}s inside the VPS (is its internet working?)")
    except FileNotFoundError:
        raise AccessError("`lxc` is not available on this host")


async def wait_for_log(container: str, log: str, pidf: str, parser, seconds: int) -> tuple:
    """Poll a log file until `parser(text)` returns something, the process dies, or the time is up."""
    deadline = time.monotonic() + seconds
    text = ""
    alive = True
    while True:
        rc, out, _ = await cexec(container, sh_read_state(log, pidf), 15, "reading tunnel log")
        alive = out.startswith("##ALIVE")
        text = out.split("\n", 1)[1] if "\n" in out else ""
        found = parser(text)
        if found:
            return found, text, alive
        if not alive or time.monotonic() >= deadline:
            return None, text, alive
        await asyncio.sleep(1.5)


async def require_running(container: str):
    if (await get_container_status(container)) != 'running':
        raise AccessError("This VPS is stopped — press **Start** first, then try again.")


async def ensure_dns(container: str, host: str):
    rc, _, _ = await cexec(container, sh_dns_check(host), 20, f"DNS lookup for {host}")
    if rc == 7:
        raise AccessError(f"This VPS cannot resolve `{host}` — its network/DNS looks broken. Check it with `{PREFIX}vps-network`.")


async def get_or_create_root_password(container: str) -> str:
    rc, out, _ = await cexec(container, f'cat {SSH_PASS_FILE} 2>/dev/null', 15, "reading saved SSH password")
    pw = out.strip()
    if not re.fullmatch(r'[A-Za-z0-9]{16,64}', pw or ''):
        pw = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
        rc, _, err = await cexec(container, f'umask 077; printf %s {pw} > {SSH_PASS_FILE}', 15, "saving SSH password")
        if rc != 0:
            raise AccessError(f"Could not save the SSH password: {err.strip()[:200]}")
    rc, _, err = await cexec(container, f'echo root:{pw} | chpasswd', 20, "setting the root password")
    if rc != 0:
        raise AccessError(f"Could not set the root password: {err.strip()[:200]}")
    return pw


async def open_pinggy_tunnel(container: str) -> tuple:
    """Returns (host, port, reused). Tries each Pinggy host; raises AccessError with the real reason."""
    last_tail = ""
    for i, host in enumerate(PINGGY_HOSTS):
        await ensure_dns(container, host)
        target = f"{PINGGY_USER}@{host}"
        rc, out, err = await cexec(container, sh_start_tunnel(target), 25, "starting the Pinggy tunnel")
        if rc != 0:
            raise AccessError(f"Could not start the tunnel: {(err or out).strip()[:300]}")
        found, text, alive = await wait_for_log(container, PINGGY_LOG, PINGGY_PID, parse_pinggy_log, 25)
        if found:
            return found[0], found[1], out.strip() == 'REUSE'
        last_tail = log_tail(text)
        logger.warning(f"Pinggy via {host} gave no address (alive={alive}): {last_tail}")
    hint = ("Free Pinggy allows about one tunnel per IP — if another VPS already has one open, this one is refused. "
            "Add a Pinggy token (`./install.sh set pinggy_token <token>`) to remove that limit." if not PINGGY_TOKEN else
            "Check that the Pinggy token is valid and its plan allows another tunnel.")
    raise AccessError(f"Pinggy did not return an address.\n```{last_tail}```{hint}")


async def run_ssh_access(container: str) -> Dict[str, Any]:
    await require_running(container)
    rc, _, err = await cexec(container, sh_install_missing(['server', 'client']), 300, "installing OpenSSH")
    if rc != 0:
        raise AccessError(f"Could not install OpenSSH in the VPS:\n```{err.strip()[:500] or 'unknown error'}```")
    rc, _, err = await cexec(container, SH_CONFIGURE_SSHD, 60, "configuring sshd")
    if rc != 0:
        raise AccessError(f"sshd could not be started:\n```{err.strip()[:500] or 'unknown error'}```")
    password = await get_or_create_root_password(container)
    host, port, reused = await open_pinggy_tunnel(container)
    return {'host': host, 'port': port, 'password': password, 'reused': reused}


async def run_sshx_access(container: str) -> Dict[str, Any]:
    await require_running(container)
    await ensure_dns(container, 'sshx.io')
    rc, _, err = await cexec(container, sh_install_missing(['curl']), 300, "installing curl")
    if rc != 0:
        raise AccessError(f"Could not install curl in the VPS:\n```{err.strip()[:500] or 'unknown error'}```")
    rc, out, _ = await cexec(container, sh_find_sshx(), 15, "looking for sshx")
    if rc != 0:
        rc, out, err = await cexec(container, sh_install_sshx(), 180, "downloading sshx")
        if rc != 0:
            why = {21: "could not download https://sshx.io/get", 22: "the sshx installer failed"}.get(rc, f"exit code {rc}")
            raise AccessError(f"sshx install failed: {why}.\n```{(err or out).strip()[:400]}```")
        rc, out, _ = await cexec(container, sh_find_sshx(), 15, "looking for sshx")
        if rc != 0:
            raise AccessError("sshx was installed but the binary could not be found in the VPS.")
    binary = out.strip().splitlines()[-1]
    rc, out2, err = await cexec(container, sh_start_sshx(binary), 20, "starting sshx")
    if rc != 0:
        raise AccessError(f"Could not start sshx: {(err or out2).strip()[:300]}")
    url, text, alive = await wait_for_log(container, SSHX_LOG, SSHX_PID, parse_sshx_log, 30)
    if not url:
        raise AccessError(f"sshx did not produce a link within 30s ({'still running' if alive else 'it exited'}).\n```{log_tail(text)}```")
    return {'url': url, 'reused': out2.strip() == 'REUSE'}


def _access_embed_base(title: str, description: str) -> discord.Embed:
    e = discord.Embed(title=f"{BRAND_ICON} {BOT_NAME} — {title}", description=description, color=0x00ff88)
    if THUMBNAIL and THUMBNAIL.startswith(('http://', 'https://')):
        e.set_thumbnail(url=THUMBNAIL)
    e.set_footer(text=FOOTER_TEXT)
    return e


async def _guarded_access(interaction: discord.Interaction, container: str, suspended: bool, label: str, runner, builder):
    if suspended:
        await interaction.followup.send(embed=create_error_embed("Access Denied", "Cannot access a suspended VPS."), ephemeral=True)
        return
    lock = access_locks.setdefault(container, asyncio.Lock())
    if lock.locked():
        await interaction.followup.send(embed=create_info_embed(label, "Already working on this VPS — please wait for the result."), ephemeral=True)
        return
    async with lock:
        await interaction.followup.send(embed=create_info_embed(label, f"Setting up access for `{container}` — this can take up to a minute…"), ephemeral=True)
        try:
            result = await asyncio.wait_for(runner(container), timeout=420)
        except AccessError as e:
            await interaction.followup.send(embed=create_error_embed(f"{label} Failed", str(e)), ephemeral=True)
            return
        except asyncio.TimeoutError:
            await interaction.followup.send(embed=create_error_embed(f"{label} Failed", "The whole operation timed out (7 min). The VPS network may be down."), ephemeral=True)
            return
        except Exception as e:
            logger.error(f"{label} failed for {container}", exc_info=True)
            await interaction.followup.send(embed=create_error_embed(f"{label} Error", f"{type(e).__name__}: {str(e)[:500]}"), ephemeral=True)
            return
        try:
            await interaction.user.send(embed=builder(container, result))
        except discord.Forbidden:
            await interaction.followup.send(embed=create_error_embed("DM Failed", f"Enable DMs from this server to receive the {label} details."), ephemeral=True)
            return
        await interaction.followup.send(embed=create_success_embed(f"{label} Ready", "Details were sent to your DMs."), ephemeral=True)


def build_ssh_embed(container: str, r: Dict[str, Any]) -> discord.Embed:
    e = _access_embed_base("SSH Access", f"SSH details for VPS `{container}`" + (" (existing tunnel reused)" if r['reused'] else ""))
    e.add_field(name="⌯⌲ Command", value=f"```ssh root@{r['host']} -p {r['port']}```", inline=False)
    e.add_field(name="⌯⌲ Username", value="`root`", inline=True)
    e.add_field(name="⌯⌲ Password", value=f"||`{r['password']}`||", inline=True)
    if PINGGY_TOKEN:
        note = "Tunnel is kept alive by Pinggy. If it drops, press **SSH** again for a fresh address."
    else:
        note = "Free Pinggy tunnels last about **60 minutes**. Press **SSH** again for a fresh address after that — the password stays the same."
    e.add_field(name="⌯⌲ Note", value=note, inline=False)
    e.add_field(name="⌯⌲ Security", value="Do not share this message. It gives root access.", inline=False)
    return e


def build_sshx_embed(container: str, r: Dict[str, Any]) -> discord.Embed:
    e = _access_embed_base("SSHX Access", f"Web terminal for VPS `{container}`" + (" (existing session reused)" if r['reused'] else ""))
    e.add_field(name="⌯⌲ Link", value=f"[Click to Open Terminal]({r['url']})", inline=False)
    e.add_field(name="⌯⌲ Security", value="This link grants direct root access. Do not share it.", inline=False)
    return e


async def handle_ssh_action(interaction: discord.Interaction, container: str, suspended: bool):
    await _guarded_access(interaction, container, suspended, "SSH Access", run_ssh_access, build_ssh_embed)


async def handle_sshx_action(interaction: discord.Interaction, container: str, suspended: bool):
    await _guarded_access(interaction, container, suspended, "SSHX Access", run_sshx_access, build_sshx_embed)


# ============================================================
#   AVM PRO  —  CUSTOMER EDITION  (paid plans only)
#   Part A: config • emoji skin • database • pricing • gateways
# ============================================================
import base64
import io
import urllib.parse
from datetime import timezone
from decimal import Decimal, ROUND_HALF_UP

try:
    from avm_emojis import EMOJI_ORDER, EMOJI_PNG_B64
except ImportError:  # pack file missing: bot still works with normal emojis
    EMOJI_ORDER, EMOJI_PNG_B64 = [], {}

PRO_EDITION = "Pro"


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------
# PRO CONFIG  (config.json / AXION_* env vars; secrets never go through Discord)
# ------------------------------------------------------------
UPI_ID = _cfg('upi_id')
UPI_NAME = _cfg('upi_name', BOT_NAME)
UPI_QR_URL = _cfg('upi_qr_url')


def _load_wallets() -> Dict[str, str]:
    raw = os.environ.get('AXION_CRYPTO_WALLETS') or CFG.get('crypto_wallets') or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as e:
            logger.error(f"crypto_wallets is not valid JSON, ignoring it: {e}")
            raw = {}
    if not isinstance(raw, dict):
        logger.error("crypto_wallets must be an object like {\"USDT-TRC20\": \"T...\"}")
        return {}
    return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(v).strip()}


CRYPTO_WALLETS = _load_wallets()
RAZORPAY_KEY_ID = _cfg('razorpay_key_id')
RAZORPAY_KEY_SECRET = _cfg('razorpay_key_secret')
STRIPE_SECRET_KEY = _cfg('stripe_secret_key')
PAYPAL_CLIENT_ID = _cfg('paypal_client_id')
PAYPAL_SECRET = _cfg('paypal_secret')
PAYPAL_MODE = _cfg('paypal_mode', 'live').lower()
RETURN_URL = _cfg('return_url', 'https://discord.com/app')

CURRENCY_CODE = 'INR'
CURRENCY_SYMBOL = _cfg('currency_symbol', '₹')
USD_RATE = _to_float(_cfg('usd_rate', '85'), 85.0) or 85.0
TAX_PERCENT = max(0.0, _to_float(_cfg('tax_percent', '0'), 0.0))
TAX_LABEL = _cfg('tax_label', 'GST')
GRACE_DAYS = max(0, _to_int(_cfg('grace_days', '3'), 3))
DELETE_ALERT_DAYS = max(1, _to_int(_cfg('delete_alert_days', '7'), 7))
MAX_OPEN_ORDERS = 3
AUTO_PAY_WINDOW_MIN = 30          # gateway links live this long
MANUAL_PAY_WINDOW_MIN = 180       # manual orders: time to press "I've Paid"
VERIFY_NUDGE_HOURS = 3            # remind staff about unreviewed payments
LATE_PAYMENT_HOURS = 24           # keep polling expired gateway orders this long


def _parse_reminders(raw: str) -> List[int]:
    days = sorted({_to_int(x, 0) for x in raw.split(',') if _to_int(x, 0) > 0}, reverse=True)
    return days or [7, 3, 1]


REMINDER_DAYS = _parse_reminders(_cfg('reminder_days', '7,3,1'))


def _sync_pro_config_to_db():
    for key in ('payment_channel_id', 'invoice_channel_id'):
        value = _cfg(key)
        if value and value != get_setting('cfg_' + key, ''):
            set_setting(key, value)
            set_setting('cfg_' + key, value)


_sync_pro_config_to_db()
PAYMENT_CHANNEL_ID = _to_int(get_setting('payment_channel_id', '0'))
INVOICE_CHANNEL_ID = _to_int(get_setting('invoice_channel_id', '0'))

PERIODS: Dict[str, Dict[str, Any]] = {
    '1':  {'months': 1,  'days': 30,  'discount': 0,  'label': '1 Month'},
    '3':  {'months': 3,  'days': 90,  'discount': 5,  'label': '3 Months'},
    '6':  {'months': 6,  'days': 180, 'discount': 10, 'label': '6 Months'},
    '12': {'months': 12, 'days': 365, 'discount': 17, 'label': '12 Months'},
}

DEFAULT_PLANS = [
    # key, name, emoji, ram, cpu, disk, price/month (INR), perks
    ('starter', 'Starter', 'rocket', 4, 2, 40, 149.0),
    ('pro', 'Pro', 'bolt', 8, 4, 80, 299.0),
    ('elite', 'Elite', 'crown', 16, 6, 160, 599.0),
    ('ultra', 'Ultra', 'star', 32, 8, 320, 1199.0),
]
DEFAULT_PERKS = "Full root access • Docker-ready • Port forwarding • Mining-guard protected"

# ------------------------------------------------------------
# EMOJI SKIN — custom emojis replace plain unicode everywhere
# ------------------------------------------------------------
EMOJI_FALLBACK = {
    'ok': '✅', 'no': '❌', 'warn': '⚠️', 'online': '🟢', 'offline': '🔴', 'pending': '🟡',
    'bolt': '⚡', 'chev': '▸', 'card': '💳', 'receipt': '🧾', 'calendar': '📅', 'rupee': '💰',
    'crown': '👑', 'rocket': '🚀', 'server': '🖥️', 'cpu': '🧠', 'ram': '🧩', 'disk': '💽',
    'shield': '🛡️', 'siren': '🚨', 'pickaxe': '⛏️', 'clock': '⏱️', 'bell': '🔔', 'tag': '🏷️',
    'coin': '🪙', 'star': '⭐', 'key': '🔑', 'lock': '🔒', 'globe': '🌐', 'link': '🔗',
    'user': '👤', 'tools': '🔧', 'chart': '📊', 'note': '📝', 'docs': '📚', 'box': '📦',
    'hourglass': '⏳', 'refresh': '🔄', 'play': '▶️', 'pause': '⏸️', 'stop': '⏹️', 'back': '⬅️',
}

_UNI_BASES = {
    '✅': 'ok', '❌': 'no', '⚠': 'warn', '🟢': 'online', '🔴': 'offline', '🟡': 'pending',
    '📚': 'docs', '🖥': 'server', '🔗': 'link', '📋': 'note', '📝': 'note', '👤': 'user',
    '👥': 'user', '🛡': 'shield', '🔧': 'tools', '⚙': 'tools', '🛠': 'tools', '✏': 'tools',
    '📊': 'chart', '🎁': 'star', '🔑': 'key', '⏳': 'hourglass', '⏱': 'clock', '🔌': 'link',
    '🤖': 'cpu', '⚡': 'bolt', '🌩': 'bolt', '🌐': 'globe', '🔄': 'refresh', '▶': 'play',
    '⏸': 'pause', '⏹': 'stop', '👑': 'crown', '⛏': 'pickaxe', '📡': 'globe', '🔒': 'lock',
    '📦': 'box', '💰': 'rupee', '💾': 'disk', '💽': 'disk', '⬅': 'back', '🚨': 'siren',
    '🧠': 'cpu', '🧩': 'ram', '🚀': 'rocket', '🎉': 'star', '🧹': 'tools', '💳': 'card',
    '🧾': 'receipt', '📅': 'calendar', '🏷': 'tag', '🪙': 'coin', '⭐': 'star', '🔔': 'bell',
}
UNI2NAME: Dict[str, str] = {'⌯⌲': 'chev'}
for _u, _n in _UNI_BASES.items():
    UNI2NAME[_u] = _n
    UNI2NAME[_u + '️'] = _n
_UNI_KEYS = sorted(UNI2NAME, key=len, reverse=True)
_CUSTOM_EMOJI_RE = re.compile(r'^<a?:\w{2,32}:\d{15,25}>$')
EMOJI_MAP: Dict[str, str] = {}


def load_emoji_map():
    global EMOJI_MAP
    raw = get_setting('emoji_map', '')
    found: Dict[str, str] = {}
    if raw:
        try:
            data = json.loads(raw)
            found = {k: v for k, v in data.items() if k in EMOJI_FALLBACK and isinstance(v, str) and _CUSTOM_EMOJI_RE.match(v)}
        except ValueError as e:
            logger.error(f"Stored emoji_map is corrupt, using fallback emojis: {e}")
    EMOJI_MAP = found
    _refresh_brand_icon()


def _refresh_brand_icon():
    global BRAND_ICON
    BRAND_ICON = em('bolt')


def em(name: str) -> str:
    return EMOJI_MAP.get(name) or EMOJI_FALLBACK.get(name, '')


def skin(text):
    """Swap plain unicode emojis for the custom pack (no-op until the pack is installed)."""
    if not text or not EMOJI_MAP or not isinstance(text, str):
        return text
    for uni in _UNI_KEYS:
        if uni in text:
            custom = EMOJI_MAP.get(UNI2NAME[uni])
            if custom:
                text = text.replace(uni, custom)
    return text


def _lead_emoji(label: str):
    """('ok', 'Rest of label') when a label starts with a mapped unicode emoji."""
    if not label or not EMOJI_MAP:
        return None, label
    for uni in _UNI_KEYS:
        if label.startswith(uni):
            custom = EMOJI_MAP.get(UNI2NAME[uni])
            if custom:
                return custom, label[len(uni):].strip()
    return None, label


def install_skin_patches():
    if getattr(discord, '_avm_skin_installed', False):
        return
    orig_embed_init = discord.Embed.__init__
    orig_add_field = discord.Embed.add_field
    orig_btn_init = discord.ui.Button.__init__
    orig_opt_init = discord.SelectOption.__init__

    def embed_init(self, *args, **kw):
        if EMOJI_MAP:
            for k in ('title', 'description'):
                if isinstance(kw.get(k), str):
                    kw[k] = skin(kw[k])
        orig_embed_init(self, *args, **kw)

    def add_field(self, *args, **kw):
        if EMOJI_MAP and not args:
            if isinstance(kw.get('name'), str):
                kw['name'] = skin(kw['name'])
            if isinstance(kw.get('value'), str):
                kw['value'] = skin(kw['value'])
        return orig_add_field(self, *args, **kw)

    def btn_init(self, *args, **kw):
        if EMOJI_MAP:
            emoji = kw.get('emoji')
            if isinstance(emoji, str) and emoji in UNI2NAME and EMOJI_MAP.get(UNI2NAME[emoji]):
                kw['emoji'] = EMOJI_MAP[UNI2NAME[emoji]]
            elif emoji is None and isinstance(kw.get('label'), str):
                custom, rest = _lead_emoji(kw['label'])
                if custom:
                    kw['emoji'] = custom
                    kw['label'] = rest or None
        orig_btn_init(self, *args, **kw)

    def opt_init(self, *args, **kw):
        if EMOJI_MAP:
            emoji = kw.get('emoji')
            if isinstance(emoji, str) and emoji in UNI2NAME and EMOJI_MAP.get(UNI2NAME[emoji]):
                kw['emoji'] = EMOJI_MAP[UNI2NAME[emoji]]
            elif emoji is None and isinstance(kw.get('label'), str):
                custom, rest = _lead_emoji(kw['label'])
                if custom and rest:
                    kw['emoji'] = custom
                    kw['label'] = rest
        orig_opt_init(self, *args, **kw)

    discord.Embed.__init__ = embed_init
    discord.Embed.add_field = add_field
    discord.ui.Button.__init__ = btn_init
    discord.SelectOption.__init__ = opt_init
    discord._avm_skin_installed = True


async def install_emoji_pack(guild) -> Dict[str, Any]:
    """Upload the bundled PNG pack to `guild` (reusing any avm_* emoji already there)."""
    global EMOJI_MAP
    res: Dict[str, Any] = {'created': [], 'reused': [], 'skipped': [], 'failed': [], 'fatal': None}
    if not EMOJI_ORDER:
        res['fatal'] = "avm_emojis.py is missing next to the bot file — upload it and restart."
        return res
    existing = {e.name: e for e in guild.emojis}
    static = sum(1 for e in guild.emojis if not e.animated)
    free = max(0, guild.emoji_limit - static)
    new_map = dict(EMOJI_MAP)
    for name in EMOJI_ORDER:
        full = f"avm_{name}"
        if full in existing:
            new_map[name] = str(existing[full])
            res['reused'].append(name)
            continue
        if free <= 0:
            res['skipped'].append(name)
            continue
        try:
            created = await guild.create_custom_emoji(name=full, image=base64.b64decode(EMOJI_PNG_B64[name]),
                                                      reason=f"{BOT_NAME} AVM Pro emoji pack")
            new_map[name] = str(created)
            res['created'].append(name)
            free -= 1
            await asyncio.sleep(0.35)
        except discord.Forbidden:
            res['fatal'] = "I need the **Manage Expressions** permission (Manage Emojis and Stickers) to upload emojis."
            break
        except discord.HTTPException as e:
            if 'maximum number' in str(e).lower():
                res['skipped'].append(name)
                free = 0
            else:
                res['failed'].append(f"{name}: {str(e)[:120]}")
    EMOJI_MAP = {k: v for k, v in new_map.items() if k in EMOJI_FALLBACK}
    set_setting('emoji_map', json.dumps(EMOJI_MAP))
    _refresh_brand_icon()
    return res


def sync_emoji_map_from_guilds() -> int:
    """Self-heal: rebuild the map from avm_* emojis that really exist in the bot's servers."""
    global EMOJI_MAP
    found: Dict[str, str] = {}
    for g in bot.guilds:
        for e in g.emojis:
            if e.name.startswith('avm_') and e.name[4:] in EMOJI_FALLBACK and e.name[4:] not in found:
                found[e.name[4:]] = str(e)
    if found != EMOJI_MAP:
        if EMOJI_MAP and not found:
            logger.warning("Saved AVM emojis no longer exist in any server — falling back to normal emojis. Run .setupemojis")
        EMOJI_MAP = found
        set_setting('emoji_map', json.dumps(found))
        _refresh_brand_icon()
    return len(found)


# ------------------------------------------------------------
# PRO DATABASE
# ------------------------------------------------------------
def init_pro_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS pro_plans (
        key TEXT PRIMARY KEY, name TEXT NOT NULL, emoji TEXT DEFAULT 'rocket',
        ram INTEGER NOT NULL, cpu INTEGER NOT NULL, disk INTEGER NOT NULL,
        price REAL NOT NULL, stock INTEGER DEFAULT -1, active INTEGER DEFAULT 1,
        sort INTEGER DEFAULT 0, perks TEXT DEFAULT ''
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS coupons (
        code TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'percent', value REAL NOT NULL,
        max_uses INTEGER DEFAULT 0, used INTEGER DEFAULT 0, expires_at TEXT,
        plan_key TEXT, active INTEGER DEFAULT 1, created_at TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS coupon_uses (
        code TEXT NOT NULL, user_id TEXT NOT NULL, order_id INTEGER, PRIMARY KEY (code, user_id)
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL, kind TEXT NOT NULL, plan_key TEXT, plan_name TEXT,
        ram INTEGER, cpu INTEGER, disk INTEGER, months INTEGER DEFAULT 1, days INTEGER DEFAULT 30,
        os_version TEXT, container_name TEXT,
        unit_price REAL, subtotal REAL, period_discount REAL, coupon_code TEXT, coupon_discount REAL,
        tax REAL, total REAL, currency TEXT DEFAULT 'INR', pay_amount REAL, pay_currency TEXT,
        method TEXT, status TEXT NOT NULL, gateway_ref TEXT, gateway_url TEXT, payer_ref TEXT,
        proof_url TEXT, note TEXT, approved_by TEXT, reject_reason TEXT,
        fulfilled INTEGER DEFAULT 0, fulfill_error TEXT, nudged INTEGER DEFAULT 0,
        created_at TEXT NOT NULL, expires_at TEXT, paid_at TEXT
    )''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)')
    cur.execute('''CREATE TABLE IF NOT EXISTS payment_refs (
        ref TEXT PRIMARY KEY, order_id INTEGER NOT NULL, user_id TEXT NOT NULL, created_at TEXT NOT NULL
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
        container_name TEXT PRIMARY KEY, user_id TEXT NOT NULL, plan_key TEXT, plan_name TEXT,
        monthly_price REAL DEFAULT 0, period_days INTEGER DEFAULT 30,
        started_at TEXT, expires_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
        last_reminder INTEGER DEFAULT 99, grace_warned INTEGER DEFAULT 0, delete_alerted INTEGER DEFAULT 0
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS invoices (
        number TEXT PRIMARY KEY, order_id INTEGER NOT NULL, user_id TEXT NOT NULL,
        issued_at TEXT NOT NULL, total REAL NOT NULL, data TEXT NOT NULL
    )''')
    cur.execute('SELECT COUNT(*) FROM pro_plans')
    if cur.fetchone()[0] == 0:
        for i, (key, name, emoji, ram, cpu, disk, price) in enumerate(DEFAULT_PLANS):
            cur.execute('INSERT INTO pro_plans (key, name, emoji, ram, cpu, disk, price, sort, perks) VALUES (?,?,?,?,?,?,?,?,?)',
                        (key, name, emoji, ram, cpu, disk, price, i, DEFAULT_PERKS))
    conn.commit()
    conn.close()


init_pro_db()
install_skin_patches()
load_emoji_map()


def db_rows(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def db_row(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    rows = db_rows(sql, params)
    return rows[0] if rows else None


def db_exec(sql: str, params: tuple = ()) -> int:
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:  # a failed statement (e.g. duplicate key) must never leave the write lock held
        conn.close()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ts(dt: datetime) -> int:
    return int(dt.timestamp())


# ------------------------------------------------------------
# PLANS, QUOTES, COUPONS
# ------------------------------------------------------------
def q2(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal('0.01'), ROUND_HALF_UP)


def money(x) -> str:
    return f"{CURRENCY_SYMBOL}{float(x):,.2f}"


def usd_amount(inr_total: float) -> float:
    return max(0.50, float(q2(Decimal(str(inr_total)) / Decimal(str(USD_RATE)))))


def get_plans(active_only: bool = True) -> List[Dict[str, Any]]:
    sql = 'SELECT * FROM pro_plans' + (' WHERE active = 1' if active_only else '') + ' ORDER BY sort, price'
    return db_rows(sql)


def get_plan(key: str) -> Optional[Dict[str, Any]]:
    return db_row('SELECT * FROM pro_plans WHERE key = ?', (key,))


def plan_in_stock(plan: Dict[str, Any]) -> bool:
    return plan['stock'] < 0 or plan['stock'] > 0


def compute_quote(unit_price: float, months: int, period_discount_pct: float = 0,
                  coupon: Optional[Dict[str, Any]] = None, tax_pct: float = None,
                  months_multiplier: bool = True) -> Dict[str, float]:
    """Pure pricing maths. Tax is added on top of the discounted price."""
    tax_pct = TAX_PERCENT if tax_pct is None else tax_pct
    base = q2(unit_price) * (months if months_multiplier else 1)
    period_disc = q2(base * Decimal(str(period_discount_pct)) / 100)
    after = base - period_disc
    coupon_disc = Decimal('0.00')
    if coupon:
        if coupon['kind'] == 'percent':
            coupon_disc = q2(after * Decimal(str(coupon['value'])) / 100)
        else:
            coupon_disc = q2(min(Decimal(str(coupon['value'])), after))
        coupon_disc = min(coupon_disc, after - Decimal('1.00')) if after > Decimal('1.00') else Decimal('0.00')
        coupon_disc = max(coupon_disc, Decimal('0.00'))
    taxable = after - coupon_disc
    tax = q2(taxable * Decimal(str(tax_pct)) / 100)
    total = taxable + tax
    return {'subtotal': float(base), 'period_discount': float(period_disc), 'coupon_discount': float(coupon_disc),
            'taxable': float(taxable), 'tax': float(tax), 'total': float(total)}


def prorate_upgrade(old_monthly: float, new_monthly: float, seconds_left: float) -> Optional[float]:
    """Price difference per day × remaining days (None if not an upgrade)."""
    diff = Decimal(str(new_monthly)) - Decimal(str(old_monthly))
    if diff <= 0:
        return None
    days_left = Decimal(str(max(0.0, seconds_left))) / Decimal('86400')
    return float(max(Decimal('1.00'), q2(diff / 30 * days_left)))


def validate_coupon(code: str, user_id: str, plan_key: Optional[str], kind: str) -> tuple:
    """Returns (coupon_row | None, error_message | None)."""
    code = (code or '').strip().upper()
    if not code:
        return None, "Enter a coupon code."
    row = db_row('SELECT * FROM coupons WHERE code = ?', (code,))
    if not row or not row['active']:
        return None, "That coupon code is not valid."
    if kind == 'upgrade':
        return None, "Coupons can't be used on plan upgrades."
    if row['expires_at'] and parse_iso(row['expires_at']) < utc_now():
        return None, "That coupon has expired."
    if row['max_uses'] and row['used'] >= row['max_uses']:
        return None, "That coupon has reached its usage limit."
    if row['plan_key'] and plan_key and row['plan_key'] != plan_key:
        plan = get_plan(row['plan_key'])
        return None, f"That coupon only works on the **{plan['name'] if plan else row['plan_key']}** plan."
    if db_row('SELECT 1 FROM coupon_uses WHERE code = ? AND user_id = ?', (code, user_id)):
        return None, "You have already used that coupon."
    return row, None


# ------------------------------------------------------------
# ORDERS
# ------------------------------------------------------------
def order_ref(order_id: int) -> str:
    return f"AVM-{int(order_id):06d}"


def parse_order_ref(text: str) -> Optional[int]:
    m = re.search(r'(\d{1,9})$', (text or '').strip())
    return int(m.group(1)) if m else None


def get_order(order_id: int) -> Optional[Dict[str, Any]]:
    return db_row('SELECT * FROM orders WHERE id = ?', (order_id,))


def insert_order(**f) -> int:
    f.setdefault('created_at', iso(utc_now()))
    cols = ', '.join(f)
    marks = ', '.join('?' for _ in f)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(f'INSERT INTO orders ({cols}) VALUES ({marks})', tuple(f.values()))
        oid = cur.lastrowid
        conn.commit()
        return oid
    finally:
        conn.close()


def update_order(order_id: int, **f) -> int:
    sets = ', '.join(f"{k} = ?" for k in f)
    return db_exec(f'UPDATE orders SET {sets} WHERE id = ?', tuple(f.values()) + (order_id,))


def transition_order(order_id: int, new_status: str, allowed_from: tuple, **f) -> bool:
    """Atomic status change; False when the order is not in an allowed state (prevents double-processing)."""
    f['status'] = new_status
    sets = ', '.join(f"{k} = ?" for k in f)
    marks = ', '.join('?' for _ in allowed_from)
    n = db_exec(f'UPDATE orders SET {sets} WHERE id = ? AND status IN ({marks})', tuple(f.values()) + (order_id,) + tuple(allowed_from))
    return n == 1


def open_orders_for(user_id: str) -> List[Dict[str, Any]]:
    return db_rows("SELECT * FROM orders WHERE user_id = ? AND status IN ('awaiting_payment','verifying') ORDER BY id DESC", (user_id,))


# ------------------------------------------------------------
# SUBSCRIPTIONS / INVOICES
# ------------------------------------------------------------
def get_sub(container: str) -> Optional[Dict[str, Any]]:
    return db_row('SELECT * FROM subscriptions WHERE container_name = ?', (container,))


def user_subs(user_id: str) -> List[Dict[str, Any]]:
    return db_rows('SELECT * FROM subscriptions WHERE user_id = ? ORDER BY expires_at', (user_id,))


def upsert_sub(container: str, user_id: str, plan_key: str, plan_name: str, monthly_price: float,
               period_days: int, expires_at: datetime, started_at: Optional[datetime] = None):
    db_exec('''INSERT INTO subscriptions (container_name, user_id, plan_key, plan_name, monthly_price, period_days,
                   started_at, expires_at, status, last_reminder, grace_warned, delete_alerted)
               VALUES (?,?,?,?,?,?,?,?, 'active', 99, 0, 0)
               ON CONFLICT(container_name) DO UPDATE SET user_id = excluded.user_id, plan_key = excluded.plan_key,
                   plan_name = excluded.plan_name, monthly_price = excluded.monthly_price,
                   period_days = excluded.period_days, expires_at = excluded.expires_at, status = 'active',
                   last_reminder = 99, grace_warned = 0, delete_alerted = 0''',
            (container, user_id, plan_key, plan_name, monthly_price, period_days,
             iso(started_at or utc_now()), iso(expires_at)))


def next_invoice_number() -> str:
    year = utc_now().year
    row = db_row("SELECT COUNT(*) AS n FROM invoices WHERE number LIKE ?", (f"INV-{year}-%",))
    return f"INV-{year}-{(row['n'] if row else 0) + 1:04d}"


def create_invoice(order: Dict[str, Any], container: str, expires_at: Optional[datetime], customer_name: str = '') -> Dict[str, Any]:
    existing = db_row('SELECT * FROM invoices WHERE order_id = ?', (order['id'],))
    if existing:
        return {**existing, 'data': json.loads(existing['data'])}
    data = {
        'order_ref': order_ref(order['id']), 'kind': order['kind'], 'plan_name': order['plan_name'],
        'months': order['months'], 'days': order['days'], 'container': container,
        'unit_price': order['unit_price'], 'subtotal': order['subtotal'],
        'period_discount': order['period_discount'], 'coupon_code': order['coupon_code'],
        'coupon_discount': order['coupon_discount'], 'tax': order['tax'], 'total': order['total'],
        'tax_label': TAX_LABEL, 'tax_percent': TAX_PERCENT, 'method': order['method'],
        'payer_ref': order['payer_ref'] or order['gateway_ref'] or '', 'paid_at': order['paid_at'],
        'valid_until': iso(expires_at) if expires_at else '', 'customer_id': order['user_id'],
        'ram': order['ram'], 'cpu': order['cpu'], 'disk': order['disk'], 'customer_name': customer_name,
    }
    for _ in range(5):
        number = next_invoice_number()
        try:
            db_exec('INSERT INTO invoices (number, order_id, user_id, issued_at, total, data) VALUES (?,?,?,?,?,?)',
                    (number, order['id'], order['user_id'], iso(utc_now()), order['total'], json.dumps(data)))
            return {'number': number, 'order_id': order['id'], 'user_id': order['user_id'],
                    'issued_at': iso(utc_now()), 'total': order['total'], 'data': data}
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Could not allocate an invoice number")


# ------------------------------------------------------------
# PAYMENT METHODS + GATEWAYS (all HTTP goes through http_json so it can be unit-tested)
# ------------------------------------------------------------
class GatewayError(Exception):
    pass


async def http_json(method: str, url: str, *, headers=None, auth=None, json_body=None, form=None, timeout: int = 20):
    kw: Dict[str, Any] = {'headers': headers or {}}
    if auth:
        kw['auth'] = aiohttp.BasicAuth(*auth)
    if json_body is not None:
        kw['json'] = json_body
    if form is not None:
        kw['data'] = form
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.request(method, url, **kw) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text) if text else {}
                except ValueError:
                    data = {'_raw': text[:300]}
                return resp.status, data
    except asyncio.TimeoutError:
        raise GatewayError("the payment provider did not answer in time")
    except aiohttp.ClientError as e:
        raise GatewayError(f"network error talking to the payment provider: {e}")


def _api_error(data: Dict[str, Any], status: int) -> str:
    err = data.get('error') if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get('description') or err.get('message') or err.get('code') or err)[:300]
    if isinstance(data, dict):
        for k in ('message', 'error_description', 'name', '_raw'):
            if data.get(k):
                return str(data[k])[:300]
    return f"HTTP {status}"


METHOD_INFO = {
    'upi':       {'label': 'UPI', 'emoji': 'rupee', 'auto': False},
    'crypto':    {'label': 'Crypto (USDT / BTC …)', 'emoji': 'coin', 'auto': False},
    'razorpay':  {'label': 'Razorpay (Card / UPI / NetBanking)', 'emoji': 'card', 'auto': True},
    'stripe':    {'label': 'Card (Stripe)', 'emoji': 'card', 'auto': True},
    'paypal':    {'label': 'PayPal', 'emoji': 'globe', 'auto': True},
}


def configured_methods() -> List[str]:
    ok = []
    if UPI_ID:
        ok.append('upi')
    if CRYPTO_WALLETS:
        ok.append('crypto')
    if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
        ok.append('razorpay')
    if STRIPE_SECRET_KEY:
        ok.append('stripe')
    if PAYPAL_CLIENT_ID and PAYPAL_SECRET:
        ok.append('paypal')
    return ok


def method_label(method: str) -> str:
    return METHOD_INFO.get(method, {}).get('label', method or '—')


# ---- Razorpay (Payment Links) ----
RZP_BASE = "https://api.razorpay.com/v1"


async def razorpay_create(order: Dict[str, Any]) -> tuple:
    paise = int((q2(order['total']) * 100).to_integral_value())
    body = {
        'amount': paise, 'currency': 'INR', 'accept_partial': False, 'reference_id': order_ref(order['id']),
        'description': f"{BOT_NAME} {order['plan_name']} ({order['kind']})"[:200],
        'expire_by': int(time.time()) + (AUTO_PAY_WINDOW_MIN + 5) * 60,
        'notify': {'sms': False, 'email': False}, 'reminder_enable': False,
        'notes': {'order': order_ref(order['id']), 'discord_id': order['user_id']},
    }
    status, data = await http_json('POST', f"{RZP_BASE}/payment_links", auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), json_body=body)
    if status not in (200, 201) or not data.get('id') or not data.get('short_url'):
        raise GatewayError(f"Razorpay rejected the request: {_api_error(data, status)}")
    return data['id'], data['short_url']


async def razorpay_status(order: Dict[str, Any]) -> tuple:
    status, data = await http_json('GET', f"{RZP_BASE}/payment_links/{order['gateway_ref']}", auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    if status != 200:
        raise GatewayError(f"Razorpay status check failed: {_api_error(data, status)}")
    st = data.get('status')
    expected = int((q2(order['total']) * 100).to_integral_value())
    if st == 'paid':
        if int(data.get('amount_paid') or 0) >= expected:
            return 'paid', f"Razorpay link {order['gateway_ref']}"
        return 'mismatch', f"Razorpay says paid but amount_paid={data.get('amount_paid')} (expected {expected} paise)"
    if st in ('expired', 'cancelled'):
        return 'expired', st
    return 'pending', st or 'created'


# ---- Stripe (Checkout Sessions) ----
STRIPE_BASE = "https://api.stripe.com/v1"


async def stripe_create(order: Dict[str, Any]) -> tuple:
    cents = int((q2(order['pay_amount']) * 100).to_integral_value())
    form = {
        'mode': 'payment', 'success_url': RETURN_URL, 'cancel_url': RETURN_URL,
        'client_reference_id': order_ref(order['id']),
        'line_items[0][quantity]': '1', 'line_items[0][price_data][currency]': 'usd',
        'line_items[0][price_data][unit_amount]': str(cents),
        'line_items[0][price_data][product_data][name]': f"{BOT_NAME} {order['plan_name']} ({order['kind']})"[:120],
        'expires_at': str(int(time.time()) + (AUTO_PAY_WINDOW_MIN + 2) * 60),
        'metadata[order]': order_ref(order['id']), 'metadata[discord_id]': order['user_id'],
    }
    status, data = await http_json('POST', f"{STRIPE_BASE}/checkout/sessions", headers={'Authorization': f"Bearer {STRIPE_SECRET_KEY}"}, form=form)
    if status not in (200, 201) or not data.get('id') or not data.get('url'):
        raise GatewayError(f"Stripe rejected the request: {_api_error(data, status)}")
    return data['id'], data['url']


async def stripe_status(order: Dict[str, Any]) -> tuple:
    status, data = await http_json('GET', f"{STRIPE_BASE}/checkout/sessions/{order['gateway_ref']}", headers={'Authorization': f"Bearer {STRIPE_SECRET_KEY}"})
    if status != 200:
        raise GatewayError(f"Stripe status check failed: {_api_error(data, status)}")
    cents = int((q2(order['pay_amount']) * 100).to_integral_value())
    if data.get('payment_status') == 'paid':
        if int(data.get('amount_total') or 0) >= cents and str(data.get('currency', 'usd')).lower() == 'usd':
            return 'paid', f"Stripe session {order['gateway_ref']}"
        return 'mismatch', f"Stripe says paid but amount_total={data.get('amount_total')} {data.get('currency')} (expected {cents} usd cents)"
    if data.get('status') == 'expired':
        return 'expired', 'expired'
    return 'pending', data.get('status') or 'open'


# ---- PayPal (Orders v2: create → payer approves → we capture) ----
_paypal_token: Dict[str, Any] = {'value': None, 'exp': 0.0}


def paypal_base() -> str:
    return "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == 'sandbox' else "https://api-m.paypal.com"


async def paypal_token() -> str:
    if _paypal_token['value'] and time.time() < _paypal_token['exp'] - 60:
        return _paypal_token['value']
    status, data = await http_json('POST', f"{paypal_base()}/v1/oauth2/token", auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
                                   form={'grant_type': 'client_credentials'})
    if status != 200 or not data.get('access_token'):
        raise GatewayError(f"PayPal login failed: {_api_error(data, status)}")
    _paypal_token.update(value=data['access_token'], exp=time.time() + int(data.get('expires_in', 300)))
    return _paypal_token['value']


async def paypal_create(order: Dict[str, Any]) -> tuple:
    token = await paypal_token()
    body = {
        'intent': 'CAPTURE',
        'purchase_units': [{'reference_id': order_ref(order['id']), 'custom_id': order['user_id'],
                            'description': f"{BOT_NAME} {order['plan_name']}"[:120],
                            'amount': {'currency_code': 'USD', 'value': f"{q2(order['pay_amount'])}"}}],
        'payment_source': {'paypal': {'experience_context': {
            'return_url': RETURN_URL, 'cancel_url': RETURN_URL, 'user_action': 'PAY_NOW', 'shipping_preference': 'NO_SHIPPING'}}},
    }
    status, data = await http_json('POST', f"{paypal_base()}/v2/checkout/orders",
                                   headers={'Authorization': f"Bearer {token}", 'PayPal-Request-Id': f"avm-{order['id']}"}, json_body=body)
    if status not in (200, 201) or not data.get('id'):
        raise GatewayError(f"PayPal rejected the request: {_api_error(data, status)}")
    link = next((l.get('href') for l in data.get('links', []) if l.get('rel') in ('payer-action', 'approve')), None)
    if not link:
        raise GatewayError("PayPal did not return an approval link")
    return data['id'], link


async def paypal_status(order: Dict[str, Any]) -> tuple:
    token = await paypal_token()
    hdr = {'Authorization': f"Bearer {token}"}
    status, data = await http_json('GET', f"{paypal_base()}/v2/checkout/orders/{order['gateway_ref']}", headers=hdr)
    if status != 200:
        raise GatewayError(f"PayPal status check failed: {_api_error(data, status)}")
    st = data.get('status')
    if st == 'APPROVED':
        status, data = await http_json('POST', f"{paypal_base()}/v2/checkout/orders/{order['gateway_ref']}/capture",
                                       headers={**hdr, 'PayPal-Request-Id': f"avm-cap-{order['id']}", 'Content-Type': 'application/json'}, json_body={})
        if status not in (200, 201):
            raise GatewayError(f"PayPal capture failed: {_api_error(data, status)}")
        st = data.get('status')
    if st == 'COMPLETED':
        try:
            cap = data['purchase_units'][0]['payments']['captures'][0]
        except (KeyError, IndexError, TypeError):
            return 'mismatch', "PayPal says completed but returned no capture details"
        if cap.get('status') != 'COMPLETED':
            return 'pending', f"capture {cap.get('status')}"
        amount = cap.get('amount', {})
        if amount.get('currency_code') == 'USD' and q2(amount.get('value', '0')) >= q2(order['pay_amount']):
            return 'paid', f"PayPal capture {cap.get('id', '')}"
        return 'mismatch', f"PayPal captured {amount} but expected {order['pay_amount']} USD"
    if st == 'VOIDED':
        return 'expired', 'voided'
    return 'pending', st or 'created'


GATEWAYS = {
    'razorpay': (razorpay_create, razorpay_status),
    'stripe': (stripe_create, stripe_status),
    'paypal': (paypal_create, paypal_status),
}


async def test_gateway_credentials() -> List[tuple]:
    """Real credential checks for `.paytest` — returns [(method, ok, detail)]."""
    out = []
    if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
        try:
            st, data = await http_json('GET', f"{RZP_BASE}/payment_links?count=1", auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
            out.append(('razorpay', st == 200, 'credentials accepted' if st == 200 else _api_error(data, st)))
        except GatewayError as e:
            out.append(('razorpay', False, str(e)))
    if STRIPE_SECRET_KEY:
        try:
            st, data = await http_json('GET', f"{STRIPE_BASE}/balance", headers={'Authorization': f"Bearer {STRIPE_SECRET_KEY}"})
            out.append(('stripe', st == 200, 'key accepted' if st == 200 else _api_error(data, st)))
        except GatewayError as e:
            out.append(('stripe', False, str(e)))
    if PAYPAL_CLIENT_ID and PAYPAL_SECRET:
        try:
            _paypal_token.update(value=None, exp=0.0)
            await paypal_token()
            out.append(('paypal', True, f"login ok ({PAYPAL_MODE})"))
        except GatewayError as e:
            out.append(('paypal', False, str(e)))
    return out


# ---- UPI helpers ----
def upi_link(total: float, order_id: int) -> str:
    return (f"upi://pay?pa={urllib.parse.quote(UPI_ID)}&pn={urllib.parse.quote(UPI_NAME)}"
            f"&am={q2(total)}&cu=INR&tn={urllib.parse.quote(order_ref(order_id))}")


def make_upi_qr(link: str) -> Optional[bytes]:
    try:
        import qrcode  # optional dependency
    except ImportError:
        return None
    try:
        img = qrcode.make(link)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    except Exception as e:
        logger.error(f"UPI QR generation failed: {type(e).__name__}: {e}")
        return None



# ============================================================
#   Part B: checkout • payment instructions • staff review •
#           fulfilment • invoices
# ============================================================
MAX_VPS_PER_USER = max(1, _to_int(_cfg('max_vps_per_user', '5'), 5))


def clean_ref(text: str) -> str:
    return re.sub(r'\s+', '', text or '')


async def dm_user(user_id, embed=None, view=None, files=None, content=None) -> bool:
    try:
        user = await bot.fetch_user(int(user_id))
        kw: Dict[str, Any] = {}
        if content:
            kw['content'] = content
        if embed:
            kw['embed'] = embed
        if view:
            kw['view'] = view
        if files:
            kw['files'] = files
        await user.send(**kw)
        return True
    except Exception as e:
        logger.info(f"DM to {user_id} not delivered: {type(e).__name__}")
        return False


async def post_to_channel(channel_id: int, *, embed=None, view=None, content=None, files=None) -> bool:
    if not channel_id:
        return False
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        kw: Dict[str, Any] = {'allowed_mentions': discord.AllowedMentions(users=True, roles=False, everyone=False)}
        for k, v in (('embed', embed), ('view', view), ('content', content), ('files', files)):
            if v:
                kw[k] = v
        await channel.send(**kw)
        return True
    except Exception as e:
        logger.error(f"Could not post to channel {channel_id}: {type(e).__name__}: {e}")
        return False


async def staff_billing_alert(embed, view=None, ping: bool = True) -> bool:
    """Billing queue: payment channel → fallback to the alert channel / admin DM (never silently lost)."""
    content = f"<@{MAIN_ADMIN_ID}>" if ping else None
    if await post_to_channel(PAYMENT_CHANNEL_ID, embed=embed, view=view, content=content):
        return True
    return await send_staff_alert(embed, view=view, content=content)


async def resolve_member(uid: str):
    guilds = []
    g = bot.get_guild(GUILD_ID) if GUILD_ID else None
    guilds = [g] if g else list(bot.guilds)
    for guild in guilds:
        member = guild.get_member(int(uid))
        if member:
            return guild, member
        try:
            return guild, await guild.fetch_member(int(uid))
        except Exception:
            continue
    return None, None


def ensure_customer(uid: str) -> Optional[str]:
    """Reason string when the user may not buy, else None."""
    banned = is_blacklisted(uid)
    if banned:
        return f"Your account is blacklisted from purchases.\nReason: {banned}"
    if MAINTENANCE_MODE and not is_staff(uid):
        return "The shop is paused while the system is in maintenance mode."
    return None


# ------------------------------------------------------------
# EMBED BUILDERS
# ------------------------------------------------------------
def price_table(q: Dict[str, float], coupon_code: Optional[str] = None, period_pct: float = 0, months: int = 1,
                unit: Optional[float] = None) -> str:
    rows = []
    label = f"Price × {months} mo" if months > 1 else "Price"
    rows.append((label, money(q['subtotal'])))
    if q['period_discount']:
        rows.append((f"Term discount ({period_pct:g}%)", "-" + money(q['period_discount'])))
    if q['coupon_discount']:
        rows.append((f"Coupon {coupon_code}", "-" + money(q['coupon_discount'])))
    if q['tax']:
        rows.append((f"{TAX_LABEL} ({TAX_PERCENT:g}%)", "+" + money(q['tax'])))
    width = max(len(a) for a, _ in rows + [("TOTAL", "")]) + 2
    vwidth = max(len(b) for _, b in rows + [("", money(q['total']))])
    lines = [a.ljust(width) + b.rjust(vwidth) for a, b in rows]
    lines.append("─" * (width + vwidth))
    lines.append("TOTAL".ljust(width) + money(q['total']).rjust(vwidth))
    return "```\n" + "\n".join(lines) + "\n```"


def specs_line(ram, cpu, disk) -> str:
    return f"{em('ram')} **{ram}GB** RAM  {em('cpu')} **{cpu}** vCPU  {em('disk')} **{disk}GB** Disk"


def state_plan(state) -> Optional[Dict[str, Any]]:
    """Plan dict for a checkout state (renewals fall back to the stored subscription when the plan is gone)."""
    if state.get('plan_key'):
        p = get_plan(state['plan_key'])
        if p:
            return p
    if state['kind'] == 'renew':
        sub = get_sub(state['container'])
        found = find_vps(state['container'])
        if sub and found:
            v = found[1]
            return {'key': sub['plan_key'], 'name': sub['plan_name'], 'emoji': 'server', 'price': sub['monthly_price'],
                    'ram': _to_int(re.sub(r'\D', '', str(v['ram']))), 'cpu': _to_int(re.sub(r'\D', '', str(v['cpu']))),
                    'disk': _to_int(re.sub(r'\D', '', str(v['storage']))), 'stock': -1, 'active': 0}
    return None


def state_quote(state) -> Optional[Dict[str, Any]]:
    plan = state_plan(state)
    if not plan:
        return None
    coupon = state.get('coupon')
    if state['kind'] == 'upgrade':
        sub = get_sub(state['container'])
        if not sub:
            return None
        left = (parse_iso(sub['expires_at']) - utc_now()).total_seconds()
        prorated = prorate_upgrade(sub['monthly_price'], plan['price'], left)
        if prorated is None:
            return None
        q = compute_quote(prorated, 1, 0, None, months_multiplier=False)
        q.update(months=1, days=0, period_pct=0, plan=plan, prorated=prorated)
        return q
    period = PERIODS[state['months_key']]
    q = compute_quote(plan['price'], period['months'], period['discount'], coupon)
    q.update(months=period['months'], days=period['days'], period_pct=period['discount'], plan=plan)
    return q


def build_summary_embed(state) -> discord.Embed:
    q = state_quote(state)
    kind = state['kind']
    titles = {'new': 'Order Summary', 'renew': 'Renew Your VPS', 'upgrade': 'Upgrade Your VPS'}
    embed = create_info_embed(f"{em('receipt')} {titles[kind]}", "Review everything, then continue to payment.")
    if not q:
        add_field(embed, "Plan", "Pick a plan below to see the price." if kind != 'upgrade' else "This VPS has no higher plan to upgrade to.", False)
        return embed
    plan = q['plan']
    add_field(embed, f"{em(plan['emoji'])} {plan['name']}", specs_line(plan['ram'], plan['cpu'], plan['disk']), False)
    if kind == 'new':
        os_label = next((o['label'] for o in OS_OPTIONS if o['value'] == state['os']), state['os'])
        add_field(embed, "Billing Period", PERIODS[state['months_key']]['label'], True)
        add_field(embed, "Operating System", os_label, True)
    elif kind == 'renew':
        sub = get_sub(state['container'])
        add_field(embed, "VPS", f"`{state['container']}`", True)
        add_field(embed, "Billing Period", PERIODS[state['months_key']]['label'], True)
        if sub:
            base = max(parse_iso(sub['expires_at']), utc_now())
            add_field(embed, "New Expiry", f"<t:{ts(base + timedelta(days=q['days']))}:F>", False)
    else:
        sub = get_sub(state['container'])
        add_field(embed, "VPS", f"`{state['container']}`", True)
        if sub:
            add_field(embed, "Current Plan", sub['plan_name'], True)
            add_field(embed, "Proration", f"Only the difference for the remaining time of your current term (expires <t:{ts(parse_iso(sub['expires_at']))}:R>).", False)
    if state.get('coupon'):
        add_field(embed, "Coupon", f"`{state['coupon']['code']}` applied", True)
    add_field(embed, "Price", price_table(q, state['coupon']['code'] if state.get('coupon') else None, q['period_pct'], q['months']), False)
    if USD_RATE:
        add_field(embed, "Card / PayPal / Crypto", f"≈ **${usd_amount(q['total']):.2f}** USD", True)
    return embed


def build_instructions_embed(order: Dict[str, Any]) -> discord.Embed:
    method = order['method']
    ref = order_ref(order['id'])
    exp = parse_iso(order['expires_at'])
    embed = create_info_embed(f"{em('card')} Complete Your Payment", f"Order **{ref}** • {order['plan_name']} • expires <t:{ts(exp)}:R>")
    if method == 'upi':
        add_field(embed, "Pay To (UPI ID)", f"`{UPI_ID}`" + (f"\n{UPI_NAME}" if UPI_NAME else ""), True)
        add_field(embed, "Exact Amount", f"**{money(order['pay_amount'])}**", True)
        add_field(embed, "Payment Note", f"`{ref}`", True)
        add_field(embed, "Steps", "1. Pay the **exact amount** to the UPI ID (or scan the QR).\n"
                                  "2. Copy the **UTR / Transaction ID** from your app.\n"
                                  "3. Press **I've Paid** and enter it.\n"
                                  f"4. Optional: send a screenshot with `{PREFIX}proof {ref}`.", False)
    elif method == 'crypto':
        lines = [f"**{net}**\n`{addr}`" for net, addr in CRYPTO_WALLETS.items()]
        add_field(embed, "Send To", "\n".join(lines), False)
        add_field(embed, "Exact Amount", f"**${order['pay_amount']:.2f}** worth (USDT, or equivalent value) • ≈ {money(order['total'])}", False)
        add_field(embed, "Steps", "1. Send on the **network you picked** — wrong network = lost funds.\n"
                                  "2. Copy the **transaction hash (TXID)**.\n"
                                  "3. Press **I've Paid** and paste it.\n"
                                  f"4. Network fees are paid by you; reference: `{ref}`.", False)
    else:
        usd = order['pay_currency'] == 'USD'
        amt = f"**${order['pay_amount']:.2f}** USD (≈ {money(order['total'])})" if usd else f"**{money(order['pay_amount'])}**"
        add_field(embed, "Amount", amt, True)
        add_field(embed, "Method", method_label(method), True)
        add_field(embed, "Delivery", "Automatic — your VPS is deployed within about a minute after payment is confirmed.", False)
        add_field(embed, "Steps", "1. Press **Open Payment Page** and pay.\n2. Come back and press **Check Payment** (or just wait, we check automatically).", False)
    return embed


def invoice_lines_embed(inv: Dict[str, Any]) -> discord.Embed:
    d = inv['data']
    paid = parse_iso(d['paid_at']) if d.get('paid_at') else None
    embed = create_success_embed(f"{em('receipt')} Invoice {inv['number']}", f"Thank you for choosing **{BOT_NAME}**!")
    add_field(embed, "Billed To", f"<@{d['customer_id']}>", True)
    add_field(embed, "Order", d['order_ref'], True)
    item = {'new': 'New VPS', 'renew': 'Renewal', 'upgrade': 'Upgrade'}.get(d['kind'], d['kind'])
    desc = f"{item} — **{d['plan_name']}**"
    if d['kind'] != 'upgrade':
        desc += f" × {d['months']} mo"
    if d.get('container'):
        desc += f"\n`{d['container']}`"
    add_field(embed, "Item", desc, False)
    q = {'subtotal': d['subtotal'], 'period_discount': d['period_discount'], 'coupon_discount': d['coupon_discount'],
         'tax': d['tax'], 'total': d['total']}
    pct = (d['period_discount'] / d['subtotal'] * 100) if d['subtotal'] else 0
    add_field(embed, "Amount", price_table(q, d.get('coupon_code'), round(pct), d['months'] if d['kind'] != 'upgrade' else 1), False)
    add_field(embed, "Paid Via", f"{method_label(d['method'])}" + (f"\nRef: `{d['payer_ref']}`" if d.get('payer_ref') else ""), True)
    if paid:
        add_field(embed, "Paid On", f"<t:{ts(paid)}:F>", True)
    if d.get('valid_until'):
        add_field(embed, "Service Valid Until", f"<t:{ts(parse_iso(d['valid_until']))}:F>", False)
    return embed


def _pil_font(size: int, bold: bool = False):
    from PIL import ImageFont
    names = (["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Arial Bold.ttf"] if bold
             else ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Arial.ttf"])
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def render_invoice_png(inv: Dict[str, Any], customer_name: str = '') -> Optional[bytes]:
    """Pretty PNG receipt. Needs Pillow; returns None (caller falls back to the embed) when unavailable."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    try:
        d = inv['data']
        W, H = 900, 780
        img = Image.new('RGB', (W, H), (250, 250, 253))
        dr = ImageDraw.Draw(img)
        for y in range(150):  # header gradient
            t = y / 150
            dr.line([(0, y), (W, y)], fill=(int(98 + 40 * t), int(58 + 20 * t), int(222 - 20 * t)))
        dr.text((48, 38), BOT_NAME.upper(), font=_pil_font(38, True), fill=(255, 255, 255))
        dr.text((48, 92), "Axion VPS Manager Pro • Cloud Hosting", font=_pil_font(18), fill=(225, 218, 255))
        dr.text((W - 48, 42), "INVOICE", font=_pil_font(34, True), fill=(255, 255, 255), anchor="ra")
        dr.text((W - 48, 92), inv['number'], font=_pil_font(20), fill=(225, 218, 255), anchor="ra")

        ink, mute = (34, 38, 56), (112, 118, 140)
        y = 190
        dr.text((48, y), "BILLED TO", font=_pil_font(14, True), fill=mute)
        dr.text((48, y + 24), customer_name or d['customer_id'], font=_pil_font(24, True), fill=ink)
        dr.text((48, y + 58), f"Discord ID {d['customer_id']}", font=_pil_font(15), fill=mute)
        dr.text((W - 48, y), "ORDER / DATE", font=_pil_font(14, True), fill=mute, anchor="ra")
        dr.text((W - 48, y + 24), d['order_ref'], font=_pil_font(24, True), fill=ink, anchor="ra")
        paid = parse_iso(d['paid_at']).strftime('%d %b %Y, %H:%M UTC') if d.get('paid_at') else '—'
        dr.text((W - 48, y + 58), paid, font=_pil_font(15), fill=mute, anchor="ra")

        y = 320
        dr.rounded_rectangle([40, y, W - 40, y + 46], radius=10, fill=(238, 235, 252))
        dr.text((60, y + 12), "DESCRIPTION", font=_pil_font(14, True), fill=(98, 58, 200))
        dr.text((W - 60, y + 12), "AMOUNT", font=_pil_font(14, True), fill=(98, 58, 200), anchor="ra")
        item = {'new': 'New VPS', 'renew': 'Renewal', 'upgrade': 'Upgrade'}.get(d['kind'], d['kind'])
        title = f"{item} — {d['plan_name']}" + (f" × {d['months']} month(s)" if d['kind'] != 'upgrade' else "")
        dr.text((60, y + 66), title, font=_pil_font(21, True), fill=ink)
        sub = f"{d['ram']}GB RAM • {d['cpu']} vCPU • {d['disk']}GB Disk" if d.get('ram') else ""
        dr.text((60, y + 98), sub, font=_pil_font(15), fill=mute)
        if d.get('container'):
            dr.text((60, y + 122), d['container'], font=_pil_font(14), fill=mute)
        dr.text((W - 60, y + 66), f"INR {d['subtotal']:,.2f}", font=_pil_font(21, True), fill=ink, anchor="ra")

        y = 500
        rows = []
        if d['period_discount']:
            rows.append(("Term discount", f"- INR {d['period_discount']:,.2f}"))
        if d['coupon_discount']:
            rows.append((f"Coupon {d.get('coupon_code') or ''}".strip(), f"- INR {d['coupon_discount']:,.2f}"))
        if d['tax']:
            rows.append((f"{d.get('tax_label', 'Tax')} ({d.get('tax_percent', 0):g}%)", f"+ INR {d['tax']:,.2f}"))
        for label, value in rows:
            dr.text((W - 300, y), label, font=_pil_font(17), fill=mute)
            dr.text((W - 60, y), value, font=_pil_font(17), fill=ink, anchor="ra")
            y += 32
        y += 6
        dr.line([(W - 330, y), (W - 60, y)], fill=(210, 212, 225), width=2)
        dr.text((W - 300, y + 14), "TOTAL PAID", font=_pil_font(18, True), fill=ink)
        dr.text((W - 60, y + 10), f"INR {d['total']:,.2f}", font=_pil_font(28, True), fill=(32, 160, 90), anchor="ra")

        dr.text((60, 560), "PAID VIA", font=_pil_font(14, True), fill=mute)
        dr.text((60, 584), method_label(d['method']).split(' (')[0], font=_pil_font(20, True), fill=ink)
        if d.get('payer_ref'):
            dr.text((60, 614), f"Ref: {d['payer_ref'][:34]}", font=_pil_font(14), fill=mute)
        if d.get('valid_until'):
            dr.text((60, 650), "SERVICE VALID UNTIL", font=_pil_font(14, True), fill=mute)
            dr.text((60, 672), parse_iso(d['valid_until']).strftime('%d %b %Y'), font=_pil_font(20, True), fill=ink)

        cx, cy = W - 150, 690
        dr.ellipse([cx - 52, cy - 52, cx + 52, cy + 52], outline=(32, 160, 90), width=5)
        dr.text((cx, cy), "PAID", font=_pil_font(26, True), fill=(32, 160, 90), anchor="mm")
        dr.text((W // 2, H - 24), f"{BOT_NAME} • Thank you for your business", font=_pil_font(13), fill=mute, anchor="mm")
        buf = io.BytesIO()
        img.save(buf, format='PNG', optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Invoice PNG render failed: {type(e).__name__}: {e}")
        return None


# ------------------------------------------------------------
# CHECKOUT FLOW (all ephemeral — only the buyer sees prices/payment details)
# ------------------------------------------------------------
class CheckoutView(discord.ui.View):
    def __init__(self, user, state: Dict[str, Any]):
        super().__init__(timeout=900)
        self.user = user
        self.state = state
        kind = state['kind']
        self.plan_select = None
        self.period_select = None
        self.os_select = None

        if kind in ('new', 'upgrade'):
            plans = self._candidate_plans()
            self.plan_select = discord.ui.Select(
                placeholder="Choose a plan",
                options=[discord.SelectOption(label=f"{p['name']} — {money(p['price'])}/mo", value=p['key'],
                                              description=f"{p['ram']}GB RAM • {p['cpu']} vCPU • {p['disk']}GB Disk"[:100],
                                              emoji=em(p['emoji']) or None, default=(state.get('plan_key') == p['key']))
                         for p in plans[:25]])
            self.plan_select.callback = self.on_plan
            self.add_item(self.plan_select)
        if kind in ('new', 'renew'):
            self.period_select = discord.ui.Select(
                placeholder="Billing period",
                options=[discord.SelectOption(label=v['label'], value=k,
                                              description=(f"Save {v['discount']}%" if v['discount'] else "Standard price"),
                                              default=(state['months_key'] == k)) for k, v in PERIODS.items()])
            self.period_select.callback = self.on_period
            self.add_item(self.period_select)
        if kind == 'new':
            self.os_select = discord.ui.Select(
                placeholder="Operating system",
                options=[discord.SelectOption(label=o['label'], value=o['value'], default=(state['os'] == o['value'])) for o in OS_OPTIONS])
            self.os_select.callback = self.on_os
            self.add_item(self.os_select)

        if kind != 'upgrade':
            self.coupon_btn = discord.ui.Button(label="Apply Coupon", style=discord.ButtonStyle.secondary, emoji=em('tag') or None)
            self.coupon_btn.callback = self.on_coupon
            self.add_item(self.coupon_btn)
        self.continue_btn = discord.ui.Button(label="Continue to Payment", style=discord.ButtonStyle.success, emoji=em('chev') or None)
        self.continue_btn.callback = self.on_continue
        self.add_item(self.continue_btn)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self.on_cancel
        self.add_item(cancel)
        self._sync()

    def _candidate_plans(self):
        plans = [p for p in get_plans() if plan_in_stock(p)]
        if self.state['kind'] == 'upgrade':
            sub = get_sub(self.state['container'])
            plans = [p for p in plans if sub and p['price'] > sub['monthly_price']]
        return plans

    def _sync(self):
        s = self.state
        ready = state_quote(s) is not None
        if s['kind'] == 'new':
            ready = ready and bool(s.get('plan_key') and s.get('os'))
        self.continue_btn.disabled = not ready
        for sel, key in ((self.plan_select, 'plan_key'), (self.period_select, 'months_key'), (self.os_select, 'os')):
            if sel:
                for opt in sel.options:
                    opt.default = (opt.value == s.get(key))

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This checkout belongs to someone else.", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction):
        self._sync()
        await interaction.response.edit_message(embed=build_summary_embed(self.state), view=self)

    async def on_plan(self, interaction):
        self.state['plan_key'] = self.plan_select.values[0]
        if self.state.get('coupon'):  # plan-locked coupons must still match
            row, err = validate_coupon(self.state['coupon']['code'], str(self.user.id), self.state['plan_key'], self.state['kind'])
            if err:
                self.state['coupon'] = None
        await self._refresh(interaction)

    async def on_period(self, interaction):
        self.state['months_key'] = self.period_select.values[0]
        await self._refresh(interaction)

    async def on_os(self, interaction):
        self.state['os'] = self.os_select.values[0]
        await self._refresh(interaction)

    async def on_coupon(self, interaction):
        await interaction.response.send_modal(CouponModal(self))

    async def on_cancel(self, interaction):
        await interaction.response.edit_message(embed=create_info_embed("Checkout Cancelled", "Nothing was charged."), view=None)

    async def on_continue(self, interaction):
        uid = str(self.user.id)
        problem = ensure_customer(uid)
        methods = configured_methods()
        s = self.state
        if not problem and len(open_orders_for(uid)) >= MAX_OPEN_ORDERS:
            problem = f"You already have {MAX_OPEN_ORDERS} unpaid orders. Finish or cancel one first (`{PREFIX}billing`)."
        if not problem and not methods:
            problem = "No payment method is configured yet. Please contact staff."
        if not problem and s['kind'] == 'new':
            plan = get_plan(s['plan_key'])
            if not plan or not plan['active'] or not plan_in_stock(plan):
                problem = "That plan just became unavailable."
            elif len(vps_data.get(uid, [])) >= MAX_VPS_PER_USER:
                problem = f"You've reached the limit of {MAX_VPS_PER_USER} VPS per account."
        if not problem and s['kind'] in ('renew', 'upgrade'):
            found = find_vps(s['container'])
            if not found or found[0] != uid or not get_sub(s['container']):
                problem = "That VPS is no longer on your account."
        if problem:
            await interaction.response.send_message(embed=create_error_embed("Can't Continue", problem), ephemeral=True)
            return
        q = state_quote(s)
        view = PaymentMethodView(self.user, s, self)
        embed = build_summary_embed(s)
        add_field(embed, "Pay With", "Choose how you'd like to pay below.", False)
        await interaction.response.edit_message(embed=embed, view=view)


class CouponModal(discord.ui.Modal, title="Apply Coupon"):
    code = discord.ui.TextInput(label="Coupon code", placeholder="e.g. WELCOME10", min_length=3, max_length=32, required=True)

    def __init__(self, checkout: CheckoutView):
        super().__init__()
        self.checkout = checkout

    async def on_submit(self, interaction):
        s = self.checkout.state
        row, err = validate_coupon(str(self.code.value), str(interaction.user.id), s.get('plan_key'), s['kind'])
        if err:
            await interaction.response.send_message(embed=create_error_embed("Coupon Not Applied", err), ephemeral=True)
            return
        s['coupon'] = row
        self.checkout._sync()
        await interaction.response.edit_message(embed=build_summary_embed(s), view=self.checkout)


class PaymentMethodView(discord.ui.View):
    def __init__(self, user, state, checkout):
        super().__init__(timeout=900)
        self.user, self.state, self.checkout = user, state, checkout
        for method in configured_methods():
            info = METHOD_INFO[method]
            b = discord.ui.Button(label=info['label'], style=discord.ButtonStyle.primary, emoji=em(info['emoji']) or None)
            b.callback = self._make_cb(method)
            self.add_item(b)
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, emoji=em('back') or None)
        back.callback = self.on_back
        self.add_item(back)

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This checkout belongs to someone else.", ephemeral=True)
            return False
        return True

    async def on_back(self, interaction):
        self.checkout._sync()
        await interaction.response.edit_message(embed=build_summary_embed(self.state), view=self.checkout)

    def _make_cb(self, method):
        async def cb(interaction):
            await create_order_and_show(interaction, self.state, method, self)
        return cb


async def create_order_and_show(interaction, state, method: str, back_view=None):
    await interaction.response.defer()
    uid = str(interaction.user.id)
    q = state_quote(state)
    problem = ensure_customer(uid)
    if not q:
        problem = problem or "Your selection is no longer valid — start again."
    if state.get('coupon') and not problem:
        _, err = validate_coupon(state['coupon']['code'], uid, state.get('plan_key'), state['kind'])
        if err:
            problem = f"Coupon problem: {err}"
    if problem:
        await interaction.edit_original_response(embed=create_error_embed("Order Not Created", problem), view=None)
        return
    plan = q['plan']
    usd = method in ('stripe', 'paypal', 'crypto')
    pay_amount = usd_amount(q['total']) if usd else q['total']
    now = utc_now()
    window = AUTO_PAY_WINDOW_MIN if METHOD_INFO[method]['auto'] else MANUAL_PAY_WINDOW_MIN
    oid = insert_order(
        user_id=uid, kind=state['kind'], plan_key=plan['key'], plan_name=plan['name'], ram=plan['ram'], cpu=plan['cpu'], disk=plan['disk'],
        months=q['months'], days=q['days'], os_version=state.get('os'), container_name=state.get('container'),
        unit_price=q.get('prorated', plan['price']), subtotal=q['subtotal'], period_discount=q['period_discount'],
        coupon_code=state['coupon']['code'] if state.get('coupon') else None, coupon_discount=q['coupon_discount'],
        tax=q['tax'], total=q['total'], currency=CURRENCY_CODE, pay_amount=pay_amount, pay_currency='USD' if usd else 'INR',
        method=method, status='awaiting_payment', expires_at=iso(now + timedelta(minutes=window)))
    order = get_order(oid)
    files = []
    view = None
    if METHOD_INFO[method]['auto']:
        try:
            ref, url = await GATEWAYS[method][0](order)
        except GatewayError as e:
            update_order(oid, status='failed', note=str(e)[:300])
            logger.error(f"Gateway {method} create failed for order {oid}: {e}")
            await staff_billing_alert(create_error_embed("Payment Provider Error", f"{method_label(method)} could not create a payment for {order_ref(oid)}.\n```{str(e)[:700]}```Check keys with `{PREFIX}paytest`."), ping=False)
            await interaction.edit_original_response(embed=create_error_embed("Payment Provider Error", f"{e}\n\nNothing was charged. Try another method or contact staff."), view=back_view)
            return
        update_order(oid, gateway_ref=ref, gateway_url=url)
        order = get_order(oid)
    embed = build_instructions_embed(order)
    if method == 'upi':
        qr = make_upi_qr(upi_link(order['pay_amount'], oid))
        if qr:
            files = [discord.File(io.BytesIO(qr), filename='upi_qr.png')]
            embed.set_image(url='attachment://upi_qr.png')
        elif UPI_QR_URL:
            embed.set_image(url=UPI_QR_URL)
    view = PaymentView(interaction.user, oid)
    kw = {'embed': embed, 'view': view}
    if files:
        kw['attachments'] = files
    await interaction.edit_original_response(**kw)


class PaymentView(discord.ui.View):
    def __init__(self, user, order_id: int):
        super().__init__(timeout=MANUAL_PAY_WINDOW_MIN * 60)
        self.user, self.order_id = user, order_id
        order = get_order(order_id)
        if METHOD_INFO[order['method']]['auto']:
            self.add_item(discord.ui.Button(label="Open Payment Page", style=discord.ButtonStyle.link, url=order['gateway_url'], emoji=em('link') or None))
            chk = discord.ui.Button(label="Check Payment", style=discord.ButtonStyle.success, emoji=em('refresh') or None)
            chk.callback = self.on_check
            self.add_item(chk)
        else:
            paid = discord.ui.Button(label="I've Paid", style=discord.ButtonStyle.success, emoji=em('ok') or None)
            paid.callback = self.on_paid
            self.add_item(paid)
        cancel = discord.ui.Button(label="Cancel Order", style=discord.ButtonStyle.danger, emoji=em('no') or None)
        cancel.callback = self.on_cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This order belongs to someone else.", ephemeral=True)
            return False
        return True

    async def on_paid(self, interaction):
        order = get_order(self.order_id)
        if order['status'] != 'awaiting_payment':
            await interaction.response.send_message(embed=create_info_embed("Order Status", f"This order is **{order['status']}**."), ephemeral=True)
            return
        await interaction.response.send_modal(PaidModal(self.order_id))

    async def on_check(self, interaction):
        await interaction.response.defer(ephemeral=True)
        order = get_order(self.order_id)
        try:
            result = await poll_order(order)
        except GatewayError as e:
            await interaction.followup.send(embed=create_error_embed("Could Not Check Payment", str(e)), ephemeral=True)
            return
        texts = {'paid': ("Payment Received", "Thank you! Your order is being processed — you'll get a DM with your invoice."),
                 'pending': ("Not Paid Yet", "We haven't received the payment yet. If you just paid, wait a minute and press again."),
                 'expired': ("Payment Expired", "This payment link expired. Please place a new order."),
                 'mismatch': ("Payment Under Review", "The amount didn't match exactly, so staff will review it shortly."),
                 'closed': ("Order Closed", f"This order is already **{order['status']}**.")}
        title, body = texts.get(result, ("Order Status", result))
        maker = create_success_embed if result == 'paid' else create_info_embed
        await interaction.followup.send(embed=maker(title, body), ephemeral=True)

    async def on_cancel(self, interaction):
        ok = transition_order(self.order_id, 'cancelled', ('awaiting_payment',))
        if ok:
            await interaction.response.edit_message(embed=create_info_embed("Order Cancelled", f"{order_ref(self.order_id)} was cancelled. You were not charged."), view=None)
        else:
            await interaction.response.send_message(embed=create_error_embed("Can't Cancel", "This order is already being processed."), ephemeral=True)


class PaidModal(discord.ui.Modal, title="Confirm Your Payment"):
    ref = discord.ui.TextInput(label="UTR / Transaction ID", placeholder="Paste the reference from your payment app",
                               min_length=8, max_length=100, required=True)

    def __init__(self, order_id: int):
        super().__init__()
        self.order_id = order_id

    async def on_submit(self, interaction):
        order = get_order(self.order_id)
        value = clean_ref(str(self.ref.value))
        min_len = 16 if order['method'] == 'crypto' else 8
        if not re.fullmatch(r'[A-Za-z0-9]+', value) or len(value) < min_len:
            await interaction.response.send_message(embed=create_error_embed("Invalid Reference", f"A valid {'transaction hash' if order['method'] == 'crypto' else 'UTR'} is at least {min_len} letters/numbers with no symbols."), ephemeral=True)
            return
        try:
            db_exec('INSERT INTO payment_refs (ref, order_id, user_id, created_at) VALUES (?,?,?,?)',
                    (value.lower(), self.order_id, str(interaction.user.id), iso(utc_now())))
        except sqlite3.IntegrityError:
            other = db_row('SELECT order_id, user_id FROM payment_refs WHERE ref = ?', (value.lower(),))
            await staff_billing_alert(create_warning_embed("Duplicate Payment Reference",
                                      f"<@{interaction.user.id}> tried to use reference `{value}` on **{order_ref(self.order_id)}**, "
                                      f"but it was already used on **{order_ref(other['order_id'])}** (user <@{other['user_id']}>)."), ping=True)
            await interaction.response.send_message(embed=create_error_embed("Reference Already Used", "That reference was already submitted for another order. Staff have been notified."), ephemeral=True)
            return
        if not transition_order(self.order_id, 'verifying', ('awaiting_payment',), payer_ref=value):
            db_exec('DELETE FROM payment_refs WHERE ref = ?', (value.lower(),))
            await interaction.response.send_message(embed=create_error_embed("Order Not Open", "This order can no longer accept a payment confirmation."), ephemeral=True)
            return
        order = get_order(self.order_id)
        posted = await post_review_request(order)
        msg = (f"Reference `{value}` submitted for **{order_ref(self.order_id)}**.\nStaff will verify it shortly — you'll get a DM when your VPS is ready."
               f"\nTip: send a screenshot with `{PREFIX}proof {order_ref(self.order_id)}` to speed things up.")
        if not posted:
            msg += "\n⚠️ I couldn't reach the staff channel; please ping staff manually."
        await interaction.response.edit_message(embed=create_success_embed("Payment Submitted", msg), view=None)


# ------------------------------------------------------------
# STAFF REVIEW (persistent buttons — survive restarts)
# ------------------------------------------------------------
def review_embed(order: Dict[str, Any], state_text: Optional[str] = None, color: Optional[int] = None) -> discord.Embed:
    usd = order['pay_currency'] == 'USD'
    embed = create_warning_embed(f"{em('card')} Payment Verification Needed", "Check your bank / wallet that the **reference and amount** match before approving.")
    add_field(embed, "Customer", f"<@{order['user_id']}> (`{order['user_id']}`)", True)
    add_field(embed, "Order", f"**{order_ref(order['id'])}** • {order['kind']}", True)
    add_field(embed, "Plan", f"{order['plan_name']} × {order['months']} mo" if order['kind'] != 'upgrade' else f"Upgrade → {order['plan_name']}", True)
    amt = f"**${order['pay_amount']:.2f}** USD (≈ {money(order['total'])})" if usd else f"**{money(order['pay_amount'])}**"
    add_field(embed, "Expected Amount", amt, True)
    add_field(embed, "Method", method_label(order['method']), True)
    add_field(embed, "Reference", f"`{order['payer_ref'] or '—'}`", True)
    if order.get('coupon_code'):
        add_field(embed, "Coupon", order['coupon_code'], True)
    if order.get('proof_url'):
        add_field(embed, "Proof", order['proof_url'], False)
    if state_text:
        add_field(embed, "Decision", state_text, False)
    if color is not None:
        embed.color = color
    embed.set_footer(text=f"{order_ref(order['id'])} • {FOOTER_TEXT}")
    return embed


async def post_review_request(order: Dict[str, Any]) -> bool:
    return await staff_billing_alert(review_embed(order), view=StaffReviewView())


def order_id_from_message(message) -> Optional[int]:
    for e in getattr(message, 'embeds', []) or []:
        text = (getattr(getattr(e, 'footer', None), 'text', None) or '')
        m = re.search(r'AVM-(\d{6,9})', text)
        if m:
            return int(m.group(1))
    return None


class StaffReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        a = discord.ui.Button(label="Approve & Deliver", style=discord.ButtonStyle.success, emoji=em('ok') or None, custom_id="avmpro:approve")
        a.callback = self.on_approve
        r = discord.ui.Button(label="Reject", style=discord.ButtonStyle.danger, emoji=em('no') or None, custom_id="avmpro:reject")
        r.callback = self.on_reject
        self.add_item(a)
        self.add_item(r)

    async def interaction_check(self, interaction) -> bool:
        if is_staff(str(interaction.user.id)):
            return True
        await interaction.response.send_message(embed=create_error_embed("Access Denied", "Staff only."), ephemeral=True)
        return False

    async def on_approve(self, interaction):
        oid = order_id_from_message(interaction.message)
        if not oid:
            await interaction.response.send_message(embed=create_error_embed("Unknown Order", "Could not read the order from this message."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        paid, msg = await finalize_order(oid, by=str(interaction.user.id), via='manual')
        order = get_order(oid)
        if paid:
            try:
                await interaction.message.edit(embed=review_embed(order, f"{em('ok')} Approved by <@{interaction.user.id}>" + ("" if order['fulfilled'] == 1 else f"\n{em('warn')} Delivery needs attention — see below."), 0x00ff88),
                                               view=RetryView() if order['fulfilled'] != 1 else None)
            except Exception as e:
                logger.error(f"Could not update review message: {e}")
            await interaction.followup.send(embed=(create_success_embed if order['fulfilled'] == 1 else create_warning_embed)("Approved", msg), ephemeral=True)
        else:
            await interaction.followup.send(embed=create_error_embed("Not Approved", msg), ephemeral=True)

    async def on_reject(self, interaction):
        oid = order_id_from_message(interaction.message)
        if not oid:
            await interaction.response.send_message(embed=create_error_embed("Unknown Order", "Could not read the order from this message."), ephemeral=True)
            return
        await interaction.response.send_modal(RejectModal(oid, interaction.message))


class RejectModal(discord.ui.Modal, title="Reject Payment"):
    reason = discord.ui.TextInput(label="Reason (sent to the customer)", style=discord.TextStyle.paragraph if hasattr(discord, 'TextStyle') else None,
                                  max_length=300, required=True)

    def __init__(self, order_id: int, message=None):
        super().__init__()
        self.order_id, self.message = order_id, message

    async def on_submit(self, interaction):
        ok, msg = await reject_order(self.order_id, str(interaction.user.id), str(self.reason.value))
        if ok and self.message is not None:
            try:
                await self.message.edit(embed=review_embed(get_order(self.order_id), f"{em('no')} Rejected by <@{interaction.user.id}>: {str(self.reason.value)[:200]}", 0xff3366), view=None)
            except Exception as e:
                logger.error(f"Could not update review message: {e}")
        await interaction.response.send_message(embed=(create_success_embed if ok else create_error_embed)("Rejected" if ok else "Not Rejected", msg), ephemeral=True)


class RetryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        b = discord.ui.Button(label="Retry Delivery", style=discord.ButtonStyle.primary, emoji=em('refresh') or None, custom_id="avmpro:retry")
        b.callback = self.on_retry
        self.add_item(b)

    async def interaction_check(self, interaction) -> bool:
        if is_staff(str(interaction.user.id)):
            return True
        await interaction.response.send_message(embed=create_error_embed("Access Denied", "Staff only."), ephemeral=True)
        return False

    async def on_retry(self, interaction):
        oid = order_id_from_message(interaction.message)
        if not oid:
            await interaction.response.send_message(embed=create_error_embed("Unknown Order", "Could not read the order from this message."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok, msg = await fulfill_order(oid)
        await interaction.followup.send(embed=(create_success_embed if ok else create_error_embed)("Delivery Retried" if ok else "Delivery Failed Again", msg), ephemeral=True)


async def reject_order(order_id: int, staff_id: str, reason: str) -> tuple:
    reason = (reason or 'No reason given').strip()[:300]
    if not transition_order(order_id, 'rejected', ('awaiting_payment', 'verifying'), reject_reason=reason, approved_by=staff_id):
        return False, "That order is not awaiting review (already processed?)."
    order = get_order(order_id)
    db_exec('DELETE FROM payment_refs WHERE order_id = ?', (order_id,))
    await dm_user(order['user_id'], create_error_embed("Payment Rejected", f"Order **{order_ref(order_id)}** could not be verified.\nReason: {reason}\n\nIf you believe this is a mistake, open a ticket with staff and include your payment proof."))
    return True, f"{order_ref(order_id)} rejected and the customer was notified."


# ------------------------------------------------------------
# PAYMENT CONFIRMATION → FULFILMENT
# ------------------------------------------------------------
async def poll_order(order: Dict[str, Any]) -> str:
    """Ask the gateway about one order. Returns paid | pending | expired | mismatch | closed."""
    if order['status'] not in ('awaiting_payment', 'expired') or not order.get('gateway_ref') or order['method'] not in GATEWAYS:
        return 'closed'
    status, detail = await GATEWAYS[order['method']][1](order)
    if status == 'paid':
        was_expired = order['status'] == 'expired'
        ok, _ = await finalize_order(order['id'], by='gateway', via=order['method'], ref=detail, allow_expired=True)
        if ok and was_expired:
            await staff_billing_alert(create_warning_embed("Late Payment Processed", f"{order_ref(order['id'])} was paid after its window expired; it was processed automatically."), ping=False)
        return 'paid' if ok else 'closed'
    if status == 'mismatch':
        if transition_order(order['id'], 'verifying', ('awaiting_payment', 'expired'), note=detail[:300]):
            await staff_billing_alert(create_warning_embed("Payment Amount Mismatch", f"{order_ref(order['id'])}: {detail}\nReview and approve/reject manually."), view=StaffReviewView())
        return 'mismatch'
    if status == 'expired':
        transition_order(order['id'], 'expired', ('awaiting_payment',))
        return 'expired'
    return 'pending'


async def finalize_order(order_id: int, by: str, via: str, ref: str = '', allow_expired: bool = False) -> tuple:
    allowed = ('awaiting_payment', 'verifying') + (('expired',) if allow_expired else ())
    if not transition_order(order_id, 'paid', allowed, paid_at=iso(utc_now()), approved_by=by,
                            payer_ref=(ref or get_order(order_id)['payer_ref'] or '')):
        order = get_order(order_id)
        return False, f"{order_ref(order_id)} is **{order['status'] if order else 'unknown'}** — it can't be approved."
    if (get_order(order_id)['coupon_code']):
        o = get_order(order_id)
        db_exec('UPDATE coupons SET used = used + 1 WHERE code = ?', (o['coupon_code'],))
        db_exec('INSERT OR IGNORE INTO coupon_uses (code, user_id, order_id) VALUES (?,?,?)', (o['coupon_code'], o['user_id'], order_id))
    ok, msg = await fulfill_order(order_id)
    return True, msg


async def fulfill_order(order_id: int) -> tuple:
    """Deliver a paid order exactly once. Returns (success, message). Failures are surfaced to staff with a retry button."""
    if db_exec('UPDATE orders SET fulfilled = 2 WHERE id = ? AND status = ? AND fulfilled IN (0, 3)', (order_id, 'paid')) != 1:
        o = get_order(order_id)
        return False, f"{order_ref(order_id)} is already delivered or being delivered." if o and o['fulfilled'] in (1, 2) else f"{order_ref(order_id)} is not a paid order."
    order = get_order(order_id)
    uid = order['user_id']
    try:
        if order['kind'] == 'new':
            container, expires = await _fulfill_new(order)
        elif order['kind'] == 'renew':
            container, expires = await _fulfill_renew(order)
        else:
            container, expires = await _fulfill_upgrade(order)
    except Exception as e:
        logger.error(f"Fulfilment of {order_ref(order_id)} failed", exc_info=True)
        update_order(order_id, fulfilled=3, fulfill_error=f"{type(e).__name__}: {e}"[:500])
        await staff_billing_alert(create_error_embed("Paid Order Needs Attention",
                                  f"**{order_ref(order_id)}** is PAID but delivery failed.\nCustomer: <@{uid}>\n```{type(e).__name__}: {str(e)[:700]}```Fix the cause, then press **Retry Delivery**.").set_footer(text=f"{order_ref(order_id)} • {FOOTER_TEXT}"), view=RetryView())
        await dm_user(uid, create_warning_embed("Payment Received", f"We received your payment for **{order_ref(order_id)}**, but delivery hit a snag. Staff were alerted and will fix it shortly — you don't need to pay again."))
        return False, f"Payment recorded, but delivery failed: {type(e).__name__}: {str(e)[:300]}"

    update_order(order_id, fulfilled=1, container_name=container, fulfill_error=None)
    order = get_order(order_id)
    user = None
    try:
        user = await bot.fetch_user(int(uid))
    except Exception:
        pass
    inv = create_invoice(order, container, expires, customer_name=(getattr(user, 'display_name', None) or getattr(user, 'name', '') or ''))
    png = render_invoice_png(inv, inv['data'].get('customer_name', ''))
    embed = invoice_lines_embed(inv)
    files = []
    if png:
        files = [discord.File(io.BytesIO(png), filename=f"{inv['number']}.png")]
        embed.set_image(url=f"attachment://{inv['number']}.png")
    dm_ok = await dm_user(uid, embed=embed, files=files)
    if order['kind'] == 'new' and user is not None:
        found = find_vps(container)
        if found:
            vps_info = dict(found[1])
            vps_info['_number'] = len(vps_data.get(uid, []))
            await send_vps_welcome_dm(user, vps_info)
    sale = create_success_embed(f"{em('rupee')} New Sale — {inv['number']}", f"<@{uid}> • **{order['plan_name']}** • {order['kind']}")
    add_field(sale, "Total", money(order['total']), True)
    add_field(sale, "Via", method_label(order['method']), True)
    add_field(sale, "VPS", f"`{container}`", True)
    await post_to_channel(INVOICE_CHANNEL_ID, embed=sale)
    note = "" if dm_ok else "\n⚠️ The customer's DMs are closed — tell them to use `.billing` / `.myplan`."
    return True, f"Delivered **{container}** — invoice {inv['number']}.{note}"


async def _fulfill_new(order: Dict[str, Any]) -> tuple:
    uid = order['user_id']
    expires = utc_now() + timedelta(days=order['days'])
    container = order['container_name']
    if container and find_vps(container):
        pass  # a previous attempt already created the VPS — don't deploy twice
    else:
        info = await deploy_vps_container(uid, order['ram'], order['cpu'], order['disk'], order['os_version'], order['plan_name'])
        container = info['container_name']
        update_order(order['id'], container_name=container)
        if not info.get('_config_ok'):
            await staff_billing_alert(create_warning_embed("Docker Config Incomplete", f"`{container}` deployed but the Docker-ready LXC config did not fully apply. Check bot.log."), ping=False)
    plan = get_plan(order['plan_key'])
    upsert_sub(container, uid, order['plan_key'], order['plan_name'], plan['price'] if plan else order['unit_price'], order['days'], expires)
    if plan and plan['stock'] > 0:
        db_exec('UPDATE pro_plans SET stock = MAX(0, stock - 1) WHERE key = ?', (order['plan_key'],))
    guild, member = await resolve_member(uid)
    if guild and member:
        await grant_vps_role(guild, member)
    return container, expires


async def _fulfill_renew(order: Dict[str, Any]) -> tuple:
    container = order['container_name']
    found = find_vps(container)
    sub = get_sub(container)
    if not found or not sub:
        raise RuntimeError(f"VPS {container} no longer exists — refund or re-create manually")
    base = max(parse_iso(sub['expires_at']), utc_now())
    expires = base + timedelta(days=order['days'])
    plan = get_plan(order['plan_key'])
    upsert_sub(container, order['user_id'], order['plan_key'], order['plan_name'], plan['price'] if plan else sub['monthly_price'], order['days'], expires, parse_iso(sub['started_at']) if sub['started_at'] else None)
    vps = found[1]
    if vps.get('suspended') and str(vps.get('suspended_reason', '')).startswith('Subscription'):
        await admin_unsuspend(container, 'billing')
    return container, expires


async def _fulfill_upgrade(order: Dict[str, Any]) -> tuple:
    container = order['container_name']
    found = find_vps(container)
    sub = get_sub(container)
    if not found or not sub:
        raise RuntimeError(f"VPS {container} no longer exists")
    vps = found[1]
    old_disk = _to_int(re.sub(r'\D', '', str(vps['storage'])))
    if order['disk'] < old_disk:
        raise RuntimeError("New plan has less disk than the VPS already has (disks can't shrink)")
    await execute_lxc(f"lxc config set {container} limits.memory {order['ram'] * 1024}MB")
    await execute_lxc(f"lxc config set {container} limits.cpu {order['cpu']}")
    await _set_root_size(container, order['disk'])
    vps['ram'], vps['cpu'], vps['storage'] = f"{order['ram']}GB", str(order['cpu']), f"{order['disk']}GB"
    vps['config'] = f"{order['ram']}GB RAM / {order['cpu']} CPU / {order['disk']}GB Disk"
    vps['plan_name'] = order['plan_name']
    save_vps_data()
    plan = get_plan(order['plan_key'])
    db_exec('UPDATE subscriptions SET plan_key = ?, plan_name = ?, monthly_price = ? WHERE container_name = ?',
            (order['plan_key'], order['plan_name'], plan['price'] if plan else order['unit_price'], container))
    return container, parse_iso(sub['expires_at'])


# ============================================================
#   Part C: shop • customer commands • billing engine •
#           staff tools • help/version • startup wiring
# ============================================================
BILLING_INTERVAL = 300          # seconds between billing cycles
PRO_VERSION = "1.0.0"
PRO_CODENAME = "Vault"


# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------
def days_left_of(sub: Dict[str, Any], now: Optional[datetime] = None) -> int:
    """Whole days left, rounded up (0 = expires within 24h, negative = overdue)."""
    secs = (parse_iso(sub['expires_at']) - (now or utc_now())).total_seconds()
    return int(-(-secs // 86400))


def new_state(kind: str, **kw) -> Dict[str, Any]:
    s = {'kind': kind, 'plan_key': None, 'months_key': '1', 'coupon': None, 'container': None,
         'os': OS_OPTIONS[0]['value'] if (kind == 'new' and OS_OPTIONS) else None}
    s.update(kw)
    return s


def sub_status_text(sub: Dict[str, Any]) -> str:
    d = days_left_of(sub)
    icon = {'active': 'online', 'grace': 'warn', 'suspended': 'offline', 'cancelled': 'offline'}.get(sub['status'], 'pending')
    exp = parse_iso(sub['expires_at'])
    if sub['status'] == 'active':
        left = f"{d} day(s) left" if d > 0 else "expires within 24h"
    elif sub['status'] == 'grace':
        left = "expired — in grace period"
    elif sub['status'] == 'suspended':
        left = "suspended — renew to restore"
    else:
        left = sub['status']
    return f"{em(icon)} **{sub['status'].title()}** • {left}\nExpires <t:{ts(exp)}:F> (<t:{ts(exp)}:R>)"


def sub_embed(sub: Dict[str, Any]) -> discord.Embed:
    found = find_vps(sub['container_name'])
    e = create_info_embed(f"{em('server')} {sub['plan_name']} • `{sub['container_name']}`", sub_status_text(sub))
    if found:
        v = found[1]
        add_field(e, "Specs", specs_line(re.sub(r'\D', '', str(v['ram'])), re.sub(r'\D', '', str(v['cpu'])), re.sub(r'\D', '', str(v['storage']))), False)
    add_field(e, "Monthly Price", money(sub['monthly_price']), True)
    add_field(e, "Reminders", ", ".join(f"{d}d" for d in REMINDER_DAYS) + " before expiry", True)
    e.set_footer(text=f"VPS:{sub['container_name']} • {FOOTER_TEXT}")
    return e


def plans_embed() -> discord.Embed:
    e = create_info_embed(f"{em('crown')} {BOT_NAME} VPS Plans", "Premium NVMe VPS • instant delivery • root access • Docker-ready")
    plans = get_plans()
    if not plans:
        add_field(e, "No plans yet", "Staff have not added any plans.", False)
    for p in plans:
        stock = "" if p['stock'] < 0 else (" • **SOLD OUT**" if p['stock'] == 0 else f" • {p['stock']} left")
        add_field(e, f"{em(p['emoji'])} {p['name']} — {money(p['price'])}/mo{stock}",
                  f"{specs_line(p['ram'], p['cpu'], p['disk'])}\n{p['perks'] or DEFAULT_PERKS}", False)
    discounts = " • ".join(f"{v['label']}: **-{v['discount']}%**" for v in PERIODS.values() if v['discount'])
    add_field(e, f"{em('tag')} Term Discounts", discounts or "—", False)
    methods = configured_methods()
    add_field(e, f"{em('card')} Payments", " • ".join(method_label(m) for m in methods) if methods else "Not configured yet", False)
    return e


async def start_checkout(interaction, kind: str, container: Optional[str] = None):
    """Open an (ephemeral) checkout for the clicking user."""
    uid = str(interaction.user.id)
    problem = ensure_customer(uid)
    if not problem and kind in ('renew', 'upgrade'):
        sub = get_sub(container) if container else None
        found = find_vps(container) if container else None
        if not sub or not found or found[0] != uid:
            problem = "That VPS has no active subscription on your account."
        elif sub['status'] == 'cancelled':
            problem = "That subscription was cancelled. Buy a new plan instead."
    if not problem and kind == 'upgrade' and not [p for p in get_plans() if sub and p['price'] > sub['monthly_price'] and plan_in_stock(p)]:
        problem = "You're already on the highest available plan."
    if not problem and kind == 'new' and not [p for p in get_plans() if plan_in_stock(p)]:
        problem = "No plans are in stock right now."
    if problem:
        await interaction.response.send_message(embed=create_error_embed("Can't Start Checkout", problem), ephemeral=True)
        return
    state = new_state(kind, container=container)
    if kind == 'renew':
        state['plan_key'] = get_sub(container)['plan_key']
    view = CheckoutView(interaction.user, state)
    await interaction.response.send_message(embed=build_summary_embed(state), view=view, ephemeral=True)


class SubPickView(discord.ui.View):
    """Choose which VPS to renew/upgrade when the user owns several."""
    def __init__(self, user, kind: str, subs: List[Dict[str, Any]]):
        super().__init__(timeout=300)
        self.user, self.kind = user, kind
        sel = discord.ui.Select(placeholder="Choose a VPS", options=[
            discord.SelectOption(label=f"{s['container_name']}"[:100], value=s['container_name'],
                                 description=f"{s['plan_name']} • {days_left_of(s)} day(s) left"[:100]) for s in subs[:25]])
        sel.callback = self.on_pick
        self.sel = sel
        self.add_item(sel)

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This menu belongs to someone else.", ephemeral=True)
            return False
        return True

    async def on_pick(self, interaction):
        await start_checkout(interaction, self.kind, self.sel.values[0])


async def pick_then_checkout(interaction, kind: str):
    uid = str(interaction.user.id)
    subs = [s for s in user_subs(uid) if s['status'] != 'cancelled' and find_vps(s['container_name'])]
    if not subs:
        await interaction.response.send_message(embed=create_info_embed("No VPS Found", f"You have no paid VPS yet. Use `{PREFIX}shop` to buy one."), ephemeral=True)
        return
    if len(subs) == 1:
        await start_checkout(interaction, kind, subs[0]['container_name'])
        return
    await interaction.response.send_message(embed=create_info_embed(f"Which VPS to {kind}?", "Pick one below."), view=SubPickView(interaction.user, kind, subs), ephemeral=True)


def billing_embed(uid: str) -> discord.Embed:
    subs = user_subs(uid)
    e = create_info_embed(f"{em('receipt')} Your Billing", f"Account <@{uid}>")
    if not subs:
        add_field(e, "Subscriptions", f"None yet — use `{PREFIX}shop` to get your first VPS.", False)
    for s in subs[:10]:
        add_field(e, f"{em('server')} {s['plan_name']} • `{s['container_name']}`", sub_status_text(s) + f"\n{money(s['monthly_price'])}/mo", False)
    opens = open_orders_for(uid)
    if opens:
        add_field(e, f"{em('pending')} Open Orders", "\n".join(f"**{order_ref(o['id'])}** • {o['plan_name']} • {money(o['total'])} • `{o['status']}`" for o in opens[:5]), False)
    inv = db_rows('SELECT number, total, issued_at FROM invoices WHERE user_id = ? ORDER BY issued_at DESC LIMIT 3', (uid,))
    if inv:
        add_field(e, f"{em('receipt')} Recent Invoices", "\n".join(f"`{i['number']}` • {money(i['total'])} • <t:{ts(parse_iso(i['issued_at']))}:d>" for i in inv) + f"\nView one with `{PREFIX}invoice <number>`", False)
    return e


# ------------------------------------------------------------
# PERSISTENT CUSTOMER VIEWS
# ------------------------------------------------------------
class ShopView(discord.ui.View):
    """Public shop panel — buttons work forever (even after restarts)."""
    def __init__(self):
        super().__init__(timeout=None)
        for label, style, icon, cid, cb in (
                ("Buy a VPS", discord.ButtonStyle.success, 'rocket', "avmpro:shop_buy", self.on_buy),
                ("My Plans", discord.ButtonStyle.primary, 'server', "avmpro:shop_my", self.on_my),
                ("Billing", discord.ButtonStyle.secondary, 'receipt', "avmpro:shop_bill", self.on_bill),
                ("Renew", discord.ButtonStyle.secondary, 'refresh', "avmpro:shop_renew", self.on_renew)):
            b = discord.ui.Button(label=label, style=style, emoji=em(icon) or None, custom_id=cid)
            b.callback = cb
            self.add_item(b)

    async def on_buy(self, interaction):
        await start_checkout(interaction, 'new')

    async def on_my(self, interaction):
        uid = str(interaction.user.id)
        subs = [s for s in user_subs(uid) if s['status'] != 'cancelled']
        if not subs:
            await interaction.response.send_message(embed=create_info_embed("No VPS Yet", "You don't have a paid VPS yet. Press **Buy a VPS**."), ephemeral=True)
            return
        await interaction.response.send_message(embeds=[sub_embed(s) for s in subs[:5]], ephemeral=True)

    async def on_bill(self, interaction):
        await interaction.response.send_message(embed=billing_embed(str(interaction.user.id)), ephemeral=True)

    async def on_renew(self, interaction):
        await pick_then_checkout(interaction, 'renew')


class RenewReminderView(discord.ui.View):
    """Attached to renewal reminders (DM). Container is read from the embed footer so it survives restarts."""
    def __init__(self):
        super().__init__(timeout=None)
        b = discord.ui.Button(label="Renew Now", style=discord.ButtonStyle.success, emoji=em('refresh') or None, custom_id="avmpro:rem_renew")
        b.callback = self.on_renew
        self.add_item(b)
        u = discord.ui.Button(label="Upgrade", style=discord.ButtonStyle.secondary, emoji=em('rocket') or None, custom_id="avmpro:rem_upgrade")
        u.callback = self.on_upgrade
        self.add_item(u)

    @staticmethod
    def _container(message) -> Optional[str]:
        for e in getattr(message, 'embeds', []) or []:
            m = re.search(r'VPS:(\S+)', getattr(getattr(e, 'footer', None), 'text', None) or '')
            if m:
                return m.group(1)
        return None

    async def on_renew(self, interaction):
        c = self._container(interaction.message)
        if not c:
            await pick_then_checkout(interaction, 'renew')
            return
        await start_checkout(interaction, 'renew', c)

    async def on_upgrade(self, interaction):
        c = self._container(interaction.message)
        if not c:
            await pick_then_checkout(interaction, 'upgrade')
            return
        await start_checkout(interaction, 'upgrade', c)


class OpenCheckoutView(discord.ui.View):
    """Short-lived button posted by prefix commands (.buy/.renew/.upgrade) → opens a private checkout."""
    def __init__(self, user, kind: str, label: str):
        super().__init__(timeout=300)
        self.user, self.kind = user, kind
        b = discord.ui.Button(label=label, style=discord.ButtonStyle.success, emoji=em('chev') or None)
        b.callback = self.on_click
        self.add_item(b)

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("That button is for someone else — use the command yourself.", ephemeral=True)
            return False
        return True

    async def on_click(self, interaction):
        if self.kind == 'new':
            await start_checkout(interaction, 'new')
        else:
            await pick_then_checkout(interaction, self.kind)


# ------------------------------------------------------------
# CUSTOMER COMMANDS
# ------------------------------------------------------------
@bot.command(name='shop')
@commands.cooldown(1, 5, commands.BucketType.user)
async def shop_cmd(ctx):
    """Show plans and buy a VPS"""
    if not await maintenance_check(ctx):
        return
    await ctx.send(embed=plans_embed(), view=OpenCheckoutView(ctx.author, 'new', "Buy a VPS"))


@bot.command(name='plans')
@commands.cooldown(1, 5, commands.BucketType.user)
async def plans_cmd(ctx):
    """Show plans"""
    await shop_cmd(ctx)


@bot.command(name='buy')
@commands.cooldown(1, 5, commands.BucketType.user)
async def buy_cmd(ctx):
    """Start buying a VPS"""
    if not await maintenance_check(ctx):
        return
    await ctx.send(embed=create_info_embed(f"{em('rocket')} Buy a VPS", "Press the button — your checkout is **private** (only you can see it)."),
                   view=OpenCheckoutView(ctx.author, 'new', "Open Checkout"))


@bot.command(name='myplan')
@commands.cooldown(1, 5, commands.BucketType.user)
async def myplan_cmd(ctx):
    """Show your plans and expiry"""
    if not await maintenance_check(ctx):
        return
    subs = [s for s in user_subs(str(ctx.author.id)) if s['status'] != 'cancelled']
    if not subs:
        await ctx.send(embed=create_info_embed("No VPS Yet", f"You don't have a paid VPS yet. Use `{PREFIX}shop`."))
        return
    await ctx.send(embeds=[sub_embed(s) for s in subs[:5]], view=RenewReminderView())


@bot.command(name='billing')
@commands.cooldown(1, 5, commands.BucketType.user)
async def billing_cmd(ctx):
    """Show subscriptions, open orders and invoices"""
    if not await maintenance_check(ctx):
        return
    await ctx.send(embed=billing_embed(str(ctx.author.id)), view=ShopView())


@bot.command(name='renew')
@commands.cooldown(1, 5, commands.BucketType.user)
async def renew_cmd(ctx):
    """Renew a VPS"""
    if not await maintenance_check(ctx):
        return
    await ctx.send(embed=create_info_embed(f"{em('refresh')} Renew", "Press the button to renew privately."), view=OpenCheckoutView(ctx.author, 'renew', "Renew a VPS"))


@bot.command(name='upgrade')
@commands.cooldown(1, 5, commands.BucketType.user)
async def upgrade_cmd(ctx):
    """Upgrade a VPS to a bigger plan (pay only the difference)"""
    if not await maintenance_check(ctx):
        return
    await ctx.send(embed=create_info_embed(f"{em('rocket')} Upgrade", "You only pay the difference for the time left on your current term."),
                   view=OpenCheckoutView(ctx.author, 'upgrade', "Upgrade a VPS"))


@bot.command(name='invoice')
@commands.cooldown(1, 5, commands.BucketType.user)
async def invoice_cmd(ctx, number: Optional[str] = None):
    """Show your invoices or re-download one"""
    if not await maintenance_check(ctx):
        return
    uid = str(ctx.author.id)
    if not number:
        rows = db_rows('SELECT number, total, issued_at FROM invoices WHERE user_id = ? ORDER BY issued_at DESC LIMIT 15', (uid,))
        if not rows:
            await ctx.send(embed=create_info_embed("No Invoices", "You have no invoices yet."))
            return
        await ctx.send(embed=create_info_embed(f"{em('receipt')} Your Invoices", "\n".join(f"`{r['number']}` • {money(r['total'])} • <t:{ts(parse_iso(r['issued_at']))}:d>" for r in rows) + f"\n\nOpen one: `{PREFIX}invoice <number>`"))
        return
    row = db_row('SELECT * FROM invoices WHERE number = ?', (number.upper(),))
    if not row or (row['user_id'] != uid and not is_staff(uid)):
        await ctx.send(embed=create_error_embed("Not Found", "No invoice with that number on your account."))
        return
    inv = {**row, 'data': json.loads(row['data'])}
    embed = invoice_lines_embed(inv)
    kw: Dict[str, Any] = {'embed': embed}
    png = render_invoice_png(inv, inv['data'].get('customer_name', ''))
    if png:
        embed.set_image(url=f"attachment://{inv['number']}.png")
        kw['file'] = discord.File(io.BytesIO(png), filename=f"{inv['number']}.png")
    await ctx.send(**kw)


@bot.command(name='proof')
@commands.cooldown(1, 10, commands.BucketType.user)
async def proof_cmd(ctx, ref: str = ''):
    """Attach a payment screenshot to an order: .proof AVM-000123 (+ image)"""
    if not await maintenance_check(ctx):
        return
    oid = parse_order_ref(ref)
    order = get_order(oid) if oid else None
    if not order or order['user_id'] != str(ctx.author.id):
        await ctx.send(embed=create_error_embed("Order Not Found", f"Usage: `{PREFIX}proof AVM-000123` with a screenshot attached."))
        return
    if order['status'] not in ('awaiting_payment', 'verifying'):
        await ctx.send(embed=create_error_embed("Order Closed", f"This order is already **{order['status']}**."))
        return
    img = next((a for a in ctx.message.attachments if (a.content_type or '').startswith('image/')), None)
    if not img:
        await ctx.send(embed=create_error_embed("No Image", "Attach a screenshot of the payment to the same message."))
        return
    update_order(oid, proof_url=img.url)
    order = get_order(oid)
    if order['status'] == 'verifying':
        await staff_billing_alert(review_embed(order, "Customer added a proof screenshot."), view=StaffReviewView(), ping=False)
    await ctx.send(embed=create_success_embed("Proof Saved", f"Screenshot attached to **{order_ref(oid)}**. Staff will see it during verification."))


# ------------------------------------------------------------
# BILLING ENGINE
# ------------------------------------------------------------
async def suspend_for_billing(uid: str, vps: Dict[str, Any], reason: str) -> Optional[str]:
    container = vps['container_name']
    error = None
    try:
        await execute_lxc(f"lxc stop {container} --force", timeout=120)
    except Exception as e:
        error = f"stop failed: {str(e)[:200]}"
        logger.error(f"Could not stop {container}: {e}")
    if error is None:
        vps['status'] = 'stopped'
    vps['suspended'] = True
    vps['suspended_reason'] = reason
    vps.setdefault('suspension_history', []).append({'time': datetime.now().isoformat(), 'reason': reason, 'by': 'Billing'})
    save_vps_data()
    log_suspension(container, uid, 'suspend', reason, 'BILLING')
    return error


def reminder_embed(sub: Dict[str, Any], d: int) -> discord.Embed:
    when = "**tomorrow**" if d <= 1 else f"in **{d} days**"
    e = create_warning_embed(f"{em('bell')} Renewal Reminder", f"Your VPS `{sub['container_name']}` ({sub['plan_name']}) expires {when}.\nRenew now to avoid any interruption.")
    exp = parse_iso(sub['expires_at'])
    add_field(e, "Expires", f"<t:{ts(exp)}:F>", True)
    add_field(e, "Renewal Price", f"{money(sub['monthly_price'])}/mo", True)
    add_field(e, "If not renewed", f"{GRACE_DAYS}-day grace period, then the VPS is suspended.", False)
    e.set_footer(text=f"VPS:{sub['container_name']} • {FOOTER_TEXT}")
    return e


async def process_subscription(sub: Dict[str, Any], now: datetime):
    c = sub['container_name']
    found = find_vps(c)
    if not found:
        db_exec("UPDATE subscriptions SET status = 'cancelled' WHERE container_name = ?", (c,))
        return
    uid, vps = found
    left = (parse_iso(sub['expires_at']) - now).total_seconds()
    if left > 0:
        if sub['status'] != 'active':
            return
        d = int(-(-left // 86400))
        due = [x for x in REMINDER_DAYS if d <= x and x < sub['last_reminder']]
        if due:
            mark = min(due)
            db_exec('UPDATE subscriptions SET last_reminder = ? WHERE container_name = ?', (mark, c))
            if not await dm_user(uid, embed=reminder_embed(sub, d), view=RenewReminderView()):
                await staff_billing_alert(create_warning_embed("Reminder Not Delivered", f"<@{uid}> has DMs closed — could not send the {d}-day renewal reminder for `{c}`."), ping=False)
        return

    over = -left / 86400
    if sub['status'] == 'active':
        db_exec("UPDATE subscriptions SET status = 'grace', grace_warned = 1 WHERE container_name = ?", (c,))
        sub['status'] = 'grace'
        if GRACE_DAYS > 0:
            e = create_error_embed(f"{em('warn')} Subscription Expired", f"`{c}` has expired. You have **{GRACE_DAYS} day(s)** to renew before it is suspended.")
            e.set_footer(text=f"VPS:{c} • {FOOTER_TEXT}")
            await dm_user(uid, embed=e, view=RenewReminderView())
    if sub['status'] == 'grace' and over >= GRACE_DAYS:
        err = await suspend_for_billing(uid, vps, "Subscription expired")
        db_exec("UPDATE subscriptions SET status = 'suspended' WHERE container_name = ?", (c,))
        sub['status'] = 'suspended'
        e = create_error_embed(f"{em('pause')} VPS Suspended", f"`{c}` was suspended because the subscription expired.\nRenew any time to restore it instantly — your data is kept for **{DELETE_ALERT_DAYS} days**.")
        e.set_footer(text=f"VPS:{c} • {FOOTER_TEXT}")
        await dm_user(uid, embed=e, view=RenewReminderView())
        await staff_billing_alert(create_info_embed("Subscription Suspended", f"`{c}` (<@{uid}>) suspended for non-payment." + (f"\n⚠️ {err}" if err else "")), ping=False)
    if sub['status'] == 'suspended' and over >= DELETE_ALERT_DAYS and not sub['delete_alerted']:
        db_exec('UPDATE subscriptions SET delete_alerted = 1 WHERE container_name = ?', (c,))
        await staff_billing_alert(create_warning_embed("VPS Eligible For Deletion",
                                  f"`{c}` (<@{uid}>) has been unpaid for **{int(over)} days**.\nDelete with `{PREFIX}delete-vps <@{uid}> <number> unpaid` or extend with `{PREFIX}extend {c} <days>`. Nothing is deleted automatically."), ping=True)


async def housekeep_orders(now: datetime):
    # gateway orders: poll open + recently expired ones
    cutoff = iso(now - timedelta(hours=LATE_PAYMENT_HOURS))
    for o in db_rows("SELECT * FROM orders WHERE method IN ('razorpay','stripe','paypal') AND gateway_ref IS NOT NULL AND "
                     "(status = 'awaiting_payment' OR (status = 'expired' AND expires_at > ?))", (cutoff,)):
        try:
            result = await poll_order(o)
        except GatewayError as e:
            logger.warning(f"Poll {order_ref(o['id'])} failed: {e}")
            continue
        except Exception:
            logger.exception(f"Poll {order_ref(o['id'])} crashed")
            continue
        if result == 'paid' or o['status'] != 'awaiting_payment':
            continue
        if parse_iso(o['expires_at']) < now:
            if transition_order(o['id'], 'expired', ('awaiting_payment',)):
                await dm_user(o['user_id'], embed=create_info_embed("Order Expired", f"**{order_ref(o['id'])}** expired unpaid. Place a new order any time."))
    # manual orders: expire when nobody pressed "I've Paid"
    for o in db_rows("SELECT * FROM orders WHERE method IN ('upi','crypto') AND status = 'awaiting_payment' AND expires_at < ?", (iso(now),)):
        if transition_order(o['id'], 'expired', ('awaiting_payment',)):
            await dm_user(o['user_id'], embed=create_info_embed("Order Expired", f"**{order_ref(o['id'])}** expired because no payment was confirmed. If you did pay, contact staff with your proof."))
    # unreviewed manual payments: nudge staff once
    for o in db_rows("SELECT * FROM orders WHERE status = 'verifying' AND nudged = 0 AND created_at < ?", (iso(now - timedelta(hours=VERIFY_NUDGE_HOURS)),)):
        update_order(o['id'], nudged=1)
        await staff_billing_alert(review_embed(o, f"⏰ Waiting for review for over {VERIFY_NUDGE_HOURS}h."), view=StaffReviewView(), ping=True)


async def run_billing_cycle():
    now = utc_now()
    await housekeep_orders(now)
    for sub in db_rows("SELECT * FROM subscriptions WHERE status IN ('active','grace','suspended')"):
        try:
            await process_subscription(sub, now)
        except Exception:
            logger.exception(f"Billing failed for {sub['container_name']}")


@tasks.loop(seconds=BILLING_INTERVAL)
async def billing_loop():
    try:
        await run_billing_cycle()
    except Exception:
        logger.exception("Billing loop error")


# ------------------------------------------------------------
# STAFF COMMANDS
# ------------------------------------------------------------
def _order_line(o: Dict[str, Any]) -> str:
    return f"**{order_ref(o['id'])}** • <@{o['user_id']}> • {o['plan_name']} • {money(o['total'])} • `{o['status']}`"


def order_detail_embed(o: Dict[str, Any]) -> discord.Embed:
    e = create_info_embed(f"{em('receipt')} {order_ref(o['id'])}", f"Status: **{o['status']}** • {o['kind']}")
    add_field(e, "Customer", f"<@{o['user_id']}>", True)
    add_field(e, "Plan", f"{o['plan_name']} × {o['months']} mo", True)
    add_field(e, "Total", money(o['total']), True)
    add_field(e, "Method", method_label(o['method']), True)
    add_field(e, "Reference", f"`{o['payer_ref'] or o['gateway_ref'] or '—'}`", True)
    if o.get('coupon_code'):
        add_field(e, "Coupon", f"{o['coupon_code']} (-{money(o['coupon_discount'])})", True)
    if o.get('container_name'):
        add_field(e, "VPS", f"`{o['container_name']}`", True)
    add_field(e, "Created", f"<t:{ts(parse_iso(o['created_at']))}:F>", True)
    if o.get('approved_by'):
        add_field(e, "Approved/Rejected By", o['approved_by'] if o['approved_by'] == 'gateway' else f"<@{o['approved_by']}>", True)
    if o.get('reject_reason'):
        add_field(e, "Reject Reason", o['reject_reason'], False)
    if o.get('fulfill_error'):
        add_field(e, "Delivery Error", f"```{o['fulfill_error'][:500]}```", False)
    if o.get('proof_url'):
        add_field(e, "Proof", o['proof_url'], False)
    e.set_footer(text=f"{order_ref(o['id'])} • {FOOTER_TEXT}")
    return e


@bot.command(name='orders')
@is_admin()
async def orders_cmd(ctx, status: str = ''):
    """List recent orders (optionally by status)"""
    if status:
        rows = db_rows('SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT 15', (status.lower(),))
    else:
        rows = db_rows('SELECT * FROM orders ORDER BY id DESC LIMIT 15')
    await ctx.send(embed=create_info_embed(f"{em('receipt')} Orders" + (f" — {status}" if status else ""),
                                           "\n".join(_order_line(o) for o in rows) or "No orders."))


@bot.command(name='order')
@is_admin()
async def order_cmd(ctx, ref: str):
    """Show one order"""
    oid = parse_order_ref(ref)
    o = get_order(oid) if oid else None
    if not o:
        await ctx.send(embed=create_error_embed("Not Found", "No such order."))
        return
    await ctx.send(embed=order_detail_embed(o), view=StaffReviewView() if o['status'] == 'verifying' else None)


@bot.command(name='approve')
@is_admin()
async def approve_cmd(ctx, ref: str):
    """Approve a payment and deliver"""
    oid = parse_order_ref(ref)
    if not oid or not get_order(oid):
        await ctx.send(embed=create_error_embed("Not Found", "No such order."))
        return
    ok, msg = await finalize_order(oid, by=str(ctx.author.id), via='manual')
    await ctx.send(embed=(create_success_embed if ok else create_error_embed)("Approved" if ok else "Can't Approve", msg))


@bot.command(name='reject')
@is_admin()
async def reject_cmd(ctx, ref: str, *, reason: str = ''):
    """Reject a payment"""
    oid = parse_order_ref(ref)
    if not oid or not get_order(oid):
        await ctx.send(embed=create_error_embed("Not Found", "No such order."))
        return
    ok, msg = await reject_order(oid, str(ctx.author.id), reason)
    await ctx.send(embed=(create_success_embed if ok else create_error_embed)("Rejected" if ok else "Can't Reject", msg))


@bot.command(name='refund')
@is_admin()
async def refund_cmd(ctx, ref: str, *, reason: str = ''):
    """Mark a paid order as refunded (you return the money yourself)"""
    oid = parse_order_ref(ref)
    o = get_order(oid) if oid else None
    if not o:
        await ctx.send(embed=create_error_embed("Not Found", "No such order."))
        return
    if not transition_order(oid, 'refunded', ('paid',), note=(reason or 'refunded')[:300]):
        await ctx.send(embed=create_error_embed("Can't Refund", f"Only **paid** orders can be refunded (this one is `{o['status']}`)."))
        return
    if o['coupon_code']:
        db_exec('UPDATE coupons SET used = MAX(0, used - 1) WHERE code = ?', (o['coupon_code'],))
        db_exec('DELETE FROM coupon_uses WHERE code = ? AND user_id = ?', (o['coupon_code'], o['user_id']))
    await dm_user(o['user_id'], embed=create_info_embed("Order Refunded", f"**{order_ref(oid)}** was marked as refunded." + (f"\nReason: {reason}" if reason else "")))
    follow = f"\nThe VPS `{o['container_name']}` was **not** touched — use `{PREFIX}suspend-vps` / `{PREFIX}delete-vps` if needed." if o.get('container_name') else ""
    await ctx.send(embed=create_success_embed("Marked Refunded", f"{order_ref(oid)} → refunded. **Send the money back manually** via {method_label(o['method'])}.{follow}"))


@bot.command(name='revenue')
@is_admin()
async def revenue_cmd(ctx):
    """Sales summary"""
    now = utc_now()

    def total_since(dt: Optional[datetime]):
        sql = "SELECT COALESCE(SUM(total),0) AS s, COUNT(*) AS n FROM orders WHERE status = 'paid' AND method != 'staff'"
        params: tuple = ()
        if dt:
            sql += " AND paid_at >= ?"
            params = (iso(dt),)
        r = db_row(sql, params)
        return r['s'], r['n']

    e = create_info_embed(f"{em('chart')} Revenue", "Paid orders only (refunded and staff grants excluded)")
    for label, dt in (("Today", now.replace(hour=0, minute=0, second=0, microsecond=0)), ("7 days", now - timedelta(days=7)),
                      ("30 days", now - timedelta(days=30)), ("All time", None)):
        s, n = total_since(dt)
        add_field(e, label, f"**{money(s)}**\n{n} order(s)", True)
    by = db_rows("SELECT method, COUNT(*) AS n, SUM(total) AS s FROM orders WHERE status = 'paid' AND method != 'staff' GROUP BY method ORDER BY s DESC")
    if by:
        add_field(e, "By Method", "\n".join(f"{method_label(r['method'])}: {money(r['s'])} ({r['n']})" for r in by), False)
    act = db_row("SELECT COUNT(*) AS n, COALESCE(SUM(monthly_price),0) AS mrr FROM subscriptions WHERE status IN ('active','grace')")
    add_field(e, "Active Subscriptions", f"{act['n']}", True)
    add_field(e, "Monthly Recurring", money(act['mrr']), True)
    pend = db_row("SELECT COUNT(*) AS n FROM orders WHERE status = 'verifying'")
    add_field(e, "Awaiting Review", f"{pend['n']}", True)
    await ctx.send(embed=e)


@bot.command(name='coupon')
@is_admin()
async def coupon_cmd(ctx, action: str = 'list', *args: str):
    """coupon list | create CODE percent|flat VALUE [max_uses] [days] [plan] | delete CODE | toggle CODE"""
    action = action.lower()
    if action == 'list':
        rows = db_rows('SELECT * FROM coupons ORDER BY created_at DESC LIMIT 20')
        lines = []
        for c in rows:
            val = f"{c['value']:g}%" if c['kind'] == 'percent' else money(c['value'])
            exp = f" • exp <t:{ts(parse_iso(c['expires_at']))}:d>" if c['expires_at'] else ""
            lines.append(f"`{c['code']}` • {val} • {c['used']}/{c['max_uses'] or '∞'}{exp}{' • plan ' + c['plan_key'] if c['plan_key'] else ''}{'' if c['active'] else ' • **off**'}")
        await ctx.send(embed=create_info_embed(f"{em('tag')} Coupons", "\n".join(lines) or "None yet."))
        return
    if action == 'create':
        if len(args) < 3:
            await ctx.send(embed=create_error_embed("Usage", f"`{PREFIX}coupon create CODE percent|flat VALUE [max_uses] [days_valid] [plan_key]`"))
            return
        code, kind, value = args[0].upper(), args[1].lower(), _to_float(args[2], -1)
        if not re.fullmatch(r'[A-Z0-9_-]{3,20}', code) or kind not in ('percent', 'flat') or value <= 0 or (kind == 'percent' and value > 90):
            await ctx.send(embed=create_error_embed("Invalid", "Code: 3-20 letters/digits. Kind: percent (max 90) or flat. Value must be positive."))
            return
        max_uses = max(0, _to_int(args[3])) if len(args) > 3 else 0
        days = _to_int(args[4]) if len(args) > 4 else 0
        plan_key = args[5].lower() if len(args) > 5 else None
        if plan_key and not get_plan(plan_key):
            await ctx.send(embed=create_error_embed("Unknown Plan", f"No plan with key `{plan_key}`."))
            return
        try:
            db_exec('INSERT INTO coupons (code, kind, value, max_uses, expires_at, plan_key, created_at) VALUES (?,?,?,?,?,?,?)',
                    (code, kind, value, max_uses, iso(utc_now() + timedelta(days=days)) if days > 0 else None, plan_key, iso(utc_now())))
        except sqlite3.IntegrityError:
            await ctx.send(embed=create_error_embed("Exists", f"Coupon `{code}` already exists."))
            return
        await ctx.send(embed=create_success_embed("Coupon Created", f"`{code}` • {value:g}{'%' if kind == 'percent' else ' ' + CURRENCY_SYMBOL + ' off'} • uses: {max_uses or 'unlimited'}" + (f" • valid {days}d" if days > 0 else "") + (f" • plan `{plan_key}`" if plan_key else "")))
        return
    if action in ('delete', 'toggle') and args:
        code = args[0].upper()
        if not db_row('SELECT 1 FROM coupons WHERE code = ?', (code,)):
            await ctx.send(embed=create_error_embed("Not Found", f"No coupon `{code}`."))
            return
        if action == 'delete':
            db_exec('DELETE FROM coupons WHERE code = ?', (code,))
            await ctx.send(embed=create_success_embed("Deleted", f"`{code}` removed."))
        else:
            db_exec('UPDATE coupons SET active = 1 - active WHERE code = ?', (code,))
            await ctx.send(embed=create_success_embed("Toggled", f"`{code}` is now **{'on' if db_row('SELECT active FROM coupons WHERE code = ?', (code,))['active'] else 'off'}**."))
        return
    await ctx.send(embed=create_error_embed("Usage", f"`{PREFIX}coupon list|create|delete|toggle`"))


@bot.command(name='proplan')
@is_admin()
async def proplan_cmd(ctx, action: str = 'list', *args: str):
    """proplan list | add KEY "Name" RAM CPU DISK PRICE [emoji] | edit KEY FIELD VALUE | stock KEY N | toggle KEY | remove KEY"""
    action = action.lower()
    if action == 'list':
        rows = get_plans(active_only=False)
        await ctx.send(embed=create_info_embed(f"{em('crown')} Plans",
                       "\n".join(f"`{p['key']}` • **{p['name']}** • {p['ram']}GB/{p['cpu']}c/{p['disk']}GB • {money(p['price'])} • stock {'∞' if p['stock'] < 0 else p['stock']}{'' if p['active'] else ' • **off**'}" for p in rows) or "None."))
        return
    if action == 'add':
        if len(args) < 6:
            await ctx.send(embed=create_error_embed("Usage", f'`{PREFIX}proplan add KEY "Name" RAM CPU DISK PRICE [emoji]`'))
            return
        key = args[0].lower()
        ram, cpu, disk, price = _to_int(args[2]), _to_int(args[3]), _to_int(args[4]), _to_float(args[5], -1)
        emoji = args[6] if len(args) > 6 and args[6] in EMOJI_FALLBACK else 'rocket'
        if not re.fullmatch(r'[a-z0-9_-]{2,20}', key) or min(ram, cpu, disk) < 1 or price <= 0:
            await ctx.send(embed=create_error_embed("Invalid", "Key: 2-20 lowercase letters/digits. RAM, CPU, DISK and PRICE must be positive numbers."))
            return
        nxt = (db_row('SELECT COALESCE(MAX(sort),0)+1 AS n FROM pro_plans') or {'n': 1})['n']
        try:
            db_exec('INSERT INTO pro_plans (key, name, emoji, ram, cpu, disk, price, sort, perks) VALUES (?,?,?,?,?,?,?,?,?)',
                    (key, args[1][:30], emoji, ram, cpu, disk, price, nxt, DEFAULT_PERKS))
        except sqlite3.IntegrityError:
            await ctx.send(embed=create_error_embed("Exists", f"Plan `{key}` already exists."))
            return
        await ctx.send(embed=create_success_embed("Plan Added", f"**{args[1]}** • {ram}GB/{cpu}c/{disk}GB • {money(price)}/mo"))
        return
    if not args or not get_plan(args[0].lower()):
        await ctx.send(embed=create_error_embed("Unknown Plan", f"Use `{PREFIX}proplan list` to see keys."))
        return
    key = args[0].lower()
    if action == 'toggle':
        db_exec('UPDATE pro_plans SET active = 1 - active WHERE key = ?', (key,))
        await ctx.send(embed=create_success_embed("Toggled", f"`{key}` is now **{'on' if get_plan(key)['active'] else 'off'}**."))
    elif action == 'remove':
        db_exec('DELETE FROM pro_plans WHERE key = ?', (key,))
        await ctx.send(embed=create_success_embed("Removed", f"`{key}` deleted. Existing subscriptions keep working."))
    elif action == 'stock' and len(args) > 1:
        n = _to_int(args[1], -2)
        if n < -1:
            await ctx.send(embed=create_error_embed("Invalid", "Stock must be a number (-1 = unlimited)."))
            return
        db_exec('UPDATE pro_plans SET stock = ? WHERE key = ?', (n, key))
        await ctx.send(embed=create_success_embed("Stock Updated", f"`{key}` stock = {'unlimited' if n < 0 else n}"))
    elif action == 'edit' and len(args) > 2:
        field, value = args[1].lower(), ' '.join(args[2:])
        if field in ('ram', 'cpu', 'disk', 'sort') and _to_int(value, 0) < (0 if field == 'sort' else 1):
            await ctx.send(embed=create_error_embed("Invalid", f"`{field}` must be a positive number."))
            return
        if field == 'price' and _to_float(value, 0) <= 0:
            await ctx.send(embed=create_error_embed("Invalid", "Price must be positive."))
            return
        if field == 'emoji' and value not in EMOJI_FALLBACK:
            await ctx.send(embed=create_error_embed("Invalid", "Emoji must be one of: " + ", ".join(sorted(EMOJI_FALLBACK))[:900]))
            return
        if field not in ('name', 'ram', 'cpu', 'disk', 'price', 'perks', 'emoji', 'sort'):
            await ctx.send(embed=create_error_embed("Invalid Field", "Fields: name, ram, cpu, disk, price, perks, emoji, sort"))
            return
        val: Any = _to_float(value) if field == 'price' else (_to_int(value) if field in ('ram', 'cpu', 'disk', 'sort') else value[:300])
        db_exec(f'UPDATE pro_plans SET {field} = ? WHERE key = ?', (val, key))
        await ctx.send(embed=create_success_embed("Plan Updated", f"`{key}`.{field} = {val}"))
    else:
        await ctx.send(embed=create_error_embed("Usage", f"`{PREFIX}proplan list|add|edit|stock|toggle|remove`"))


@bot.command(name='subs')
@is_admin()
async def subs_cmd(ctx, member: Optional[discord.Member] = None):
    """List subscriptions (soonest expiry first)"""
    if member:
        rows = db_rows('SELECT * FROM subscriptions WHERE user_id = ? ORDER BY expires_at', (str(member.id),))
    else:
        rows = db_rows("SELECT * FROM subscriptions WHERE status != 'cancelled' ORDER BY expires_at LIMIT 20")
    lines = [f"`{s['container_name']}` • <@{s['user_id']}> • {s['plan_name']} • `{s['status']}` • <t:{ts(parse_iso(s['expires_at']))}:R>" for s in rows]
    await ctx.send(embed=create_info_embed(f"{em('server')} Subscriptions", "\n".join(lines) or "None."))


@bot.command(name='extend')
@is_admin()
async def extend_cmd(ctx, container: str, days: int):
    """Add days to a subscription (restores billing-suspended VPS)"""
    sub = get_sub(container)
    if not sub or not 1 <= days <= 3650:
        await ctx.send(embed=create_error_embed("Invalid", f"Usage: `{PREFIX}extend <container> <1-3650 days>` for a VPS that has a subscription."))
        return
    base = max(parse_iso(sub['expires_at']), utc_now())
    new = base + timedelta(days=days)
    db_exec("UPDATE subscriptions SET expires_at = ?, status = 'active', last_reminder = 99, grace_warned = 0, delete_alerted = 0 WHERE container_name = ?", (iso(new), container))
    found = find_vps(container)
    note = ""
    if found and found[1].get('suspended') and str(found[1].get('suspended_reason', '')).startswith('Subscription'):
        try:
            await admin_unsuspend(container, str(ctx.author.id))
            note = "\nVPS was unsuspended and started."
        except Exception as e:
            note = f"\n⚠️ Could not unsuspend: {str(e)[:200]}"
    await ctx.send(embed=create_success_embed("Extended", f"`{container}` now expires <t:{ts(new)}:F>.{note}"))
    if found:
        await dm_user(found[0], embed=create_success_embed("Subscription Extended", f"Staff added **{days} day(s)** to `{container}`. New expiry: <t:{ts(new)}:F>."))


@bot.command(name='grant')
@is_admin()
async def grant_cmd(ctx, member: discord.Member, plan_key: str, days: int = 30):
    """Give a member a free paid-plan VPS (gift, contest, partner)"""
    plan = get_plan(plan_key.lower())
    if not plan or not 1 <= days <= 3650:
        await ctx.send(embed=create_error_embed("Invalid", f"Usage: `{PREFIX}grant @user <plan_key> [days]`. Keys: `{PREFIX}proplan list`"))
        return
    if len(vps_data.get(str(member.id), [])) >= MAX_VPS_PER_USER:
        await ctx.send(embed=create_error_embed("Limit Reached", f"They already own {MAX_VPS_PER_USER} VPS."))
        return
    async with ctx.typing():
        oid = insert_order(user_id=str(member.id), kind='new', plan_key=plan['key'], plan_name=plan['name'], ram=plan['ram'], cpu=plan['cpu'], disk=plan['disk'],
                           months=max(1, round(days / 30)), days=days, os_version=OS_OPTIONS[0]['value'], unit_price=0, subtotal=0, period_discount=0,
                           coupon_discount=0, tax=0, total=0, currency=CURRENCY_CODE, pay_amount=0, pay_currency=CURRENCY_CODE, method='staff',
                           status='paid', approved_by=str(ctx.author.id), paid_at=iso(utc_now()), fulfilled=0)
        ok, msg = await fulfill_order(oid)
    await ctx.send(embed=(create_success_embed if ok else create_error_embed)("Granted" if ok else "Grant Failed", msg))


@bot.command(name='setpaychannel')
@is_admin()
async def setpaychannel_cmd(ctx, channel: discord.TextChannel):
    """Channel for payment verification + billing alerts"""
    global PAYMENT_CHANNEL_ID
    set_setting('payment_channel_id', str(channel.id))
    PAYMENT_CHANNEL_ID = channel.id
    await ctx.send(embed=create_success_embed("Payment Channel Set", f"Payment reviews and billing alerts go to {channel.mention}."))


@bot.command(name='setinvoicechannel')
@is_admin()
async def setinvoicechannel_cmd(ctx, channel: discord.TextChannel):
    """Channel that receives a line for every sale"""
    global INVOICE_CHANNEL_ID
    set_setting('invoice_channel_id', str(channel.id))
    INVOICE_CHANNEL_ID = channel.id
    await ctx.send(embed=create_success_embed("Invoice Channel Set", f"Sales log goes to {channel.mention}."))


@bot.command(name='payments')
@is_admin()
async def payments_cmd(ctx):
    """Show which payment methods are configured"""
    e = create_info_embed(f"{em('card')} Payment Setup", "Secrets are read from `config.json` / environment only.")
    methods = configured_methods()
    for m, info in METHOD_INFO.items():
        add_field(e, f"{em(info['emoji'])} {info['label']}", "✅ configured" if m in methods else "❌ not configured", True)
    add_field(e, "Payment Channel", f"<#{PAYMENT_CHANNEL_ID}>" if PAYMENT_CHANNEL_ID else f"not set — alerts DM the admin (`{PREFIX}setpaychannel`)", False)
    add_field(e, "Invoice Channel", f"<#{INVOICE_CHANNEL_ID}>" if INVOICE_CHANNEL_ID else "not set", False)
    add_field(e, "Settings", f"Tax: {TAX_PERCENT:g}% {TAX_LABEL} • USD rate: {USD_RATE:g} • Grace: {GRACE_DAYS}d • Reminders: {', '.join(map(str, REMINDER_DAYS))}d", False)
    if 'upi' in methods and not make_upi_qr(upi_link(1, 0)):
        add_field(e, "UPI QR", "⚠️ `pip install qrcode[pil]` to auto-generate QR codes" + (" (using your uploaded QR image meanwhile)" if UPI_QR_URL else ""), False)
    await ctx.send(embed=e)


@bot.command(name='paytest')
@is_admin()
@commands.cooldown(1, 15, commands.BucketType.user)
async def paytest_cmd(ctx):
    """Check gateway API keys for real"""
    async with ctx.typing():
        results = await test_gateway_credentials()
    if not results:
        await ctx.send(embed=create_info_embed("Nothing To Test", "No automatic gateway (Razorpay / Stripe / PayPal) is configured."))
        return
    e = create_info_embed(f"{em('shield')} Gateway Test", "Live credential check")
    for m, ok, detail in results:
        add_field(e, method_label(m), ("✅ " if ok else "❌ ") + detail[:300], False)
    await ctx.send(embed=e)


@bot.command(name='shoppanel')
@is_admin()
async def shoppanel_cmd(ctx, channel: Optional[discord.TextChannel] = None):
    """Post the permanent shop panel (buttons never expire)"""
    channel = channel or ctx.channel
    try:
        await channel.send(embed=plans_embed(), view=ShopView())
    except discord.Forbidden:
        await ctx.send(embed=create_error_embed("No Permission", f"I can't send messages in {channel.mention}."))
        return
    if channel.id != ctx.channel.id:
        await ctx.send(embed=create_success_embed("Posted", f"Shop panel sent to {channel.mention}."))


@bot.command(name='setupemojis')
@is_admin()
@commands.cooldown(1, 30, commands.BucketType.guild)
async def setupemojis_cmd(ctx):
    """Upload the custom emoji pack to this server"""
    if not ctx.guild:
        await ctx.send(embed=create_error_embed("Server Only", "Run this inside your server."))
        return
    async with ctx.typing():
        res = await install_emoji_pack(ctx.guild)
    if res['fatal'] and not (res['created'] or res['reused']):
        await ctx.send(embed=create_error_embed("Emoji Setup Failed", res['fatal']))
        return
    e = create_success_embed(f"{em('star')} Emoji Pack Installed", f"Created **{len(res['created'])}**, reused **{len(res['reused'])}**.")
    if res['skipped']:
        add_field(e, "Skipped (server emoji slots full)", ", ".join(res['skipped'])[:900], False)
    if res['failed']:
        add_field(e, "Failed", "\n".join(res['failed'])[:900], False)
    if res['fatal']:
        add_field(e, "Stopped", res['fatal'], False)
    add_field(e, "Next", "Buttons and embeds use the new emojis right away. Restart the bot once so persistent panels refresh their button icons.", False)
    await ctx.send(embed=e)


@bot.command(name='emojis')
@is_admin()
async def emojis_cmd(ctx):
    """Show which custom emojis are active"""
    if not EMOJI_MAP:
        await ctx.send(embed=create_info_embed("Using Default Emojis", f"Run `{PREFIX}setupemojis` to upload the custom pack."))
        return
    await ctx.send(embed=create_info_embed(f"{em('star')} Custom Emojis ({len(EMOJI_MAP)}/{len(EMOJI_FALLBACK)})", " ".join(EMOJI_MAP.values())[:3900]))


# ------------------------------------------------------------
# HELP / VERSION
# ------------------------------------------------------------
@bot.command(name='help')
@commands.cooldown(1, 3, commands.BucketType.user)
async def pro_help(ctx):
    """Show commands"""
    if not await maintenance_check(ctx):
        return
    p = PREFIX
    e = create_info_embed(f"{em('crown')} {BOT_NAME} Pro", "Premium VPS hosting — buy, renew and manage right here in Discord.")
    add_field(e, f"{em('rocket')} Shop", f"`{p}shop` plans & buy • `{p}buy` • `{p}upgrade` • `{p}renew`", False)
    add_field(e, f"{em('receipt')} Billing", f"`{p}myplan` expiry • `{p}billing` orders & invoices • `{p}invoice [no]` • `{p}proof <order>`", False)
    add_field(e, f"{em('server')} Your VPS", f"`{p}myvps` • `{p}manage` • `{p}vpsinfo` • `{p}vps-stats` • `{p}restart-vps` • `{p}snapshot` • `{p}ports`", False)
    if is_staff(ctx.author.id):
        add_field(e, f"{em('shield')} Staff — Sales", f"`{p}orders [status]` • `{p}order <ref>` • `{p}approve` • `{p}reject` • `{p}refund` • `{p}revenue` • `{p}grant` • `{p}extend` • `{p}subs`", False)
        add_field(e, f"{em('tools')} Staff — Setup", f"`{p}proplan` • `{p}coupon` • `{p}shoppanel` • `{p}payments` • `{p}paytest` • `{p}setpaychannel` • `{p}setinvoicechannel` • `{p}setupemojis` • `{p}emojis`", False)
        add_field(e, f"{em('siren')} Staff — System", f"`{p}setalertchannel` • `{p}setstatuschannel` • `{p}miningguard` • `{p}minerscan` • `{p}suspend-vps` • `{p}delete-vps` • `{p}list-all` • `{p}branding`", False)
    await ctx.send(embed=e)


@bot.command(name='commands')
@commands.cooldown(1, 3, commands.BucketType.user)
async def pro_commands_alias(ctx):
    """Alias for help"""
    await pro_help(ctx)


@bot.command(name='version')
@commands.cooldown(1, 5, commands.BucketType.user)
async def pro_version(ctx):
    """Show version"""
    e = create_info_embed(f"AVM Pro v{PRO_VERSION} \"{PRO_CODENAME}\"", f"**Axion VPS Manager — Pro Edition** by {BOT_NAME}")
    add_field(e, "Includes", "Shop & checkout • UPI/Crypto/Razorpay/Stripe/PayPal • invoices • renewal reminders • grace & auto-suspend • coupons • upgrades • mining guard • live status", False)
    add_field(e, "Uptime", fmt_duration(time.time() - BOT_START_TS), True)
    add_field(e, "Methods", ", ".join(method_label(m).split(' ')[0] for m in configured_methods()) or "none", True)
    await ctx.send(embed=e)


# ------------------------------------------------------------
# STARTUP WIRING
# ------------------------------------------------------------
_base_setup_hook = bot.setup_hook


async def _pro_setup_hook():
    await _base_setup_hook()
    for view in (StaffReviewView(), RetryView(), ShopView(), RenewReminderView()):
        bot.add_view(view)
    if not billing_loop.is_running():
        billing_loop.start()

bot.setup_hook = _pro_setup_hook


@bot.listen('on_ready')
async def pro_on_ready():
    try:
        sync_emoji_map_from_guilds()
    except Exception:
        logger.exception("Emoji sync failed")
    methods = configured_methods()
    logger.info(f"AVM Pro v{PRO_VERSION} ready • payment methods: {', '.join(methods) or 'NONE'} • payment_channel={PAYMENT_CHANNEL_ID or 'unset'}")
    if not methods:
        logger.warning("No payment method configured — customers cannot buy. See config.example.json (upi_id, razorpay_*, ...).")
    if not PAYMENT_CHANNEL_ID:
        logger.warning(f"Payment channel not set — reviews will DM the admin. Use {PREFIX}setpaychannel.")


# ============ TYPO HANDLING ============

@bot.command(name='mangage')
async def manage_typo(ctx):
    """Handle typo for manage command"""
    embed = create_info_embed("Command Correction", f"Did you mean `{PREFIX}manage`? Use the correct command.")
    await ctx.send(embed=embed)

# ============ RUN THE BOT ============

if __name__ == "__main__":
    if DISCORD_TOKEN:
        bot.run(DISCORD_TOKEN)
    else:
        logger.error("No Discord token configured. Run ./install.sh (or ./install.sh set token <TOKEN>), or set AXION_TOKEN.")
        raise SystemExit(1)
