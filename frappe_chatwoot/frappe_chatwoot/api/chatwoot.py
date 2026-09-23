# Copyright (c) 2026, Hypedrive
# License: MIT
"""
Whitelisted API surface for frappe_chatwoot.

Mirrors the contract shape of crm/api/whatsapp.py (see frappe_whatsapp_pattern.md
research doc, section 3) so a future CRM-side crm/api/chatwoot.py can proxy
these calls the same way CRM proxies frappe_whatsapp's doctype today — except
here there is no local doctype to query with frappe.get_all; every call is a
live Chatwoot API read/write via utils/chatwoot_client.py.

Contact resolution: this Frappe/CRM install has no pre-existing
`chatwoot_contact_id` field synced onto Lead/Deal/Contact (that field exists
in a DIFFERENT integration, the Twenty CRM <-> Chatwoot sync — not this one).
So contact resolution here is live: look up the reference doc's phone/email,
call Chatwoot's /contacts/search, then /contacts/{id}/conversations. This is
the "zero-storage" option the build explicitly preferred over persisting a
mapping doctype, since Chatwoot's search endpoint is fast enough for
interactive (view-time) use and avoids a second place drift can occur.
"""

import json
import os
import re
from urllib.parse import unquote, urlparse

import frappe

from frappe_chatwoot.utils import autoria
from frappe_chatwoot.utils import chatwoot_client as cw

ALLOWED_ROLES = ["System Manager", "Sales Manager", "Sales User"]
MAX_ADJUNTOS = 5


def validate_role():
    """Role gate only — the part of validate_access that does not need a
    reference doc. Split out so the conversation_id-keyed endpoints
    (get_messages/get_new_messages/send_message), which have no reference-doc
    context to check, still enforce the role allowlist when called directly
    via /api/method/ instead of through crm.api.chatwoot's wrapper."""
    user_roles = frappe.get_roles(frappe.session.user)
    if not any(role in user_roles for role in ALLOWED_ROLES) and frappe.session.user != "Administrator":
        frappe.throw("Not permitted to access Chatwoot conversations", frappe.PermissionError)


def validate_access(reference_doctype: str, reference_name: str, permtype: str = "read"):
    """Same shape as crm.api.whatsapp.validate_access: role gate + document-level
    permission check. Raises frappe.PermissionError on failure."""
    if not reference_doctype or not reference_name:
        frappe.throw("reference_doctype and reference_name are required")

    validate_role()

    reference_doc = frappe.get_doc(reference_doctype, reference_name)
    if not reference_doc.has_permission(permtype):
        frappe.throw("Not permitted to access this record", frappe.PermissionError)
    return reference_doc


@frappe.whitelist()
def is_chatwoot_installed() -> bool:
    return frappe.db.exists("DocType", "Chatwoot Settings") is not None


@frappe.whitelist()
def is_chatwoot_enabled() -> bool:
    if not frappe.db.exists("DocType", "Chatwoot Settings"):
        return False
    enabled = frappe.get_cached_value("Chatwoot Settings", "Chatwoot Settings", "enabled")
    base_url = frappe.get_cached_value("Chatwoot Settings", "Chatwoot Settings", "base_url")
    return bool(enabled) and bool(base_url)


def _extract_contact_query(reference_doc) -> list[str]:
    """Pull phone/email candidates off a reference doc, cheapest-first.
    Handles the field-name variance across CRM Lead / CRM Deal / Contact."""
    candidates = []
    for fieldname in ("mobile_no", "phone", "phone_number", "whatsapp_no"):
        val = reference_doc.get(fieldname)
        if val:
            candidates.append(val)
    for fieldname in ("email_id", "email"):
        val = reference_doc.get(fieldname)
        if val:
            candidates.append(val)
    return candidates


@frappe.whitelist()
def get_conversations_for_contact(reference_doctype: str, reference_name: str) -> list[dict]:
    """Resolve live conversations for a Frappe record by searching Chatwoot's
    contact directory by phone/email, then listing that contact's conversations.
    Returns [] gracefully if not installed/enabled/no match — same degrade-soft
    contract as crm.api.whatsapp.get_whatsapp_messages."""
    if not is_chatwoot_enabled():
        return []

    reference_doc = validate_access(reference_doctype, reference_name)

    # CRM Deal special-case, mirroring frappe_whatsapp's own precedent: a Deal
    # typically starts life as a Lead, so early conversation history may only
    # be resolvable via the Deal's linked Lead's contact details.
    query_docs = [reference_doc]
    if reference_doctype == "CRM Deal":
        lead = reference_doc.get("lead")
        if lead and frappe.db.exists("CRM Lead", lead):
            validate_access("CRM Lead", lead)
            query_docs.append(frappe.get_doc("CRM Lead", lead))

    seen_conversation_ids = set()
    results = []
    for doc in query_docs:
        for query in _extract_contact_query(doc):
            try:
                contacts = cw.search_contacts(query)
            except cw.ChatwootAPIError:
                continue
            for contact in contacts:
                try:
                    conversations = cw.get_conversations_for_contact(contact["id"])
                except cw.ChatwootAPIError:
                    continue
                for conv in conversations:
                    if conv["id"] in seen_conversation_ids:
                        continue
                    seen_conversation_ids.add(conv["id"])
                    results.append(conv)

    results.sort(key=lambda c: c.get("last_activity_at") or c.get("timestamp") or 0, reverse=True)
    return results


