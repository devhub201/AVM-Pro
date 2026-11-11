"""
AVM-Pro — Dedicated IPv4 add-on (Serververse Transit)
======================================================
Lets the bot hand a VPS a real dedicated public IPv4 (via Serververse Transit,
https://serververs.com) instead of the shared-IP/port-forward model.

How it works
------------
Serververse doesn't publish a lease/release HTTP API — each dedicated IP is
bound to a one-shot install command of the form:

    bash <(curl -fsSL https://labs-api.serververs.com/api/v0.3/provision/install.sh) --token <TOKEN>

Each TOKEN is tied to exactly one IPv4 on their side. So instead of an API
pool, this module keeps its own small pool of tokens the admin bought on
Serververse: `.addip <token>` stores one, `assign_dedicated_ip()` hands the
next free one to a container by running the install command *inside* that
container with `lxc exec`, then detects the IPv4 it added by diffing
`ip -4 -br a` before/after (the installer's own stdout is also scanned as a
fallback — it is NOT guaranteed to print the IP, since we can't see what the
downloaded `sv-transit` binary does).

Caveats (be upfront with the admin about these):
  - Not tested against a live Serververse token in this environment (no
    outbound internet / no LXD here) — the shell logic is syntax-checked,
    but a real run should be watched once via `.iplist` + `.vps-network`.
  - "Release" best-effort runs the uninstall flag; Serververse may still
    require freeing/re-leasing the IP on their own dashboard.
  - One token = one IP. Buy as many tokens as dedicated VPS you'll sell.
"""
import re
import sys
import time
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any

M = sys.modules.get('__main__')
if M is None or not hasattr(M, 'bot'):
    import avm_pro_bot as M  # type: ignore

bot = M.bot
get_db = M.get_db
run_lxc_exec = M.run_lxc_exec
is_admin = M.is_admin
PREFIX = M.PREFIX
create_success_embed = M.create_success_embed
create_error_embed = M.create_error_embed
create_info_embed = M.create_info_embed
em = getattr(M, 'em', lambda name: '')

logger = logging.getLogger('avm_transit')

INSTALL_URL = "https://labs-api.serververs.com/api/v0.3/provision/install.sh"
IPV4_RE = re.compile(r'\b(?!127\.|0\.)(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?:/\d{1,2})?\b')
RUN_TIMEOUT = 180


