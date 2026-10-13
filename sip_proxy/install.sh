#!/usr/bin/env bash
#
#  Інсталяція SIP-проксі (Asterisk) для маршруту:
#  ElevenLabs -> Asterisk (TLS) -> Zadarma (UDP)
#
#  Запускати на VPS під root:
#    sudo bash install.sh
#

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Запустіть скрипт від root: sudo bash install.sh"
  exit 1
fi

CONFIG_DIR="$(cd "$(dirname "$0")" && pwd)"
DOMAIN="bot-telegram.ai-agent.website"

echo "==> Встановлюю Asterisk..."
apt update
DEBIAN_FRONTEND=noninteractive apt install -y \
  asterisk \
  asterisk-modules \
  asterisk-config \
  ufw

echo "==> Перевіряю Let's Encrypt cert для $DOMAIN..."
if [[ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
  echo "❌ Не знайдено сертифікат /etc/letsencrypt/live/$DOMAIN/fullchain.pem"
  echo "   Спочатку випустіть його через certbot, потім перезапустіть скрипт."
  exit 1
fi

echo "==> Даю Asterisk доступ до Let's Encrypt cert..."
groupadd -f ssl-cert
usermod -a -G ssl-cert asterisk
chgrp -R ssl-cert /etc/letsencrypt/live /etc/letsencrypt/archive
chmod -R g+rX /etc/letsencrypt/live /etc/letsencrypt/archive
chmod g+rx /etc/letsencrypt

# Renew-hook: після оновлення cert перезавантажити pjsip
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/asterisk-reload.sh <<'HOOK'
#!/usr/bin/env bash
chgrp -R ssl-cert /etc/letsencrypt/live /etc/letsencrypt/archive 2>/dev/null || true
chmod -R g+rX /etc/letsencrypt/live /etc/letsencrypt/archive 2>/dev/null || true
asterisk -rx 'pjsip reload' 2>/dev/null || true
HOOK
chmod +x /etc/letsencrypt/renewal-hooks/deploy/asterisk-reload.sh

echo "==> Бекап існуючих конфігів Asterisk..."
TS=$(date +%Y%m%d_%H%M%S)
mkdir -p "/etc/asterisk/backup_$TS"
for f in pjsip.conf extensions.conf rtp.conf logger.conf asterisk.conf; do
  if [[ -f "/etc/asterisk/$f" ]]; then
    cp "/etc/asterisk/$f" "/etc/asterisk/backup_$TS/$f"
  fi
done

echo "==> Копіюю нові конфіги..."
cp "$CONFIG_DIR/pjsip.conf"      /etc/asterisk/pjsip.conf
cp "$CONFIG_DIR/extensions.conf" /etc/asterisk/extensions.conf
cp "$CONFIG_DIR/rtp.conf"        /etc/asterisk/rtp.conf
cp "$CONFIG_DIR/logger.conf"     /etc/asterisk/logger.conf
cp "$CONFIG_DIR/asterisk.conf"   /etc/asterisk/asterisk.conf

chown -R asterisk:asterisk /etc/asterisk

echo "==> Налаштовую UFW..."
ufw allow 5061/tcp comment "SIP TLS (ElevenLabs)" || true
ufw allow 5060/udp comment "SIP UDP (Zadarma)" || true
ufw allow 10000:20000/udp comment "RTP" || true

echo "==> Вмикаю та перезапускаю Asterisk..."
systemctl enable asterisk
systemctl restart asterisk

sleep 3

echo
echo "==> Стан Asterisk:"
systemctl status asterisk --no-pager -l | head -n 20 || true
echo
echo "==> Транспорти:"
asterisk -rx 'pjsip show transports' || true
echo
echo "==> Endpoint'и:"
asterisk -rx 'pjsip show endpoints' || true
echo
echo "==> Реєстрація на Zadarma:"
asterisk -rx 'pjsip show registrations' || true
echo
echo "==================================================================="
echo "✅ Установка завершена."
echo
echo "ВАЖЛИВО: відредагуйте /etc/asterisk/pjsip.conf і замініть:"
echo "  __PUBLIC_IP__                  -> публічний IP вашого VPS"
echo "  __ELEVENLABS_TRUNK_PASSWORD__  -> пароль для ElevenLabs SIP trunk"
echo "  __ZADARMA_SIP_USER__           -> логін Zadarma SIP (напр. 558542-100)"
echo "  __ZADARMA_SIP_PASSWORD__       -> пароль Zadarma SIP"
echo
echo "А також /etc/asterisk/extensions.conf:"
echo "  __ZADARMA_CALLERID__           -> ваш номер у Zadarma (напр. 380931775053)"
echo
echo "Після правок:"
echo "  asterisk -rx 'pjsip reload'"
echo "  asterisk -rx 'dialplan reload'"
echo "==================================================================="