def _annotate_direction(messages: list[dict]) -> list[dict]:
    # Normalize the numeric message_type into a readable direction, and drop
    # the private/internal-note distinction cleanly for FE consumption.
    # Note: private messages are already stripped by
    # chatwoot_client.list_messages/_drop_private_messages before this point
    # — this is purely a display-shape normalization, not a filtering step.
    type_map = {0: "incoming", 1: "outgoing", 2: "activity", 3: "template"}
    for m in messages:
        m["direction"] = type_map.get(m.get("message_type"), "unknown")
    return messages


def _extract_assignee(meta: dict) -> dict | None:
    """Flatten Chatwoot's `meta.assignee` (User#push_event_data: id, name,
    available_name, avatar_url, type, availability_status, thumbnail) down to
    the {name, avatar} shape CRM's UI actually needs. Only sourced from the
    messages-index `meta` — the conversations-list endpoint renders its
    assignee through a different jbuilder partial with no avatar_url key at
    all, so reading it from there would silently yield no avatar (verified
    against Chatwoot source, see wa-relay's thread.ts handler for the same
    finding). Returns None (not a dict) when unassigned, so callers can rely
    on truthiness rather than checking for an empty name."""
    assignee = (meta or {}).get("assignee") or {}
    name = (assignee.get("name") or "").strip()
    if not name:
        return None
    avatar = (assignee.get("thumbnail") or assignee.get("avatar_url") or "").strip()
    return {"name": name, "avatar": avatar or None}


def _chatwoot_url(conversation_id: int) -> str | None:
    """Deep link to this conversation in the agent's Chatwoot dashboard:
    {base_url}/app/accounts/{account_id}/conversations/{conversation_id}.
    Composed server-side since both halves are server-only Settings values;
    a caller with only a conversation_id could never assemble this
    correctly. None if Chatwoot isn't configured (defensive; callers already
    gate on is_chatwoot_enabled before reaching here in practice)."""
    try:
        settings = frappe.get_single("Chatwoot Settings")
    except Exception:
        return None
    if not settings.base_url or not settings.account_id:
        return None
    return f"{settings.base_url}/app/accounts/{settings.account_id}/conversations/{conversation_id}"


@frappe.whitelist()
def get_messages(conversation_id: int, before: int = None) -> dict:
    """Live message page for one conversation. Enforces the role allowlist, but
    cannot check reference-doc access on its own — a conversation_id alone
    carries no reference-doc context. Callers that HAVE that context must bind
    the conversation to it before calling: crm.api.chatwoot does this in
    _validate_conversation_ownership, which confirms the id belongs to the
    reference doc's own conversations. Use that wrapper from CRM rather than
    calling this directly."""
    validate_role()
    if not is_chatwoot_enabled():
        frappe.throw("Chatwoot integration is not enabled")
    conversation_id = frappe.utils.cint(conversation_id)
    raw = cw.list_messages(conversation_id, before=frappe.utils.cint(before) if before else None)
    messages = _annotate_direction(raw.get("payload") or [])
    meta = raw.get("meta") or {}

    result = {
        "meta": meta,
        "messages": messages,
        "chatwoot_url": _chatwoot_url(conversation_id),
    }
    assignee = _extract_assignee(meta)
    if assignee:
        result["assignee"] = assignee
    return result


@frappe.whitelist()
def get_new_messages(conversation_id: int, since_id: int = None) -> dict:
    """Incremental poll: every message newer than `since_id`, using the
    bounded after=-cursor drain loop in chatwoot_client.list_messages_incremental
    (see that function's docstring for the pagination-loss fix this closes).
    Callers should persist `max_id_seen` and pass it back as `since_id` on
    the next poll. `truncated=True` means the drain hit its page/time bound
    with more data potentially still pending — call again immediately (or on
    the very next poll tick) rather than treating this as caught-up.

    Role-gated only — see get_messages' docstring on why reference-doc access
    must be bound by the caller (crm.api.chatwoot._validate_conversation_ownership)."""
    validate_role()
    if not is_chatwoot_enabled():
        frappe.throw("Chatwoot integration is not enabled")
    conversation_id = frappe.utils.cint(conversation_id)
    since_id = frappe.utils.cint(since_id) if since_id else None
    result = cw.list_messages_incremental(conversation_id, since_id=since_id)
    result["messages"] = _annotate_direction(result.get("messages") or [])
    return result


@frappe.whitelist()
def send_message(conversation_id: int, content: str, inbox_id: int = None,
                 adjuntos=None) -> dict:
    """Role-gated only — see get_messages' docstring on why reference-doc access
    must be bound by the caller (crm.api.chatwoot._validate_conversation_ownership).

    lavendi.mx: un envío manual desde el CRM **NO** pausa al agente (decisión de
    Alejandro, 2026-09-15). Antes sí lo pausaba, y esa pausa era dura — el bot
    quedaba mudo hasta que alguien apretara "Reanudar". Ahora el mensaje se marca
    como humano (`content_attributes.humano`), el agente lo toma como contexto y
    aplica su propio freno de tiempo (`humanoRecienteEn`, 90 min) que sí se vence
    solo. Si se quiere silencio definitivo, está el botón "Pausar agente".
    `inbox_id` se mantiene por compatibilidad del frontend; ya no se usa.

    `adjuntos` (opcional): lista JSON de `{url, nombre, mime}` — archivos ya subidos
    al sitio por el composer. Se mandan como media nativa (multipart), no como URL
    pegada al texto; sin esto la imagen llegaría como enlace."""
    validate_role()
    if not is_chatwoot_enabled():
        frappe.throw("Chatwoot integration is not enabled")

    content = (content or "").strip()
    if isinstance(adjuntos, str):
        try:
            adjuntos = json.loads(adjuntos or "[]")
        except ValueError:
            adjuntos = []
    adjuntos = adjuntos or []
    if len(adjuntos) > MAX_ADJUNTOS:
        frappe.throw(f"Máximo {MAX_ADJUNTOS} adjuntos por mensaje")

    if not content and not adjuntos:
        frappe.throw("Message content cannot be empty")

    conversation_id = frappe.utils.cint(conversation_id)
    # Quién manda, no solo "un humano": la burbuja del hilo muestra las iniciales
    # del usuario de la sesión. Ver utils/autoria.py — `sender` de Chatwoot no
    # sirve para esto (todo saliente viaja con el dueño del token de la API).
    marca = autoria.marca_humana()

    if adjuntos:
        archivos = []
        for a in adjuntos:
            leido = cw.leer_adjunto(a.get("url") or a.get("archivo"))
            if leido:
                archivos.append(leido)
        if archivos:
            return cw.create_message_with_attachments(
                conversation_id, content, archivos=archivos, content_attributes=marca)
        if not content:
            frappe.throw("No se pudieron leer los adjuntos")

    return cw.create_message(conversation_id, content, content_attributes=marca)


