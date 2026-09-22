"""Campos del CFDI en la Sales Invoice (2026-09-18). Idempotente.

Se corre con

    bench --site crm.lavendi.mx execute frappe_chatwoot.campos_cfdi_setup.ejecutar

No usar `bench console` con este archivo: el console de IPython pierde las
definiciones de función al leer el script por stdin y truena con NameError
(mismo problema documentado en `campos_pago_setup.py`).

POR QUÉ
    Hasta hoy la cuenta por cobrar no sabía si ya tenía CFDI. La única forma de
    averiguarlo era buscar a mano en Gmail el correo con el XML adjunto — es
    literalmente lo que tuvo que hacer la sesión de `cfo` con la factura de
    Félix/Estrublock el 18-sep. Sin este dato, cualquier automatismo que se
    acerque a facturar corre el riesgo de emitir un CFDI duplicado, y un CFDI
    timbrado es caro de cancelar.

    Paso 1 del plan `planes/facturacion-cfdi-de-detectar-a-ejecutar.md`.

POR QUÉ `cfdi_uuid` ES UNA LISTA Y NO UN CAMPO SIMPLE
    Porque el primer caso real que se probó ya lo rompe: `ACC-SINV-2026-00025`
    (Félix, $59,400) se facturó en dos mitades de $29,700 — dos CFDI para UNA
    factura. Un campo `Data` con un solo UUID habría dicho "ya facturada" con la
    mitad sin timbrar, que es el error opuesto y peor: deja de avisar.

    Formato de cada línea, pensado para parsear sin ambigüedad (el UUID y la
    fecha ISO no contienen `|`):

        <uuid>|<fecha_timbrado ISO>|<total>|<PUE|PPD>

    Los tres campos escalares (`cfdi_total`, `cfdi_metodo_pago`,
    `cfdi_fecha_timbrado`) son derivados de esa lista — existen para poder
    filtrar y reportar sin parsear texto en cada consulta. La lista es la
    fuente de verdad; se recalculan juntos en `api/facturacion.marcar_cfdi`.

POR QUÉ `read_only` Y NO UN CAMPO EDITABLE
    Nadie debe teclear un folio fiscal a mano: o lo escribe quien timbró, o no
    se escribe. `allow_on_submit` es indispensable — la factura ya está
    presentada (docstatus=1) cuando se emite el CFDI.

NOTA — estos campos NO viajan a sitios de cliente
    `hooks.py` solo exporta fixtures de Sales Invoice para... nada: la lista
    `DOCTYPES_ERPNext` contiene solo `Customer`. Es el mismo estado que ya tenía
    `token_pago`. Es correcto para este caso: la cartera que se factura desde
    guatson es la de lavendi.mx, que vive únicamente en crm.lavendi.mx. Si algún
    día un cliente factura desde su propio sitio, hay que meter "Sales Invoice"
    en `DOCTYPES_ERPNext` y re-exportar.
"""

import frappe

CAMPOS_SALES_INVOICE = [
    {
        "fieldname": "cfdi_seccion",
        "fieldtype": "Section Break",
        "label": "Datos fiscales (CFDI)",
        "collapsible": 1,
        "insert_after": "token_pago",
    },
    {
        "fieldname": "cfdi_uuid",
        "fieldtype": "Small Text",
        "label": "Folios fiscales (UUID)",
        "description": (
            "Una línea por CFDI timbrado: uuid|fecha|total|metodo. "
            "Append-only: lo escribe quien timbra, nadie lo edita ni lo borra."
        ),
        "read_only": 1,
        "allow_on_submit": 1,
        "insert_after": "cfdi_seccion",
    },
    {
        "fieldname": "cfdi_total",
        "fieldtype": "Currency",
        "label": "Total timbrado en CFDI",
        "description": (
            "Suma de los CFDI emitidos contra esta factura. Si es menor al total "
            "de la factura, queda saldo sin timbrar."
        ),
        "read_only": 1,
        "allow_on_submit": 1,
        "insert_after": "cfdi_uuid",
    },
    {
        "fieldname": "cfdi_metodo_pago",
        "fieldtype": "Select",
        "label": "Método de pago del CFDI",
        "options": "\nPUE\nPPD",
        "description": (
            "PPD gana si algún CFDI de esta factura es PPD: es el que obliga a "
            "emitir complemento de pago por cada pago recibido."
        ),
        "read_only": 1,
        "allow_on_submit": 1,
        "insert_after": "cfdi_total",
    },
    {
        "fieldname": "cfdi_fecha_timbrado",
        "fieldtype": "Datetime",
        "label": "Último timbrado",
        "read_only": 1,
        "allow_on_submit": 1,
        "insert_after": "cfdi_metodo_pago",
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
    agregar_campos("Sales Invoice", CAMPOS_SALES_INVOICE)

    # El índice es lo que hace baratas las dos preguntas que este campo existe
    # para responder: "¿esta factura ya tiene CFDI?" y "¿este UUID ya está
    # pegado a otra factura?" (la guarda anti-doble-factura de `marcar_cfdi`).
    # `cfdi_uuid` es Small Text -> longtext en MySQL, que NO acepta índice sin
    # prefijo; se indexa `cfdi_total`, que es el campo por el que se filtra
    # "sin timbrar", y la búsqueda por UUID va por LIKE sobre un universo de
    # ~230 facturas (barato y medido).
    if not frappe.db.has_index("tabSales Invoice", "cfdi_total_index"):
        frappe.db.add_index("Sales Invoice", ["cfdi_total"], index_name="cfdi_total_index")
        print("Índice cfdi_total creado")
    else:
        print("Índice cfdi_total ya existe")

    frappe.db.commit()
    print("Listo")
