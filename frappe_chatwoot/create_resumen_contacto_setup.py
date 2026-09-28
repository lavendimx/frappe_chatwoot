"""Custom fields del quick summary IA por contacto (job utils/resumen_contacto.py).

Idempotente. Crea:

  - Reunion Agendada.resumen_generado_at — idempotencia del job. Sin ella, el
    barrido regeneraría la nota en cada corrida.
  - Chatwoot Settings.resumen_contacto_activo — el interruptor. Nace apagado:
    se enciende a mano después de validar contra citas reales.

Correr:  bench --site crm.lavendi.mx console < este_archivo
"""

import frappe

CAMPOS = [
    {
        "dt": "Reunion Agendada",
        "fieldname": "resumen_generado_at",
        "label": "Resumen generado",
        "fieldtype": "Datetime",
        "insert_after": "planeacion_generada_at",
        "read_only": 1,
    },
    {
        "dt": "Chatwoot Settings",
        "fieldname": "resumen_contacto_activo",
        "label": "Resumen de contacto activo",
        "fieldtype": "Check",
        "default": "0",
        "insert_after": "planeacion_llamadas_activo",
        "description": "Genera una nota de resumen IA ('Resumen — <fecha>') en la "
                       "ficha del contacto al terminar cada cita. Apagado hasta "
                       "validar contra citas reales.",
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
