# Añadido por lavendi.mx (fork sofía) — configuración del agente IA + handoff.
#
# agente-ia (Node, fuera de Frappe) ya no lee config/clients.json: llama a
# get_active_agentes() por HTTP con API key/secret. Frappe es ahora la única
# fuente de verdad de "qué agente responde en qué inbox", igual que ya lo es
# para leads/deals — cierra el hueco de "¿desde dónde lo gestiona el cliente?".
#
# El pausado es el mecanismo de handoff humano↔agente: send_message (abajo)
# pausa la conversación automáticamente en cuanto un humano responde desde la
# página Conversaciones — nunca hay que acordarse de pausar a mano. Reanudar
# sí es una acción explícita (botón en la UI), a propósito: que el agente
# vuelva a hablar solo porque el contacto escribió de nuevo sería repetir el
# mismo bug que dejó a medio resolver el server.js original.

import time

import frappe

from frappe_chatwoot.frappe_chatwoot.api.chatwoot import _pause_conversation, validate_role


@frappe.whitelist()
def get_active_agentes() -> dict:
    """Forma idéntica a agente-ia/config/clients.json: {inbox_id: {name, provider,
    model, systemPrompt, calendarId}}. Sin gate de rol — la llama agente-ia con API
    key de servicio, no un usuario humano navegando Sofía."""
    rows = frappe.get_all(
        "Agente IA",
        filters={"active": 1},
        fields=[
            "name",
            "client_name",
            "provider",
            "model",
            "system_prompt",
            "calendar_id",
            "shadow_mode",
            "gate_intencion",
        ],
    )
    return {
        row.name: {
            "name": row.client_name,
            "provider": row.provider,
            "model": row.model,
            "systemPrompt": row.system_prompt,
            "calendarId": row.calendar_id or None,
            "shadowMode": bool(row.shadow_mode),
            # Sofía Lite: el número del cliente también recibe proveedores, personal y
            # vecinos. Apagado por default — los inboxes que ya existen no cambian.
            "gateIntencion": bool(row.gate_intencion),
        }
        for row in rows
    }


@frappe.whitelist()
def get_paused_conversations(inbox_id) -> list[int]:
    """IDs de conversación pausadas para ese inbox — agente-ia las salta en
    cada ciclo de polling. Sin gate de rol, mismo motivo que arriba."""
    rows = frappe.get_all(
        "Chatwoot Pausa",
        filters={"inbox_id": str(inbox_id)},
        pluck="conversation_id",
    )
    return rows


@frappe.whitelist()
def resume_conversation(conversation_id: int) -> None:
    """Reanuda el agente en esta conversación. Acción explícita desde la UI
    (botón 'Reanudar agente') — nunca automática, ver docstring del módulo."""
    validate_role()
    cid = frappe.utils.cint(conversation_id)
    name = frappe.db.exists("Chatwoot Pausa", {"conversation_id": cid})
    if name:
        frappe.delete_doc("Chatwoot Pausa", name, ignore_permissions=True)
    # Además de quitar la pausa, deja marca de CUÁNDO se reanudó (ver get_reanudaciones).
    # Sin esto, "Reanudar agente" no bastaba: el bot seguía callado los 90 min de
    # silencio-humano desde el último mensaje del equipo anterior a la reanudación
    # (caso Laisha, 2026-09-22).
    _marcar_reanudacion(cid)


def _marcar_reanudacion(conversation_id: int) -> None:
    """Registra el instante de la reanudación explícita para esa conversación
    (upsert por conversation_id). agente-ia lo lee con get_reanudaciones."""
    existente = frappe.db.exists("Chatwoot Reanudacion", {"conversation_id": conversation_id})
    if existente:
        doc = frappe.get_doc("Chatwoot Reanudacion", existente)
    else:
        doc = frappe.new_doc("Chatwoot Reanudacion")
        doc.conversation_id = conversation_id
    doc.reanudado_por = frappe.session.user
    doc.reanudado_at = frappe.utils.now()
    # Epoch absoluto en ms para que agente-ia compare 1:1 contra created_at de Chatwoot.
    # `frappe.utils.get_timestamp` no sirve: devuelve el valor en otra base horaria
    # (verificado 2026-09-22) y corría la ventana de silencio varias horas.
    doc.reanudado_ms = int(time.time() * 1000)
    doc.save(ignore_permissions=True)
    frappe.db.commit()


@frappe.whitelist()
def get_reanudaciones() -> dict:
    """{conversation_id: epoch_ms} de cada reanudación explícita. agente-ia lo usa
    para NO contar como silencio-humano los mensajes del equipo ANTERIORES a la
    reanudación — así el bot retoma en cuanto el cliente vuelve a escribir, en vez
    de esperar HUMANO_SILENCIO_MS desde un mensaje humano ya superado. Un mensaje
    humano POSTERIOR a la marca vuelve a silenciar con normalidad.
    Sin gate de rol — mismo motivo que get_paused_conversations."""
    rows = frappe.get_all(
        "Chatwoot Reanudacion",
        fields=["conversation_id", "reanudado_ms"],
    )
    out = {}
    for row in rows:
        if row.reanudado_ms:
            out[str(row.conversation_id)] = int(row.reanudado_ms)
    return out


@frappe.whitelist()
def pause_conversation(conversation_id: int, inbox_id: int = None) -> None:
    """Pausa el agente en esta conversación a mano, sin esperar a que un humano
    responda primero (ver send_message._pause_conversation, mismo mecanismo).
    Espejo de resume_conversation — botón 'Pausar agente' en la UI."""
    validate_role()
    _pause_conversation(frappe.utils.cint(conversation_id), inbox_id)
