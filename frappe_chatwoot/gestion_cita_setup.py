"""Campos para que el cliente pueda cancelar o reagendar su cita él solo (2026-09-13).

Idempotente. Se copia a `apps/frappe_chatwoot/frappe_chatwoot/gestion_cita_setup.py`
dentro del contenedor y se corre con

    bench --site crm.lavendi.mx execute frappe_chatwoot.gestion_cita_setup.ejecutar

No usar `bench console` con este archivo: el console de IPython pierde las definiciones
de función al leer el script por stdin y truena con NameError.

POR QUÉ
    La agenda pública que reemplazó a los 3 widgets de GHL (11-sep) sabe reservar, pero
    no cancelar ni reagendar — y GHL sí lo hacía. Hoy todo cambio de cita cae en WhatsApp
    manual: el cliente escribe, alguien lo lee, alguien entra a Google Calendar. Es la
    mayor brecha funcional que quedaba frente a GHL.

    `token_publico` es la llave que le deja al cliente tocar SU cita sin login. Es
    aleatorio de 128 bits, no derivable del nombre ni del correo: con el id del documento
    (`REUNION-00042`) cualquiera cancelaría la cita del siguiente.

    `perfil` hace falta para reagendar: las reglas (duración, horario, buffer, cupo) son
    distintas por perfil y hasta hoy no se guardaban — la cita solo recordaba su hora, no
    con qué reglas se pactó.
"""

import frappe

CAMPOS_REUNION = [
    {
        "fieldname": "token_publico",
        "fieldtype": "Data",
        "label": "Token público",
        "description": "Llave con la que el participante gestiona su cita sin login. No compartir.",
        "read_only": 1,
        "insert_after": "crm_contacto",
    },
    {
        "fieldname": "perfil",
        "fieldtype": "Data",
        "label": "Perfil de agenda",
        "description": "leads / clientes / entrevista. Define las reglas al reagendar.",
        "read_only": 1,
        "insert_after": "token_publico",
    },
]


def agregar_campos(doctype: str, campos: list) -> None:
    for campo in campos:
        if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": campo["fieldname"]}):
            print(f"{doctype}.{campo['fieldname']} ya existe")
            continue
        frappe.get_doc({"doctype": "Custom Field", "dt": doctype, **campo}).insert(
            ignore_permissions=True)
        print(f"{doctype}.{campo['fieldname']} creado")


def ejecutar() -> None:
    agregar_campos("Reunion Agendada", CAMPOS_REUNION)
    # Las citas que ya existen se quedan SIN token a propósito: darles uno ahora no le
    # serviría a nadie (el link va en la invitación del evento, que ya se envió) y un
    # token que nadie recibió es superficie de ataque sin contrapartida. Si alguna de
    # esas citas hay que moverla, se hace desde el CRM como hasta hoy.
    frappe.db.commit()
    print("Listo")
