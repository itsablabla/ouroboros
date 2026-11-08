"""A2A Bridge — serve this Ouroboros as an Agent2Agent-protocol endpoint.

Routes (mounted under /api/extensions/a2a/, the one public gate prefix):

    GET  .well-known/agent-card.json   -> agent discovery card (public)
    POST rpc                           -> A2A JSON-RPC message/send (Bearer-gated)

Inbound peer text enters the host as a normal user turn in a dedicated synthetic
A2A chat (one per A2A contextId) via the loopback Host Service, with
wait_for_response so the HTTP call resolves with Ouroboros's actual reply.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import uuid
from typing import Any, Dict, Optional

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

_HOST_PORT_DEFAULT = 8767
_INJECT_TIMEOUT_SEC = 600          # default synchronous wait for the agent's reply
_INJECT_TIMEOUT_CAP = 1700         # host caps wait_for_response at 1800
_MAX_MESSAGE_CHARS = 10_000        # matches models.room.MAX_MESSAGE_LENGTH
_STATE_FILENAME = "a2a_state.json"

_SETTINGS_KEYS = (
    "A2A_PEER_TOKEN",
    "A2A_PUBLIC_BASE_URL",
    "A2A_AGENT_NAME",
    "A2A_AGENT_DESCRIPTION",
)


# --------------------------------------------------------------------------
# settings / state
# --------------------------------------------------------------------------

def _settings(api) -> Dict[str, str]:
    try:
        values = api.get_settings(list(_SETTINGS_KEYS))
    except Exception:
        values = {}
    return {key: str(values.get(key) or "").strip() for key in _SETTINGS_KEYS}


def _public_base(api) -> str:
    return _settings(api)["A2A_PUBLIC_BASE_URL"].rstrip("/")


def _state_path(api) -> pathlib.Path:
    info = api.get_runtime_info()
    state_dir = pathlib.Path(str(info.get("state_dir") or info.get("skill_dir") or "."))
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / _STATE_FILENAME


def _load_state(api) -> Dict[str, Any]:
    try:
        return json.loads(_state_path(api).read_text())
    except Exception:
        return {"contexts": {}}


def _save_state(api, state: Dict[str, Any]) -> None:
    tmp = _state_path(api).with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, _state_path(api))


# --------------------------------------------------------------------------
# host service plumbing (same contract the telegram skill uses)
# --------------------------------------------------------------------------

def _host_port() -> int:
    try:
        port = int(os.environ.get("OUROBOROS_HOST_SERVICE_PORT", str(_HOST_PORT_DEFAULT)))
    except (TypeError, ValueError):
        raise RuntimeError("OUROBOROS_HOST_SERVICE_PORT is invalid")
    if not 1 <= port <= 65535:
        raise RuntimeError("OUROBOROS_HOST_SERVICE_PORT is invalid")
    return port


def _host_headers(api) -> Dict[str, str]:
    return {"X-Skill-Token": api.get_skill_token().use_in_request()}


async def _host_post(api, path: str, payload: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    async with httpx.AsyncClient(timeout=_INJECT_TIMEOUT_CAP + 60, trust_env=False,
                                 follow_redirects=False) as client:
        response = await client.post(
            f"http://127.0.0.1:{_host_port()}{path}",
            headers=_host_headers(api),
            json=payload,
        )
    try:
        body = response.json()
    except Exception:
        body = {}
    return int(response.status_code), (body if isinstance(body, dict) else {})


async def _chat_id_for_context(api, context_id: str) -> int:
    """One synthetic A2A chat per A2A contextId (allocated from the host's a2a range)."""
    state = _load_state(api)
    known = state["contexts"].get(context_id)
    if isinstance(known, int):
        return known
    status, body = await _host_post(api, "/chat/allocate-internal", {"range_name": "a2a"})
    if status != 200 or not isinstance(body.get("chat_id"), int):
        raise RuntimeError(f"chat allocation failed (HTTP {status})")
    chat_id = int(body["chat_id"])
    state["contexts"][context_id] = chat_id
    _save_state(api, state)
    return chat_id


# --------------------------------------------------------------------------
# agent card
# --------------------------------------------------------------------------

def _card(api) -> Dict[str, Any]:
    settings = _settings(api)
    base = _public_base(api)
    return {
        "name": settings["A2A_AGENT_NAME"] or "Ouroboros",
        "description": settings["A2A_AGENT_DESCRIPTION"]
        or "Ouroboros: a general-purpose autonomous AI agent with durable memory, "
           "code and file tools, and long-running task execution.",
        "url": f"{base}/rpc",
        "version": "1.0.0",
        "protocolVersion": "0.3.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [],
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
    }


def _make_card_route(api):
    async def agent_card(_request: Request) -> JSONResponse:
        if not _public_base(api):
            return JSONResponse(
                {"error": "A2A_PUBLIC_BASE_URL is not configured for the a2a skill."},
                status_code=503,
            )
        return JSONResponse(_card(api))

    return agent_card


# --------------------------------------------------------------------------
# JSON-RPC endpoint
# --------------------------------------------------------------------------

def _rpc_error(rpc_id: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _extract_text(message: Dict[str, Any]) -> str:
    parts = message.get("parts") or []
    texts = [str(p.get("text") or "") for p in parts
             if isinstance(p, dict) and p.get("kind") == "text"]
    return "\n".join(t for t in texts if t).strip()


async def _handle_message_send(api, rpc_id: Any, params: Dict[str, Any]) -> JSONResponse:
    message = params.get("message") or {}
    if not isinstance(message, dict):
        return _rpc_error(rpc_id, -32602, "params.message must be an object")
    text = _extract_text(message)
    if not text:
        return _rpc_error(rpc_id, -32602, "message has no text parts")
    if len(text) > _MAX_MESSAGE_CHARS:
        return _rpc_error(rpc_id, -32602, "message exceeds the host's per-message limit")

    context_id = str(message.get("contextId") or params.get("contextId") or "").strip()
    if not context_id:
        context_id = uuid.uuid4().hex
    message_id = str(message.get("messageId") or uuid.uuid4().hex)[:128]

    try:
        chat_id = await _chat_id_for_context(api, context_id)
    except Exception as exc:
        return _rpc_error(rpc_id, -32603, f"chat allocation failed: {exc}", status=502)

    status, body = await _host_post(api, "/chat/inject", {
        "chat_id": chat_id,
        "text": text,
        "sender_label": "a2a-peer",
        "client_message_id": message_id,   # retries rejoin instead of re-running
        "wait_for_response": True,
        "timeout_sec": _INJECT_TIMEOUT_SEC,
    })

    if status == 200 and body.get("ok") and str(body.get("response") or "").strip():
        reply = str(body["response"])
    elif status == 504 or (body.get("status") in {"timeout", "pending"}):
        # Honest "outcome unknown": the turn may still complete inside Ouroboros;
        # the peer can retry with the same messageId to rejoin and collect it.
        return _rpc_error(
            rpc_id, -32000,
            f"outcome unknown: the agent did not answer within {_INJECT_TIMEOUT_SEC}s; "
            "retry with the same messageId to rejoin the in-flight turn",
        )
    elif status == 200 and body.get("ok") and body.get("status") in {"cancelled", "failed"}:
        return _rpc_error(rpc_id, -32000, f"the agent turn ended: {body['status']}")
    else:
        return _rpc_error(rpc_id, -32603,
                          f"host inject failed (HTTP {status}): {str(body)[:200]}",
                          status=502)

    return JSONResponse({
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {
            "kind": "message",
            "role": "agent",
            "messageId": uuid.uuid4().hex,
            "contextId": context_id,
            "parts": [{"kind": "text", "text": reply}],
        },
    })


def _make_rpc_route(api):
    async def rpc(request: Request) -> JSONResponse:
        token = _settings(api)["A2A_PEER_TOKEN"]
        if not token:
            return JSONResponse(
                {"error": "A2A_PEER_TOKEN is not configured for the a2a skill."},
                status_code=503,
            )
        auth = request.headers.get("authorization", "")
        scheme, _, presented = auth.partition(" ")
        if scheme.lower() != "bearer" or not presented or presented != token:
            return JSONResponse({"error": "Unauthorized."}, status_code=401)

        try:
            payload = await request.json()
        except Exception:
            return _rpc_error(None, -32700, "parse error")
        if not isinstance(payload, dict):
            return _rpc_error(None, -32600, "batch requests are not supported")

        rpc_id = payload.get("id")
        method = str(payload.get("method") or "")
        params = payload.get("params") or {}
        if method != "message/send":
            # message/stream, tasks/*, push notifications: not served by design.
            return _rpc_error(rpc_id, -32601, f"method not supported: {method or '(missing)'}")
        if not isinstance(params, dict):
            return _rpc_error(rpc_id, -32602, "params must be an object")
        return await _handle_message_send(api, rpc_id, params)

    return rpc


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def register(api) -> None:
    api.register_route(
        ".well-known/agent-card.json",
        handler=_make_card_route(api),
        methods=("GET",),
    )
    api.register_route("rpc", handler=_make_rpc_route(api), methods=("POST",))
    api.log("info", "a2a bridge registered: card + rpc routes active")
