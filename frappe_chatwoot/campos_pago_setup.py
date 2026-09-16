"""Campos para el link de pago corto y para etiquetar el plan SofÍA GPT de un
cliente (2026-09-14). Idempotente.

Se copia a `apps/frappe_chatwoot/frappe_chatwoot/campos_pago_setup.py` dentro
del contenedor y se corre con

    bench --site crm.lavendi.mx execute frappe_chatwoot.campos_pago_setup.ejecutar

No usar `bench console` con este archivo: el console de IPython pierde las
definiciones de función al leer el script por stdin y truena con NameError
(mismo problema documentado en `agregar_campos_gestion_cita.py`).

POR QUÉ
    `token_pago` es la llave del link corto de pago (`/pagar?f=<token>`, ver
    `api/stripe_pagos.py`) — reemplaza el link crudo de Stripe
    (`checkout.stripe.com/c/pay/cs_live_...`), largo y sin marca, que un
    cliente puede confundir con phishing. Aleatorio de 96 bits, no derivable
    del nombre de la factura, mismo criterio que `token_publico` de
    `Reunion Agendada`.

    `plan_sgpt` en Customer es la asociación que faltaba entre un cliente y su
    plan de SofÍA GPT — hasta hoy solo vivía implícito en el monto de la
    factura ($2,000, $3,750...). Sirve para los 3 clientes migrados de GHL con
    el precio especial "in house" (Ena Barrera, Ena Paulina Moreno, Rafael
    Cardeño Oficial/medicare.mx — confirmado contra ~18 meses de facturas cada
    uno) y para cualquier cliente futuro de los planes de autoservicio.
"""

import frappe

CAMPOS_SALES_INVOICE = [
    {
        "fieldname": "token_pago",
        "fieldtype": "Data",
        "label": "Token de pago",
        "description": "Llave del link corto /pagar?f=. Se genera solo, la primera vez que se pide un link.",
        "read_only": 1,
        "allow_on_submit": 1,
        "insert_after": "remarks",
    },
]

CAMPOS_CUSTOMER = [
    {
        "fieldname": "plan_sgpt",
        "fieldtype": "Select",
        "label": "Plan SofÍA GPT",
        "options": "\nstarter\nilimitados\nenterprise\nin_house",
        "description": "Solo si el cliente contrata SofÍA GPT propio (no servicios de agencia). Vacío si no aplica.",
        "insert_after": "customer_name",
    },
]

# Los 3 clientes con el precio especial "in house" ($2,000/mes), verificados
# el 2026-09-14 contra sus facturas migradas de GHL (~18 meses cada uno).
CLIENTES_IN_HOUSE = [
    "Ena Barrera",
    "Ena Paulina Moreno",
    "Rafael Cardeño Oficial",
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
    agregar_campos("Sales Invoice", CAMPOS_SALES_INVOICE)
    agregar_campos("Customer", CAMPOS_CUSTOMER)

    # Las facturas ya emitidas se quedan sin token a propósito — se genera
    # solo, la primera vez que alguien pide un link de pago para esa factura
    # puntual (ver `stripe_pagos._token_pago`).
    if not frappe.db.has_index("tabSales Invoice", "token_pago_index"):
        frappe.db.add_index("Sales Invoice", ["token_pago"], index_name="token_pago_index")
        print("Índice token_pago creado")

    for cliente in CLIENTES_IN_HOUSE:
        if not frappe.db.exists("Customer", cliente):
            print(f"Customer '{cliente}' no existe — omitido")
            continue
        if frappe.db.get_value("Customer", cliente, "plan_sgpt") == "in_house":
            print(f"{cliente} ya estaba etiquetado")
            continue
        frappe.db.set_value("Customer", cliente, "plan_sgpt", "in_house", update_modified=False)
        print(f"{cliente} etiquetado como in_house")

    frappe.db.commit()
    print("Listo")
