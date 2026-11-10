"""AVM Pro v1.1 extras — payments set from Discord, ticket system, log channels, reminder control, dashboard.
Loaded by avm_pro_bot.py (see load_extras hook). Uses the main module's helpers (embeds, settings, DB)."""
import io, os, re, sys, json, time, asyncio, logging
from datetime import datetime, timedelta
import discord
from discord.ext import commands

M = sys.modules.get('__main__')
if not hasattr(M, 'bot'):
    import avm_pro_bot as M          # when started some other way
bot = M.bot
log = logging.getLogger("avm.extras")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(M.__file__)), 'data')
STATIC_QR = os.path.join(DATA_DIR, 'upi_qr.png')
LOG_TYPES = {'tickets': 'Ticket opened / closed + transcripts', 'mod': 'Staff command audit log', 'vps': 'VPS actions',
             'reminders': 'Renewal reminder reports', 'payments': 'Payment events'}
TICKET_TOPICS = [  # value, label, description, emoji
    ('billing', 'Billing & Payments', 'Payment, invoice, refund or renewal help', '💳'),
    ('support', 'Technical Support', 'VPS not working, access or setup problems', '🛠️'),
    ('sales', 'Sales & Plans', 'Questions before you buy', '🚀'),
    ('abuse', 'Report / Abuse', 'Report abuse or a security issue', '🛡️')]
MAX_OPEN_PER_USER = 2
_cool = {}


def E(n): return M.em(n)
def ok_embed(t, d=""): return M.create_success_embed(t, d)
def err_embed(t, d=""): return M.create_error_embed(t, d)
def info_embed(t, d="", color=0x2b6cff): return M.create_embed(t, d, color)
def db(): return M.get_db()


