# Copyright (c) 2026, lavendi.mx
"""Campos contacto / organización / oportunidad en `CRM Task` (idempotente).

`CRM Task` solo trae un vínculo (`reference_doctype`/`reference_docname`), así
que en el panel de Tareas las tareas se ven aisladas. Estos campos las ligan al
contacto, la organización y la oportunidad, y los autollena
`frappe_chatwoot.utils.tareas.autollenar` (doc_event `validate`).

Uso: bench --site crm.lavendi.mx execute agregar_campos_tarea.run
"""

import frappe

CAMPOS = [
    ("contacto", "Contact", "Contacto", 0),
    ("organizacion", "CRM Organization", "Organización", 1),
    ("deal", "CRM Deal", "Oportunidad", 1),
]


def run():
    for fieldname, options, label, en_lista in CAMPOS:
        existente = frappe.db.get_value(
            "Custom Field", {"dt": "CRM Task", "fieldname": fieldname}, "name"
        )
        if existente:
            print(f"ya existe: {fieldname}")
            continue
        frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "CRM Task",
            "fieldname": fieldname,
            "label": label,
            "fieldtype": "Link",
            "options": options,
            "insert_after": "reference_docname",
            "in_list_view": en_lista,
            "in_standard_filter": 1,
        }).insert(ignore_permissions=True)
        print(f"creado: {fieldname} -> {options}")
    frappe.db.commit()
    print("listo")
