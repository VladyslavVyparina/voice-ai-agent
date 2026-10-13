# SIP proxy: ElevenLabs, Asterisk, Zadarma

Asterisk on your VPS sits between ElevenLabs (TLS and SRTP, Opus or PCMA) and Zadarma (UDP, PCMA or PCMU).

```
ElevenLabs --TLS:5061--> Asterisk on the VPS --UDP:5060--> Zadarma --> the mobile networks
```

The ElevenLabs address in this project is `bot-telegram.ai-agent.website`. Zadarma’s PBX is `pbx.zadarma.com`.

## What the proxy does

1. Accepts calls from ElevenLabs on TLS port 5061. The certificate is Let’s Encrypt.
2. Converts SRTP/Opus to ordinary RTP/PCMA, which Zadarma accepts reliably.
3. Normalizes the number: `+380XXXXXXXXX`, `380XXXXXXXXX`, or `0XXXXXXXXX` becomes `0XXXXXXXXX`.
4. Sets a real CallerID (your Zadarma number). Without it, Zadarma often blocks calls to some Ukrainian mobile networks.
5. Registers on Zadarma as a SIP client and sends the call through that trunk. That is more stable than an anonymous invite.

The proxy cannot fix a route that Zadarma itself will not complete to one operator. In that case keep a second SIP provider and fail over in the dialplan. The last section shows the shape of that dialplan.

## Why a direct ElevenLabs to Zadarma call is rejected as “in-country only”

Zadarma support describes a national Ukrainian number as a number that is served inside the country. When ElevenLabs connects straight to Zadarma, signaling or media often comes from ElevenLabs infrastructure in another country. Zadarma sees a foreign origin on a national line and rejects it.

The proxy splits the call into two legs:

1. ElevenLabs to your Asterisk is only a TLS session to your domain. Zadarma does not treat that leg as the national exit.
2. Asterisk to Zadarma is a new outbound call from a registered SIP client to `pbx.zadarma.com`. That traffic leaves from the public IP of your VPS. If that VPS is in Ukraine, or Zadarma has allowed that IP as the home address for the account, Zadarma sees a normal national exit.

If Asterisk runs in another country, the “in-country only” error can remain, because the second leg is still not from Ukraine. Then you need a Ukrainian VPS, or a Zadarma product or another provider without that limit.

In ElevenLabs the Address field must be your proxy, not `sip.zadarma.com`. Otherwise you are back on the direct path.

## Before you start

- Ubuntu 20.04, 22.04, or 24.04 on the VPS.
- For a national Zadarma number, the Asterisk VPS should be in Ukraine, or the IP must be allowed by Zadarma as the home SIP address.
- The domain points at this VPS with an A record.
- A Let’s Encrypt certificate already exists for that domain.
- Outbound calls are enabled in the Zadarma cabinet, and you have the SIP login and password. That password is not the password for the web cabinet.

## Deploy

### 1. Copy `sip_proxy/` to the VPS

```bash
scp -r sip_proxy root@your-vps:/root/
```

### 2. Install Asterisk

```bash
cd /root/sip_proxy
sudo bash install.sh
```

The script installs Asterisk and its modules, grants Asterisk access to the Let’s Encrypt files through the `ssl-cert` group, copies the configs into `/etc/asterisk/`, opens `5060/udp`, `5061/tcp`, and `10000-20000/udp`, and starts the service.

### 3. Fill credentials

```bash
sudo bash configure.sh
```

It asks for the public IP (detected automatically; confirm with Enter), the Zadarma SIP login (shape `558542-100`) and password, the CallerID number, and a long password ElevenLabs will use to authenticate to this proxy. Copy that password. You need it in the next step.

When it finishes, `zadarma-registration` should say `Registered`.

### 4. Point ElevenLabs at the new trunk

ElevenLabs Dashboard, Phone Numbers, your SIP trunk, Outbound configuration:

| Field | Value |
|---|---|
| Address | your proxy domain |
| Port | `5061` |
| Transport | `TLS` |
| Username | `elevenlabs` |
| Password | the password from step 3 |
| Outbound number | your Zadarma number in E.164 |
| Media encryption | `Allowed` (SRTP) |

Save. Calls now go through the VPS.

## Checks

Registration:

```bash
sudo asterisk -rx 'pjsip show registrations'
```

`Registered` is the healthy state. `Rejected` means a wrong login or password, or the VPS IP is not on the Zadarma allow list.

Live log during a test call from the CRM:

```bash
sudo asterisk -rvvvvv
```

You want lines that show the call entering from ElevenLabs, the dial to Zadarma, and a connected PJSIP channel. `403 Forbidden` from Zadarma is a bad CallerID or a cabinet restriction. `486 Busy` or `480 Temporarily Unavailable` is operator routing, covered next.

SIP trace:

```bash
sudo asterisk -rx 'pjsip set logger on'
sudo tail -f /var/log/asterisk/full
```

Turn the logger off when you are done: `sudo asterisk -rx 'pjsip set logger off'`.

## If one mobile network still does not connect

Zadarma is not routing that operator from your number.

1. Send Zadarma support the `Call-ID` and the time from the log.
2. Add a second trunk and fail over. In `extensions.conf`:

```
exten => _X.,1,Dial(PJSIP/${EXTEN}@zadarma,30)
 same => n,GotoIf($["${DIALSTATUS}" = "ANSWER"]?hangup)
 same => n,Dial(PJSIP/${EXTEN}@second-provider,30)
 same => n(hangup),Hangup()
```

Asterisk tries Zadarma, then the second provider if nobody answers in 30 seconds. The second endpoint has to exist in `pjsip.conf`.

3. Or move ElevenLabs to a provider that already routes to every Ukrainian mobile network.

## Files

```
sip_proxy/
├── README.md
├── install.sh
├── configure.sh
├── pjsip.conf          transports, elevenlabs endpoint, zadarma endpoint
├── extensions.conf     dialplan and number normalization
├── rtp.conf            RTP ports 10000-20000
├── logger.conf
└── asterisk.conf
```

## If `transport-tls` is missing and only UDP exists

1. `/etc/asterisk/pjsip.conf` must not still contain `__PUBLIC_IP__`. That is not a valid address, and the TLS transport may never be created. Finish `configure.sh`, or replace the placeholders with the VPS public IP.
2. `sudo systemctl restart asterisk`, then `sudo asterisk -rx 'pjsip show transports'`.
3. Check the module: `ls /usr/lib/asterisk/modules/res_pjsip_transport_tls.so` and `sudo asterisk -rx 'module show like transport_tls'`.
4. `sudo journalctl -u asterisk -b --no-pager | tail -120` and look for TLS or certificate lines.

## If Zadarma shows `Rejected`

`pjsip show endpoints` must not still show the literal `__ZADARMA_SIP_USER__`. If it does, `configure.sh` did not finish the replacement. Run it again. On the IP question, press Enter only. Check the SIP login and password in the Zadarma cabinet, and add the VPS IP to the SIP allow list if the cabinet has one.

## Useful commands

```bash
sudo systemctl restart asterisk
sudo asterisk -rx 'pjsip reload'
sudo asterisk -rx 'dialplan reload'
sudo asterisk -rx 'core show channels'
sudo asterisk -rx 'pjsip set logger off'
```