# Firma que el bridge de Evolution antepone al `content` de los mensajes entrantes:
# `**+52 19932344476 - Zaira Jimenez:**`. Al reenviar a OTRO contacto esa firma
# revelaría el teléfono y el nombre del contacto original — justo el dato que la
# confirmación de destinatario del frontend cuida. El patrón es deliberadamente
# estricto (teléfono + " - " + nombre) para no mutilar un mensaje que empiece con
# texto en negritas escrito por una persona (`**Importante:** …`).
_FIRMA_BRIDGE = re.compile(r"^\*\*\s*\+?\d[\d\s\-]{7,}\s*-\s*[^*\n]+:\*\*\s*\n+")

_EXT_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    "gif": "image/gif", "pdf": "application/pdf", "mp4": "video/mp4", "mp3": "audio/mpeg",
    "ogg": "audio/ogg", "wav": "audio/wav", "webm": "video/webm",
}


def _quitar_firma_bridge(content: str) -> str:
    """Quita la firma del bridge al inicio del `content`, si está. Ver _FIRMA_BRIDGE."""
    return _FIRMA_BRIDGE.sub("", content or "", count=1)


def _ruta_adjunto(data_url: str) -> str | None:
    """Ruta interna (`/rails/active_storage/...`) a partir del `data_url` de Chatwoot.
    `fetch_attachment` solo acepta path — nunca URL completa — para no volverse un SSRF."""
    if not data_url:
        return None
    try:
        return urlparse(data_url).path or None
    except ValueError:
        return None


def _nombre_adjunto(adjunto: dict, ruta: str) -> str:
    """Nombre del archivo para el multipart. Chatwoot no guarda el nombre original en
    el objeto del adjunto: se recupera del final de la URL y, si no trae, se arma con
    la extensión que sí trae el adjunto."""
    base = os.path.basename(unquote(ruta or ""))
    if base:
        return base
    ext = (adjunto.get("extension") or "").lower()
    return f"archivo.{ext}" if ext else "adjunto"


@frappe.whitelist()
def reenviar_mensaje(message_id, conversation_id, conversation_id_destino) -> dict:
    """Reenvía a OTRA conversación el texto y los adjuntos de un mensaje.

    El mensaje origen se lee de Chatwoot, no se acepta su contenido desde el
    cliente: la fuente de verdad es el servidor. Chatwoot no expone un GET de un
    mensaje puntual, pero sí pagina por id — `list_messages(before=message_id+1)`
    devuelve la página cuyo último elemento (id más alto ≤ message_id) es justo ese
    mensaje, en una sola llamada.

    Los adjuntos NO se mandan como link: se baja el binario con
    `chatwoot_client.fetch_attachment` y se resube como media nativa (multipart),
    mismo criterio que `send_message` — con la URL pegada al texto el destinatario
    vería un enlace, no la imagen.

    Role-gated only, igual que send_message: un conversation_id no lleva contexto de
    documento de referencia, así que el binding al CRM lo hace el wrapper del SPA.

    `conversation_id_destino` es un conversation_id de Chatwoot (no un contacto): la
    búsqueda del destinatario en el frontend reusa `search_conversations`, que ya
    viene limitado al inbox de este sitio.
    """
    validate_role()
    if not is_chatwoot_enabled():
        frappe.throw("Chatwoot integration is not enabled")

    msg_id = frappe.utils.cint(message_id)
    origen_id = frappe.utils.cint(conversation_id)
    destino_id = frappe.utils.cint(conversation_id_destino)
    if not msg_id or not destino_id:
        frappe.throw("Falta el mensaje o la conversación destino")
    if origen_id and origen_id == destino_id:
        frappe.throw("El mensaje ya está en esa conversación")

    try:
        pagina = cw.list_messages(origen_id, before=msg_id + 1)
    except cw.ChatwootAPIError as exc:
        frappe.throw(f"No se pudo leer la conversación origen: {exc}")

    origen = next((m for m in (pagina.get("payload") or []) if m.get("id") == msg_id), None)
    if not origen:
        frappe.throw("No se encontró el mensaje a reenviar")

    content = _quitar_firma_bridge((origen.get("content") or "").strip())
    adjuntos = origen.get("attachments") or []
    if len(adjuntos) > MAX_ADJUNTOS:
        frappe.throw(f"El mensaje tiene más de {MAX_ADJUNTOS} adjuntos y no se puede reenviar completo")
    if not content and not adjuntos:
        frappe.throw("El mensaje no tiene contenido que reenviar")

    archivos = []
    for a in adjuntos:
        ruta = _ruta_adjunto(a.get("data_url"))
        if not ruta:
            continue
        try:
            datos, content_type = cw.fetch_attachment(ruta)
        except cw.ChatwootAPIError:
            continue
        mime = content_type or a.get("content_type") or _EXT_MIME.get((a.get("extension") or "").lower())
        archivos.append((_nombre_adjunto(a, ruta), datos, mime))

    # El origen tenía adjuntos y no se pudo bajar ninguno: NO reenviar solo el texto
    # a medias — el destinatario recibiría un mensaje incompleto sin saberlo.
    if adjuntos and not archivos:
        frappe.throw("No se pudieron descargar los adjuntos del mensaje")

    marca = autoria.marca_humana()
    if archivos:
        creado = cw.create_message_with_attachments(
            destino_id, content, archivos=archivos, content_attributes=marca)
    else:
        creado = cw.create_message(destino_id, content, content_attributes=marca)

    return {
        "ok": True,
        "message_id": creado.get("id"),
        "send_failed": bool(creado.get("send_failed")),
        "send_error": creado.get("send_error"),
    }


