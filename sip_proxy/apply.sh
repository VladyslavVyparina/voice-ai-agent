#!/usr/bin/env bash
#
#  Неінтерактивне заповнення /etc/asterisk/pjsip.conf і extensions.conf
#  зі значень із credentials.env, що лежить у тій самій папці.
#
#  Запускати від root:
#    sudo bash apply.sh
#

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Запустіть від root: sudo bash apply.sh"
  exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
CRED="$DIR/credentials.env"
SRC_PJ="$DIR/pjsip.conf"
SRC_EX="$DIR/extensions.conf"
PJ=/etc/asterisk/pjsip.conf
EX=/etc/asterisk/extensions.conf

if [[ ! -f "$CRED" ]]; then
  echo "❌ Не знайдено $CRED."
  echo "   Скопіюйте credentials.env.example у credentials.env і заповніть значення:"
  echo "   cp credentials.env.example credentials.env && nano credentials.env"
  exit 1
fi

# shellcheck disable=SC1090
set -a; source "$CRED"; set +a

DEFAULT_IP=$(curl -s -4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
PUBLIC_IP="${PUBLIC_IP:-$DEFAULT_IP}"

require() {
  local name="$1" val="${!1:-}"
  if [[ -z "$val" ]]; then
    echo "❌ У credentials.env не задано $name"
    exit 1
  fi
}

require ZADARMA_SIP_USER
require ZADARMA_SIP_PASSWORD
require ZADARMA_CALLERID
require ELEVENLABS_TRUNK_PASSWORD

if ! [[ "$PUBLIC_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "❌ Невірний PUBLIC_IP: $PUBLIC_IP"
  exit 1
fi

if [[ ! -f "$SRC_PJ" ]] || [[ ! -f "$SRC_EX" ]]; then
  echo "❌ Не знайдено $SRC_PJ або $SRC_EX"
  exit 1
fi

echo "==> Відновлюю свіжі шаблони з $DIR у /etc/asterisk/..."
TS=$(date +%Y%m%d_%H%M%S)
mkdir -p "/etc/asterisk/backup_$TS"
[[ -f "$PJ" ]] && cp "$PJ" "/etc/asterisk/backup_$TS/pjsip.conf"
[[ -f "$EX" ]] && cp "$EX" "/etc/asterisk/backup_$TS/extensions.conf"
cp "$SRC_PJ" "$PJ"
cp "$SRC_EX" "$EX"

escape_sed() { printf '%s' "$1" | sed -e 's/[\/&|]/\\&/g'; }

P_PUBLIC_IP=$(escape_sed "$PUBLIC_IP")
P_ZUSER=$(escape_sed "$ZADARMA_SIP_USER")
P_ZPASS=$(escape_sed "$ZADARMA_SIP_PASSWORD")
P_ZCID=$(escape_sed "$ZADARMA_CALLERID")
P_ELPASS=$(escape_sed "$ELEVENLABS_TRUNK_PASSWORD")

echo "==> Записую значення у конфіги..."
sed -i \
  -e "s|__PUBLIC_IP__|$P_PUBLIC_IP|g" \
  -e "s|__ZADARMA_SIP_USER__|$P_ZUSER|g" \
  -e "s|__ZADARMA_SIP_PASSWORD__|$P_ZPASS|g" \
  -e "s|__ELEVENLABS_TRUNK_PASSWORD__|$P_ELPASS|g" \
  "$PJ"

sed -i \
  -e "s|__ZADARMA_CALLERID__|$P_ZCID|g" \
  "$EX"

if grep -qE '__PUBLIC_IP__|__ZADARMA_|__ELEVENLABS_' "$PJ" "$EX" 2>/dev/null; then
  echo "❌ У конфігах лишилися плейсхолдери — щось пішло не так."
  grep -nE '__PUBLIC_IP__|__ZADARMA_|__ELEVENLABS_' "$PJ" "$EX" || true
  exit 1
fi

chown asterisk:asterisk "$PJ" "$EX"
chmod 640 "$PJ" "$EX"

DOMAIN="bot-telegram.ai-agent.website"
if [[ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
  echo "==> Доступ Asterisk до Let's Encrypt ($DOMAIN)..."
  groupadd -f ssl-cert
  usermod -a -G ssl-cert asterisk
  chgrp -R ssl-cert /etc/letsencrypt/live /etc/letsencrypt/archive 2>/dev/null || true
  chmod -R g+rX /etc/letsencrypt/live /etc/letsencrypt/archive 2>/dev/null || true
  chmod g+rx /etc/letsencrypt 2>/dev/null || true
else
  echo "⚠️  Немає /etc/letsencrypt/live/$DOMAIN/fullchain.pem — transport-tls не підніметься."
  echo "   certbot certonly --standalone -d $DOMAIN"
fi

echo "==> Перезапуск Asterisk..."
systemctl restart asterisk
sleep 3

echo
echo "==> Транспорти (має бути transport-udp + transport-tls):"
asterisk -rx 'pjsip show transports'
echo
echo "==> Endpoint'и (у zadarma-auth/... має бути ваш SIP-логін, не __ZADARMA_SIP_USER__):"
asterisk -rx 'pjsip show endpoints'
echo
echo "==> Реєстрація на Zadarma (треба 'Registered'):"
asterisk -rx 'pjsip show registrations'

echo
echo "==================================================================="
echo "Якщо у транспортах НЕМАЄ transport-tls — подивіться логи:"
echo "  journalctl -u asterisk -b --no-pager | tail -120"
echo
echo "Якщо реєстрація 'Rejected' — перевірте:"
echo "  - правильний SIP-логін і SIP-пароль (не API-ключ!) у credentials.env"
echo "  - у кабінеті Zadarma -> SIP -> Безпека додайте IP $PUBLIC_IP"
echo "==================================================================="
