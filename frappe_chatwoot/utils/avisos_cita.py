"""Aviso al asesor 10 minutos antes de la cita.

Complementa a `recordatorios.py` (que avisa al CLIENTE 60 min antes por WhatsApp
y hace un push al asesor). Este job hace SOLO el push al asesor a los 10 min,
que es lo que Irene pidió en la llamada del 2026-10-20: que la asesora reciba
el aviso cuando está a punto de empezar la llamada, no una hora antes.

El WhatsApp al asesor queda pendiente de capturar sus números; cuando existan,
este mismo job es el punto de inserción.
"""

import frappe

MINUTOS_ANTES = 10
HOLGURA_ATRAS = 4
MARGEN_ADELANTO = 2


def _activo():
    try:
        return bool(frappe.db.get_single_value("Chatwoot Settings", "recordatorio_citas_activo"))
    except Exception:
        return False


def _responsable(reunion):
    from frappe_chatwoot.utils.recordatorios import _responsable as base
    return base(reunion)


def avisar():
    if not _activo():
        return {"activo": False}

    ahora = frappe.utils.now_datetime()
    desde = frappe.utils.add_to_date(ahora, minutes=MINUTOS_ANTES - HOLGURA_ATRAS)
    hasta = frappe.utils.add_to_date(ahora, minutes=MINUTOS_ANTES + MARGEN_ADELANTO)

    citas = frappe.get_all(
        "Reunion Agendada",
        filters={
            "start_datetime": ["between", [desde, hasta]],
            "aviso_10_enviado_at": ["is", "not set"],
        },
        fields=["name", "nombre_participante", "start_datetime", "crm_contacto",
                "chatwoot_conversation_id", "modalidad"],
    )

    enviados, saltados = [], []
    for cita in citas:
        try:
            responsable = _responsable(cita)
            if not responsable:
                saltados.append((cita.name, "sin responsable"))
            else:
                from frappe_chatwoot.frappe_chatwoot.api import push
                push.notify_user(
                    responsable,
                    title="Tu cita es en 10 minutos",
                    body=f"Con {cita.nombre_participante or 'contacto'}",
                    url=f"/crm/conversaciones?conv={cita.chatwoot_conversation_id or ''}",
                    tag=f"cita10-{cita.name}",
                )
                enviados.append(cita.name)
            frappe.db.set_value("Reunion Agendada", cita.name, "aviso_10_enviado_at",
                                ahora, update_modified=False)
        except Exception as exc:
            frappe.log_error(f"aviso10 {cita.name}: {exc}", "Aviso 10 min")
            saltados.append((cita.name, str(exc)[:80]))

    if enviados or saltados:
        frappe.db.commit()
    return {"activo": True, "enviados": enviados, "saltados": saltados}