def _pause_conversation(conversation_id: int, inbox_id: int = None) -> None:
    if frappe.db.exists("Chatwoot Pausa", {"conversation_id": conversation_id}):
        return
    if inbox_id is None:
        try:
            inbox_id = cw.get_conversation(conversation_id).get("inbox_id")
        except cw.ChatwootAPIError:
            inbox_id = None
    frappe.get_doc({
        "doctype": "Chatwoot Pausa",
        "conversation_id": conversation_id,
        "inbox_id": str(inbox_id) if inbox_id else "",
        "paused_by": frappe.session.user,
        "paused_at": frappe.utils.now_datetime(),
    }).insert(ignore_permissions=True)


@frappe.whitelist()
def toggle_status(conversation_id: int, status: str) -> dict:
    """Resolve/reopen a conversation via Chatwoot's native toggle_status
    endpoint. Role-gated only — see get_messages' docstring on why
    reference-doc access must be bound by the caller
    (crm.api.chatwoot._validate_conversation_ownership)."""
    validate_role()
    if not is_chatwoot_enabled():
        frappe.throw("Chatwoot integration is not enabled")
    if status not in ("open", "resolved"):
        frappe.throw("status must be 'open' or 'resolved'")
    conversation_id = frappe.utils.cint(conversation_id)
    cw.toggle_conversation_status(conversation_id, status)
    # Chatwoot's toggle_status response shape isn't guaranteed stable enough
    # to trust for the new status — read the conversation back so the caller
    # gets the authoritative post-toggle state rather than an echo of what it
    # asked for (which could be wrong if the toggle silently no-oped, e.g.
    # already-resolved).
    conversation = cw.get_conversation(conversation_id)
    return {"status": conversation.get("status") or status}


@frappe.whitelist()
def get_canned_responses() -> list[dict]:
    """Role-gated only, same as send_message — canned responses are an
    account-level list, not bound to any specific reference doc."""
    validate_role()
    if not is_chatwoot_enabled():
        return []
    return cw.list_canned_responses()


def _default_inbox_id() -> int:
    settings = frappe.get_single("Chatwoot Settings")
    inbox_id = settings.default_inbox_id
    if not inbox_id:
        frappe.throw(
            "Chatwoot Settings has no Default Inbox ID configured — required to look up "
            "WhatsApp message templates."
        )
    return frappe.utils.cint(inbox_id)


@frappe.whitelist()
def get_templates(inbox_id: int = None) -> list[dict]:
    """Chatwoot-native WhatsApp template list, sourced straight from the
    inbox's own `message_templates` array (already synced from Meta by
    Chatwoot — see chatwoot_client.get_inbox docstring). Role-gated only,
    same posture as get_canned_responses: templates are account/inbox-level,
    not bound to any specific reference doc.

    `inbox_id` defaults to Chatwoot Settings.default_inbox_id when omitted."""
    validate_role()
    if not is_chatwoot_enabled():
        return []
    resolved_inbox_id = frappe.utils.cint(inbox_id) if inbox_id else _default_inbox_id()
    return cw.list_message_templates(resolved_inbox_id)


@frappe.whitelist()
def send_template(
    conversation_id: int,
    template_name: str,
    category: str,
    language: str,
    processed_params: dict | str | None = None,
) -> dict:
    """Send a WhatsApp template message through Chatwoot's own native
    /messages endpoint with a `template_params` payload — this is the path
    that works even when the conversation is outside the 24h reply window
    (`can_reply: false`), which is the whole point of templates. Role-gated
    only — see get_messages' docstring on why reference-doc access must be
    bound by the caller (crm.api.chatwoot._validate_conversation_ownership)."""
    validate_role()
    if not is_chatwoot_enabled():
        frappe.throw("Chatwoot integration is not enabled")
    if not template_name:
        frappe.throw("template_name is required")
    if isinstance(processed_params, str):
        processed_params = frappe.parse_json(processed_params) if processed_params else {}
    processed_params = processed_params or {}

    conversation_id = frappe.utils.cint(conversation_id)
    result = cw.send_template_message(
        conversation_id,
        name=template_name,
        category=category,
        language=language,
        processed_params=processed_params,
    )
    return result


