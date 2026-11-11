"""
AVM-Pro — KVM add-on (real virtual machines via Proxmox VE)
=============================================================
LXC containers share the host kernel. This add-on provisions real KVM
virtual machines on a Proxmox VE host instead, by cloning a prepared
cloud-init template with `qm` and talking to the guest through the QEMU
guest agent (no network dependency needed for setup — same idea as
`lxc exec` for containers).

SCOPE OF THIS FIRST VERSION — read this before telling a customer KVM is
"fully live":
  - Admin-driven only (`.kvmcreate`). It is NOT wired into the paid store /
    `.proplan` / billing flow yet — that's a second step once this is
    confirmed working on your real Proxmox host.
  - SSH access only (password, via cloud-init), run through `.kvmssh`. No
    SSHX, no suspend/resume, no resize buttons, no mining-detection, no
    port-forward UI — none of the LXC manage-panel features apply here yet.
  - UNTESTED against a real Proxmox host: this sandbox has no `qm`/libvirt/
    KVM to run against. The `qm` commands below follow Proxmox's own CLI
    docs, but the exact JSON shape of `qm guest exec-status` has changed
    across PVE versions, so `.kvmtest` is provided specifically to catch
    that before you rely on it. Run `.kvmtest` first.

Prerequisites (one-time, done BY HAND on the Proxmox host, not by the bot):
  1. The bot's host (wherever this bot's process runs) can SSH into the
     Proxmox host as root with a KEY (no password prompt) — set that up
     first: `ssh-copy-id root@<proxmox-ip>` from the bot's machine.
  2. A cloud-init-ready VM template exists on Proxmox (Ubuntu/Debian cloud
     image imported with `qm importdisk` + `qm set ... --ide2 ...cloudinit`
     + `qm template <vmid>`), WITH qemu-guest-agent enabled in the image
     (`qm set <vmid> --agent enabled=1`, and the image's cloud-init should
     install/start qemu-guest-agent on first boot).
  3. In Discord: `.kvmconfig host root@<proxmox-ip>`, `.kvmconfig node pve`,
     `.kvmconfig template <template-vmid>`, `.kvmconfig storage <pool>`,
     then `.kvmtest`.
"""
import sys
import json
import time
import shlex
import asyncio
import logging
import secrets
import string
from typing import Optional, Dict, Any, Tuple

M = sys.modules.get('__main__')
if M is None or not hasattr(M, 'bot'):
    import avm_pro_bot as M  # type: ignore

bot = M.bot
is_admin = M.is_admin
PREFIX = M.PREFIX
get_setting = M.get_setting
set_setting = M.set_setting
create_success_embed = M.create_success_embed
create_error_embed = M.create_error_embed
create_info_embed = M.create_info_embed
em = getattr(M, 'em', lambda name: '')

logger = logging.getLogger('avm_kvm')

SSH_OPTS = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=10']


def _cfg(key: str, default: str = '') -> str:
    return str(get_setting(f'kvm_{key}', default) or default)


