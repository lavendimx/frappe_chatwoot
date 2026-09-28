"""Custom fields de las notas de Gemini → CRM (job utils/notas_gemini.py).

Idempotente. Crea:

  - Reunion Agendada.notas_gemini_url / .notas_gemini_msg_id / .notas_gemini_at
    — el link del Doc, el msg_id dedup y la marca de procesado.
  - Chatwoot Settings.notas_gemini_activo — el interruptor. Nace apagado.

Correr:  bench --site crm.lavendi.mx console < este_archivo
"""

import frappe

CAMPOS = [
    {
        "dt": "Reunion Agendada",
        "fieldname": "notas_gemini_url",
        "label": "Notas Gemini (URL)",
        "fieldtype": "Data",
        "insert_after": "resumen_generado_at",
        "read_only": 1,
    },
    {
        "dt": "Reunion Agendada",
        "fieldname": "notas_gemini_msg_id",
        "label": "Notas Gemini (Message ID)",
        "fieldtype": "Data",
        "insert_after": "notas_gemini_url",
        "read_only": 1,
    },
    {
        "dt": "Reunion Agendada",
        "fieldname": "notas_gemini_at",
        "label": "Notas Gemini (procesado)",
        "fieldtype": "Datetime",
        "insert_after": "notas_gemini_msg_id",
        "read_only": 1,
    },
    {
        "dt": "Chatwoot Settings",
        "fieldname": "notas_gemini_activo",
        "label": "Notas de Gemini activas",
        "fieldtype": "Check",
        "default": "0",
        "insert_after": "resumen_contacto_activo",
        "description": "Trae los correos de gemini-notes@ ya parseados por el agente-ia "
                       "y los escribe al CRM: nota de resumen + comentario con el Doc + "
                       "una tarea por próximo paso. Apagado hasta validar.",
    },
]


def ejecutar():
    creados = []
    for campo in CAMPOS:
        nombre = f"{campo['dt']}-{campo['fieldname']}"
        if frappe.db.exists("Custom Field", nombre):
            continue
        frappe.get_doc({"doctype": "Custom Field", **campo}).insert(ignore_permissions=True)
        creados.append(nombre)
    frappe.db.commit()
    frappe.clear_cache()
    return {"creados": creados}