@frappe.whitelist()
def clear_chatwoot_cache():
    """Admin escape hatch — manually invalidate the read cache without
    waiting out the TTL. System Manager only (frappe.whitelist default
    guest=False already requires a logged-in session; this adds the role gate)."""
    if "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw("Not permitted", frappe.PermissionError)
    cw.clear_cache()
    return {"ok": True}


@frappe.whitelist(allow_guest=True)
def webhook():
    """Receive Chatwoot webhook events and push a realtime signal to the CRM.

    This is the INSTANT replacement for the 60-second poll in
    realtime_bridge.poll_and_broadcast — Chatwoot's own documented external
    integration primitive. Chatwoot POSTs `message_created` (and related)
    events here the moment they happen; we invalidate the read cache for the
    affected conversation and fire the SAME `chatwoot_message` realtime event
    the poll used, so the CRM frontend refetches immediately instead of waiting
    up to a minute for the next poll tick.

    Guest-open (Chatwoot calls it unauthenticated), so it is gated by a shared
    secret: Chatwoot's Automation/webhook can't send custom auth headers, so the
    token is carried as a `?token=` query param on the URL registered in
    Chatwoot, and compared here in constant time. If no webhook_token is
    configured the check is skipped (poll still covers correctness) but logged.
    """
    settings = frappe.get_cached_doc("Chatwoot Settings") if frappe.db.exists(
        "DocType", "Chatwoot Settings"
    ) else None
    if not settings or not settings.enabled:
        return {"ok": False, "reason": "disabled"}

    if not _webhook_authentic(settings):
        frappe.local.response["http_status_code"] = 401
        return {"ok": False, "reason": "invalid token"}

    try:
        data = frappe.request.get_json(silent=True) or frappe.local.form_dict or {}
    except Exception:
        data = {}

    event = data.get("event")
    # Only conversation/message activity needs to nudge the UI. Chatwoot also
    # sends webhook_verification / contact events we can ignore.
    if event not in (
        "message_created",
        "message_updated",
        "conversation_created",
        "conversation_updated",
        "conversation_status_changed",
    ):
        return {"ok": True, "ignored": event}

    # Chatwoot nests the conversation under `conversation` for message events,
    # or the top-level object IS the conversation for conversation events.
    conversation = data.get("conversation") or data
    # `id` here is Chatwoot's display_id (the human/sequential conversation id),
    # which is exactly what the CRM uses as conversation_id everywhere else.
    conv_id = conversation.get("id") or conversation.get("display_id")
    inbox_id = conversation.get("inbox_id") or data.get("inbox", {}).get("id")

    if conv_id is None:
        return {"ok": True, "no_conversation": True}

    # Drop the cached message page for this conversation so the frontend's
    # immediate refetch gets fresh data instead of a stale cache hit.
    try:
        cw.clear_cache()
    except Exception:
        pass

    frappe.publish_realtime(
        "chatwoot_message",
        {
            "conversation_id": conv_id,
            "inbox_id": inbox_id,
            "updated_at": conversation.get("updated_at") or conversation.get("last_activity_at"),
        },
    )
    return {"ok": True, "event": event, "conversation_id": conv_id}


def _webhook_authentic(settings) -> bool:
    """Constant-time shared-secret check for the guest-open webhook."""
    import hmac

    token = (settings.get("webhook_token") or "").strip() if settings.meta.has_field(
        "webhook_token"
    ) else ""
    if not token:
        frappe.log_error(
            "Chatwoot webhook received but no webhook_token is configured — "
            "signal accepted without verification. Set Chatwoot Settings > "
            "Webhook Token and append ?token=<it> to the Chatwoot webhook URL.",
            "frappe_chatwoot: unverified webhook",
        )
        return True
    provided = frappe.form_dict.get("token") or frappe.get_request_header("X-Chatwoot-Token") or ""
    return hmac.compare_digest(str(provided), str(token))


# Añadido por lavendi.mx (fork sofía) — endpoint de BANDEJA.
#
# El upstream (hypedrive-app/frappe_chatwoot) solo resuelve conversaciones
# ligadas a un documento de Frappe (get_conversations_for_contact). Para la
# vista "Conversaciones" dentro del SPA de CRM necesitamos lo contrario: la
# bandeja completa de la cuenta, como la ve un agente en GHL, sin partir de un
# Lead/Deal. cw.list_conversations ya existe en el cliente; aquí solo se expone
# con el mismo gate de roles y se aplana la forma para la UI.


@frappe.whitelist()
def list_inboxes() -> list[dict]:
    """Inboxes de la cuenta (uno por cliente/canal). Sirve para el filtro de la
    bandeja y para etiquetar cada conversación con su origen."""
    validate_role()
    if not is_chatwoot_enabled():
        return []
    return [
        {
            "id": inbox.get("id"),
            "name": inbox.get("name"),
            "channel_type": inbox.get("channel_type"),
        }
        for inbox in cw.list_inboxes()
    ]


# Tope del barrido de la bandeja (ver get_conversations). Chatwoot pagina de 25
# en 25; 10 páginas = 250 conversaciones. Es un backstop contra una cuenta que
# crezca sin que nadie lo note, no un límite de producto: hoy el inbox más
# grande (lavendi.mx) son 91 conversaciones = 4 páginas.
MAX_PAGINAS_BANDEJA = 10

# Segundos que se guardan en caché las páginas PROFUNDAS del barrido (2 en
# adelante). Ver _barrer_canal.
TTL_PAGINAS_PROFUNDAS = 60


