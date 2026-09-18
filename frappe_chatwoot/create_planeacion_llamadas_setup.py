"""Custom fields de la preparación de llamada v2 (reemplazo del script GHL
`preparacion-llamadas/` de `/root/projects/ventas`, ya retirado).

Idempotente. Crea:

  - Reunion Agendada.planeacion_generada_at — idempotencia del job. Sin ella,
    el barrido horario regeneraría la nota en cada corrida.
  - Chatwoot Settings.planeacion_llamadas_activo — el interruptor. Nace
    apagado: se encien de a mano después de validar contra las citas futuras
    reales (Adolfo Díaz 25-sep, Ena Barrera 28-sep).

Correr:  bench --site crm.lavendi.mx console < este_archivo
"""

import frappe

CAMPOS = [
    {
        "dt": "Reunion Agendada",
        "fieldname": "planeacion_generada_at",
        "label": "Planeación generada",
        "fieldtype": "Datetime",
        "insert_after": "recordatorio_enviado_at",
        "read_only": 1,
    },
    {
        "dt": "Chatwoot Settings",
        "fieldname": "planeacion_llamadas_activo",
        "label": "Preparación de llamadas activa",
        "fieldtype": "Check",
        "default": "0",
        "insert_after": "recordatorio_citas_activo",
        "description": "Genera una nota de planeación (hoja 0-6) en la ficha del "
                       "contacto al detectar cada cita nueva. Apagado hasta validar "
                       "contra citas reales.",
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
