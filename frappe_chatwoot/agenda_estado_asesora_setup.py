# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Campos `estado` y `asesora` en `Reunion Agendada` — Pieza C del scope de agenda nativa
(planes/scope-agenda-nativa-frappe.md, decisiones #17/#22-#25 del plan de migración de
Estrublock), corre SOLO en el sitio de Estrublock.

Idempotente. Se copia a `apps/frappe_chatwoot/frappe_chatwoot/frappe_chatwoot/agenda_estado_asesora_setup.py`
dentro del contenedor y se corre con

    bench --site estrublock.lavendi.mx execute frappe_chatwoot.agenda_estado_asesora_setup.ejecutar

No usar `bench console` con este archivo: el console de IPython pierde las definiciones de
función al leer el script por stdin y truena con NameError.

POR QUÉ SOLO ESTRUBLOCK
    Los Custom Field son por SITIO, no globales al app (`frappe_chatwoot` corre igual en
    crm.lavendi.mx, sixgardens.lavendi.mx y estrublock.lavendi.mx, pero cada uno tiene su
    propia base de datos y su propio esquema de doctype). Correr esto en crm.lavendi.mx o
    sixgardens.lavendi.mx les añadiría campos que ningún flujo suyo llena ni necesita — no
    es destructivo, pero es ruido sin dueño. Se corre SOLO donde la agenda nativa (fuente
    "frappe" en `AGENDA_CITAS` de agente-ia) se va a usar: Estrublock.

    Sin backfill: al momento de escribir esto, `Reunion Agendada` en estrublock.lavendi.mx
    tiene 0 filas (verificado — el inbox de Chatwoot de Estrublock aún no existe, F3
    bloqueado). No hay nada que migrar.

`estado`
    Réplica de los estados de cita de GHL. Hasta hoy cancelar una cita BORRABA la fila
    (`agente-ia/lib/agenda-fuente.js::cancelFrappe`, antes de este cambio) — sin campo de
    estado no hay forma de distinguir "nunca existió" de "se canceló", y se pierde el
    baseline de no-show (12.8% medido en GHL: showed 101 · confirmed 93 · noshow 30 ·
    cancelled 3) justo en el corte, que es cuando más falta hace para comparar antes/después.
    Default "confirmada": toda cita nueva nace en ese estado sin que el código que la crea
    tenga que declararlo.

`asesora`
    Link a User (= el email, que es como `agente-ia` ya identifica asesoras en
    `AGENDA_CITAS` y en `reasignarDueno()`). Hoy la asesora se infiere indirectamente del
    `calendar_id` del evento; con la agenda nativa no hay Google Calendar del que inferirla,
    así que pasa a ser un campo de primera clase. Sirve también para la vista de calendario
    (Pieza E: color/filtro por asesora) y para el alta manual multi-asesora (Pieza F).

Ripple que este cambio EXIGE en el lado de agente-ia (Node), ya hecho en el mismo commit:
    · `lib/agenda-fuente.js::busyRangesFrappe` excluye `estado = cancelada`.
    · `lib/agenda-fuente.js::cancelFrappe` marca `estado: cancelada` en vez de borrar.
    · `lib/agenda-fuente.js::bookFrappe` escribe `asesora` si se lo pasan.
    · `lib/agenda-publica.js::citasDelDia` y `lib/recordatorio-cita.js::citasEnVentana`
      excluyen `estado = cancelada`, pero SOLO cuando `fuenteDe(inboxId) === 'frappe'` — para
      lavendi.mx y sixgardens (fuente Google, sin este campo) el filtro NUNCA se agrega:
      añadirlo incondicionalmente les rompería la consulta con "Unknown column 'estado'".

⚠ Orden de despliegue: este patch tiene que correr ANTES de que `AGENDA_CITAS` declare
`"fuente": "frappe"` para cualquier inbox. Si se declara antes, `busyRangesFrappe` filtraría
sobre una columna que todavía no existe en ese sitio y la consulta fallaría. Hoy ningún
inbox en producción tiene `fuente: "frappe"` (el gate está apagado), así que no hay carrera.
"""

import frappe

CAMPOS_REUNION = [
    {
        "fieldname": "estado",
        "fieldtype": "Select",
        "label": "Estado",
        "options": "confirmada\nreagendada\ncancelada\nasistio\nno_asistio",
        "default": "confirmada",
        "in_list_view": 1,
        "insert_after": "meet_link",
    },
    {
        "fieldname": "asesora",
        "fieldtype": "Link",
        "options": "User",
        "label": "Asesora",
        "description": "Dueña del calendario en el que vive esta cita (AGENDA_CITAS de agente-ia).",
        "in_list_view": 1,
        "insert_after": "estado",
    },
]


def agregar_campos(doctype: str, campos: list[dict]) -> None:
    for campo in campos:
        if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": campo["fieldname"]}):
            print(f"{doctype}.{campo['fieldname']} ya existe")
            continue
        frappe.get_doc({"doctype": "Custom Field", "dt": doctype, **campo}).insert(
            ignore_permissions=True)
        print(f"{doctype}.{campo['fieldname']} creado")


def ejecutar() -> None:
    agregar_campos("Reunion Agendada", CAMPOS_REUNION)
    frappe.db.commit()
    print("Listo")