def init_tables():
    c = db(); cur = c.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT, user_id TEXT NOT NULL, topic TEXT,
        status TEXT NOT NULL DEFAULT 'open', claimed_by TEXT, created_at TEXT, closed_at TEXT, closed_by TEXT)''')
    c.commit(); c.close()


def gs(k, d=None): return M.get_setting(k, d)
def ss(k, v): M.set_setting(k, str(v))


def is_staff_member(m) -> bool:
    try:
        if m.guild_permissions.administrator or m.guild_permissions.manage_guild: return True
    except AttributeError: pass
    rid = gs('ticket_support_role_id')
    if rid and any(str(r.id) == str(rid) for r in getattr(m, 'roles', [])): return True
    try: return bool(M.is_staff(m.id))
    except Exception: return False


def admin_only():
    return M.is_admin()


# ───────────────────────── logs ─────────────────────────
async def send_log(kind: str, embed: discord.Embed, file: discord.File = None):
    cid = gs(f'log_{kind}')
    if not cid and kind == 'payments': cid = getattr(M, 'PAYMENT_CHANNEL_ID', None)
    ch = bot.get_channel(int(cid)) if cid else None
    if not ch: return False
    try:
        await ch.send(embed=embed, file=file) if file else await ch.send(embed=embed)
        return True
    except discord.HTTPException as e:
        log.warning("log %s failed: %s", kind, e); return False
M.send_log = send_log


@bot.command(name='setlog')
@admin_only()
async def setlog_cmd(ctx, kind: str, channel: discord.TextChannel):
    """.setlog <tickets|mod|vps|reminders|payments> #channel"""
    kind = kind.lower()
    if kind not in LOG_TYPES:
        return await ctx.send(embed=err_embed("Unknown log type", "Use one of: " + ", ".join(f"`{k}`" for k in LOG_TYPES)))
    ss(f'log_{kind}', channel.id)
    await ctx.send(embed=ok_embed("Log channel set", f"{E('ok')} **{kind}** logs → {channel.mention}"))


@bot.command(name='dellog')
@admin_only()
async def dellog_cmd(ctx, kind: str):
    ss(f'log_{kind.lower()}', ''); await ctx.send(embed=ok_embed("Log channel removed", f"`{kind}` logs are off."))


@bot.command(name='logs')
@admin_only()
async def logs_cmd(ctx):
    lines = []
    for k, desc in LOG_TYPES.items():
        cid = gs(f'log_{k}'); ch = bot.get_channel(int(cid)) if cid else None
        lines.append(f"{E('ok') if ch else E('no')} **{k}** — {ch.mention if ch else 'not set'}\n╰ {desc}")
    await ctx.send(embed=info_embed("Log Channels", "\n".join(lines) + "\n\nSet one with `.setlog <type> #channel`"))


@bot.listen('on_command_completion')
async def audit(ctx):
    if not ctx.guild or ctx.command is None or not is_staff_member(ctx.author): return
    if ctx.command.name in ('help', 'commands', 'ping', 'version', 'logs', 'payinfo'): return
    e = discord.Embed(title="Staff command", color=0x6b7280, timestamp=discord.utils.utcnow())
    e.add_field(name="Who", value=f"{ctx.author.mention} (`{ctx.author.id}`)", inline=True)
    e.add_field(name="Where", value=ctx.channel.mention, inline=True)
    txt = ctx.message.content[:400]
    if ctx.command.name in ('setwallet', 'setupi'): txt = f"{ctx.prefix}{ctx.command.name} …(hidden)"
    e.add_field(name="Command", value=f"`{txt}`", inline=False)
    await send_log('mod', e)


# ───────────────────── payments from Discord ─────────────────────
UPI_RE = re.compile(r'^[A-Za-z0-9._\-]{2,}@[A-Za-z0-9.\-]{2,}$')


def apply_payment_settings():
    upi = gs('upi_id'); name = gs('upi_name')
    if upi is not None and gs('upi_id') != '': M.UPI_ID = upi
    if upi == '': M.UPI_ID = ''
    if name: M.UPI_NAME = name
    w = gs('crypto_wallets')
    if w:
        try: M.CRYPTO_WALLETS.clear(); M.CRYPTO_WALLETS.update(json.loads(w))
        except ValueError: pass


@bot.command(name='setupi')
@admin_only()
async def setupi_cmd(ctx, upi_id: str, *, name: str = ""):
    """.setupi <upi@bank> [Display Name]   (or `.setupi off`)"""
    if upi_id.lower() == 'off':
        ss('upi_id', ''); M.UPI_ID = ''
        return await ctx.send(embed=ok_embed("UPI disabled", "Customers can't pay by UPI until you set one again."))
    if not UPI_RE.match(upi_id):
        return await ctx.send(embed=err_embed("Invalid UPI ID", "Format: `name@bank` — e.g. `axionhost@oksbi`"))
    ss('upi_id', upi_id); ss('upi_name', name.strip()[:40] or M.BOT_NAME)
    apply_payment_settings()
    await ctx.send(embed=ok_embed("UPI saved", f"{E('rupee')} Pay to **{M.UPI_ID}** ({M.UPI_NAME})\nAdd a QR image with `.setqr` (attach it to the message)."))
    try: await ctx.message.delete()
    except discord.HTTPException: pass


@bot.command(name='setqr')
@admin_only()
async def setqr_cmd(ctx):
    """.setqr  — attach your UPI QR image to the same message"""
    if not ctx.message.attachments:
        return await ctx.send(embed=err_embed("No image", "Attach your QR code image to the same message as `.setqr`."))
    a = ctx.message.attachments[0]
    if not (a.content_type or '').startswith('image/') or a.size > 5 * 1024 * 1024:
        return await ctx.send(embed=err_embed("Bad file", "Send a PNG/JPG image under 5 MB."))
    os.makedirs(DATA_DIR, exist_ok=True)
    await a.save(STATIC_QR)
    e = ok_embed("QR saved", "Customers will see this QR on UPI payments (when the auto-QR library isn't installed).")
    f = discord.File(STATIC_QR, filename='upi_qr.png'); e.set_image(url='attachment://upi_qr.png')
    await ctx.send(embed=e, file=f)


@bot.command(name='delqr')
@admin_only()
async def delqr_cmd(ctx):
    try: os.remove(STATIC_QR)
    except FileNotFoundError: pass
    await ctx.send(embed=ok_embed("QR removed"))


@bot.command(name='setwallet')
@admin_only()
async def setwallet_cmd(ctx, coin: str, address: str):
    """.setwallet USDT-TRC20 T..."""
    if len(address) < 15 or len(coin) > 20:
        return await ctx.send(embed=err_embed("Check the details", "Usage: `.setwallet USDT-TRC20 <address>`"))
    w = dict(M.CRYPTO_WALLETS); w[coin.upper()] = address; ss('crypto_wallets', json.dumps(w)); apply_payment_settings()
    await ctx.send(embed=ok_embed("Wallet saved", f"{E('coin')} **{coin.upper()}** → `{address}`"))
    try: await ctx.message.delete()
    except discord.HTTPException: pass


@bot.command(name='delwallet')
@admin_only()
async def delwallet_cmd(ctx, coin: str):
    w = dict(M.CRYPTO_WALLETS); w.pop(coin.upper(), None); ss('crypto_wallets', json.dumps(w)); apply_payment_settings()
    await ctx.send(embed=ok_embed("Wallet removed", f"`{coin.upper()}` removed."))


@bot.command(name='payinfo')
@admin_only()
async def payinfo_cmd(ctx):
    yes = lambda b: f"{E('ok')} on" if b else f"{E('no')} off"
    e = info_embed("Payment Setup", "What customers can use right now:")
    e.add_field(name=f"{E('rupee')} UPI", value=f"{yes(M.UPI_ID)}\n`{M.UPI_ID or '—'}` {('• ' + M.UPI_NAME) if M.UPI_ID else ''}\nQR image: {'uploaded' if os.path.exists(STATIC_QR) else 'none'}", inline=False)
    e.add_field(name=f"{E('coin')} Crypto", value="\n".join(f"`{k}` {v[:10]}…{v[-6:]}" for k, v in M.CRYPTO_WALLETS.items()) or "none", inline=False)
    e.add_field(name=f"{E('card')} Gateways", value=f"Razorpay {yes(M.RAZORPAY_KEY_ID)} • Stripe {yes(M.STRIPE_SECRET_KEY)} • PayPal {yes(M.PAYPAL_CLIENT_ID)}", inline=False)
    e.add_field(name="Commands", value="`.setupi` `.setqr` `.delqr` `.setwallet` `.delwallet` `.setpaychannel` `.paytest`", inline=False)
    await ctx.send(embed=e)


# ───────────────────────── tickets ─────────────────────────
def transcript_text(channel, msgs) -> str:
    out = [f"Transcript of #{channel.name} — generated {datetime.utcnow():%Y-%m-%d %H:%M} UTC", "=" * 60]
    for m in msgs:
        line = f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author} : {m.clean_content}"
        if m.attachments: line += "  " + " ".join(a.url for a in m.attachments)
        out.append(line)
    return "\n".join(out)


class TicketSelect(discord.ui.Select):
    def __init__(self):
        opts = [discord.SelectOption(label=l, value=v, description=d, emoji=em) for v, l, d, em in TICKET_TOPICS]
        super().__init__(custom_id='avm:tk:select', placeholder='🎫  Choose a topic to open a ticket…', options=opts)

    async def callback(self, interaction: discord.Interaction):
        await open_ticket(interaction, self.values[0])


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None); self.add_item(TicketSelect())


class ConfirmClose(discord.ui.View):
    def __init__(self, opener):
        super().__init__(timeout=30); self.opener = opener

    @discord.ui.button(label="Yes, close it", style=discord.ButtonStyle.danger, emoji="🔒")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Closing…", view=None)
        await close_ticket(interaction.channel, interaction.user)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="🙋", custom_id='avm:tk:claim')
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff_member(interaction.user):
            return await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)
        c = db(); c.execute("UPDATE tickets SET claimed_by=? WHERE channel_id=?", (str(interaction.user.id), str(interaction.channel.id))); c.commit(); c.close()
        await interaction.response.send_message(embed=info_embed("Ticket claimed", f"{E('ok')} {interaction.user.mention} is handling this ticket."))

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id='avm:tk:close')
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        c = db(); row = c.execute("SELECT user_id FROM tickets WHERE channel_id=? AND status='open'", (str(interaction.channel.id),)).fetchone(); c.close()
        if not row: return await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
        if not (is_staff_member(interaction.user) or str(interaction.user.id) == row[0]):
            return await interaction.response.send_message("Only the ticket owner or staff can close it.", ephemeral=True)
        await interaction.response.send_message("Close this ticket and save a transcript?", view=ConfirmClose(interaction.user), ephemeral=True)


async def open_ticket(interaction: discord.Interaction, topic: str):
    await interaction.response.defer(ephemeral=True)
    g, u = interaction.guild, interaction.user
    if not g: return await interaction.followup.send("Use this in the server.", ephemeral=True)
    now = time.time()
    if now - _cool.get(u.id, 0) < 20: return await interaction.followup.send("Slow down a little — try again in a few seconds.", ephemeral=True)
    _cool[u.id] = now
    c = db(); rows = c.execute("SELECT channel_id FROM tickets WHERE user_id=? AND status='open'", (str(u.id),)).fetchall()
    live = [r[0] for r in rows if g.get_channel(int(r[0]))]
    if len(live) >= MAX_OPEN_PER_USER:
        c.close(); return await interaction.followup.send(f"You already have {len(live)} open tickets: " + " ".join(f"<#{x}>" for x in live), ephemeral=True)
    cur = c.execute("INSERT INTO tickets (channel_id, user_id, topic, created_at) VALUES ('', ?, ?, ?)", (str(u.id), topic, datetime.utcnow().isoformat())); tid = cur.lastrowid; c.commit(); c.close()
    role = g.get_role(int(gs('ticket_support_role_id') or 0))
    cat = g.get_channel(int(gs('ticket_category_id') or 0))
    perms = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True, embed_links=True)
    ow = {g.default_role: discord.PermissionOverwrite(view_channel=False), u: perms,
          g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True, embed_links=True, attach_files=True)}
    if role: ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True, manage_messages=True)
    name = f"ticket-{tid:04d}-{re.sub(r'[^a-z0-9]', '', u.name.lower())[:12] or 'user'}"
    try:
        ch = await g.create_text_channel(name, category=cat if isinstance(cat, discord.CategoryChannel) else None, overwrites=ow, topic=f"{topic} ticket • owner {u.id}")
    except discord.HTTPException as e:
        c = db(); c.execute("DELETE FROM tickets WHERE id=?", (tid,)); c.commit(); c.close()
        return await interaction.followup.send(f"Couldn't create the ticket channel ({e}). Ask an admin to check my permissions.", ephemeral=True)
    c = db(); c.execute("UPDATE tickets SET channel_id=? WHERE id=?", (str(ch.id), tid)); c.commit(); c.close()
    label = next((l for v, l, _, _ in TICKET_TOPICS if v == topic), topic)
    e = info_embed(f"Ticket #{tid:04d} — {label}", f"Hi {u.mention}! {E('rocket')} Our team will be with you shortly.\n\n**Please tell us:**\n• what you need help with\n• your VPS name / order number (if any)\n• screenshots if it helps\n\n{E('bell')} Staff: press **Claim** to take this ticket.")
    await ch.send(content=f"{u.mention} {role.mention if role else ''}", embed=e, view=TicketControlView())
    await interaction.followup.send(f"{E('ok')} Ticket created: {ch.mention}", ephemeral=True)
    le = discord.Embed(title="Ticket opened", color=0x22c55e, timestamp=discord.utils.utcnow(), description=f"#{tid:04d} • **{label}** • {u.mention} • {ch.mention}")
    await send_log('tickets', le)


async def close_ticket(channel, closer):
    c = db(); row = c.execute("SELECT id, user_id, topic FROM tickets WHERE channel_id=? AND status='open'", (str(channel.id),)).fetchone()
    if not row: c.close(); return
    tid, uid, topic = row
    c.execute("UPDATE tickets SET status='closed', closed_at=?, closed_by=? WHERE id=?", (datetime.utcnow().isoformat(), str(closer.id), tid)); c.commit(); c.close()
    msgs = [m async for m in channel.history(limit=2000, oldest_first=True)]
    data = transcript_text(channel, msgs).encode('utf-8', 'replace')
    fname = f"transcript-ticket-{tid:04d}.txt"
    e = discord.Embed(title="Ticket closed", color=0xef4444, timestamp=discord.utils.utcnow(), description=f"#{tid:04d} • **{topic}** • owner <@{uid}> • closed by {closer.mention} • {len(msgs)} messages")
    await send_log('tickets', e, discord.File(io.BytesIO(data), filename=fname))
    try:
        user = await bot.fetch_user(int(uid))
        await user.send(embed=info_embed(f"Your ticket #{tid:04d} was closed", "A transcript is attached. Open a new ticket anytime if you need more help."), file=discord.File(io.BytesIO(data), filename=fname))
    except (discord.HTTPException, ValueError): pass
    try: await channel.send(embed=info_embed("Closing ticket", "This channel will be deleted in 5 seconds."))
    except discord.HTTPException: pass
    await asyncio.sleep(5)
    try: await channel.delete(reason=f"Ticket #{tid:04d} closed by {closer}")
    except discord.HTTPException as ex: log.warning("ticket delete failed: %s", ex)


@bot.command(name='ticketsetup')
@admin_only()
async def ticketsetup_cmd(ctx, channel: discord.TextChannel = None):
    """.ticketsetup [#channel] — post the ticket panel"""
    channel = channel or ctx.channel
    e = info_embed("Support Center", f"{E('bell')} Need help? Pick a topic below and a private channel will open for you and our team.\n\n💳 **Billing** • 🛠️ **Support** • 🚀 **Sales** • 🛡️ **Abuse**\n\n{E('clock')} We usually reply within a few hours.")
    await channel.send(embed=e, view=TicketPanelView())
    await ctx.send(embed=ok_embed("Panel posted", f"Ticket panel is live in {channel.mention}.\nOptional: `.setticketcategory <category>` and `.setsupportrole @role`"))


@bot.command(name='setticketcategory')
@admin_only()
async def setticketcategory_cmd(ctx, category: discord.CategoryChannel):
    ss('ticket_category_id', category.id); await ctx.send(embed=ok_embed("Ticket category set", f"New tickets go under **{category.name}**."))


@bot.command(name='setsupportrole')
@admin_only()
async def setsupportrole_cmd(ctx, role: discord.Role):
    ss('ticket_support_role_id', role.id); await ctx.send(embed=ok_embed("Support role set", f"{role.mention} can see and answer all tickets."))


@bot.command(name='close')
async def close_cmd(ctx):
    """.close — close the current ticket"""
    c = db(); row = c.execute("SELECT user_id FROM tickets WHERE channel_id=? AND status='open'", (str(ctx.channel.id),)).fetchone(); c.close()
    if not row: return
    if not (is_staff_member(ctx.author) or str(ctx.author.id) == row[0]): return await ctx.send("Only the ticket owner or staff can close this.")
    await close_ticket(ctx.channel, ctx.author)


@bot.command(name='tadd')
async def tadd_cmd(ctx, member: discord.Member):
    """.tadd @user — add someone to this ticket (staff)"""
    c = db(); row = c.execute("SELECT 1 FROM tickets WHERE channel_id=? AND status='open'", (str(ctx.channel.id),)).fetchone(); c.close()
    if not row or not is_staff_member(ctx.author): return
    await ctx.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
    await ctx.send(embed=ok_embed("User added", f"{member.mention} can now see this ticket."))


# ───────────────────── reminders & dashboard ─────────────────────
def _days_left(s):
    try: return (datetime.fromisoformat(str(s)) - datetime.utcnow()).total_seconds() / 86400
    except ValueError: return None


@bot.command(name='setreminders')
@admin_only()
async def setreminders_cmd(ctx, *days: int):
    """.setreminders 7 3 1  — days before expiry to remind paid users"""
    ds = sorted({d for d in days if 0 < d <= 60}, reverse=True)
    if not ds: return await ctx.send(embed=err_embed("Usage", "`.setreminders 7 3 1` (days before expiry, 1-60)"))
    ss('reminder_days', ",".join(map(str, ds))); M.REMINDER_DAYS = ds
    await ctx.send(embed=ok_embed("Reminders updated", f"{E('bell')} Paid users get reminders **{', '.join(map(str, ds))} days** before expiry (DM with a Renew button)."))


@bot.command(name='upcoming')
@admin_only()
async def upcoming_cmd(ctx, days: int = 7):
    """.upcoming [days] — subscriptions expiring soon"""
    c = db(); rows = c.execute("SELECT container_name, user_id, plan_name, expires_at, status FROM subscriptions WHERE status IN ('active','grace') ORDER BY expires_at").fetchall(); c.close()
    soon = [(r, _days_left(r[3])) for r in rows]; soon = [(r, d) for r, d in soon if d is not None and d <= days]
    if not soon: return await ctx.send(embed=info_embed("Upcoming renewals", f"{E('ok')} Nothing expires in the next {days} days."))
    lines = [f"{E('calendar')} `{r[0]}` • <@{r[1]}> • {r[2] or '—'} • **{max(0, d):.1f}d** left" + (" ⚠️ grace" if r[4] == 'grace' else "") for r, d in soon[:20]]
    await ctx.send(embed=info_embed(f"Expiring in {days} days ({len(soon)})", "\n".join(lines)))


@bot.command(name='dashboard')
@admin_only()
async def dashboard_cmd(ctx):
    """.dashboard — business overview"""
    c = db(); q = lambda s, *a: c.execute(s, a).fetchone()
    month = datetime.utcnow().strftime('%Y-%m')
    subs = q("SELECT COUNT(*) FROM subscriptions WHERE status='active'")[0]
    grace = q("SELECT COUNT(*) FROM subscriptions WHERE status='grace'")[0]
    susp = q("SELECT COUNT(*) FROM subscriptions WHERE status='suspended'")[0]
    rev_all = q("SELECT COALESCE(SUM(total),0), COUNT(*) FROM orders WHERE status='paid' AND method != 'staff'")
    rev_m = q("SELECT COALESCE(SUM(total),0), COUNT(*) FROM orders WHERE status='paid' AND method != 'staff' AND paid_at LIKE ?", month + '%')
    pend = q("SELECT COUNT(*) FROM orders WHERE status IN ('verifying','awaiting_payment')")[0]
    tk = q("SELECT COUNT(*) FROM tickets WHERE status='open'")[0]
    vps = q("SELECT COUNT(*) FROM vps")[0]
    soon = sum(1 for (x,) in c.execute("SELECT expires_at FROM subscriptions WHERE status='active'").fetchall() if (_days_left(x) or 99) <= 7)
    c.close()
    e = info_embed("Business Dashboard", f"{E('chart')} Live overview • {datetime.utcnow():%d %b %Y}")
    e.add_field(name=f"{E('rupee')} Revenue", value=f"This month **₹{rev_m[0]:,.0f}** ({rev_m[1]} orders)\nAll time **₹{rev_all[0]:,.0f}** ({rev_all[1]} orders)", inline=False)
    e.add_field(name=f"{E('crown')} Subscriptions", value=f"{E('online')} active **{subs}**\n{E('warn')} grace **{grace}**\n{E('offline')} suspended **{susp}**", inline=True)
    e.add_field(name=f"{E('calendar')} Renewals", value=f"Expiring ≤7d **{soon}**\nPending payments **{pend}**", inline=True)
    e.add_field(name=f"{E('server')} Infrastructure", value=f"Total VPS **{vps}**\nOpen tickets **{tk}**", inline=True)
    await ctx.send(embed=e)


# ───────────────────────── setup ─────────────────────────
def setup_extras():
    """Call once from the main bot's setup hook."""
    init_tables(); apply_payment_settings()
    rd = gs('reminder_days')
    if rd:
        try: M.REMINDER_DAYS = sorted({int(x) for x in rd.split(',') if x.strip()}, reverse=True) or M.REMINDER_DAYS
        except ValueError: pass
    for v in (TicketPanelView(), TicketControlView()): bot.add_view(v)
    log.info("AVM extras loaded (tickets, logs, payments-from-discord, dashboard)")
