#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════╗
# ║  AVM — Axion VPS Manager  •  Installer & Control Panel   ║
# ║  Made by Peter Parker                                    ║
# ╚══════════════════════════════════════════════════════════╝
#
#   ./install.sh                       guided install (token, IDs, logo…)
#   ./install.sh set <key> <value>     change one config value
#   ./install.sh logo <url>            set the logo      (direct image link)
#   ./install.sh banner <url>          set the banner    (direct image link)
#   ./install.sh show                  print config (token hidden)
#   ./install.sh service               install + start the 24/7 systemd service
#   ./install.sh update <url>          download a new bot file, verify, swap, restart
#   ./install.sh status | start | stop | restart | logs | help
#
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="$DIR/config.json"
VENV="$DIR/venv"
SVC="axionhost-bot"
WORKLOG="$DIR/.install.log"
AUTHOR="Peter Parker"
PRO=0; PNAME="Axion VPS Manager"; VERKEY="VERSION"
REQ="$DIR/requirements.txt"
if [ -f "$DIR/avm_pro_bot.py" ]; then PRO=1; [ -f "$DIR/requirements-pro.txt" ] && REQ="$DIR/requirements-pro.txt"; SVC="avm-pro-bot"; PNAME="Axion VPS Manager PRO"; VERKEY="PRO_VERSION"; fi

# ───────────────────────── look & feel ─────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then FANCY=1; else FANCY=0; fi
if [ "$FANCY" = 1 ]; then
  R=$'\e[0m'; B=$'\e[1m'; DIM=$'\e[2m'
  RED=$'\e[38;5;203m'; GRN=$'\e[38;5;84m'; YEL=$'\e[38;5;221m'; BLU=$'\e[38;5;75m'
  PUR=$'\e[38;5;141m'; PNK=$'\e[38;5;213m'; CYN=$'\e[38;5;87m'; WHT=$'\e[38;5;255m'; GRY=$'\e[38;5;245m'
  A1=$'\e[38;5;213m'; A2=$'\e[38;5;177m'; A3=$'\e[38;5;141m'
  trap 'printf "\e[?25h"' EXIT
  trap 'printf "\e[?25h\n"; exit 130' INT TERM
else
  R=''; B=''; DIM=''; RED=''; GRN=''; YEL=''; BLU=''; PUR=''; PNK=''; CYN=''; WHT=''; GRY=''; A1=''; A2=''; A3=''
fi
FRAMES=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)

hide_cursor() { [ "$FANCY" = 1 ] && printf '\e[?25l'; return 0; }
show_cursor() { [ "$FANCY" = 1 ] && printf '\e[?25h'; return 0; }
nap() { [ "$FANCY" = 1 ] && sleep "$1"; return 0; }

ok()   { printf ' %s✔%s %s\n' "$GRN" "$R" "$*"; }
info() { printf ' %s➜%s %s\n' "$CYN" "$R" "$*"; }
warn() { printf ' %s⚠%s  %s%s%s\n' "$YEL" "$R" "$YEL" "$*" "$R"; }
bad()  { printf ' %s✘%s %s\n' "$RED" "$R" "$*"; }

die() {
  show_cursor
  printf '\n %s✘ ERROR%s  %s%s%s\n\n' "$RED$B" "$R" "$WHT" "$*" "$R" >&2
  exit 1
}

