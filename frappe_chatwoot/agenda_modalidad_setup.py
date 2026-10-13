# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Campos `modalidad` y `ubicacion` en `Reunion Agendada` — reuniones presenciales.

Hasta hoy toda cita del CRM se asume videollamada: `agente-ia/lib/calendar.js::bookMeeting`
creaba SIEMPRE `conferenceData` de Meet y `utils/recordatorios.py::_mensaje` hardcodeaba
"confirmando nuestra videollamada ... el enlace para unirte es ...". Cuando una reunión se
pacta en persona, ese flujo manda una liga de Meet que no existe y un recordatorio que le
dice al cliente que es videollamada — que es justo lo que hay que evitar.

`modalidad`
    Select `videollamada` (default) | `presencial`. Default = videollamada para que TODA
    cita que no lo declare (el agente `agendar_reunion`, la agenda pública, la fuente
    `frappe` de Estrublock — que es llamada telefónica) siga exactamente igual que hoy.
    Frappe aplica el default también a los POST de API que no mandan el campo, así que el
    código que crea citas no necesita declararlo.

`ubicacion`
    Dirección de la reunión presencial (opcional). Va al `location` del evento de Google y,
    si está, al texto del recordatorio de WhatsApp. Solo se muestra en el formulario cuando
    `modalidad == presencial`.

Idempotente. Se corre en cada sitio con:

    bench --site <site> execute frappe_chatwoot.agenda_modalidad_setup.ejecutar

POR QUÉ EN TODOS LOS SITIOS CON EL CRM
    Los Custom Field son por SITIO (cada uno tiene su propia base). El diálogo
    `NuevaCitaDialog.vue` es código compartido y manda `modalidad` cuando la cita es
    presencial; `citas_de_contacto()` la lee en el panel de Conversaciones. Un sitio sin el
    campo rechazaría el POST con "Unknown column". Es aditivo y sin backfill: las citas
    existentes quedan con `modalidad` vacío, que se trata como videollamada.
"""

import frappe

CAMPOS_REUNION = [
    {
        "fieldname": "modalidad",
        "fieldtype": "Select",
        "label": "Modalidad",
        "options": "videollamada\npresencial",
        "default": "videollamada",
        "description": "Presencial = sin liga de Meet y recordatorio en texto presencial.",
        "in_list_view": 1,
        "insert_after": "meet_link",
    },
    {
        "fieldname": "ubicacion",
        "fieldtype": "Data",
        "label": "Ubicación",
        "description": "Dirección de la reunión presencial (opcional).",
        "depends_on": "eval:doc.modalidad=='presencial'",
        "insert_after": "modalidad",
    },
]


def agregar_campos(doctype: str, campos: list[dict]) -> None:
    if not frappe.db.exists("DocType", doctype):
        print(f"{doctype} no existe en este sitio: se omite")
        return
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
