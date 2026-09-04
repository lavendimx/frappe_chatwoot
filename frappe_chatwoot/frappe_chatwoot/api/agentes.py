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

import frappe

from frappe_chatwoot.frappe_chatwoot.api.chatwoot import validate_role


@frappe.whitelist()
def get_active_agentes() -> dict:
    """Forma idéntica a agente-ia/config/clients.json: {inbox_id: {name, provider,
    model, systemPrompt}}. Sin gate de rol — la llama agente-ia con API key de
    servicio, no un usuario humano navegando Sofía."""
    rows = frappe.get_all(
        "Agente IA",
        filters={"active": 1},
        fields=["name", "client_name", "provider", "model", "system_prompt"],
    )
    return {
        row.name: {
            "name": row.client_name,
            "provider": row.provider,
            "model": row.model,
            "systemPrompt": row.system_prompt,
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
    name = frappe.db.exists("Chatwoot Pausa", {"conversation_id": frappe.utils.cint(conversation_id)})
    if name:
        frappe.delete_doc("Chatwoot Pausa", name, ignore_permissions=True)