def _es_grupo(sender: dict) -> bool:
    """Un grupo de WhatsApp se reconoce por el JID que Evolution API guarda en el
    `identifier` del contacto de Chatwoot: los grupos terminan en `@g.us`, los
    individuales en `@s.whatsapp.net` (o vienen vacíos si el contacto se creó a
    mano desde la UI, que también es individual).

    Se usa el identifier y no el nombre — varios grupos traen "(GROUP)" en el
    nombre, pero eso lo escribe Evolution al crearlos y cualquiera puede
    renombrarlos desde Chatwoot; el JID no cambia nunca."""
    return (sender.get("identifier") or "").endswith("@g.us")


def _shape_conversation(conv: dict, pausadas: set = None, archivadas: set = None) -> dict:
    """Aplana el objeto de Chatwoot a lo que la bandeja necesita pintar.

    El preview sale de `last_non_activity_message`, que _scrub_conversation_preview
    ya dejó en None si era una nota privada — por eso aquí se lee sin volver a
    filtrar: la invariante de confidencialidad ya se aplicó aguas arriba.

    `pausadas`/`archivadas` son conjuntos de conversation_id precargados de una
    sola consulta por quien llama en lote. Sin eso esto haría dos SELECT por
    conversación y el barrido completo de la bandeja (ver get_conversations)
    dispararía ~180 consultas por carga."""
    meta = conv.get("meta") or {}
    sender = meta.get("sender") or {}
    last = conv.get("last_non_activity_message") or {}
    assignee = meta.get("assignee") or {}
    cid = conv.get("id")
    paused = (
        cid in pausadas
        if pausadas is not None
        else bool(frappe.db.exists("Chatwoot Pausa", {"conversation_id": cid}))
    )
    archived = (
        cid in archivadas
        if archivadas is not None
        else bool(frappe.db.exists("Chatwoot Archivo", {"conversation_id": cid}))
    )
    return {
        "id": cid,
        "inbox_id": conv.get("inbox_id"),
        "status": conv.get("status"),
        "unread_count": conv.get("unread_count") or 0,
        "last_activity_at": conv.get("last_activity_at") or conv.get("timestamp"),
        "contact": {
            "name": sender.get("name") or sender.get("identifier") or "Sin nombre",
            "phone": sender.get("phone_number"),
            "email": sender.get("email"),
            "avatar": sender.get("thumbnail") or sender.get("avatar_url") or None,
        },
        "assignee": (assignee.get("name") or "").strip() or None,
        "preview": (last.get("content") or "").strip(),
        "preview_direction": {0: "incoming", 1: "outgoing"}.get(last.get("message_type")),
        # lavendi.mx: si el agente IA está pausado en esta conversación (ver
        # frappe_chatwoot.api.agentes) — la bandeja lo marca para que quede
        # claro quién está respondiendo antes de escribir encima.
        "agent_paused": paused,
        # lavendi.mx: conversación archivada por el equipo — sale de la bandeja
        # principal sin borrarse ni tocar su estado en Chatwoot (ver archivar()).
        "archived": archived,
        # lavendi.mx: grupo de WhatsApp. La bandeja los pinta en una sección
        # aparte, debajo de las conversaciones individuales.
        "is_group": _es_grupo(sender),
    }


def _ids_marcados(doctype: str) -> set:
    """conversation_id marcados en un doctype de banderas (Chatwoot Pausa /
    Chatwoot Archivo), en una sola consulta. Ver _shape_conversation."""
    try:
        return set(frappe.get_all(doctype, pluck="conversation_id"))
    except Exception:
        # Un doctype que todavía no existe (instalación a medio migrar) no debe
        # tumbar la bandeja — degrada a "nada marcado", mismo criterio que el
        # resto de este módulo.
        return set()


def _barrer_canal(canal: int, status: str) -> list[dict]:
    """Todas las páginas del canal, con la página 1 SIEMPRE fresca y las
    profundas en caché.

    El motivo del reparto: la bandeja recarga por polling cada 8 s, y barrer 4-5
    páginas en cada ciclo multiplica por 5 el tráfico contra Chatwoot sin ganar
    nada — una conversación de la página 3 no se mueve sola. Pero cachear TODO
    metería el retraso del caché en la llegada de mensajes nuevos, que es
    justamente lo que la bandeja tiene que mostrar rápido.

    Chatwoot ordena por actividad, así que todo lo que cambia entra por la
    página 1: dejándola fuera del caché la latencia de un mensaje nuevo queda
    igual que antes de este cambio, y en régimen el barrido cuesta 1 petición en
    vez de 5.

    La dedup por id prefiere la versión fresca: una conversación que acaba de
    subir a la página 1 también sigue en la copia cacheada de su página vieja,
    y la buena es la nueva.
    """
    frescas = cw.list_conversations(inbox_id=canal, status=status, page=1)
    if len(frescas) == 0:
        return []

    clave = f"frappe_chatwoot:bandeja_profunda:{canal}:{status}"
    cache = frappe.cache()
    # `expires=True` es obligatorio aquí, no cosmético: sin él, un get que
    # devuelve None deja ese None guardado en el caché local del proceso
    # (frappe.local.cache), y el set_value con expires_in_sec NO lo actualiza —
    # solo escribe en Redis. Resultado: el caché nunca pegaba y el barrido
    # seguía costando 5 peticiones en cada poll.
    profundas = cache.get_value(clave, expires=True)

    if profundas is None:
        profundas = []
        for pagina in range(2, MAX_PAGINAS_BANDEJA + 1):
            lote = cw.list_conversations(inbox_id=canal, status=status, page=pagina)
            if not lote:
                break
            profundas.extend(lote)
        else:
            frappe.log_error(
                title="Bandeja truncada: se alcanzó el tope de páginas",
                message=(
                    f"canal={canal} status={status}: se leyeron {MAX_PAGINAS_BANDEJA} páginas "
                    f"({len(frescas) + len(profundas)} conversaciones) y Chatwoot seguía "
                    "devolviendo más. Subir MAX_PAGINAS_BANDEJA o paginar la bandeja de verdad."
                ),
            )
        cache.set_value(clave, profundas, expires_in_sec=TTL_PAGINAS_PROFUNDAS)

    por_id = {c.get("id"): c for c in profundas}
    por_id.update({c.get("id"): c for c in frescas})
    return list(por_id.values())


