# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Cobranza: el rol `Autorizador Cobranza` y el campo `motivo_cancelacion`
(2026-09-23). Idempotente.

Se corre con

    bench --site crm.lavendi.mx execute frappe_chatwoot.cobranza_setup.ejecutar
    bench --site sofiav2.lavendi.mx execute frappe_chatwoot.cobranza_setup.ejecutar

No usar `bench console` con este archivo: el console de IPython pierde las
definiciones de función al leer el script por stdin y truena con NameError
(mismo problema documentado en `campos_cfdi_setup.py`).

POR QUÉ
    Cancelar un cargo anula un documento contable ya emitido: no puede quedar al
    alcance de cualquier usuario que vea la cartera. Alejandro definió un grupo de
    aprobación explícito (2026-09-23); aquí se materializa como un rol de Frappe
    — no se inventa un sistema de usuarios — y `api/facturacion.cancelar_factura`
    lo exige. El campo `motivo_cancelacion` guarda el porqué junto a la factura.

CÓMO ASIGNAR EL ROL A OTROS USUARIOS

    bench --site crm.lavendi.mx execute frappe_chatwoot.cobranza_setup.asignar \\
        --kwargs "{'user': 'zaira@lavendi.mx'}"

    El rol también se puede asignar desde el Desk (User > Roles).

POR QUÉ ESTOS SON POR-SITIO Y NO FIXTURES
    `motivo_cancelacion` es un Custom Field de `Sales Invoice`, y los campos de
    ERPNext no viajan como fixture (ver `campos_cfdi_setup.py`): la cartera que se
    factura vive en crm.lavendi.mx y su espejo sofiav2. Un sitio de cliente que
    algún día facture necesita correr este script también.
"""

import frappe

ROL = "Autorizador Cobranza"
USUARIO_POR_DEFECTO = "alejandro.moreno@lavendi.mx"

CAMPO_MOTIVO = {
    "dt": "Sales Invoice",
    "fieldname": "motivo_cancelacion",
    "fieldtype": "Long Text",
    "label": "Motivo de cancelación",
    "description": (
        "Por qué se anuló esta factura. Lo escribe `api/facturacion.cancelar_factura` "
        "al cancelar; append-only desde la interfaz (solo lectura)."
    ),
    "read_only": 1,
    "allow_on_submit": 1,
}


def crear_rol() -> None:
    if frappe.db.exists("Role", ROL):
        print(f"Rol '{ROL}' ya existe")
        return
    frappe.get_doc({
        "doctype": "Role",
        "role_name": ROL,
        "desk_access": 1,
        "description": (
            "Grupo de aprobación de cobranza: puede anular cargos/facturas "
            "emitidas desde el CRM (api.facturacion.cancelar_factura)."
        ),
    }).insert(ignore_permissions=True)
    print(f"Rol '{ROL}' creado")


def crear_campo_motivo() -> None:
    if frappe.db.exists("Custom Field", {"dt": "Sales Invoice",
                                         "fieldname": "motivo_cancelacion"}):
        print("Sales Invoice.motivo_cancelacion ya existe")
        return
    campo = dict(CAMPO_MOTIVO)
    # Colocarlo junto a los datos fiscales si el setup de CFDI ya corrió; si no,
    # se deja al final (insert_after inexistente lo mandaría igual al final).
    if frappe.db.exists("Custom Field", {"dt": "Sales Invoice",
                                         "fieldname": "cfdi_fecha_timbrado"}):
        campo["insert_after"] = "cfdi_fecha_timbrado"
    frappe.get_doc({"doctype": "Custom Field", **campo}).insert(ignore_permissions=True)
    print("Sales Invoice.motivo_cancelacion creado")


def asignar(user: str | None = None) -> None:
    user = (user or USUARIO_POR_DEFECTO).strip()
    if not frappe.db.exists("User", user):
        print(f"El usuario '{user}' no existe en este sitio; no se asignó nada")
        return
    if ROL in frappe.get_roles(user):
        print(f"'{user}' ya tiene el rol '{ROL}'")
        return
    doc = frappe.get_doc("User", user)
    doc.append("roles", {"role": ROL})
    doc.save(ignore_permissions=True)
    print(f"Rol '{ROL}' asignado a '{user}'")


def ejecutar(usuario: str | None = None) -> None:
    crear_rol()
    crear_campo_motivo()
    asignar(usuario)
    frappe.db.commit()
    print("Listo")
