"""Campos que necesita el agendamiento manual desde el CRM (2026-09-10).

Idempotente: se puede volver a correr. Se copia a
`apps/frappe_chatwoot/frappe_chatwoot/agenda_setup.py` dentro del contenedor y se corre con

    bench --site crm.lavendi.mx execute frappe_chatwoot.agenda_setup.ejecutar

No usar `bench console` con este archivo: el console de IPython pierde las definiciones de
función al leer el script por stdin y truena con NameError.

Dos grupos:

1. `Reunion Agendada` — hasta hoy todas las citas las creaba el agente IA, así que no
   había por qué distinguir origen. Con el alta manual sí: quién agendó cambia a quién se
   le pregunta si algo no cuadra. `origen` default "agente" para que los registros ya
   existentes y el agente (que no manda el campo) queden bien clasificados sin migración.

2. `Chatwoot Settings` — dónde vive el agente Node y con qué token hablarle. Van aquí y no
   en el .env por el mismo motivo que la config de Evolution: el contenedor de Frappe no
   comparte el .env del host.
"""

import frappe

CAMPOS_REUNION = [
    {
        "fieldname": "origen",
        "fieldtype": "Select",
        "label": "Origen",
        "options": "agente\nmanual",
        "default": "agente",
        "in_list_view": 1,
        "insert_after": "meet_link",
    },
    {
        "fieldname": "crm_contacto",
        "fieldtype": "Link",
        "label": "Contacto del CRM",
        "options": "Contact",
        "insert_after": "origen",
    },
]

CAMPOS_SETTINGS = [
    {
        "fieldname": "agenda_url",
        "fieldtype": "Data",
        "label": "URL del agente (agendamiento)",
        "description": "Base del proceso agente-ia que habla con Google Calendar. Ej. http://10.0.3.1:8095",
        "insert_after": "evolution_api_key",
    },
    {
        "fieldname": "agenda_token",
        "fieldtype": "Password",
        "label": "Token de agendamiento",
        "description": "Debe coincidir con AGENDA_TOKEN del .env de agente-ia.",
        "insert_after": "agenda_url",
    },
]


def agregar_campos(doctype: str, campos: list[dict]) -> None:
    for campo in campos:
        if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": campo["fieldname"]}):
            print(f"{doctype}.{campo['fieldname']} ya existe")
            continue
        doc = frappe.get_doc({"doctype": "Custom Field", "dt": doctype, **campo})
        doc.insert(ignore_permissions=True)
        print(f"{doctype}.{campo['fieldname']} creado")


def ejecutar() -> None:
    agregar_campos("Reunion Agendada", CAMPOS_REUNION)
    agregar_campos("Chatwoot Settings", CAMPOS_SETTINGS)
    # Las citas que ya existían son todas del agente; sin esto quedarían con origen vacío
    # y la tabla las mostraría como si nadie supiera de dónde salieron.
    frappe.db.sql(
        "UPDATE `tabReunion Agendada` SET origen = 'agente' WHERE origen IS NULL OR origen = ''"
    )
    frappe.db.commit()
    print("Listo")
