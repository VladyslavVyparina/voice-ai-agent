# SIP-проксі: ElevenLabs ↔ Asterisk ↔ Zadarma

Asterisk на вашому VPS працює як посередник між ElevenLabs (TLS/SRTP, кодек Opus/PCMA) і Zadarma (UDP, кодек PCMA/PCMU).

```
ElevenLabs ──TLS:5061──► Asterisk на VPS ──UDP:5060──► Zadarma ──► оператори (Київстар, Vodafone, Lifecell)
                        bot-telegram.ai-agent.website                pbx.zadarma.com
```

## Що робить проксі

1. Приймає виклики від ElevenLabs по TLS:5061 (TLS-сертифікат береться з Let's Encrypt).
2. Перекодовує медіа з SRTP/Opus у звичайний RTP/PCMA — формат, який стабільно приймає Zadarma.
3. Нормалізує номер: `+380XXXXXXXXX` / `380XXXXXXXXX` / `0XXXXXXXXX` → `0XXXXXXXXX`.
4. Підставляє коректний `CallerID` (ваш Zadarma-номер) — без нього Zadarma часто блокує дзвінки на Lifecell/Vodafone.
5. Реєструється на Zadarma як SIP-клієнт і відправляє виклик через зареєстрований trunk (стабільніше, ніж anonymous).

> ⚠️ Проксі **не зможе** виправити проблему, якщо її коренем є **маршрутизація Zadarma до конкретного оператора** (зокрема Lifecell). У такому випадку треба паралельно тримати другого SIP-провайдера (наприклад, Binotel/Stream Telecom/Sippy) і робити failover у dialplan. Як це додати — описано в кінці.

## Чому при прямому ElevenLabs → Zadarma з’являється «такі дзвінки можливі тільки всередині країни»

Підтримка Zadarma пояснює це так: **національний** український номер розрахований на обслуговування **в межах країни**. Коли ElevenLabs підключається до Zadarma **напряму**, сигналізація та/або медіа йдуть з інфраструктури ElevenLabs (часто **США або інша країна**). Zadarma бачить **закордонне походження** виклику на лінію з типом «національний» і відхиляє його відповідним повідомленням.

**Як проксі Asterisk це змінює**

Тут два окремі «ноги» виклику:

1. **ElevenLabs → ваш Asterisk** — зовнішня сесія лише до вашого домену (TLS). Це не той маршрут, який Zadarma оцінює як «національний вихід у мережу».
2. **Asterisk → Zadarma** — Asterisk як зареєстрований SIP-клієнт відправляє вже **новий** вихідний виклик на `pbx.zadarma.com`. Цей трафік має виходити з **публічної IP-адреси вашого VPS**. Якщо VPS фізично в **Україні** (або IP явно дозволений Zadarma як домашній для вашого акаунту — уточніть у підтримки), для Zadarma це виглядає як **звичайний внутрішній** SIP-вихід національного номера, а не як міжнародний «привіз» з США.

**Важливо:** якщо Asterisk стоїть на VPS у **Німеччині, Нідерландах, США** тощо, помилка про «лише в межах країни» може **зберегтися**, бо друга нога все одно йде «не з України». У такому випадку потрібен **український** VPS або інший продукт Zadarma/інший провайдер без такого обмеження.

Після налаштування в ElevenLabs у полі **Address** має бути **ваш проксі** (`bot-telegram.ai-agent.website`), а **не** `sip.zadarma.com` — інакше ви знову на прямому шляху ElevenLabs → Zadarma.

## Передумови

- Ubuntu 20.04 / 22.04 / 24.04 на VPS.
- Для національного номера Zadarma: **VPS з Asterisk має бути в Україні** (або IP дозволений Zadarma як домашній для вашого SIP — інакше помилка «лише в межах країни» може не зникнути).
- Домен `bot-telegram.ai-agent.website` указує на IP цього VPS (A-запис).
- Уже існує сертифікат Let's Encrypt: `/etc/letsencrypt/live/bot-telegram.ai-agent.website/`.
- У Zadarma в кабінеті дозволені вихідні дзвінки і відомі **SIP-логін/пароль** (не той, що для входу в особистий кабінет).

## Деплой за 4 кроки

### 1. Залити папку `sip_proxy/` на VPS

З локальної машини:

```bash
scp -r /Users/macbookpro/Desktop/bot_ai-agent/sip_proxy root@bot-telegram.ai-agent.website:/root/
```

### 2. Установити Asterisk

На VPS:

```bash
cd /root/sip_proxy
sudo bash install.sh
```

Скрипт:
- встановить `asterisk` та `asterisk-modules`,
- видасть Asterisk доступ до Let's Encrypt (через групу `ssl-cert`),
- підкладе наші конфіги в `/etc/asterisk/`,
- відкриє у фаєрволі порти `5060/udp`, `5061/tcp`, `10000-20000/udp`,
- запустить сервіс.

### 3. Заповнити креденшли

```bash
sudo bash configure.sh
```

Скрипт інтерактивно запитає:
- публічний IP VPS (визначається автоматично — підтвердіть Enter),
- Zadarma SIP-логін (формат `558542-100`) і пароль,
- ваш Zadarma номер для CallerID (наприклад `380931775053`),
- пароль, який ElevenLabs використовуватиме для авторизації на нашому проксі (придумайте довгий, скопіюйте — потрібен у наступному кроці).

Після завершення скрипт виведе стан реєстрації. Має бути `Registered` напроти `zadarma-registration`.

### 4. Переключити ElevenLabs на новий trunk

У ElevenLabs Dashboard → **Phone Numbers** → ваш SIP trunk номер → **Outbound Configuration**:

| Поле | Значення |
|---|---|
| **Address** | `bot-telegram.ai-agent.website` |
| **Port** | `5061` |
| **Transport** | `TLS` |
| **Username** | `elevenlabs` |
| **Password** | (пароль, який ви ввели на кроці 3) |
| **Outbound number** | ваш Zadarma номер у E.164, напр. `+380931775053` |
| **Media encryption** | `Allowed` (SRTP) |

Збережіть. Виклики тепер ідуть через ваш VPS.

## Перевірка

### А. Реєстрація Zadarma

```bash
sudo asterisk -rx 'pjsip show registrations'
```

Має бути `Registered`. Якщо `Rejected` — невірний логін/пароль або IP VPS не в whitelist у Zadarma.

### Б. Live-логи під час дзвінка

```bash
sudo asterisk -rvvvvv
```

У сусідньому вікні запустіть тестовий виклик через CRM. У логах має бути:

```
== IN ELEVENLABS +380: +380931775053
== Dialing Zadarma: 0931775053
== Connected to PJSIP/0931775053-...
```

Якщо бачите `403 Forbidden` від Zadarma — неправильний CallerID або обмеження в Zadarma.
Якщо `486 Busy` / `480 Temporarily Unavailable` — проблема в маршрутизації оператора (див. наступний розділ).

### В. SIP-трейс

```bash
sudo asterisk -rx 'pjsip set logger on'
sudo tail -f /var/log/asterisk/full
```

Усі SIP-пакети йтимуть у лог. Корисно для діалогу з підтримкою Zadarma/ElevenLabs.

## Якщо Lifecell все одно не дзвонить

Це означає, що Zadarma не маршрутизує дзвінки на цього оператора з вашого номера. Варіанти:

1. **Контакт із Zadarma-підтримкою** — надайте `Call-ID` з логів і час; нехай вони перевірять маршрут.
2. **Додати другий SIP-trunk** (Binotel / Stream / Sippy) і зробити failover. У `pjsip.conf` додаєте другий endpoint `binotel`, у `extensions.conf`:

   ```
   exten => _X.,1,Dial(PJSIP/${EXTEN}@zadarma,30)
    same => n,GotoIf($["${DIALSTATUS}" = "ANSWER"]?hangup)
    same => n,Dial(PJSIP/${EXTEN}@binotel,30)
    same => n(hangup),Hangup()
   ```

   Asterisk спочатку пробує Zadarma, якщо за 30 с немає відповіді — пробує Binotel.

3. **Винести ElevenLabs на іншого SIP-провайдера** з прямою маршрутизацією на всі українські оператори (наприклад, Stream Telecom з мобільним номером).

## Структура файлів

```
sip_proxy/
├── README.md            ← цей файл
├── install.sh           ← інсталяція Asterisk + копіювання конфігів
├── configure.sh         ← заповнення креденшлів і релоад
├── pjsip.conf           ← транспорти + endpoint elevenlabs + endpoint zadarma
├── extensions.conf      ← діалплан з нормалізацією номерів
├── rtp.conf             ← порти RTP 10000-20000
├── logger.conf          ← рівні логів
└── asterisk.conf        ← runuser/rungroup
```

## Якщо немає `transport-tls` (лише UDP)

1. Переконайся, що в `/etc/asterisk/pjsip.conf` **немає** рядків з `__PUBLIC_IP__` — це невалідна адреса, TLS-транспорт може не створитися. Запусти `configure.sh` до кінця або заміни плейсхолдери вручну на `173.242.58.156` (свій IP).
2. Перезапуск: `sudo systemctl restart asterisk`, потім `sudo asterisk -rx 'pjsip show transports'`.
3. Перевір модуль TLS: `ls /usr/lib/asterisk/modules/res_pjsip_transport_tls.so` і `sudo asterisk -rx 'module show like transport_tls'`.
4. Логи: `sudo journalctl -u asterisk -b --no-pager | tail -120` — шукай рядки про TLS/cert.

## Якщо Zadarma показує `Rejected`

У виводі `pjsip show endpoints` не має бути літералу `__ZADARMA_SIP_USER__` — якщо він є, **configure не завершив заміну**. Запусти `sudo bash configure.sh` ще раз: на питання про IP натисни лише **Enter** (не вставляй команди). Перевір реальний SIP-логін і пароль у кабінеті Zadarma; за потреби додай IP VPS у whitelist SIP.

## Корисні команди

```bash
# рестарт Asterisk
sudo systemctl restart asterisk

# тільки релоад конфігів (без розриву викликів)
sudo asterisk -rx 'pjsip reload'
sudo asterisk -rx 'dialplan reload'

# перегляд активних викликів
sudo asterisk -rx 'core show channels'

# вимкнути SIP-логер після діагностики
sudo asterisk -rx 'pjsip set logger off'
```
