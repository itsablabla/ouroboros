---
name: a2a
title: A2A Bridge
description: Serves this Ouroboros as an Agent2Agent-protocol endpoint — a public agent card plus a token-gated message/send route, so agent networks (Hybro and compatible) can discover it and delegate work.
version: 0.1.0
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
os: any
permissions: [net, route, inject_chat, read_settings]
env_from_settings: [A2A_PEER_TOKEN, A2A_PUBLIC_BASE_URL, A2A_AGENT_NAME, A2A_AGENT_DESCRIPTION]
when_to_use: The owner wants this Ouroboros to join an A2A agent network (Hybro or compatible) so peer agents and orchestrators can delegate tasks to it over the open protocol.
model_experience:
  what_model_sees: No new tools. A2A peer messages arrive as ordinary user turns in synthetic A2A chats (one chat per peer conversation context); the reply travels back to the peer as the protocol response.
  token_effect: Near-zero while idle — each A2A call is one normal user turn.
timeout_sec: 120
---

# A2A Bridge

Exposes this Ouroboros instance as an [Agent2Agent (A2A)](https://a2a-protocol.org/)
server so agent networks such as Hybro can register and call it.

Two routes are mounted under `/api/extensions/a2a/` (this namespace is the one
public prefix in the network-password gate; see `server_auth`):

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `.well-known/agent-card.json` | GET | none (public by A2A convention) | Agent discovery card |
| `rpc` | POST | `Authorization: Bearer <A2A_PEER_TOKEN>` | A2A JSON-RPC (`message/send`) |

## Setup

1. In Settings → Secrets add `A2A_PEER_TOKEN` (a long random string) and grant it
   to this skill. Every caller must present it as a bearer token.
2. Set `A2A_PUBLIC_BASE_URL` to this instance's public base URL plus the skill
   prefix, e.g. `https://jada.example.com/api/extensions/a2a`. The advertised
   card URL becomes `<base>/rpc`.
3. Optional: `A2A_AGENT_NAME`, `A2A_AGENT_DESCRIPTION` for the card identity.
4. Enable the skill, then register `<base>` (without `/rpc`) with the network —
   e.g. Hybro → Network → Add agent.

## Semantics

- Each A2A `contextId` maps to a dedicated synthetic A2A chat (allocated via the
  Host Service `a2a` range), so peer conversations have single-conversation
  semantics and stay out of human-facing history.
- The peer's `messageId` is used as the host's `client_message_id`: a retried
  delivery REJOINS the in-flight or settled operation instead of running twice.
- `wait_for_response` injects make the call synchronous: the POST returns when
  Ouroboros has replied, up to the configured timeout (default 600s).
- A call that outlives the timeout returns an explicit `outcome unknown` error —
  never a fabricated reply.
- Streaming (`message/stream`) and task push notifications are not served; the
  card advertises `streaming: false`, `pushNotifications: false`.
