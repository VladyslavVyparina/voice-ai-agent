# Voice AI agent

A small service that pairs a Telegram bot with a SIP proxy so a voice agent can take or place calls, with prompts and a CRM record kept next to the conversation.

## Problem

Chat bots and phone lines were separate. The operator needed one place for the script the agent speaks, the call path, and a note of who the caller was.

## What was built

- `run_bot.py` starts the Telegram bot.
- `run_api.py` starts the HTTP API.
- `agent_prompts/` holds the scripts the agent follows.
- `sip_proxy/` is the telephony side.
- Media files sit beside the bot for prompts that play audio.
- The working copy also had a local CRM database. That file is not in this repository.

## How it works

The bot process and the API process start separately so the chat channel and the call path can be restarted on their own. Prompts are files, not strings buried in handlers, so the script can change without a rewrite of the transport. The SIP proxy is the bridge between the agent and the phone network. Customer records that lived in the local SQLite file stay out of the public tree.

## Stack

Python, Telegram Bot API, SIP, a local CRM store in the private copy.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
python run_api.py
python run_bot.py
```

Fill the example env with your own bot token and SIP credentials.

## What this public copy leaves out

`.env` and `crm.db`.

## Documents

- `sip_proxy/README.md` is the Asterisk bridge: TLS from ElevenLabs, UDP to Zadarma, number normalization, CallerID, and what to do when one mobile network still rejects the call.
- `agent_prompts/valeriy/README.md` splits the voice agent into four files: how he speaks, what he knows, the goal of one call, and the CRM fields written after it. Do not merge those files into one prompt.
