"""Recordatorio de videollamada — reemplazo del workflow 5 de GHL.

GHL mandaba, **60 minutos antes** de cada cita confirmada, un WhatsApp con el
link de la reunión. 856 personas han pasado por ese workflow; era el único con
inscripción activa el día que se inventarió. El módulo de Calendario del nuevo
Sofía agendaba pero no recordaba: este job cierra ese hueco.

Diferencias deliberadas con GHL, y por qué:

  - GHL esperaba dentro del propio workflow (una instancia viva por cita).
    Aquí es un job que barre cada 15 minutos la ventana [+45 min, +63 min].
    No hay estado que sobreviva a un reinicio, que es justo lo que ya nos
    mordió con `lastProcessed` del agente el 2026-09-06.
  - GHL usaba `appointmentCondition: skip` para no avisar de citas que ya
    cambiaron. Aquí cancelar una cita **borra la fila**, así que una cita
    cancelada sencillamente no aparece en la ventana.
  - El mensaje **no pausa al agente IA**, a diferencia de una respuesta humana.
    Es un aviso automático: si el cliente contesta "sí, ahí estaré", Sofía debe
    poder seguir la conversación. En GHL tampoco pausaba.
"""

import frappe

from . import autoria
from ..frappe_chatwoot.api import chatwoot as api_cw
from ..frappe_chatwoot.api import push

# Ventana del barrido, asimétrica a propósito: HOLGURA_ATRAS es más ancha que
# el intervalo del cron para que, si una corrida se salta (reinicio, cola
# atorada), la siguiente todavía alcance la cita. MARGEN_ADELANTO es solo el
# jitter del scheduler — si fuera simétrico, el primer tick que ve una cita
# alineada a la rejilla de 15 min (12:00 -> tick de 10:45) la marca a 75 min
# y el tick de los 60 min reales ya nunca la encuentra. La marca
# `recordatorio_enviado_at` evita el doble aviso que la holgura haría posible.
MINUTOS_ANTES = 60
HOLGURA_ATRAS = 15
MARGEN_ADELANTO = 3


def _activo():
    try:
        return bool(frappe.db.get_single_value("Chatwoot Settings", "recordatorio_citas_activo"))
    except Exception:
        return False


def _mensaje(reunion):
    """Copia literal del SMS de GHL. El `{{contact.first_name}}` de allá se
    resuelve aquí con el nombre del participante, que es el dato que la cita
    tiene a la mano."""
    nombre = (reunion.nombre_participante or "").strip().split(" ")[0]
    saludo = f"Hola {nombre} 🙂" if nombre else "Hola 🙂"
    return (
        f"{saludo}, confirmando nuestra videollamada / Llamada dentro de 1 hora, correcto? 🗓️\n\n"
        f"El enlace para unirte es el siguiente: \n{reunion.meet_link or ''}"
    ).rstrip()


def _conversacion(reunion):
    """El id guardado en la cita es la vía buena — lo pone quien agenda, que
    sabe en qué hilo está. El fallback por contacto existe para las citas
    creadas antes de que el campo existiera; no adivina por nombre ni correo,
    solo sigue el enlace duro `crm_contacto` → deal/lead."""
    directo = (reunion.get("chatwoot_conversation_id") or "").strip()
    if directo:
        return directo
    contacto = (reunion.get("crm_contacto") or "").strip()
    if not contacto:
        return ""
    for doctype in ("CRM Deal", "CRM Lead"):
        fila = frappe.get_all(
            doctype,
            filters={"contact": contacto, "chatwoot_conversation_id": ["!=", ""]},
            fields=["chatwoot_conversation_id"],
            order_by="modified desc",
            limit=1,
        )
        if fila:
            return fila[0].chatwoot_conversation_id
    return ""


def _responsable(reunion):
    """Usuario Frappe a avisar por push de "tu cita es en 1 hora" — el dueño del
    deal/lead ligado al contacto de la cita. Sin contacto o sin deal/lead con dueño
    no hay a quién atribuírsela individualmente; se omite en vez de hacer broadcast
    a todo el equipo por cada cita (fatiga de alertas, mismo criterio ya aplicado en
    `push.py` — el WhatsApp al cliente ya salió de cualquier forma)."""
    contacto = (reunion.get("crm_contacto") or "").strip()
    if not contacto:
        return None
    for doctype, campo in (("CRM Deal", "deal_owner"), ("CRM Lead", "lead_owner")):
        fila = frappe.get_all(
            doctype, filters={"contact": contacto}, fields=[campo],
            order_by="modified desc", limit=1,
        )
        if fila and fila[0].get(campo):
            return fila[0][campo]
    return None


def enviar_recordatorios():
    """Scheduler cada 15 minutos. Barato: una consulta acotada por fecha; si
    no hay citas en la ventana no hace nada más."""
    if not _activo():
        return {"activo": False}

    ahora = frappe.utils.now_datetime()
    desde = frappe.utils.add_to_date(ahora, minutes=MINUTOS_ANTES - HOLGURA_ATRAS)
    hasta = frappe.utils.add_to_date(ahora, minutes=MINUTOS_ANTES + MARGEN_ADELANTO)

    citas = frappe.get_all(
        "Reunion Agendada",
        filters={
            "start_datetime": ["between", [desde, hasta]],
            "recordatorio_enviado_at": ["is", "not set"],
        },
        fields=["name", "nombre_participante", "meet_link", "start_datetime",
                "inbox_id", "crm_contacto", "chatwoot_conversation_id"],
    )

    enviados, saltados = [], []
    for cita in citas:
        conv = _conversacion(cita)
        if not conv:
            # Se marca igual: sin conversación no hay a dónde mandarlo, y
            # dejarla sin marca haría que el job la reintente cada 15 minutos
            # durante media hora sin que nada cambie.
            frappe.db.set_value("Reunion Agendada", cita.name, "recordatorio_enviado_at",
                                ahora, update_modified=False)
            saltados.append((cita.name, "sin conversación de Chatwoot"))
            continue
        try:
            api_cw.cw.create_message(
                int(conv), _mensaje(cita),
                content_attributes=autoria.marca_automatica(
                    "recordatorio", "Recordatorio de videollamada"))
            frappe.db.set_value("Reunion Agendada", cita.name, "recordatorio_enviado_at",
                                ahora, update_modified=False)
            enviados.append(cita.name)
            try:
                responsable = _responsable(cita)
                if responsable:
                    push.notify_user(
                        responsable,
                        title="Videollamada en 1 hora",
                        body=f"Con {cita.nombre_participante or 'contacto'}",
                        url=f"/crm/conversaciones?conv={conv}",
                        tag=f"cita-{cita.name}",
                    )
            except Exception:
                # El push es adicional — el WhatsApp real al cliente ya salió
                # (línea de arriba); un fallo aquí no debe verse como fallo del
                # recordatorio.
                pass
        except Exception as exc:
            # No se marca: un fallo de red merece reintento en la corrida
            # siguiente, que todavía cae dentro de la ventana.
            frappe.log_error(f"recordatorio {cita.name}: {exc}", "Recordatorio de citas")
            saltados.append((cita.name, str(exc)[:80]))

    if enviados or saltados:
        frappe.db.commit()
    return {"activo": True, "enviados": enviados, "saltados": saltados}