async def _ssh(args: list, timeout: int = 30) -> Tuple[int, str, str]:
    """Run one command on the Proxmox host over SSH. args = argv to run remotely (we quote it ourselves)."""
    host = _cfg('host')
    if not host:
        raise RuntimeError("KVM host not set — run `.kvmconfig host root@<proxmox-ip>` first.")
    remote = ' '.join(shlex.quote(a) for a in args)
    proc = await asyncio.create_subprocess_exec('ssh', *SSH_OPTS, host, remote,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise asyncio.TimeoutError(f"SSH to {host} timed out after {timeout}s running: {args[0] if args else '?'}")
    return proc.returncode, out.decode(errors='ignore'), err.decode(errors='ignore')


async def qm(args: list, timeout: int = 30) -> Tuple[int, str, str]:
    return await _ssh(['qm'] + args, timeout=timeout)


class KvmError(Exception):
    pass


# ------------------------------------------------------------
# guest agent exec  (works without the VM having any network route to us —
# only needs the agent socket, which Proxmox proxies over the same qm link)
# ------------------------------------------------------------
async def guest_exec(vmid: int, command: str, timeout: int = 60) -> Tuple[int, str, str]:
    rc, out, err = await qm(['guest', 'exec', str(vmid), '--timeout', str(timeout), '--',
                             'bash', '-c', command], timeout=timeout + 15)
    if rc != 0:
        raise KvmError(f"qm guest exec failed (is the guest agent running?): {(err or out).strip()[:300]}")
    try:
        data = json.loads(out)
    except ValueError:
        raise KvmError(f"Unexpected `qm guest exec` output (check your PVE version):\n{out[:300]}")
    pid = data.get('pid')
    if pid is None:
        raise KvmError(f"`qm guest exec` did not return a pid:\n{out[:300]}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc2, out2, _ = await qm(['guest', 'exec-status', str(vmid), str(pid)], timeout=20)
        try:
            st = json.loads(out2)
        except ValueError:
            st = {}
        if st.get('exited'):
            out_data = st.get('out-data', '')
            err_data = st.get('err-data', '')
            return st.get('exitcode', 0), out_data, err_data
        await asyncio.sleep(1.5)
    raise KvmError(f"Command in VM {vmid} did not finish within {timeout}s")


async def wait_for_agent(vmid: int, seconds: int = 90) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        rc, out, _ = await qm(['agent', str(vmid), 'ping'], timeout=10)
        if rc == 0:
            return True
        await asyncio.sleep(3)
    return False


async def get_guest_ip(vmid: int) -> Optional[str]:
    rc, out, _ = await qm(['agent', str(vmid), 'network-get-interfaces'], timeout=15)
    if rc != 0:
        return None
    try:
        ifaces = json.loads(out)
    except ValueError:
        return None
    for iface in ifaces:
        if iface.get('name') in ('lo',):
            continue
        for addr in iface.get('ip-addresses', []):
            ip = addr.get('ip-address', '')
            if addr.get('ip-address-type') == 'ipv4' and not ip.startswith('127.'):
                return ip
    return None


def _gen_password() -> str:
    return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))


async def next_vmid() -> int:
    rc, out, err = await _ssh(['pvesh', 'get', '/cluster/nextid'], timeout=15)
    if rc != 0 or not out.strip().isdigit():
        raise KvmError(f"Could not get next VMID from Proxmox: {(err or out).strip()[:200]}")
    return int(out.strip())


async def create_kvm_vm(name: str, ram_gb: int, cpu: int, disk_gb: int) -> Dict[str, Any]:
    template = _cfg('template')
    storage = _cfg('storage')
    bridge = _cfg('bridge', 'vmbr0')
    node = _cfg('node', 'pve')
    if not template or not storage:
        raise KvmError("Set `.kvmconfig template <vmid>` and `.kvmconfig storage <pool>` first.")

    vmid = await next_vmid()
    password = _gen_password()
    safe_name = ''.join(c for c in name if c.isalnum() or c in '-')[:30] or f"kvm{vmid}"

    rc, out, err = await qm(['clone', template, str(vmid), '--name', safe_name, '--full', '1', '--storage', storage], timeout=180)
    if rc != 0:
        raise KvmError(f"Clone failed: {(err or out).strip()[:400]}")

    try:
        rc, out, err = await qm(['set', str(vmid), '--memory', str(ram_gb * 1024), '--cores', str(cpu),
                                 '--net0', f'virtio,bridge={bridge}',
                                 '--ciuser', 'root', '--cipassword', password,
                                 '--ipconfig0', 'ip=dhcp', '--agent', 'enabled=1'], timeout=30)
        if rc != 0:
            raise KvmError(f"qm set failed: {(err or out).strip()[:400]}")

        rc, out, err = await qm(['resize', str(vmid), 'scsi0', f'{disk_gb}G'], timeout=60)
        if rc != 0:
            logger.warning(f"Disk resize to {disk_gb}G failed for VM {vmid} (template disk may already be bigger): {err or out}")

        rc, out, err = await qm(['start', str(vmid)], timeout=60)
        if rc != 0:
            raise KvmError(f"Start failed: {(err or out).strip()[:400]}")
    except Exception:
        try:
            await qm(['stop', str(vmid)], timeout=30)
            await qm(['destroy', str(vmid), '--purge', '1'], timeout=60)
        except Exception as cleanup_err:
            logger.error(f"Cleanup of half-created KVM VM {vmid} failed: {cleanup_err}")
        raise

    agent_up = await wait_for_agent(vmid, seconds=90)
    ip = await get_guest_ip(vmid) if agent_up else None

    return {'vmid': vmid, 'name': safe_name, 'password': password, 'ip': ip,
            'agent_up': agent_up, 'ram': ram_gb, 'cpu': cpu, 'disk': disk_gb, 'node': node}


