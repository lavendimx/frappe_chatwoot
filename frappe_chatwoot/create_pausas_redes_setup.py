"""Custom Field de la expiración automática de pausas (tarea de plataforma
2026-09-24). Idempotente. Crea:

  - Chatwoot Settings.expirar_pausas_activo — el interruptor del job
    `frappe_chatwoot.utils.pausas.expirar_pausas`. Nace apagado: hay ~20
    `Chatwoot Pausa` reales acumuladas (algunas de semanas) que resucitarían
    al agente de golpe en la primera corrida si el switch naciera encendido.
    Ver el docstring de utils/pausas.py.

Correr:  bench --site crm.lavendi.mx execute frappe_chatwoot.create_pausas_redes_setup.ejecutar
"""

import frappe

CAMPOS = [
    {
        "dt": "Chatwoot Settings",
        "fieldname": "expirar_pausas_activo",
        "label": "Auto-expirar pausas humanas",
        "fieldtype": "Check",
        "default": "0",
        "insert_after": "correo_secuencias_via_frappe",
        "description": "Levanta sola una `Chatwoot Pausa` pasados los minutos "
                       "configurados en `Agente IA.minutos_pausa_humana` del "
                       "inbox de esa conversación. Apagado hasta revisar las "
                       "pausas ya acumuladas — ver utils/pausas.py.",
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