typewrite() {  # text [delay]
  local text="$1" d="${2:-0.012}" i
  if [ "$FANCY" != 1 ]; then printf '%s\n' "$text"; return; fi
  for ((i = 0; i < ${#text}; i++)); do printf '%s' "${text:$i:1}"; sleep "$d"; done
  printf '\n'
}

bar() {  # pct [width] → coloured bar string
  local pct=$1 w=${2:-24} f e i out=""
  [ "$pct" -gt 100 ] && pct=100
  f=$((pct * w / 100)); e=$((w - f))
  local cols=(213 177 141 105 75 81 87 84)
  for ((i = 0; i < f; i++)); do
    if [ "$FANCY" = 1 ]; then out+=$'\e[38;5;'"${cols[$((i * ${#cols[@]} / w))]}"'m█'; else out+='#'; fi
  done
  out+="${GRY}"
  for ((i = 0; i < e; i++)); do out+='░'; done
  printf '%s%s' "$out" "$R"
}

banner() {
  [ "$FANCY" = 1 ] && printf '\e[2J\e[H'
  printf '\n'
  printf '   %s╔═╗╦  ╦╔╦╗%s\n' "$A1" "$R"
  printf '   %s╠═╣╚╗╔╝║║║%s   %s%s%s\n' "$A2" "$R" "$B$WHT" "$PNAME" "$R"
  printf '   %s╩ ╩ ╚╝ ╩ ╩%s   %sby AxionHost%s\n' "$A3" "$R" "$GRY" "$R"
  printf '\n'
  local pad=$((36 - 2 - 2 - 1 - 8 - ${#AUTHOR}))
  printf ' %s╭────────────────────────────────────╮%s\n' "$PUR" "$R"
  printf ' %s│%s  %s⚡%s %sMade by %s%s%s%*s%s│%s\n' "$PUR" "$R" "$YEL" "$R" "$GRY" "$B$PNK" "$AUTHOR" "$R" "$pad" "" "$PUR" "$R"
  printf ' %s╰────────────────────────────────────╯%s\n\n' "$PUR" "$R"
  nap 0.25
}

loading_intro() {  # quick animated bar for style
  [ "$FANCY" = 1 ] || return 0
  hide_cursor
  local p
  for p in 0 8 17 29 41 55 68 80 91 100; do
    printf '\r %s %s%3d%%%s %sstarting up…%s' "$(bar "$p" 26)" "$B" "$p" "$R" "$GRY" "$R"
    sleep 0.06
  done
  printf '\r\e[2K'
  show_cursor
}

step() {  # n total title
  local n=$1 t=$2; shift 2
  printf '\n %s[%s/%s]%s %s%s%s\n' "$PUR" "$n" "$t" "$R" "$B$WHT" "$*" "$R"
  printf ' %s  %s%s %s%d%%%s\n' "" "$(bar $(((n - 1) * 100 / t)) 20)" "$R" "$GRY" $(((n - 1) * 100 / t)) "$R"
}

run_step() {  # "label" cmd args…   (spinner; full output goes to .install.log)
  local label="$1"; shift
  local pid i=0 start=$SECONDS rc
  { echo; echo "### $label"; } >>"$WORKLOG" 2>/dev/null
  "$@" >>"$WORKLOG" 2>&1 &
  pid=$!
  if [ "$FANCY" = 1 ]; then
    hide_cursor
    while kill -0 "$pid" 2>/dev/null; do
      printf '\r\e[2K %s%s%s %s %s%ss%s' "$PNK" "${FRAMES[i % ${#FRAMES[@]}]}" "$R" "$label" "$GRY" "$((SECONDS - start))" "$R"
      i=$((i + 1)); sleep 0.08
    done
  else
    printf ' … %s\n' "$label"
  fi
  wait "$pid"; rc=$?
  if [ $rc -eq 0 ]; then
    [ "$FANCY" = 1 ] && printf '\r\e[2K'
    printf ' %s✔%s %s %s%ss%s\n' "$GRN" "$R" "$label" "$GRY" "$((SECONDS - start))" "$R"
  else
    [ "$FANCY" = 1 ] && printf '\r\e[2K'
    printf ' %s✘%s %s %s(failed, exit %s)%s\n' "$RED" "$R" "$label" "$RED" "$rc" "$R"
    printf '%s' "$DIM"; tail -n 8 "$WORKLOG" | sed 's/^/     /'; printf '%s' "$R"
  fi
  show_cursor
  return $rc
}

# ───────────────────────── helpers ─────────────────────────
find_bot() {
  if [ -f "$DIR/avm_pro_bot.py" ]; then BOT="$DIR/avm_pro_bot.py"
  elif [ -f "$DIR/avm_bot.py" ]; then BOT="$DIR/avm_bot.py"
  elif [ -f "$DIR/axionhost_bot.py" ]; then BOT="$DIR/axionhost_bot.py"
  else die "Bot file not found. Put avm_bot.py / avm_pro_bot.py next to install.sh."; fi
}

need_py() { command -v python3 >/dev/null 2>&1 || die "python3 not found — run: apt install -y python3 python3-venv python3-pip"; }

cfg_set() {  # key value
  need_py
  python3 - "$CFG" "$1" "$2" <<'PY'
import json, os, sys
path, key, value = sys.argv[1:4]
data = {}
if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
data[key] = value
with open(path, "w") as f:
    json.dump(data, f, indent=2)
os.chmod(path, 0o600)
PY
}

cfg_get() {  # key → value ('' if unset)
  [ -f "$CFG" ] || { echo ""; return; }
  python3 - "$CFG" "$1" <<'PY' 2>/dev/null
import json, sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2], "") or "")
except Exception:
    print("")
PY
}

check_url() {
  local url="$1"
  [[ "$url" =~ ^https?:// ]] || die "URL must start with http:// or https://"
  if [[ "$url" == *cdn.discordapp.com* || "$url" == *media.discordapp.net* ]]; then
    die "Discord CDN links expire in ~24h. Use a permanent link (GitHub raw URL), or attach the image to .setlogo inside Discord."
  fi
  command -v curl >/dev/null 2>&1 || die "curl not found — run: apt install -y curl"
  local ct
  ct=$(curl -sIL --max-time 15 -o /dev/null -w '%{http_code} %{content_type}' "$url") || die "Could not reach $url"
  [[ "$ct" == 200\ image/* ]] || die "URL is not a reachable image (got: $ct)"
}

mask() {  # hide most of a secret
  local v="$1"
  [ -z "$v" ] && { echo "not set"; return; }
  [ "${#v}" -le 8 ] && { echo "••••"; return; }
  echo "${v:0:4}••••••${v: -3}"
}

ask() {  # "Label" key [secret|digits] ["hint"]
  local label="$1" key="$2" mode="${3:-}" hint="${4:-}" cur val shown tries=0
  cur="$(cfg_get "$key")"
  if [ "$mode" = secret ]; then shown="$(mask "$cur")"; else shown="${cur:-}"; fi
  while :; do
    printf ' %s❯%s %s%s%s' "$PUR" "$R" "$B$WHT" "$label" "$R"
    [ -n "$hint" ] && printf ' %s(%s)%s' "$GRY" "$hint" "$R"
    [ -n "$shown" ] && printf ' %s[%s]%s' "$GRY" "$shown" "$R"
    printf '%s: ' ""
    if [ "$mode" = secret ]; then read -r -s val; echo; else read -r val; fi
    [ -z "$val" ] && return 0                       # Enter = keep current
    if [ "$mode" = digits ] && ! [[ "$val" =~ ^[0-9]{5,25}$ ]]; then
      tries=$((tries + 1))
      bad "That should be a numeric Discord ID."
      [ $tries -ge 3 ] && { warn "Skipped $label — set it later with: ./install.sh set $key <id>"; return 0; }
      continue
    fi
    cfg_set "$key" "$val"
    ok "$label saved"
    return 0
  done
}

setup_venv() {
  need_py
  if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV" || return 1
  fi
  "$VENV/bin/pip" install -q -r "$REQ"
}

# ───────────────────────── commands ─────────────────────────
cmd_install() {
  banner
  loading_intro
  : >"$WORKLOG"

  step 1 4 "Checking your server"
  need_py
  ok "Python $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  find_bot
  ok "Bot file: $(basename "$BOT")"
  [ "$(id -u)" -eq 0 ] && ok "Running as root" || warn "Not root — the 24/7 service step needs sudo"
  if command -v lxc >/dev/null 2>&1; then ok "LXD found"; else warn "LXD not found — install it: snap install lxd && lxd init --auto"; fi
  nap 0.3

  step 2 4 "Preparing Python environment"
  run_step "Creating virtual environment" bash -c "[ -d '$VENV' ] || python3 -m venv '$VENV'" \
    || die "Could not create the virtualenv — run: apt install -y python3-venv"
  run_step "Installing dependencies (discord.py …)" "$VENV/bin/pip" install -q -r "$REQ" \
    || die "pip install failed — see $WORKLOG"

  step 3 4 "Configuration"
  [ -f "$CFG" ] || echo '{}' >"$CFG"
  chmod 600 "$CFG"
  cfg_set brand_name AxionHost
  printf ' %sPress Enter to keep a value shown in [brackets].%s\n\n' "$GRY" "$R"
  ask "Discord bot token" token secret
  ask "Your Discord user ID" main_admin_id digits "main admin"
  ask "Your server (guild) ID" guild_id digits
  ask "This node's public IP" server_ip
  ask "Node name" node_name "" "e.g. Node-1"
  local L B2
  printf ' %s❯%s %sLogo URL%s %s(direct image link, Enter to skip)%s: ' "$PUR" "$R" "$B$WHT" "$R" "$GRY" "$R"; read -r L
  if [ -n "$L" ]; then (check_url "$L") && cfg_set logo_url "$L" && ok "Logo saved" || warn "Logo skipped"; fi
  printf ' %s❯%s %sBanner URL%s %s(Enter to skip)%s: ' "$PUR" "$R" "$B$WHT" "$R" "$GRY" "$R"; read -r B2
  if [ -n "$B2" ]; then (check_url "$B2") && cfg_set banner_url "$B2" && ok "Banner saved" || warn "Banner skipped"; fi

  if [ "$PRO" = 1 ]; then
    printf '\n %sPayments — press Enter to skip any method you do not use.%s\n\n' "$GRY" "$R"
    ask "UPI ID" upi_id "" "e.g. name@bank"
    ask "UPI display name" upi_name
    ask "Payment-review channel ID" payment_channel_id digits "staff approves here"
    ask "Sales/invoice channel ID" invoice_channel_id digits
    ask "Razorpay Key ID" razorpay_key_id
    ask "Razorpay Key Secret" razorpay_key_secret secret
    ask "Stripe secret key" stripe_secret_key secret
    ask "PayPal Client ID" paypal_client_id
    ask "PayPal Secret" paypal_secret secret
    info "Crypto wallets: ./install.sh wallet add USDT-TRC20 <address>"
  fi

  step 4 4 "Finishing up"
  nap 0.3
  summary_box
}

BOXW=40
boxrow() {  # colour text  → one aligned row of the green summary box
  local col="$1" text="$2" n
  n=$(printf '%s' "$text" | LC_ALL=C.UTF-8 wc -m 2>/dev/null | tr -d ' ')
  [[ "$n" =~ ^[0-9]+$ ]] || n=${#text}
  printf ' %s│%s%s%s%s%*s%s│%s\n' "$GRN" "$R" "$col" "$text" "$R" $((BOXW - n)) "" "$GRN" "$R"
}

summary_box() {
  local tok; tok="$(cfg_get token)"
  printf '\n %s╭%s╮%s\n' "$GRN" "$(printf '─%.0s' $(seq 1 $BOXW))" "$R"
  boxrow "$B$GRN" "  ✔  All set!"
  printf ' %s├%s┤%s\n' "$GRN" "$(printf '─%.0s' $(seq 1 $BOXW))" "$R"
  boxrow "$WHT" "  1. sudo ./install.sh service"
  boxrow "$WHT" "  2. ./install.sh logs"
  boxrow "$WHT" "  3. In Discord, run:"
  if [ "$PRO" = 1 ]; then
    boxrow "$CYN" "     .setupemojis"
    boxrow "$CYN" "     .setpaychannel #payments"
    boxrow "$CYN" "     .paytest   then   .shoppanel"
  else
    boxrow "$CYN" "     .setalertchannel #alerts"
    boxrow "$CYN" "     .setstatuschannel #node-status"
    boxrow "$CYN" "     .branding"
  fi
  printf ' %s╰%s╯%s\n' "$GRN" "$(printf '─%.0s' $(seq 1 $BOXW))" "$R"
  [ -z "$tok" ] && warn "No bot token saved yet — run: ./install.sh set token <TOKEN>"
  printf '\n %s♥ Made by %s%s%s\n\n' "$GRY" "$B$PNK" "$AUTHOR" "$R"
}

cmd_show() {
  [ -f "$CFG" ] || die "No config.json yet — run ./install.sh first."
  need_py
  banner
  python3 - "$CFG" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
tok = d.get("token") or ""
d["token"] = "•••• hidden ••••" if tok else "NOT SET"
for k in ("razorpay_key_secret", "stripe_secret_key", "paypal_secret"):
    if d.get(k):
        d[k] = "•••• hidden ••••"
w = max(len(k) for k in d) if d else 0
PUR, GRY, END = "\033[38;5;141m", "\033[38;5;245m", "\033[0m"
for k, v in d.items():
    shown = v if v != "" else GRY + "—" + END
    print("  " + PUR + k.ljust(w) + END + "  " + str(shown))
PY
  echo
}

cmd_wallet() {  # add NAME ADDRESS | remove NAME | list
  need_py
  [ -f "$CFG" ] || echo '{}' >"$CFG"
  chmod 600 "$CFG"
  case "${1:-list}" in
    add)    [ $# -ge 3 ] || die "usage: ./install.sh wallet add <name> <address>   e.g. USDT-TRC20 TXxxxx" ;;
    remove) [ $# -ge 2 ] || die "usage: ./install.sh wallet remove <name>" ;;
    list)   ;;
    *)      die "usage: ./install.sh wallet add|remove|list" ;;
  esac
  python3 - "$CFG" "${1:-list}" "${2:-}" "${3:-}" <<'PY' || exit 1
import json, os, sys
path, action, name, addr = sys.argv[1:5]
data = json.load(open(path)) if os.path.exists(path) else {}
w = data.get("crypto_wallets")
if not isinstance(w, dict):
    w = {}
if action == "add":
    if len(addr) < 10:
        print("That address looks too short."); sys.exit(1)
    w[name] = addr
elif action == "remove":
    if name not in w:
        print("No wallet named " + name); sys.exit(1)
    del w[name]
data["crypto_wallets"] = w
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
for k, v in w.items():
    print("  " + k + "  " + v[:6] + "••••" + v[-4:])
if not w:
    print("  (no wallets)")
PY
  [ "${1:-list}" = list ] || { ok "Saved"; info "Apply it with: ./install.sh restart"; }
}

cmd_set() {
  [ $# -ge 2 ] || die "usage: ./install.sh set <key> <value>"
  local key="$1" val="$2"
  [[ "$key" == *url ]] && [ -n "$val" ] && check_url "$val"
  cfg_set "$key" "$val"
  ok "Saved ${B}$key${R}"
  info "Apply it with: ./install.sh restart"
}

svc_check() {
  command -v systemctl >/dev/null 2>&1 || die "systemd not found on this server."
  [ -d /run/systemd/system ] || die "systemd is not running here (container / WSL?). Run the bot manually:  $VENV/bin/python <bot file>"
  [ "$(id -u)" -eq 0 ] || die "Run as root:  sudo ./install.sh $1"
}

cmd_service() {
  svc_check service
  find_bot
  [ -f "$CFG" ] || die "No config.json — run ./install.sh first."
  python3 -c "import json,sys;sys.exit(0 if json.load(open('$CFG')).get('token') else 1)" || die "Token missing — run: ./install.sh set token <TOKEN>"
  banner
  : >"$WORKLOG"
  step 1 2 "Installing the service"
  [ -x "$VENV/bin/python" ] || run_step "Preparing Python environment" setup_venv || die "Environment setup failed — see $WORKLOG"
  cat >"/etc/systemd/system/$SVC.service" <<UNIT
[Unit]
Description=$PNAME (AxionHost)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$VENV/bin/python $BOT
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
UNIT
  ok "Service file written (runs $(basename "$BOT"))"
  run_step "Enabling & starting the bot" bash -c "systemctl daemon-reload && systemctl enable --now $SVC" || die "systemctl failed — see $WORKLOG"
  step 2 2 "Health check"
  local i
  for i in 1 2 3 4 5 6; do sleep 1; systemctl is-active --quiet "$SVC" || break; done
  if systemctl is-active --quiet "$SVC"; then
    ok "${GRN}${B}Bot is running 24/7${R}"
    info "Watch it live:  ./install.sh logs"
    printf '\n %s♥ Made by %s%s%s\n\n' "$GRY" "$B$PNK" "$AUTHOR" "$R"
  else
    journalctl -u "$SVC" -n 25 --no-pager 2>/dev/null | sed 's/^/     /'
    die "The service did not stay up — read the log above."
  fi
}

cmd_update() {
  [ $# -ge 1 ] || die "usage: ./install.sh update <direct-url-to-bot-file>   (e.g. a GitHub raw link)"
  local url="$1" tmp
  [[ "$url" =~ ^https?:// ]] || die "URL must start with http:// or https://"
  need_py; find_bot
  command -v curl >/dev/null 2>&1 || die "curl not found — run: apt install -y curl"
  banner
  : >"$WORKLOG"
  tmp="$(mktemp)"
  step 1 3 "Downloading"
  run_step "Fetching new bot file" curl -fsSL --max-time 90 -o "$tmp" "$url" || { rm -f "$tmp"; die "Download failed (private repo or wrong link?)"; }
  step 2 3 "Verifying"
  run_step "Checking Python syntax" python3 -c "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$tmp" || { rm -f "$tmp"; die "That file is not valid Python — nothing was changed."; }
  grep -q "commands.Bot" "$tmp" || { rm -f "$tmp"; die "That doesn't look like the AVM bot file — nothing was changed."; }
  local ver; ver="$(grep -m1 -E "^${VERKEY} *= *" "$tmp" | sed -E 's/.*"([^"]+)".*/\1/')"
  ok "New version: ${B}${ver:-unknown}${R}"
  step 3 3 "Swapping in the new version"
  cp "$BOT" "$BOT.bak-$(date +%Y%m%d-%H%M%S)" && ok "Old file backed up"
  cat "$tmp" >"$BOT" && rm -f "$tmp"
  ok "Updated $(basename "$BOT")"
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$SVC" 2>/dev/null; then
    run_step "Restarting the bot" systemctl restart "$SVC" || die "Restart failed — roll back with the .bak file."
  else
    info "Service is not running — start it with: sudo ./install.sh service"
  fi
  printf '\n %s♥ Made by %s%s%s\n\n' "$GRY" "$B$PNK" "$AUTHOR" "$R"
}

cmd_status() {
  find_bot
  banner
  local state="stopped" col="$RED" ver
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$SVC" 2>/dev/null; then state="running"; col="$GRN"; fi
  ver="$(grep -m1 -E "^${VERKEY} *= *" "$BOT" | sed -E 's/.*"([^"]+)".*/\1/')"
  printf '  %s●%s Service   %s%s%s\n' "$col" "$R" "$B" "$state" "$R"
  printf '  %s●%s Bot file  %s\n' "$BLU" "$R" "$(basename "$BOT")"
  printf '  %s●%s Version   %s\n' "$BLU" "$R" "${ver:-unknown}"
  printf '  %s●%s Token     %s\n' "$BLU" "$R" "$(mask "$(cfg_get token)")"
  printf '  %s●%s Node      %s\n\n' "$BLU" "$R" "$(cfg_get node_name)"
}

cmd_ctl() {  # start|stop|restart
  svc_check "$1"
  run_step "${1^}ing the bot" systemctl "$1" "$SVC" || die "systemctl $1 failed — try: ./install.sh logs"
  ok "Done"
}

cmd_help() {
  banner
  local rows=(
    "install|Guided setup (token, IDs, logo)"
    "set <key> <value>|Change one config value"
    "logo <url>|Set the logo"
    "banner <url>|Set the banner"
    "show|Print config (secrets hidden)"
    "wallet add/remove/list|Crypto payment wallets (Pro)"
    "service|Install + start the 24/7 service"
    "update <url>|Safely update the bot file"
    "status|Is it running? which version?"
    "start / stop / restart|Control the service"
    "logs|Follow the live log"
  )
  local r
  for r in "${rows[@]}"; do printf '  %s./install.sh %-22s%s %s%s%s\n' "$PUR" "${r%%|*}" "$R" "$GRY" "${r##*|}" "$R"; done
  printf '\n %s♥ Made by %s%s%s\n\n' "$GRY" "$B$PNK" "$AUTHOR" "$R"
}

main() {
  case "${1:-install}" in
    install)         cmd_install ;;
    set)             shift; cmd_set "$@" ;;
    logo)            [ $# -ge 2 ] || die "usage: ./install.sh logo <url>";   check_url "$2"; cfg_set logo_url "$2";   ok "Logo saved — ./install.sh restart" ;;
    banner)          [ $# -ge 2 ] || die "usage: ./install.sh banner <url>"; check_url "$2"; cfg_set banner_url "$2"; ok "Banner saved — ./install.sh restart" ;;
    show)            cmd_show ;;
    wallet)          shift; cmd_wallet "$@" ;;
    service)         cmd_service ;;
    update)          shift; cmd_update "$@" ;;
    status)          cmd_status ;;
    start|stop|restart) cmd_ctl "$1" ;;
    logs)            command -v journalctl >/dev/null 2>&1 || die "journalctl not found"; journalctl -u "$SVC" -f -n 50 ;;
    help|-h|--help)  cmd_help ;;
    *)               die "Unknown command '$1' — run ./install.sh help" ;;
  esac
}

# run only when executed (lets tests `source` this file)
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
