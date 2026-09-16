# Copyright (c) 2026, lavendi.mx
"""Campos de secuencia denormalizados en `CRM Deal` (idempotente).

POR QUÉ
    El detalle de la oportunidad y el panel lateral de Conversaciones leen el
    `CRM Deal`, no el `Secuencia Inscripcion`. Para mostrar "está enrolado en X,
    paso N de M, estado Y" sin forkear `Deal.vue` (upstream: un rebuild del
    contenedor lo revierte), el motor escribe un resumen en estos 3 campos y el
    CRM los pinta nativos desde el layout del Side Panel.

    Los llena `frappe_chatwoot.utils.secuencias` (helper `_reflejar_en_deal`,
    invocado por TODAS las escrituras de estado) y los rellena el backfill
    `secuencias.reflejar_todas()`.

⚠ SEGUNDO LUGAR DONDE VIVE LA VERDAD
    Si un call site del motor olvida reflejar el cambio, el campo miente en
    silencio. Mitigado con un helper único + backfill + chequeo en el vigilante.

Uso: bench --site crm.lavendi.mx execute agregar_campos_secuencia_deal.run
"""

import json

import frappe

CAMPOS = [
    # fieldname, label, in_standard_filter
    ("secuencia_actual", "Secuencia actual", 1),
    ("secuencia_paso", "Paso en la secuencia", 0),
    ("secuencia_estado", "Estado en la secuencia", 1),
]

SECCION = {
    "label": "Secuencia de seguimiento",
    "name": "secuencia_section",
    "opened": True,
    "columns": [
        {
            "name": "column_secuencia",
            "fields": ["secuencia_actual", "secuencia_paso", "secuencia_estado"],
        }
    ],
}


def run():
    for fieldname, label, en_filtro in CAMPOS:
        if frappe.db.get_value("Custom Field", {"dt": "CRM Deal", "fieldname": fieldname}, "name"):
            print(f"ya existe: {fieldname}")
            continue
        frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "CRM Deal",
            "fieldname": fieldname,
            "label": label,
            "fieldtype": "Data",
            # Solo lectura: los escribe el motor, no una persona. Editarlos a
            # mano desincronizaría el campo del `Secuencia Inscripcion`.
            "read_only": 1,
            "insert_after": "ghl_producto",
            "in_standard_filter": en_filtro,
        }).insert(ignore_permissions=True)
        print(f"creado: {fieldname}")

    _insertar_en_side_panel()
    frappe.db.commit()
    frappe.clear_cache(doctype="CRM Deal")
    print("listo")


def _insertar_en_side_panel():
    """Mete la sección "Secuencia de seguimiento" en el layout del Side Panel.

    El layout es una lista plana de secciones; `get_sidepanel_sections` filtra
    `meta.fields` por los fieldnames que aparecen aquí, así que un campo custom
    agregado al layout se pinta nativo. Idempotente: si la sección ya está, no
    la duplica.
    """
    nombre = "CRM Deal-Side Panel"
    if not frappe.db.exists("CRM Fields Layout", nombre):
        print("⚠ no existe el layout del Side Panel; se omite")
        return
    doc = frappe.get_doc("CRM Fields Layout", nombre)
    layout = json.loads(doc.layout or "[]")
    if any(s.get("name") == SECCION["name"] for s in layout):
        print("sección ya presente en el Side Panel")
        return
    layout.append(SECCION)
    doc.layout = json.dumps(layout)
    doc.save(ignore_permissions=True)
    print("sección agregada al Side Panel")
