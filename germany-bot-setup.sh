#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"
[[ $EUID == 0 && -f /etc/most/bot.env ]] || { echo "Запустите через sudo на сервере бота."; exit 1; }
identity=/etc/most/germany.key
known=/etc/most/germany.known_hosts
case "${1:-}" in
  key)
    if [[ ! -f "$identity" ]]; then ssh-keygen -q -t ed25519 -N '' -f "$identity"; fi
    ssh-keygen -y -f "$identity"
    ;;
  enable)
    [[ -s "$identity" ]] || { echo "Сначала запустите germany-bot-setup.sh key"; exit 1; }
    read -rp "Вставьте публичную строку SSH-сервера из Германии: " host_key
    read -r kind data _ <<< "$host_key"
    [[ "$kind" == ssh-ed25519 && "$data" =~ ^[A-Za-z0-9+/]{68}$ ]] || { echo "Ожидается публичный ключ Ed25519"; exit 1; }
    tmp=$(mktemp /etc/most/germany.host.XXXXXX)
    trap 'rm -f "$tmp"' EXIT
    printf '185.21.8.78 %s %s\n' "$kind" "$data" >"$tmp"
    # Проверяем SSH, ключ сервера и ограниченную команду до обновления работающего бота.
    printf 'status\n' | ssh -F /dev/null -T -o BatchMode=yes -o ConnectTimeout=7 -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$tmp" -i "$identity" root@185.21.8.78
    /opt/most/venv/bin/python -m py_compile bot/bot.py
    mv "$tmp" "$known"
    install -m 0644 bot/bot.py /opt/most/bot.py.new
    mv /opt/most/bot.py.new /opt/most/bot.py
    systemctl restart most-bot
    systemctl is-active most-bot
    ;;
  *) echo "sudo bash germany-bot-setup.sh key | enable"; exit 2 ;;
esac
