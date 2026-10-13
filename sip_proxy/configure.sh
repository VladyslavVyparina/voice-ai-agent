#!/usr/bin/env bash
#
#  Інтерактивно заповнює плейсхолдери у /etc/asterisk/pjsip.conf
#  і /etc/asterisk/extensions.conf, після чого перезавантажує модулі.
#
#  Запускати на VPS під root ПІСЛЯ install.sh:
#    sudo bash configure.sh
#

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Запустіть скрипт від root: sudo bash configure.sh"
  exit 1
fi

PJ=/etc/asterisk/pjsip.conf
EX=/etc/asterisk/extensions.conf

if [[ ! -f "$PJ" ]] || [[ ! -f "$EX" ]]; then
  echo "❌ Не знайдено $PJ або $EX. Спершу запустіть install.sh."
  exit 1
fi

DEFAULT_IP=$(curl -s -4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

echo "НЕ вставляйте сюди команди — лише IP (або Enter)."
read -r -p "Публічний IP цього VPS [$DEFAULT_IP]: " PUBLIC_IP
PUBLIC_IP=${PUBLIC_IP:-$DEFAULT_IP}
PUBLIC_IP=$(echo "$PUBLIC_IP" | tr -d '[:space:]')

if ! [[ "$PUBLIC_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "⚠️  Невірний формат IP («$PUBLIC_IP»). Використовую за замовчуванням: $DEFAULT_IP"
  PUBLIC_IP="$DEFAULT_IP"
fi

read -r -p "Логін Zadarma SIP (напр. 558542-100): " ZADARMA_USER
read -r -s -p "Пароль Zadarma SIP: " ZADARMA_PASS; echo
read -r -p "CallerID для Zadarma (ваш номер, напр. 380931775053): " ZADARMA_CID
read -r -s -p "Пароль для ElevenLabs trunk (придумайте міцний): " EL_PASS; echo

echo
echo "==> Записую значення у конфіги..."

# Безпечна заміна: екрануємо &, /, \, |
escape_sed() { printf '%s' "$1" | sed -e 's/[\/&|]/\\&/g'; }

P_PUBLIC_IP=$(escape_sed "$PUBLIC_IP")
P_ZUSER=$(escape_sed "$ZADARMA_USER")
P_ZPASS=$(escape_sed "$ZADARMA_PASS")
P_ZCID=$(escape_sed "$ZADARMA_CID")
P_ELPASS=$(escape_sed "$EL_PASS")

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
  echo "❌ У конфігах лишилися плейсхолдери __...__. Перевірте введені дані і запустіть скрипт знову."
  exit 1
fi

chown asterisk:asterisk "$PJ" "$EX"
chmod 640 "$PJ" "$EX"

echo "==> Перезавантажую Asterisk (повний restart — TLS-транспорт частіше з’являється після нього)..."
systemctl restart asterisk
sleep 2
asterisk -rx 'module load res_pjsip_transport_tls.so' 2>/dev/null || true
asterisk -rx 'pjsip reload'
asterisk -rx 'dialplan reload'

sleep 2

echo
echo "==> Транспорти:"
asterisk -rx 'pjsip show transports'
echo
echo "==> Endpoint'и:"
asterisk -rx 'pjsip show endpoints'
echo
echo "==> Реєстрація на Zadarma (має бути 'Registered'):"
asterisk -rx 'pjsip show registrations'

echo
echo "==================================================================="
echo "✅ Готово."
echo
echo "Тепер у ElevenLabs у налаштуваннях SIP Trunk Phone Number:"
echo "  Address      : bot-telegram.ai-agent.website"
echo "  Port         : 5061"
echo "  Transport    : TLS"
echo "  Username     : elevenlabs"
echo "  Password     : (той пароль, який ви щойно ввели)"
echo "  Outbound num : (номер у E.164, напр. +380931775053)"
echo
echo "Лайв-логи Asterisk під час дзвінка:"
echo "  asterisk -rvvvvv"
echo "==================================================================="