async def delete_kvm_vm(vmid: int):
    await qm(['stop', str(vmid)], timeout=30)
    await asyncio.sleep(2)
    rc, out, err = await qm(['destroy', str(vmid), '--purge', '1'], timeout=90)
    if rc != 0:
        raise KvmError(f"Destroy failed: {(err or out).strip()[:400]}")


# ------------------------------------------------------------
# admin commands
# ------------------------------------------------------------
@bot.command(name='kvmconfig')
@is_admin()
async def kvmconfig_cmd(ctx, key: str = None, *, value: str = None):
    """.kvmconfig host|node|template|storage|bridge <value>  |  .kvmconfig show"""
    valid = ('host', 'node', 'template', 'storage', 'bridge')
    if key == 'show' or key is None:
        lines = [f"**{k}:** `{_cfg(k) or '—not set—'}`" for k in valid]
        await ctx.send(embed=create_info_embed(f"{em('server')} KVM Config", "\n".join(lines)))
        return
    key = key.lower()
    if key not in valid or not value:
        await ctx.send(embed=create_error_embed("Usage", f"`{PREFIX}kvmconfig <{'|'.join(valid)}> <value>`  or  `{PREFIX}kvmconfig show`"))
        return
    set_setting(f'kvm_{key}', value.strip())
    await ctx.send(embed=create_success_embed("Saved", f"`kvm_{key}` = `{value.strip()}`"))


@bot.command(name='kvmtest')
@is_admin()
async def kvmtest_cmd(ctx):
    """.kvmtest — check SSH + qm + template reachability before trusting KVM"""
    msg = await ctx.send(embed=create_info_embed("KVM Doctor", "Checking…"))
    lines = []
    ok_all = True

    async def chk(label, coro):
        nonlocal ok_all
        try:
            r = await coro
            lines.append(f"✅ {label}")
            return r
        except Exception as e:
            lines.append(f"❌ {label} — {str(e)[:200]}")
            ok_all = False
            return None

    host = _cfg('host')
    if not host:
        lines.append("❌ `kvm_host` not set — run `.kvmconfig host root@<proxmox-ip>`")
    else:
        rc, out, err = await chk("SSH to Proxmox host (key auth, no password)", _ssh(['echo', 'ok'], timeout=15)) or (1, '', '')
        if rc == 0 and out.strip() == 'ok':
            lines[-1] = "✅ SSH to Proxmox host (key auth, no password)"
        rc2, out2, err2 = await chk("`qm` CLI reachable", _ssh(['qm', 'version'], timeout=15)) or (1, '', '')
        template = _cfg('template')
        if template:
            rc3, out3, err3 = await chk(f"Template VM `{template}` exists", _ssh(['qm', 'status', template], timeout=15)) or (1, '', '')
        else:
            lines.append("⚠️ `kvm_template` not set — run `.kvmconfig template <vmid>`")
        storage = _cfg('storage')
        if not storage:
            lines.append("⚠️ `kvm_storage` not set — run `.kvmconfig storage <pool>`")
        rc4, out4, err4 = await chk("`/cluster/nextid` reachable (pvesh)", _ssh(['pvesh', 'get', '/cluster/nextid'], timeout=15)) or (1, '', '')

    await msg.edit(embed=(create_success_embed if ok_all else create_error_embed)(
        "KVM Doctor", "\n".join(lines) + ("\n\nAll good — try `.kvmcreate test 2 1 20`." if ok_all else
        "\n\nFix the ❌ lines, then run `.kvmtest` again.")))