@frappe.whitelist()
def get_conversations(inbox_id=None, status: str = "open", page=None, archivadas=0) -> list[dict]:
    """Bandeja completa. Degrada suave (lista vacía) si Chatwoot no está
    configurado, mismo contrato que get_conversations_for_contact.

    Barre TODAS las páginas de Chatwoot, no solo la primera. Antes devolvía la
    página 1 (25 conversaciones) y eso dejaba fuera de la bandeja la mayor parte
    del canal: medido el 2026-09-21 en lavendi.mx, de 91 conversaciones se veían
    25, y de los 9 grupos de WhatsApp solo 1 — los otros 8 vivían en las páginas
    3 y 4, invisibles. Con la lista partida en individuales y grupos eso dejaba
    la sección de grupos prácticamente vacía sin que nada lo indicara.

    Costo medido del barrido en frío: 0.68 s contra 0.21 s de una sola página (5
    peticiones a Chatwoot en vez de 1). En régimen vuelve a costar 1 petición
    porque las páginas profundas quedan en caché — ver _barrer_canal, que
    explica por qué la página 1 se deja siempre fresca. Crece con el tamaño del
    canal, por eso el tope de MAX_PAGINAS_BANDEJA, que se registra en el log
    cuando muerde en vez de truncar en silencio.

    `page` se conserva en la firma porque el frontend viejo lo mandaba; ya no se
    usa — pedir una página suelta es justo lo que causaba el problema.
    """
    validate_role()
    if not is_chatwoot_enabled():
        return []
    if status not in ("open", "resolved", "pending", "snoozed", "all"):
        frappe.throw("status inválido")
    # Aislamiento entre clientes: sin canal explicito se usa el canal de ESTE
    # sitio, nunca 'todos'. Antes devolvia None y Chatwoot entregaba las
    # conversaciones de todos los inboxes de la cuenta compartida (Six Gardens
    # aparecia en la bandeja de lavendi.mx). Ver docs/project_agente_whatsapp.md.
    canal = frappe.utils.cint(inbox_id) or _default_inbox_id()

    conversations = _barrer_canal(canal, status)

    pausadas = _ids_marcados("Chatwoot Pausa")
    archivadas_ids = _ids_marcados("Chatwoot Archivo")
    shaped = [_shape_conversation(c, pausadas, archivadas_ids) for c in conversations]
    # El archivado es una vista, no un borrado: o se ven solo las archivadas o
    # solo las vivas, nunca mezcladas — que es justo lo que se pidió evitar.
    quiere_archivadas = bool(frappe.utils.cint(archivadas))
    shaped = [c for c in shaped if c["archived"] == quiere_archivadas]
    # Orden por recencia pura (Alejandro, 2026-09-22, corrección del mismo día):
    # lo más reciente arriba, lo más antiguo abajo. Se había probado antes
    # "no contestados primero", pero eso dejaba hilos recién contestados (17:12)
    # por debajo de pendientes viejos (09-08). El badge de no leídos sigue marcando
    # lo pendiente sin tocar el orden. Va en el backend para que valga en TODAS las
    # vistas (individuales, grupos y archivadas), sin depender de cada pantalla.
    shaped.sort(key=lambda c: c.get("last_activity_at") or 0, reverse=True)
    return shaped


@frappe.whitelist()
def obtener_conversacion(conversation_id: int) -> dict | None:
    """Una conversación por id, con la forma de la bandeja, sin pasar por el
    filtro de la lista.

    Existe porque la bandeja resuelve los enlaces directos (`?conv=`, la ruta
    móvil `/conversaciones/:id`, el buscador global, las notificaciones push)
    buscando el id dentro de la lista ya cargada. Eso falla en silencio —no
    abre nada— cuando la conversación no está en el filtro activo: archivada,
    de otro canal, o de un estado que la vista no pide. Aquí se pregunta por
    ella directamente.
    """
    validate_role()
    if not is_chatwoot_enabled():
        return None
    cid = frappe.utils.cint(conversation_id)
    if not cid:
        return None
    try:
        conv = cw.get_conversation(cid)
    except cw.ChatwootAPIError:
        return None
    if not conv:
        return None
    return _shape_conversation(
        conv, _ids_marcados("Chatwoot Pausa"), _ids_marcados("Chatwoot Archivo")
    )


