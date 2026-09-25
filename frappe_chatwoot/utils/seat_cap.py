"""Tope de usuarios de un sitio de cliente (Sofía Lite, hasta 2026-09 solo Six Gardens).

Sofía Lite ahora incluye acceso real al CRM (modulo base), con un tope de usuarios por
contrato — no lo aplica frappe/crm de forma nativa (su panel de Settings > Users invita
sin limite). El tope vive en `site_config.json` (`sofia_lite_max_usuarios`), NUNCA en
codigo: un sitio sin esa llave (crm.lavendi.mx, estrublock.lavendi.mx) no queda afectado.

Solo cuentan como "asiento del cliente" los correos que no son de lavendi.mx ni cuentas
de servicio — el equipo interno (revision de calidad, el usuario del agente) no debe
competir por el tope que el cliente esta pagando.
"""

import frappe

CUENTAS_EXCLUIDAS = {"Administrator", "agente-ia@lavendi.mx"}


def _es_asiento_de_cliente(email):
    if email in CUENTAS_EXCLUIDAS:
        return False
    return not email.endswith("@lavendi.mx")


def validar_tope_usuarios(doc, method=None):
    tope = frappe.conf.get("sofia_lite_max_usuarios")
    if not tope:
        return
    if not _es_asiento_de_cliente(doc.name or doc.email):
        return

    usuarios_activos = frappe.get_all(
        "User", filters={"enabled": 1, "user_type": "System User"}, pluck="name"
    )
    ocupados = [u for u in usuarios_activos if _es_asiento_de_cliente(u)]

    if len(ocupados) >= tope:
        frappe.throw(
            f"Este sitio tiene un tope de {tope} usuarios en el plan actual. "
            "Contacta a lavendi.mx para ampliarlo.",
            title="Tope de usuarios alcanzado",
        )