def init_tables():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS transit_ips (
        token TEXT PRIMARY KEY,
        ip TEXT,
        status TEXT NOT NULL DEFAULT 'available',   -- available | assigned | error
        container TEXT,
        assigned_at TEXT,
        added_at TEXT NOT NULL
    )''')
    try:
        cur.execute("ALTER TABLE pro_plans ADD COLUMN ip_type TEXT NOT NULL DEFAULT 'shared'")
    except Exception:
        pass  # already there
    conn.commit()
    conn.close()


def _mask(tok: str) -> str:
    tok = tok or ''
    return f"...{tok[-6:]}" if len(tok) > 10 else "...?"


def pool_counts() -> Dict[str, int]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, COUNT(*) FROM transit_ips GROUP BY status")
    rows = dict(cur.fetchall())
    conn.close()
    return {'available': rows.get('available', 0), 'assigned': rows.get('assigned', 0), 'error': rows.get('error', 0)}


def add_token(token: str) -> bool:
    token = (token or '').strip()
    if not token:
        return False
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO transit_ips (token, status, added_at) VALUES (?, 'available', ?)",
                    (token, datetime.now().isoformat()))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


def remove_token(token_suffix: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM transit_ips WHERE status = 'available' AND token LIKE ?", (f"%{token_suffix}",))
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


def list_pool():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT token, ip, status, container, added_at FROM transit_ips ORDER BY added_at")
    rows = cur.fetchall()
    conn.close()
    return rows


def _row_for_container(container: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM transit_ips WHERE container = ? AND status = 'assigned' LIMIT 1", (container,))
    row = cur.fetchone()
    conn.close()
    return row


class TransitError(Exception):
    pass


async def _snapshot_ips(container: str) -> set:
    rc, out, _ = await run_lxc_exec(container, ['sh', '-c', 'ip -4 -o addr show 2>/dev/null'], timeout=20)
    return set(IPV4_RE.findall(out or ''))


async def assign_dedicated_ip(container: str) -> str:
    """Pick a free token, run the Serververse installer inside `container`, return the new IPv4."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT token FROM transit_ips WHERE status = 'available' ORDER BY added_at LIMIT 1")
    row = cur.fetchone()
    if not row:
        conn.close()
        raise TransitError("No dedicated IPv4 left in the pool — add one with `.addip <token>`.")
    token = row[0] if not isinstance(row, dict) else row['token']
    cur.execute("UPDATE transit_ips SET status = 'assigned', container = ?, assigned_at = ? WHERE token = ?",
                (container, datetime.now().isoformat(), token))
    conn.commit()
    conn.close()

    before = await _snapshot_ips(container)
    cmd = f'bash <(curl -fsSL --max-time 60 {INSTALL_URL}) --token {token}'
    try:
        rc, out, err = await run_lxc_exec(container, ['bash', '-c', cmd], timeout=RUN_TIMEOUT)
    except Exception as e:
        _mark_error(token)
        raise TransitError(f"Could not run the Serververse installer inside `{container}`: {e}")
    if rc != 0:
        _mark_error(token)
        raise TransitError(f"Serververse install exited with {rc}:\n```{(err or out).strip()[:400]}```")

    # give the tunnel a moment to come up, then diff interfaces
    ip = None
    for _ in range(6):
        await asyncio.sleep(2)
        after = await _snapshot_ips(container)
        new_ips = after - before
        if new_ips:
            ip = sorted(new_ips)[0]
            break
    if not ip:
        found = IPV4_RE.findall((out or '') + (err or ''))
        ip = found[-1] if found else None
    if not ip:
        _mark_error(token)
        raise TransitError(
            f"Installer ran but no new IPv4 was detected on `{container}`.\n"
            f"```{(out or err or '').strip()[:400]}```Check `lxc exec {container} -- sv-transit status` manually."
        )
    conn = get_db()
    conn.execute("UPDATE transit_ips SET ip = ? WHERE token = ?", (ip, token))
    conn.commit()
    conn.close()
    return ip


async def reprovision_if_dedicated(container: str) -> Optional[str]:
    """After a reinstall wipes the container, re-run the install for whatever token was already bound to it."""
    row = _row_for_container(container)
    if not row:
        return None
    token = row['token'] if isinstance(row, dict) else row[0]
    before = await _snapshot_ips(container)
    cmd = f'bash <(curl -fsSL --max-time 60 {INSTALL_URL}) --token {token}'
    try:
        await run_lxc_exec(container, ['bash', '-c', cmd], timeout=RUN_TIMEOUT)
    except Exception as e:
        logger.error(f"Re-provisioning dedicated IP on {container} failed: {e}")
        return None
    for _ in range(6):
        await asyncio.sleep(2)
        after = await _snapshot_ips(container)
        new_ips = after - before
        if new_ips:
            ip = sorted(new_ips)[0]
            conn = get_db()
            conn.execute("UPDATE transit_ips SET ip = ? WHERE token = ?", (ip, token))
            conn.commit()
            conn.close()
            return ip
    return None


async def release_dedicated_ip(container: str) -> bool:
    """Best-effort: uninstall inside the container (if it's still alive) and free the pool slot."""
    row = _row_for_container(container)
    if not row:
        return False
    token = row['token'] if isinstance(row, dict) else row[0]
    try:
        cmd = f'bash <(curl -fsSL --max-time 30 {INSTALL_URL}) --remove --token {token}'
        await run_lxc_exec(container, ['bash', '-c', cmd], timeout=45)
    except Exception as e:
        logger.warning(f"Could not cleanly uninstall dedicated IP on {container} (freeing pool slot anyway): {e}")
    conn = get_db()
    conn.execute("UPDATE transit_ips SET status = 'available', container = NULL, ip = NULL, assigned_at = NULL WHERE token = ?", (token,))
    conn.commit()
    conn.close()
    return True


def _mark_error(token: str):
    conn = get_db()
    conn.execute("UPDATE transit_ips SET status = 'error' WHERE token = ?", (token,))
    conn.commit()
    conn.close()


# ------------------------------------------------------------
# admin commands
# ------------------------------------------------------------
@bot.command(name='addip')
@is_admin()
async def addip_cmd(ctx, token: str = None):
    """.addip <serververse_token>  — add one dedicated-IPv4 token to the pool"""
    if not token:
        await ctx.send(embed=create_error_embed("Usage", f"`{PREFIX}addip <serververse_token>`\nGet the token from your Serververse Transit dashboard — each one is good for exactly one IPv4."))
        return
    try:
        await ctx.message.delete()
    except Exception:
        pass
    if add_token(token):
        c = pool_counts()
        await ctx.send(embed=create_success_embed("IPv4 Added", f"Pool: **{c['available']}** available • {c['assigned']} assigned • {c['error']} error"))
    else:
        await ctx.send(embed=create_error_embed("Not Added", "That token is already in the pool, or empty."))


@bot.command(name='rmip')
@is_admin()
async def rmip_cmd(ctx, token_suffix: str = None):
    """.rmip <last 6 chars of token>  — remove an unused token from the pool"""
    if not token_suffix:
        await ctx.send(embed=create_error_embed("Usage", f"`{PREFIX}rmip <last-6-chars-of-token>`"))
        return
    n = remove_token(token_suffix)
    if n:
        await ctx.send(embed=create_success_embed("Removed", f"{n} unused token(s) removed from the pool."))
    else:
        await ctx.send(embed=create_error_embed("Not Found", "No *available* (unassigned) token matched that suffix."))


@bot.command(name='iplist')
@is_admin()
async def iplist_cmd(ctx):
    """.iplist — show the dedicated-IPv4 pool"""
    rows = list_pool()
    if not rows:
        await ctx.send(embed=create_info_embed("Dedicated IPv4 Pool", "Empty — add one with `.addip <token>`."))
        return
    lines = []
    for r in rows:
        token = r['token'] if isinstance(r, dict) else r[0]
        ip = (r['ip'] if isinstance(r, dict) else r[1]) or '—'
        status = r['status'] if isinstance(r, dict) else r[2]
        container = (r['container'] if isinstance(r, dict) else r[3]) or '—'
        icon = {'available': '🟢', 'assigned': '🔵', 'error': '🔴'}.get(status, '⚪')
        lines.append(f"{icon} `{_mask(token)}` → **{ip}** • {status}" + (f" • `{container}`" if container != '—' else ''))
    c = pool_counts()
    await ctx.send(embed=create_info_embed(f"{em('server')} Dedicated IPv4 Pool",
                    "\n".join(lines)[:3900] + f"\n\n**{c['available']}** available • **{c['assigned']}** assigned • **{c['error']}** error"))


@bot.command(name='setdedicated')
@is_admin()
async def setdedicated_cmd(ctx, plan_key: str = None, value: str = None):
    """.setdedicated <plan_key> on|off  — mark a store plan as giving a real dedicated IPv4"""
    if not plan_key or value not in ('on', 'off'):
        await ctx.send(embed=create_error_embed("Usage", f"`{PREFIX}setdedicated <plan_key> on|off`\nSee plan keys with `{PREFIX}proplan list`."))
        return
    plan = M.get_plan(plan_key.lower())
    if not plan:
        await ctx.send(embed=create_error_embed("Unknown Plan", f"No plan `{plan_key}`. See `{PREFIX}proplan list`."))
        return
    ip_type = 'dedicated' if value == 'on' else 'shared'
    M.db_exec("UPDATE pro_plans SET ip_type = ? WHERE key = ?", (ip_type, plan_key.lower()))
    await ctx.send(embed=create_success_embed("Updated", f"`{plan_key}` now gives a **{ip_type}** IPv4."))


def setup_transit():
    init_tables()
    logger.info("avm_transit loaded — dedicated IPv4 add-on ready")