@frappe.whitelist()
def archivar(conversation_id: int, inbox_id=None) -> dict:
    """Saca una conversación de la bandeja principal sin borrarla.

    Es una bandera propia del CRM, NO el estado de Chatwoot. A propósito:
    `resolved` ya se usa para "este asunto se cerró" y la bandeja abre en
    "Todas", así que resolver no saca nada de la vista; y tocar el estado de
    Chatwoot cambiaría lo que ve el agente IA y lo que reportan las métricas del
    canal. Archivar es sobre la bandeja, no sobre la conversación.

    Global para el equipo, no por usuario — mismo criterio que Chatwoot Pausa:
    la bandeja es compartida y "ya no estorba" es una afirmación sobre el
    negocio, no sobre quién la miró. Se registra quién archivó y cuándo para que
    haya a quién preguntar.

    Reversible con desarchivar(). Idempotente: archivar dos veces no duplica.
    """
    validate_role()
    cid = frappe.utils.cint(conversation_id)
    if not cid:
        frappe.throw("conversation_id inválido")
    if frappe.db.exists("Chatwoot Archivo", {"conversation_id": cid}):
        return {"archived": True}
    frappe.get_doc(
        {
            "doctype": "Chatwoot Archivo",
            "conversation_id": cid,
            "inbox_id": str(inbox_id) if inbox_id else None,
            "archivado_por": frappe.session.user,
            "archivado_at": frappe.utils.now_datetime(),
        }
    ).insert(ignore_permissions=True)
    return {"archived": True}


@frappe.whitelist()
def desarchivar(conversation_id: int) -> dict:
    """Devuelve la conversación a la bandeja principal. Espejo de archivar()."""
    validate_role()
    cid = frappe.utils.cint(conversation_id)
    name = frappe.db.exists("Chatwoot Archivo", {"conversation_id": cid})
    if name:
        frappe.delete_doc("Chatwoot Archivo", name, ignore_permissions=True)
    return {"archived": False}


@frappe.whitelist()
def search_conversations(q: str = "", limit=40) -> list[dict]:
    """Buscador de la bandeja: encuentra conversaciones por nombre, teléfono o
    correo del contacto, ignorando el filtro de estado y canal de la vista.

    Por qué no basta filtrar en el navegador lo que ya está cargado: la bandeja
    pinta la página 1 de Chatwoot (25 conversaciones) del estado y canal
    activos. Lo que el equipo busca casi siempre queda fuera de eso — un hilo
    resuelto de hace dos semanas, o de otro canal.

    Chatwoot no expone búsqueda de conversaciones por texto libre, así que se
    busca el CONTACTO (/contacts/search cubre nombre, correo y teléfono) y de
    ahí se traen sus conversaciones. Mismo motor que usa el buscador global
    (api.buscar), pero devolviendo conversaciones con la forma completa de la
    bandeja para que la lista y el hilo se pinten igual que siempre.
    """
    validate_role()
    if not is_chatwoot_enabled():
        return []
    q = (q or "").strip()
    # Mismo umbral que buscar_global: con 1 carácter Chatwoot devuelve cientos
    # de contactos y la búsqueda se vuelve inútil además de lenta.
    if len(q) < 2:
        return []

    try:
        contactos = cw.search_contacts(q)[:10]
    except cw.ChatwootAPIError as exc:
        frappe.log_error(title="search_conversations: Chatwoot no responde", message=str(exc))
        # A diferencia de la bandeja, aquí NO se degrada a lista vacía: "sin
        # resultados" y "no pude buscar" se ven igual en pantalla y llevan a
        # concluir que la conversación no existe.
        frappe.throw("No se pudo buscar en Chatwoot")

    pausadas = _ids_marcados("Chatwoot Pausa")
    archivadas = _ids_marcados("Chatwoot Archivo")
    vistas = set()
    filas = []
    for contacto in contactos:
        try:
            convs = cw.get_conversations_for_contact(contacto.get("id"))
        except cw.ChatwootAPIError:
            continue
        for conv in convs:
            cid = conv.get("id")
            if not cid or cid in vistas:
                continue
            vistas.add(cid)
            # El buscador SÍ devuelve archivadas (marcadas como tales): ya ignora
            # el filtro de estado y canal por el mismo motivo — "no aparece" se
            # lee como "no existe" y manda a la gente a buscar fuera del CRM.
            filas.append(_shape_conversation(conv, pausadas, archivadas))

    # El buscador ignora a proposito el filtro de estado/canal de la vista, pero NO
    # debe cruzar de cliente: se limita al inbox de este sitio (mismo criterio que
    # get_conversations). Ver docs/project_agente_whatsapp.md.
    scoped_inbox = _default_inbox_id()
    filas = [f for f in filas if f.get("inbox_id") == scoped_inbox]
    filas.sort(key=lambda c: c.get("last_activity_at") or 0, reverse=True)
    return filas[: frappe.utils.cint(limit) or 40]


@frappe.whitelist()
def adjunto(path: str, nombre: str = None):
    """Sirve un adjunto de una conversación (foto, PDF, audio, video).

    Añadido por lavendi.mx el 2026-09-10, después de que Alejandro reportara que
    los archivos que manda un cliente por WhatsApp "no se reflejan" en el CRM.
    Sí llegaban — están completos en Chatwoot — pero eran inalcanzables desde el
    navegador: sus URLs apuntan al host interno de Coolify por http, y una página
    servida por https no carga contenido mixto.

    Se responde `inline` y no como descarga: si no, cada foto de una conversación
    se bajaría al disco en vez de verse en el hilo.
    """
    validate_role()
    if not is_chatwoot_enabled():
        frappe.throw("Chatwoot integration is not enabled")

    contenido, content_type = cw.fetch_attachment(path)
    frappe.local.response.filename = nombre or path.rsplit("/", 1)[-1] or "adjunto"
    frappe.local.response.filecontent = contenido
    frappe.local.response.content_type = content_type
    # "download" es la llave que Frappe mapea a `as_raw` — la única que respeta
    # `content_type` y `display_content_as`. Con "inline", el navegador la pinta en
    # el hilo en vez de bajarla al disco.
    frappe.local.response.type = "download"
    frappe.local.response.display_content_as = "inline"