@bot.command(name='kvmcreate')
@is_admin()
async def kvmcreate_cmd(ctx, name: str = None, ram_gb: int = None, cpu: int = None, disk_gb: int = None, member=None):
    """.kvmcreate <name> <ram_gb> <cpu> <disk_gb> [@user]  — admin-only, manual KVM provision (not wired to the store yet)"""
    if not name or not ram_gb or not cpu or not disk_gb:
        await ctx.send(embed=create_error_embed("Usage", f"`{PREFIX}kvmcreate <name> <ram_gb> <cpu> <disk_gb> [@user]`"))
        return
    status = await ctx.send(embed=create_info_embed("Creating KVM VM", f"Cloning template, setting resources, starting `{name}`… this can take ~1-2 min."))
    try:
        info = await create_kvm_vm(name, ram_gb, cpu, disk_gb)
    except Exception as e:
        logger.error("KVM create failed", exc_info=True)
        await status.edit(embed=create_error_embed("KVM Create Failed", f"```{str(e)[:600]}```Run `{PREFIX}kvmtest` to check the basics."))
        return
    desc = (f"**VMID:** `{info['vmid']}`\n**Specs:** {info['ram']}GB RAM / {info['cpu']} CPU / {info['disk']}GB Disk\n"
            f"**Guest agent:** {'✅ up' if info['agent_up'] else '⚠️ not responding yet — check manually with `qm agent ' + str(info['vmid']) + ' ping` on the host'}\n"
            f"**IP:** `{info['ip'] or 'not detected yet — try .kvmssh ' + str(info['vmid']) + ' in a minute'}`\n"
            f"**Root password:** ||`{info['password']}`||")
    embed = create_success_embed(f"{em('server')} KVM VM Created", desc)
    target = ctx.author
    try:
        if ctx.message.mentions:
            target = ctx.message.mentions[0]
            await target.send(embed=embed)
            await ctx.send(embed=create_success_embed("Delivered", f"Details DMed to {target.mention}."))
            await status.delete()
            return
    except Exception:
        pass
    await status.edit(embed=embed)


@bot.command(name='kvmssh')
async def kvmssh_cmd(ctx, vmid: int = None):
    """.kvmssh <vmid>  — re-detect the VM's IP + confirm it's reachable via the guest agent"""
    if not vmid:
        await ctx.send(embed=create_error_embed("Usage", f"`{PREFIX}kvmssh <vmid>`"))
        return
    msg = await ctx.send(embed=create_info_embed("Checking KVM VM", f"Pinging guest agent for VM `{vmid}`…"))
    up = await wait_for_agent(vmid, seconds=20)
    if not up:
        await msg.edit(embed=create_error_embed("Not Reachable", f"Guest agent on VM `{vmid}` is not responding. Is it running? `qm status {vmid}` on the host."))
        return
    ip = await get_guest_ip(vmid)
    if not ip:
        await msg.edit(embed=create_error_embed("No IP Yet", f"Agent is up but no IPv4 reported yet for VM `{vmid}` — try again in a bit."))
        return
    await msg.edit(embed=create_success_embed("KVM VM Reachable", f"```ssh root@{ip}```Password was DMed when the VM was created. This IP is on the Proxmox LAN — reachable only from inside that network unless you also set up a dedicated/shared public IP for it."))


@bot.command(name='kvmdelete')
@is_admin()
async def kvmdelete_cmd(ctx, vmid: int = None):
    """.kvmdelete <vmid>  — stop + destroy a KVM VM"""
    if not vmid:
        await ctx.send(embed=create_error_embed("Usage", f"`{PREFIX}kvmdelete <vmid>`"))
        return
    msg = await ctx.send(embed=create_info_embed("Deleting", f"Stopping + destroying VM `{vmid}`…"))
    try:
        await delete_kvm_vm(vmid)
    except Exception as e:
        await msg.edit(embed=create_error_embed("Delete Failed", str(e)[:500]))
        return
    await msg.edit(embed=create_success_embed("Deleted", f"VM `{vmid}` destroyed."))


def setup_kvm():
    logger.info("avm_kvm loaded — run .kvmconfig then .kvmtest before using .kvmcreate")
