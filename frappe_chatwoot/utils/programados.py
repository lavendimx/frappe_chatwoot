# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Mensajes programados ("enviar más tarde") para Conversaciones.

Réplica del "Send Later" de GHL: un humano programa un mensaje (texto y/o
adjuntos) para una hora futura; un cron cada minuto lo manda por Chatwoot.

Decisiones de Alejandro (2026-09-14/15):
  - Si el contacto escribe antes de la hora, el programado se **cancela**.
  - Enviar desde el CRM **no pausa al agente**: la intervención humana entra
    como contexto y el agente retoma solo (ver `_marcar_humano` y el guard de
    `humanoRecienteEn` en el proceso `agente-ia`).
  - Texto **y adjuntos**.

Por qué un doctype y no `frappe.enqueue(eta=...)`: el enqueue se pierde si el
worker reinicia y no se puede listar ni cancelar desde la UI. El doctype es
visible, cancelable y sobrevive reinicios (mismo criterio que `recordatorios`).

Por qué el envío NO crea un `Chatwoot Pausa`: ese registro es una pausa dura,
sin auto-reanudar — el bot quedaba mudo hasta que alguien apretara "Reanudar".
La intervención manual se marca con `content_attributes.humano = true` para que
el agente la reconozca como humana (un envío del CRM no trae `source_id:
WAID:` ni la firma `**Nombre:**` del bridge, que son las dos señales que el
agente usa hoy) y aplique su propio freno de tiempo, que sí se vence solo.
"""

import datetime as dt
import json
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import now_datetime

from . import chatwoot_client as cw

MAX_ADJUNTOS = 5
LOTE = 50


# ── Utilidades ───────────────────────────────────────────────────────────────

def _validar_rol():
    # Mismo gate que el resto de Conversaciones (api/chatwoot.validate_role).
    from ..frappe_chatwoot.api.chatwoot import validate_role
    validate_role()


def _tz_sitio():
    try:
        nombre = frappe.db.get_single_value("System Settings", "time_zone") or "America/Mexico_City"
        return ZoneInfo(nombre)
    except Exception:
        return ZoneInfo("America/Mexico_City")


def _epoch_a_local(epoch) -> dt.datetime:
    """Un `created_at` de Chatwoot (epoch UTC) como datetime local naive, para
    compararlo contra `doc.creation` (que Frappe guarda en la zona del sitio).
    Compararlos en crudo daría un desfase de la zona horaria."""
    return dt.datetime.fromtimestamp(float(epoch or 0), dt.timezone.utc) \
        .astimezone(_tz_sitio()).replace(tzinfo=None)


def _leer_adjunto(url: str):
    """Delegado a `chatwoot_client.leer_adjunto` — el envío inmediato desde el
    composer necesita la misma lógica, así que vive una sola vez allá."""
    return cw.leer_adjunto(url)


# ── API (whitelisted) ────────────────────────────────────────────────────────

@frappe.whitelist()
def programar_mensaje(conversation_id, contenido, enviar_at, inbox_id=None, adjuntos=None):
    _validar_rol()
    conversation_id = frappe.utils.cint(conversation_id)
    if not conversation_id:
        frappe.throw("Conversación inválida")

    contenido = (contenido or "").strip()
    if isinstance(adjuntos, str):
        try:
            adjuntos = json.loads(adjuntos or "[]")
        except ValueError:
            adjuntos = []
    adjuntos = adjuntos or []
    if not contenido and not adjuntos:
        frappe.throw("El mensaje no puede ir vacío")
    if len(adjuntos) > MAX_ADJUNTOS:
        frappe.throw(f"Máximo {MAX_ADJUNTOS} adjuntos por mensaje")

    enviar_at = frappe.utils.get_datetime(enviar_at)
    if not enviar_at:
        frappe.throw("Fecha y hora inválidas")
    if enviar_at <= now_datetime():
        frappe.throw("La hora programada debe ser futura")

    doc = frappe.get_doc({
        "doctype": "Mensaje Programado",
        "conversation_id": conversation_id,
        "inbox_id": str(inbox_id) if inbox_id else "",
        "contenido": contenido,
        "enviar_at": enviar_at,
        "estado": "Pendiente",
        "programado_por": frappe.session.user,
        "adjuntos": [
            {
                "archivo": a.get("url") or a.get("archivo"),
                "nombre": a.get("nombre") or "",
                "mime": a.get("mime") or "",
            }
            for a in adjuntos
            if (a.get("url") or a.get("archivo"))
        ],
    })
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "enviar_at": str(doc.enviar_at)}


@frappe.whitelist()
def listar_programados(conversation_id):
    _validar_rol()
    conversation_id = frappe.utils.cint(conversation_id)
    if not conversation_id:
        return []
    filas = frappe.get_all(
        "Mensaje Programado",
        filters={"conversation_id": conversation_id, "estado": "Pendiente"},
        fields=["name", "contenido", "enviar_at", "programado_por"],
        order_by="enviar_at asc",
        limit_page_length=20,
    )
    for f in filas:
        f["adjuntos"] = frappe.get_all(
            "Mensaje Programado Adjunto",
            filters={"parent": f["name"]},
            pluck="archivo",
        )
    return filas


@frappe.whitelist()
def cancelar_programado(name):
    _validar_rol()
    doc = frappe.get_doc("Mensaje Programado", name)
    if doc.estado != "Pendiente":
        frappe.throw("Ese mensaje ya no está pendiente")
    doc.db_set({"estado": "Cancelado", "motivo_cancelacion": "Cancelado a mano"})
    return {"ok": True}


# ── Job ──────────────────────────────────────────────────────────────────────

def enviar_programados():
    """Cron cada minuto. Sale en la primera línea si no hay nada vencido, así
    que no cuesta nada cuando nadie está usando la función."""
    ahora = now_datetime()
    nombres = frappe.get_all(
        "Mensaje Programado",
        filters={"estado": "Pendiente", "enviar_at": ["<=", ahora]},
        pluck="name",
        order_by="enviar_at asc",
        limit_page_length=LOTE,
    )
    for name in nombres:
        try:
            _procesar(frappe.get_doc("Mensaje Programado", name))
            frappe.db.commit()
        except Exception:
            traza = frappe.get_traceback()
            frappe.log_error(traza, f"Mensaje Programado {name}")
            frappe.db.set_value("Mensaje Programado", name,
                                {"estado": "Error", "error": traza[-500:]})
            frappe.db.commit()


def _procesar(doc):
    if _contacto_respondio(doc):
        doc.db_set({
            "estado": "Cancelado",
            "motivo_cancelacion": "El contacto escribió antes de la hora programada",
        })
        return
    _enviar(doc)


def _contacto_respondio(doc) -> bool:
    """¿Entró algún mensaje del cliente después de que se programó?

    Se decide al momento de enviar, no al recibir: si el contacto contesta y el
    programado ya venció, se cancela en esa corrida. El costo es que entre la
    respuesta y la hora programada el mensaje sigue apareciendo "Pendiente" en
    la UI; el desenlace es el mismo.
    """
    try:
        crudo = cw.list_messages(int(doc.conversation_id))
    except Exception as exc:
        # No se pudo leer: ante la duda se envía. Un mensaje de más es menos
        # grave que perder la programación por un fallo de red pasajero.
        frappe.log_error(f"programados: no pude leer conv {doc.conversation_id}: {exc}",
                         "Mensaje Programado")
        return False
    programado_en = frappe.utils.get_datetime(doc.creation)
    for m in (crudo.get("payload") or []):
        if m.get("private"):
            continue
        if m.get("message_type") in (0, "incoming"):
            if _epoch_a_local(m.get("created_at")) > programado_en:
                return True
    return False


def _enviar(doc):
    texto = (doc.contenido or "").strip()
    archivos = []
    for a in doc.adjuntos:
        leido = _leer_adjunto(a.archivo)
        if leido:
            archivos.append(leido)
        else:
            frappe.log_error(f"programados: adjunto ilegible {a.archivo}",
                             f"Mensaje Programado {doc.name}")

    marca = {"humano": True, "programado": True}
    if archivos:
        cw.create_message_with_attachments(int(doc.conversation_id), texto,
                                           archivos=archivos, content_attributes=marca)
    else:
        cw.create_message(int(doc.conversation_id), texto, content_attributes=marca)

    doc.db_set({"estado": "Enviado", "enviado_at": now_datetime()})
