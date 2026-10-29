#!/usr/bin/env bash
# MOST — пустить клиентов AmneziaWG этого сервера в Нидерланды.
# Телефон -> AmneziaWG (RU) -> Xray -> NL-сервер. Конфиги AmneziaWG менять не нужно.
#
#   sudo bash awg.sh on      включить
#   sudo bash awg.sh off     выключить (AmneziaWG снова выходит из России)
#   sudo bash awg.sh status  показать состояние
#
# Требует, чтобы мост уже был установлен через install.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
XRAY=/usr/local/bin/xray
CONFIG=/usr/local/etc/xray/config.json
IFACE="${MOST_AWG_IFACE:-awg0}"
PORT=12345
RULES=/usr/local/sbin/most-awg-rules
UNIT=/etc/systemd/system/most-awg.service

die() { echo "Ошибка: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ -f "$CONFIG" ]] || die "мост не установлен — сначала запустите install.sh"

# Добавляет (on) или убирает (off) в конфиге Xray вход для трафика AmneziaWG.
patch_config() {
  python3 - "$CONFIG" "$1" "$PORT" <<'EOF'
import json, sys
path, action, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
cfg = json.load(open(path))
cfg["inbounds"] = [i for i in cfg["inbounds"] if i.get("tag") != "awg-tproxy"]
if action == "on":
    cfg["inbounds"].append({
        "tag": "awg-tproxy",
        "listen": "0.0.0.0",
        "port": port,
        "protocol": "dokodemo-door",
        "settings": {"network": "tcp,udp", "followRedirect": True},
        "streamSettings": {"sockopt": {"tproxy": "tproxy"}},
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True},
    })
json.dump(cfg, open(path, "w"), indent=2, ensure_ascii=False)
EOF
}

restart_xray() {
  "$XRAY" run -test -c "$CONFIG" >/dev/null || die "Xray отверг конфиг: $XRAY run -test -c $CONFIG"
  systemctl restart xray
  sleep 1
  systemctl is-active --quiet xray || { journalctl -u xray -n 20 --no-pager; die "Xray не запустился"; }
}

case "${1:-}" in
  on)
    ip link show "$IFACE" >/dev/null 2>&1 \
      || die "интерфейс $IFACE не найден. Пришлите вывод: ip -br link"
    cp "$CONFIG" "$CONFIG.bak.$(date +%s)"
    patch_config on
    restart_xray

    install -m 755 "$SCRIPT_DIR/awg-rules.sh" "$RULES"
    cat >"$UNIT" <<EOF
[Unit]
Description=MOST: AmneziaWG -> Xray -> NL
After=network-online.target xray.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=MOST_AWG_IFACE=$IFACE MOST_AWG_PORT=$PORT
ExecStart=$RULES up
ExecStop=$RULES down

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable most-awg >/dev/null 2>&1
    systemctl restart most-awg

    # Перехваченные пакеты проходят через INPUT — ufw должен их пропустить.
    if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
      ufw allow in on "$IFACE" comment "MOST: AmneziaWG -> Xray" >/dev/null
    fi

    cat <<EOF

Готово. Клиенты AmneziaWG теперь выходят в интернет через Нидерланды.
На телефоне ничего менять не нужно: переподключитесь в AmneziaWG и откройте ipinfo.io.
Российские сайты по-прежнему открываются напрямую из России.

Выключить: sudo bash awg.sh off
EOF
    ;;
  off)
    systemctl disable --now most-awg >/dev/null 2>&1 || true
    "$RULES" down 2>/dev/null || true
    rm -f "$UNIT" "$RULES"
    systemctl daemon-reload
    patch_config off
    restart_xray
    if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
      ufw delete allow in on "$IFACE" >/dev/null 2>&1 || true
    fi
    echo "Выключено: AmneziaWG снова выходит в интернет из России."
    ;;
  status)
    systemctl is-active --quiet most-awg && echo "most-awg: включён" || echo "most-awg: выключен"
    ss -lntup | grep ":$PORT " || echo "Xray не слушает порт $PORT"
    iptables -t mangle -S MOST_AWG 2>/dev/null | head -n 3 || true
    ;;
  *)
    echo "Использование: sudo bash awg.sh on|off|status" >&2
    exit 1
    ;;
esac
