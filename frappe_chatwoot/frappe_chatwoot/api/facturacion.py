"""Módulo de facturación del nuevo Sofía — API para la página /crm/facturacion.

Añadido por lavendi.mx; no es upstream de Frappe CRM ni de ERPNext.

POR QUÉ EXISTE
    La cartera ya vive en ERPNext (226 facturas, 218 pagos, migradas de GHL el
    10-sep) y el espejo horario la mantiene al día. Pero los datos solo eran
    alcanzables por la interfaz Desk de ERPNext (/app/sales-invoice): otra UI,
    en inglés parcial, con todo el aparato contable a la vista. El equipo no
    tenía forma de ver "cuánto nos deben" desde el CRM que usa a diario.

    Esta API expone lo mínimo operativo — lo que hacía GHL: qué se facturó,
    cuánto se cobró, quién debe, y registrar un pago recibido. Todo lo demás
    (asientos, conciliación, reportes contables) se sigue haciendo en el Desk
    de ERPNext, que es la herramienta correcta para eso.

    El identificador de GHL de cada factura quedó en `remarks` durante la
    migración (`... — orden <order_number> — ghl_invoice_id <id>`), no en un
    campo propio: de ahí se saca el folio que el equipo reconoce.
"""

import re

import frappe

# Contabilidad y dirección comercial. Un Sales User cualquiera no ve la cartera
# completa de la agencia: es información de negocio, no de su pipeline.
ALLOWED_ROLES = ["System Manager", "Accounts Manager", "Accounts User", "Sales Manager"]

# Los filtros que el equipo realmente pide, no los 7 status de ERPNext.
FILTROS = {
    "por_cobrar": ["Overdue", "Unpaid", "Partly Paid"],
    "vencidas": ["Overdue"],
    "pagadas": ["Paid"],
    "todas": None,
}

ESTADO_ES = {
    "Paid": "Pagada",
    "Overdue": "Vencida",
    "Unpaid": "Sin pagar",
    "Partly Paid": "Pago parcial",
    "Return": "Devolución",
    "Credit Note Issued": "Nota de crédito",
    "Draft": "Borrador",
    "Cancelled": "Cancelada",
}

# Los modos vienen en inglés desde los masters de ERPNext ('Wire Transfer', 'Cash'…).
# El nombre en inglés es la llave contable con la que están amarradas las cuentas de
# banco — se traduce solo para mostrar, nunca se manda el valor traducido a ERPNext.
MODOS_ES = {
    "Wire Transfer": "Transferencia (SPEI)",
    "Cash": "Efectivo",
    "Cheque": "Cheque",
    "Credit Card": "Tarjeta de crédito",
    "Bank Draft": "Giro bancario",
}


def validate_role():
    roles = frappe.get_roles(frappe.session.user)
    if not any(r in roles for r in ALLOWED_ROLES) and frappe.session.user != "Administrator":
        frappe.throw("Sin permiso para consultar facturación", frappe.PermissionError)


def _moldes() -> set:
    """Facturas que sirven de plantilla a una recurrente. Una sola consulta —
    son una decena, no vale la pena consultarlas fila por fila."""
    return {
        r.reference_document
        for r in frappe.get_all(
            "Auto Repeat", filters={"reference_doctype": "Sales Invoice"},
            fields=["reference_document"], limit_page_length=0,
        )
        if r.reference_document
    }


def _folio_ghl(remarks: str | None, auto_repeat: str | None = None,
               factura: str = None, moldes: set = None) -> str | None:
    """El número de orden de GHL, que es el folio que el equipo y el cliente
    conocen. El nombre de ERPNext (ACC-SINV-2026-000xx) no le dice nada a nadie
    todavía.

    Se ignora en las facturas *generadas* por una recurrente: Auto Repeat copia
    `remarks` del documento de referencia a propósito (lo fuerza en `update_doc`
    aunque el campo sea `no_copy`), así que una factura generada heredaría el
    folio de GHL de la factura que le sirvió de molde — dos facturas distintas
    mostrando el mismo folio.

    La molde misma sí conserva su folio: Frappe le escribe `auto_repeat` al
    documento de referencia (`update_auto_repeat_id`), así que ese campo por sí
    solo no distingue "generada" de "plantilla".
    """
    if not remarks:
        return None
    if auto_repeat and not (factura and factura in (moldes if moldes is not None else _moldes())):
        return None
    m = re.search(r"orden\s+([^\s—]+)", remarks)
    return m.group(1) if m else None


@frappe.whitelist()
def resumen() -> dict:
    """Cifras de arriba de la página. Una sola consulta: el dashboard de
    cobranza ya demostró que estas cuatro son las que se miran."""
    validate_role()
    row = frappe.db.sql(
        """
        SELECT
            COUNT(*)                                                          AS n,
            COALESCE(SUM(grand_total), 0)                                     AS facturado,
            COALESCE(SUM(grand_total - outstanding_amount), 0)                AS cobrado,
            COALESCE(SUM(outstanding_amount), 0)                              AS por_cobrar,
            COALESCE(SUM(CASE WHEN status = 'Overdue' THEN outstanding_amount ELSE 0 END), 0) AS vencido,
            SUM(CASE WHEN status = 'Overdue' THEN 1 ELSE 0 END)               AS n_vencidas,
            SUM(CASE WHEN outstanding_amount > 0.009 THEN 1 ELSE 0 END)       AS n_abiertas
        FROM `tabSales Invoice`
        WHERE docstatus = 1
        """,
        as_dict=True,
    )[0]
    row["n_clientes"] = frappe.db.count("Customer")
    row["n_pagos"] = frappe.db.count("Payment Entry", {"docstatus": 1})
    return row


@frappe.whitelist()
def listar(filtro: str = "por_cobrar", q: str = "", limit=200) -> list[dict]:
    """Facturas para la tabla. El orden depende del filtro a propósito: en
    'por cobrar' lo urgente es lo que vence primero; en el resto, lo último
    facturado."""
    validate_role()
    if filtro not in FILTROS:
        frappe.throw("filtro inválido")

    condiciones = ["si.docstatus = 1"]
    valores: dict = {}

    estados = FILTROS[filtro]
    if estados:
        condiciones.append("si.status IN %(estados)s")
        valores["estados"] = tuple(estados)

    q = (q or "").strip()
    if len(q) >= 2:
        condiciones.append(
            "(si.customer_name LIKE %(q)s OR si.name LIKE %(q)s OR si.remarks LIKE %(q)s)"
        )
        valores["q"] = f"%{q}%"

    orden = "si.due_date ASC" if filtro in ("por_cobrar", "vencidas") else "si.posting_date DESC"
    valores["limite"] = frappe.utils.cint(limit) or 200

    filas = frappe.db.sql(
        f"""
        SELECT si.name, si.customer, si.customer_name, si.posting_date, si.due_date,
               si.grand_total, si.outstanding_amount, si.status, si.currency, si.remarks,
               si.auto_repeat, si.cfdi_total, si.cfdi_metodo_pago
        FROM `tabSales Invoice` si
        WHERE {' AND '.join(condiciones)}
        ORDER BY {orden}
        LIMIT %(limite)s
        """,
        valores,
        as_dict=True,
    )

    hoy = frappe.utils.today()
    moldes = _moldes() if any(f.get("auto_repeat") for f in filas) else set()
    for f in filas:
        f["pagado"] = round(float(f["grand_total"]) - float(f["outstanding_amount"]), 2)
        f["estado_es"] = ESTADO_ES.get(f["status"], f["status"])
        f["folio_ghl"] = _folio_ghl(
            f.pop("remarks", None), f.get("auto_repeat"), f["name"], moldes
        )
        f["recurrente"] = bool(f.get("auto_repeat"))
        # Días de atraso: dato que ERPNext no muestra en su lista y es lo primero
        # que se pregunta al llamar a cobrar.
        f["dias_vencida"] = (
            frappe.utils.date_diff(hoy, f["due_date"])
            if f["due_date"] and float(f["outstanding_amount"]) > 0.009
            else 0
        )
        if f["dias_vencida"] < 0:
            f["dias_vencida"] = 0
        # Estado fiscal en la lista: `None` en `cfdi_total` significa "no se
        # sabe" (el campo nació vacío el 18-sep y se llena por goteo), NO
        # "no tiene CFDI". Por eso se expone el número, no un booleano.
        f["cfdi_timbrado"] = frappe.utils.flt(f.pop("cfdi_total", 0))
        f["cfdi_sin_timbrar"] = round(
            float(f["grand_total"]) - f["cfdi_timbrado"], 2)
    return filas


@frappe.whitelist()
def detalle(factura: str) -> dict:
    """Factura + conceptos + pagos aplicados. Los pagos salen de Payment Entry
    Reference (la tabla hija), que es la única fuente que dice cuánto de ese
    pago se aplicó a ESTA factura — un pago puede repartirse entre varias."""
    validate_role()
    doc = frappe.get_doc("Sales Invoice", factura)

    pagos = frappe.db.sql(
        """
        SELECT pe.name, pe.posting_date, pe.mode_of_payment, pe.reference_no,
               per.allocated_amount, pe.remarks
        FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent
        WHERE per.reference_name = %s AND pe.docstatus = 1
        ORDER BY pe.posting_date ASC
        """,
        factura,
        as_dict=True,
    )

    return {
        "name": doc.name,
        "cliente": doc.customer_name,
        "customer": doc.customer,
        "posting_date": doc.posting_date,
        "due_date": doc.due_date,
        "grand_total": doc.grand_total,
        "outstanding_amount": doc.outstanding_amount,
        "pagado": round(float(doc.grand_total) - float(doc.outstanding_amount), 2),
        "status": doc.status,
        "estado_es": ESTADO_ES.get(doc.status, doc.status),
        "currency": doc.currency,
        "folio_ghl": _folio_ghl(doc.remarks, doc.get("auto_repeat"), doc.name),
        "auto_repeat": doc.get("auto_repeat"),
        "remarks": doc.remarks,
        "conceptos": [
            {"descripcion": i.item_name or i.item_code, "cantidad": i.qty, "importe": i.amount}
            for i in doc.items
        ],
        "pagos": pagos,
        # Estado fiscal completo (lista de UUID, timbrado, faltante). Va aquí
        # para que el detalle de una factura responda "¿ya tiene CFDI?" sin una
        # segunda llamada — era la pregunta que obligaba a buscar en Gmail.
        "cfdi": _cfdi_de(factura),
    }


@frappe.whitelist()
def editar_factura(factura: str, due_date: str) -> dict:
    """Edita el vencimiento de una factura ya emitida.

    Es el único campo seguro de tocar en una `Sales Invoice` con docstatus=1:
    `due_date` no vive en ningún `GL Entry` (es metadata de aging/cobranza), a
    diferencia de monto, cliente o concepto — esos sí están amarrados al
    asiento contable ya sellado, y la vía correcta de ERPNext para corregirlos
    es cancelar + reemitir, no un edit (Alejandro, 2026-09-17).

    Por eso se escribe con `frappe.db.set_value` (bypassa el candado de
    submit) en vez de `doc.save()`, que rechazaría el cambio en un documento
    presentado. `set_status(update=True)` recalcula Vencida/Sin pagar acorde
    al vencimiento nuevo — si no, `status` quedaría desfasado hasta el
    scheduled job diario de ERPNext.

    OJO — el vencimiento que decide "Vencida" no es este campo: es el de
    `Payment Schedule` (child table que ERPNext crea al someter, aunque no
    haya términos de pago explícitos). Sin recorrerla, `status` se habría
    quedado en Vencida aunque el vencimiento ya apuntara a futuro (encontrado
    al validar esta función). Se desplaza cada fila el mismo número de días
    que el vencimiento principal, para no romper el espaciado si algún día
    hay más de una.
    """
    validate_role()
    inv = frappe.db.get_value(
        "Sales Invoice", factura, ["docstatus", "posting_date", "due_date"], as_dict=True
    )
    if not inv:
        frappe.throw("La factura no existe")
    if inv.docstatus != 1:
        frappe.throw("La factura no está emitida")

    nuevo = frappe.utils.getdate(due_date)
    if nuevo < frappe.utils.getdate(inv.posting_date):
        frappe.throw("El vencimiento no puede ser anterior a la fecha de emisión")

    delta = frappe.utils.date_diff(nuevo, inv.due_date) if inv.due_date else 0
    frappe.db.set_value("Sales Invoice", factura, "due_date", nuevo)
    if delta:
        for fila in frappe.get_all(
            "Payment Schedule", filters={"parent": factura, "parenttype": "Sales Invoice"},
            fields=["name", "due_date"],
        ):
            frappe.db.set_value(
                "Payment Schedule", fila.name, "due_date",
                frappe.utils.add_days(fila.due_date, delta),
            )
    frappe.get_doc("Sales Invoice", factura).set_status(update=True)
    frappe.db.commit()
    return detalle(factura)


@frappe.whitelist()
def modos_de_pago() -> list[dict]:
    """Los modos vienen en inglés desde los masters de ERPNext ('Wire Transfer',
    'Cash'…). Se traducen para la interfaz pero se manda el valor original: el
    nombre en inglés es la llave contable con la que están amarradas las cuentas
    de banco, renombrarlo rompería los asientos."""
    validate_role()
    modos = [r.name for r in frappe.get_all("Mode of Payment", filters={"enabled": 1}, order_by="name")]
    # Transferencia primero: es como paga la mayoría de los clientes.
    modos.sort(key=lambda m: (m != "Wire Transfer", m))
    return [{"value": m, "label": MODOS_ES.get(m, m)} for m in modos]


@frappe.whitelist()
def listar_pagos(q: str = "", desde: str = None, hasta: str = None, limit=200) -> dict:
    """Transacciones para la pestaña "Transacciones". Es la vista de Payment Entry
    que faltaba: hoy un pago solo se ve entrando a la factura que abonó, o en el
    Desk de ERPNext — no hay forma de preguntar "¿qué cobramos esta semana?" desde
    el CRM.

    218 de los 219 pagos vienen de la migración de GHL y llegaron sin
    `mode_of_payment` ni `reference_no` (GHL no lo capturaba) — se muestran como
    "Sin especificar" en vez de ocultarlos, y `remarks` es el único dato de
    contexto que sí trae la migración para esos casos."""
    validate_role()

    condiciones = ["pe.docstatus = 1"]
    valores: dict = {}

    q = (q or "").strip()
    if len(q) >= 2:
        condiciones.append(
            """(pe.party_name LIKE %(q)s OR pe.remarks LIKE %(q)s OR pe.reference_no LIKE %(q)s
                OR EXISTS (
                    SELECT 1 FROM `tabPayment Entry Reference` per
                    WHERE per.parent = pe.name AND per.reference_name LIKE %(q)s
                ))"""
        )
        valores["q"] = f"%{q}%"

    if desde:
        condiciones.append("pe.posting_date >= %(desde)s")
        valores["desde"] = desde
    if hasta:
        condiciones.append("pe.posting_date <= %(hasta)s")
        valores["hasta"] = hasta

    valores["limite"] = frappe.utils.cint(limit) or 200

    filas = frappe.db.sql(
        f"""
        SELECT pe.name, pe.posting_date, pe.party_name, pe.paid_amount, pe.mode_of_payment,
               pe.reference_no, pe.remarks
        FROM `tabPayment Entry` pe
        WHERE {' AND '.join(condiciones)}
        ORDER BY pe.posting_date DESC, pe.creation DESC
        LIMIT %(limite)s
        """,
        valores,
        as_dict=True,
    )

    total = round(sum(float(f["paid_amount"] or 0) for f in filas), 2)
    if not filas:
        return {"pagos": [], "total": total, "n": 0}

    refs = frappe.db.sql(
        """
        SELECT per.parent, per.reference_name, si.remarks AS si_remarks, si.auto_repeat
        FROM `tabPayment Entry Reference` per
        LEFT JOIN `tabSales Invoice` si ON si.name = per.reference_name
        WHERE per.parent IN %(nombres)s AND per.reference_doctype = 'Sales Invoice'
        """,
        {"nombres": [f["name"] for f in filas]},
        as_dict=True,
    )
    moldes = _moldes()
    facturas_por_pago: dict = {}
    for r in refs:
        folio = _folio_ghl(r.get("si_remarks"), r.get("auto_repeat"), r["reference_name"], moldes)
        facturas_por_pago.setdefault(r["parent"], []).append(
            {"factura": r["reference_name"], "folio_ghl": folio}
        )

    for f in filas:
        f["facturas"] = facturas_por_pago.get(f["name"], [])
        f["modo_es"] = MODOS_ES.get(f["mode_of_payment"], f["mode_of_payment"] or None) or "Sin especificar"

    return {"pagos": filas, "total": total, "n": len(filas)}


def _pago_duplicado(factura: str, monto: float, fecha: str) -> str | None:
    """Mismo criterio que el script `registrar_pago.py` de Perla: misma factura,
    mismo monto, mismo día. Registrar dos veces el mismo abono descuadra la
    cartera y hay que revertirlo a mano en la contabilidad."""
    filas = frappe.db.sql(
        """
        SELECT pe.name, per.allocated_amount
        FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent
        WHERE per.reference_name = %s AND pe.docstatus = 1 AND pe.posting_date = %s
        """,
        (factura, fecha),
        as_dict=True,
    )
    for f in filas:
        if abs(float(f["allocated_amount"]) - float(monto)) <= 0.01:
            return f["name"]
    return None


@frappe.whitelist()
def registrar_pago(factura: str, monto, fecha: str = None, modo: str = None, nota: str = None) -> dict:
    """Registra un abono contra una factura.

    Se arma con `get_payment_entry`, el mismo helper que usó la migración y que
    usa Perla: construir el Payment Entry a mano falla con MandatoryError
    (paid_to, paid_to_account_currency) porque las cuentas contables las
    resuelve ERPNext según la company y el modo de pago. Adivinarlas desde
    fuera es justo lo que no se debe hacer con asientos contables.
    """
    validate_role()
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    monto = round(float(monto), 2)
    if monto <= 0:
        frappe.throw("El monto debe ser mayor a cero")
    fecha = fecha or frappe.utils.today()

    inv = frappe.db.get_value(
        "Sales Invoice", factura,
        ["name", "customer_name", "outstanding_amount", "docstatus"], as_dict=True,
    )
    if not inv:
        frappe.throw("La factura no existe")
    if inv.docstatus != 1:
        frappe.throw("La factura no está emitida")

    saldo = round(float(inv.outstanding_amount), 2)
    if saldo <= 0:
        frappe.throw("La factura no tiene saldo pendiente")
    # Se rechaza en vez de recortar el monto: un abono mayor al saldo casi
    # siempre significa que se eligió la factura equivocada.
    if monto > saldo + 0.01:
        frappe.throw(f"El monto ({monto:,.2f}) es mayor al saldo ({saldo:,.2f})")

    dup = _pago_duplicado(factura, monto, fecha)
    if dup:
        return {
            "ok": True, "duplicado": True, "payment_entry": dup, "factura": factura,
            "mensaje": "Ya había un pago igual ese día — no se creó otro.",
        }

    pe = get_payment_entry("Sales Invoice", factura)
    pe.posting_date = fecha
    pe.reference_date = fecha
    pe.paid_amount = monto
    pe.received_amount = monto
    pe.reference_no = nota or f"Pago {factura}"
    if nota:
        pe.remarks = nota
    if modo:
        pe.mode_of_payment = modo
    for ref in pe.references:
        ref.allocated_amount = monto
    pe.insert()
    pe.submit()

    inv2 = frappe.db.get_value(
        "Sales Invoice", factura, ["outstanding_amount", "status"], as_dict=True
    )
    return {
        "ok": True,
        "duplicado": False,
        "payment_entry": pe.name,
        "factura": factura,
        "cliente": inv.customer_name,
        "monto": monto,
        "fecha": fecha,
        "saldo_anterior": saldo,
        "saldo_nuevo": float(inv2.outstanding_amount),
        "estado_nuevo": ESTADO_ES.get(inv2.status, inv2.status),
    }


# ---------------------------------------------------------------------------
# Facturas recurrentes
# ---------------------------------------------------------------------------
#
# POR QUÉ EXISTE ESTA SECCIÓN
#     GHL emite solo las mensualidades de la agencia: 10 plantillas activas por
#     $52,450/mes al 10-sep-2026. Eso NO migró con la cartera — lo que llegó a
#     ERPNext son las facturas que la recurrencia de GHL ya había emitido, no la
#     regla que las emite. Si GHL se apaga sin replicar esto, las mensualidades
#     simplemente dejan de facturarse.
#
# POR QUÉ AUTO REPEAT Y NO SUBSCRIPTION
#     `Subscription` de ERPNext gira alrededor de Subscription Plans, precios de
#     catálogo y periodos de facturación — un aparato pensado para SaaS con
#     planes. Lo de la agencia es más simple y es literalmente lo que hace GHL:
#     "esta factura, repítela cada mes el día N". Eso es `Auto Repeat`, que clona
#     un documento de referencia con la cadencia dada. Menos piezas, y el
#     documento generado es una Sales Invoice normal que ya cae en la cartera,
#     en /cobranza y en el resto de esta API sin tocar nada más.
#
# LO QUE ESTA VÍA *NO* HACE, Y HAY QUE DECIRLO
#     - **No cobra.** Auto Repeat emite la factura; no pasa la tarjeta. Las 4
#       plantillas de GHL con `autoPayment` (Cessa, masfortunia, EnaBarrera y
#       Esplendido — esta última con la tarjeta vencida desde hace meses)
#       necesitan Stripe aparte. Migrarlas sin resolver eso convierte un cobro
#       automático en uno manual, que es un retroceso.
#     - **No envía la factura al cliente.** `notify_by_email` queda apagado a
#       propósito: el correo saldría con la plantilla en inglés de Frappe y
#       remitente sin configurar. El envío sigue siendo manual hasta que se
#       arme la plantilla en español.

FRECUENCIAS = {
    "Monthly": "Mensual",
    "Quarterly": "Trimestral",
    "Half-yearly": "Semestral",
    "Yearly": "Anual",
    "Weekly": "Semanal",
    "Daily": "Diaria",
}

MARCA_RECURRENTE = "Recurrente creada desde el CRM"
MARCA_FACTURA_CRM = "Factura creada desde el CRM"

# Meses que avanza cada frecuencia. Semanal/diaria se quedan fuera: nadie las
# usa desde el CRM (ver `frecuencias` en NuevaRecurrenteDialog.vue) y su
# cadencia no se presta al cálculo de mes exacto de abajo.
MESES_POR_FRECUENCIA = {"Monthly": 1, "Quarterly": 3, "Half-yearly": 6, "Yearly": 12}


def _start_date_para_emision(objetivo, frecuencia: str):
    """`start_date` de Auto Repeat que hace que su PRIMERA emisión caiga
    exactamente en `objetivo`.

    Frappe ancla la cadencia así (`auto_repeat.get_next_schedule_date` /
    `get_next_date`): `next_schedule_date = start_date + relativedelta(months=
    periodo, day=repeat_on_day)`. El día de `start_date` no importa, solo su
    MES — así que basta con que `start_date` caiga en el mes exacto anterior
    a `objetivo` (`objetivo - periodo` meses).

    El riesgo es `Auto Repeat.before_insert`, que empuja `start_date` a hoy si
    se manda una fecha pasada: si el mes necesario ya pasó, ese empujón
    cambiaría el mes del ancla y la emisión ya no caería en `objetivo`. Por
    eso, cuando el mes necesario coincide con el de hoy, se usa `hoy` mismo
    (no es "pasado", así que no lo empuja); cuando es un mes futuro, cualquier
    día de ese mes ya es posterior a hoy y tampoco lo toca.

    Devuelve `None` si `objetivo` no es alcanzable — cae este mes o antes. Es
    una restricción real de Frappe (una recurrente nueva no puede tener su
    primera emisión antes del mes que sigue a hoy), no un límite nuestro.
    """
    periodo = MESES_POR_FRECUENCIA.get(frecuencia)
    if not periodo:
        frappe.throw("Frecuencia sin soporte para fecha exacta")
    objetivo = frappe.utils.getdate(objetivo)
    hoy = frappe.utils.getdate(frappe.utils.today())

    idx_objetivo = objetivo.year * 12 + objetivo.month
    idx_hoy = hoy.year * 12 + hoy.month
    idx_necesario = idx_objetivo - periodo

    if idx_necesario < idx_hoy:
        return None
    if idx_necesario == idx_hoy:
        return hoy
    anio, mes = divmod(idx_necesario - 1, 12)
    return frappe.utils.getdate(f"{anio}-{mes + 1:02d}-01")


def _primera_emision_posible(frecuencia: str, dia: int):
    """La fecha más próxima que sí es alcanzable, para el mensaje de error de
    `_start_date_para_emision` cuando `objetivo` no lo es."""
    periodo = MESES_POR_FRECUENCIA[frecuencia]
    hoy = frappe.utils.getdate(frappe.utils.today())
    idx = hoy.year * 12 + hoy.month + periodo
    anio, mes = divmod(idx - 1, 12)
    return frappe.utils.getdate(f"{anio}-{mes + 1:02d}-01") + frappe.utils.relativedelta(day=dia)


# La migración cargó las 226 facturas con un solo concepto genérico y dejó el
# nombre real del servicio de GHL ("Sofía GPT - Estrublock") en la descripción.
# Mostrar el genérico haría indistinguibles todas las recurrentes entre sí.
CONCEPTO_GENERICO = "Servicios lavendi.mx"


def _concepto(factura: str | None) -> str | None:
    if not factura:
        return None
    fila = frappe.db.get_value(
        "Sales Invoice Item", {"parent": factura}, ["item_name", "description"], as_dict=True
    )
    if not fila:
        return None
    desc = (fila.description or "").strip()
    if desc and desc != CONCEPTO_GENERICO and "migrado de GHL" not in desc:
        return desc[:140]
    return fila.item_name


def _resumen_recurrente(ar: dict) -> dict:
    """Aplana un Auto Repeat + su factura molde a lo que la tabla necesita.

    El monto y el cliente no viven en el Auto Repeat: viven en la factura de
    referencia. Es el mismo modelo de GHL (plantilla -> facturas), solo que ahí
    el monto venía en el propio schedule.
    """
    inv = frappe.db.get_value(
        "Sales Invoice", ar.get("reference_document"),
        ["customer", "customer_name", "grand_total", "currency", "posting_date", "docstatus"],
        as_dict=True,
    ) or {}
    concepto = _concepto(ar.get("reference_document"))
    activa = not ar.get("disabled") and ar.get("status") == "Active"
    return {
        "name": ar.get("name"),
        "factura_molde": ar.get("reference_document"),
        "cliente": inv.get("customer"),
        "cliente_nombre": inv.get("customer_name"),
        "concepto": concepto,
        "monto": float(inv.get("grand_total") or 0),
        "currency": inv.get("currency") or "MXN",
        "frecuencia": ar.get("frequency"),
        "frecuencia_es": FRECUENCIAS.get(ar.get("frequency"), ar.get("frequency")),
        "dia": ar.get("repeat_on_day"),
        "inicio": ar.get("start_date"),
        "fin": ar.get("end_date"),
        "proxima": ar.get("next_schedule_date") if activa else None,
        "status": ar.get("status"),
        "activa": activa,
        "estado_es": "Activa" if activa else ("Terminada" if ar.get("status") == "Completed" else "Pausada"),
        "emitidas": frappe.db.count(
            "Sales Invoice", {"auto_repeat": ar.get("name"), "docstatus": 1}
        ),
    }


@frappe.whitelist()
def listar_recurrentes(incluir_inactivas: int = 1) -> dict:
    """Plantillas de facturación recurrente, activas primero."""
    validate_role()
    filtros = {"reference_doctype": "Sales Invoice"}
    if not frappe.utils.cint(incluir_inactivas):
        filtros["disabled"] = 0

    filas = [
        _resumen_recurrente(ar)
        for ar in frappe.get_all(
            "Auto Repeat",
            filters=filtros,
            fields=[
                "name", "reference_document", "frequency", "repeat_on_day",
                "start_date", "end_date", "next_schedule_date", "status", "disabled",
            ],
            order_by="disabled asc, next_schedule_date asc",
            limit_page_length=0,
        )
    ]
    activas = [f for f in filas if f["activa"]]
    return {
        "recurrentes": filas,
        "resumen": {
            "n_activas": len(activas),
            "n_total": len(filas),
            # Solo suma las mensuales: mezclar una anual con una mensual en un
            # mismo "al mes" daría una cifra que no significa nada.
            "mensual": round(sum(f["monto"] for f in activas if f["frecuencia"] == "Monthly"), 2),
            "n_mensuales": len([f for f in activas if f["frecuencia"] == "Monthly"]),
        },
    }


@frappe.whitelist()
def detalle_recurrente(name: str) -> dict:
    """La plantilla + las facturas que ya generó."""
    validate_role()
    ar = frappe.db.get_value(
        "Auto Repeat", name,
        ["name", "reference_document", "frequency", "repeat_on_day", "start_date",
         "end_date", "next_schedule_date", "status", "disabled", "submit_on_creation"],
        as_dict=True,
    )
    if not ar:
        frappe.throw("La recurrente no existe")

    d = _resumen_recurrente(ar)
    d["facturas"] = frappe.get_all(
        "Sales Invoice",
        filters={"auto_repeat": name, "docstatus": ["<", 2]},
        fields=["name", "posting_date", "due_date", "grand_total", "outstanding_amount", "status"],
        order_by="posting_date desc",
        limit_page_length=50,
    )
    for f in d["facturas"]:
        f["estado_es"] = ESTADO_ES.get(f["status"], f["status"])
    return d


def _duplicado_recurrente(customer: str, monto: float, excluir: str = None) -> dict | None:
    """Guarda contra el defecto que se encontró en GHL el 10-sep-2026: la
    Universidad Olmeca tenía tres plantillas activas al mismo tiempo ($23,000 +
    $29,999.99 + $22,999.99) y medicare.mx dos, porque nadie canceló la vieja al
    subir el precio. En GHL nada avisaba; aquí sí.
    """
    for ar in frappe.get_all(
        "Auto Repeat",
        filters={"reference_doctype": "Sales Invoice", "disabled": 0, "status": "Active"},
        fields=["name", "reference_document"],
        limit_page_length=0,
    ):
        if excluir and ar.name == excluir:
            continue
        inv = frappe.db.get_value(
            "Sales Invoice", ar.reference_document, ["customer", "grand_total"], as_dict=True
        )
        if inv and inv.customer == customer:
            return {"name": ar.name, "monto": float(inv.grand_total or 0)}
    return None


def _asegurar_credito(customer: str, dias: int) -> str | None:
    """Le fija al cliente un plazo de pago, y de paso arregla un defecto real de
    Auto Repeat.

    `Sales Invoice.on_recurring` borra el vencimiento de la factura generada
    (`self.due_date = None`) contando con que ERPNext lo recalcule desde los
    términos de pago. Como la migración no trajo ninguno — 0 Payment Terms
    Template en el sitio — el vencimiento terminaba igual a la fecha de emisión:
    cada mensualidad nacía VENCIDA el mismo día, y aparecería en /cobranza como
    atrasada desde el minuto uno.

    Se pone en el Cliente, no en la factura molde: `payment_terms_template` es
    `no_copy`, así que en la factura no sobreviviría al clonado. En el cliente
    además es lo correcto de negocio — "este cliente paga a N días" — y aplica
    también a sus facturas manuales.
    """
    dias = frappe.utils.cint(dias)
    if dias <= 0:
        return None
    actual = frappe.db.get_value("Customer", customer, "payment_terms")
    if actual:
        return actual

    nombre = f"{dias} días"
    if not frappe.db.exists("Payment Term", nombre):
        frappe.get_doc({
            "doctype": "Payment Term", "payment_term_name": nombre,
            "invoice_portion": 100, "due_date_based_on": "Day(s) after invoice date",
            "credit_days": dias,
        }).insert(ignore_permissions=True)
    if not frappe.db.exists("Payment Terms Template", nombre):
        frappe.get_doc({
            "doctype": "Payment Terms Template", "template_name": nombre,
            "terms": [{"payment_term": nombre, "invoice_portion": 100,
                       "due_date_based_on": "Day(s) after invoice date", "credit_days": dias}],
        }).insert(ignore_permissions=True)
    frappe.db.set_value("Customer", customer, "payment_terms", nombre)
    return nombre


def _crear_auto_repeat(factura: str, frecuencia: str, dia, inicio: str, fin: str = None) -> str:
    ar = frappe.get_doc({
        "doctype": "Auto Repeat",
        "reference_doctype": "Sales Invoice",
        "reference_document": factura,
        "frequency": frecuencia,
        "start_date": inicio,
        "end_date": fin or None,
        # La factura nace emitida, no en borrador: un borrador no aparece en la
        # cartera ni en /cobranza, así que la mensualidad existiría sin que
        # nadie la vea ni la cobre.
        "submit_on_creation": 1,
        # Ver la nota de arriba: el correo al cliente sigue siendo manual.
        "notify_by_email": 0,
    })
    if frecuencia in ("Monthly", "Quarterly", "Half-yearly", "Yearly") and dia:
        ar.repeat_on_day = frappe.utils.cint(dia)
    ar.insert()
    return ar.name


@frappe.whitelist()
def crear_desde_factura(factura: str, frecuencia: str = "Monthly",
                        primera_emision: str = None, fin: str = None,
                        dias_credito: int = None, forzar: int = 0) -> dict:
    """Convierte una factura ya emitida en la plantilla de una recurrente.

    Es la vía correcta para migrar las plantillas de GHL: la factura que GHL ya
    emitió este mes se vuelve el molde, y ERPNext emite la siguiente. No genera
    ningún cargo nuevo hoy.

    `primera_emision` es la fecha EXACTA en la que debe caer la próxima
    factura que ERPNext genere — no "el día del mes", una fecha real. Se
    resuelve con `_start_date_para_emision` y se verifica después de crear la
    plantilla que `next_schedule_date` cayó donde se pidió; si no coincide se
    revierte y se avisa, nunca se deja una recurrente cobrando un día distinto
    al que el usuario pidió.

    Si se omite, se conserva el comportamiento de siempre: se hereda el día
    de la propia factura molde y Frappe decide el mes (empuja al que sigue si
    el de este mes ya pasó) — es la vía rápida para "lo antes posible" sin
    pensar en calendarios.
    """
    validate_role()
    if frecuencia not in FRECUENCIAS:
        frappe.throw("Frecuencia inválida")

    inv = frappe.db.get_value(
        "Sales Invoice", factura,
        ["name", "customer", "customer_name", "grand_total", "posting_date", "due_date",
         "docstatus", "auto_repeat"],
        as_dict=True,
    )
    if not inv:
        frappe.throw("La factura no existe")
    if inv.docstatus != 1:
        frappe.throw("La factura no está emitida")
    if inv.auto_repeat:
        frappe.throw(f"Esa factura ya es la plantilla de la recurrente {inv.auto_repeat}")

    if not frappe.utils.cint(forzar):
        dup = _duplicado_recurrente(inv.customer, float(inv.grand_total))
        if dup:
            frappe.throw(
                f"{inv.customer_name} ya tiene una recurrente activa "
                f"({dup['name']}, ${dup['monto']:,.2f}). Cancélala primero o marca "
                f"que sí quieres dos — dos plantillas vivas le facturan dos veces."
            )

    if primera_emision:
        objetivo = frappe.utils.getdate(primera_emision)
        inicio = _start_date_para_emision(objetivo, frecuencia)
        if inicio is None:
            minimo = _primera_emision_posible(frecuencia, objetivo.day)
            frappe.throw(
                f"La primera emisión no puede ser antes del "
                f"{frappe.utils.formatdate(minimo)}. Elige esa fecha o una posterior."
            )
        dia = objetivo.day
    else:
        inicio = str(inv.posting_date)
        dia = frappe.utils.getdate(inv.posting_date).day

    # El plazo se hereda de la propia factura molde si no lo dicen: es el que ya
    # se le venía dando a ese cliente.
    if dias_credito in (None, ""):
        dias_credito = (
            frappe.utils.date_diff(inv.due_date, inv.posting_date)
            if inv.due_date and inv.posting_date else 15
        )
    plazo = _asegurar_credito(inv.customer, dias_credito)

    name = _crear_auto_repeat(factura, frecuencia, dia, str(inicio), fin)

    if primera_emision:
        real = frappe.db.get_value("Auto Repeat", name, "next_schedule_date")
        if frappe.utils.getdate(real) != objetivo:
            frappe.delete_doc("Auto Repeat", name, force=True, ignore_permissions=True)
            frappe.db.set_value("Sales Invoice", factura, "auto_repeat", "")
            frappe.throw(
                f"No se pudo fijar la primera emisión el "
                f"{frappe.utils.formatdate(objetivo)}: ERPNext calculó "
                f"{frappe.utils.formatdate(real)}. Intenta con esa fecha."
            )

    return {"ok": True, "recurrente": name, "plazo_pago": plazo, **detalle_recurrente(name)}


def _emitir_factura(customer: str, concepto: str, monto: float, emision, dias_credito,
                    marca: str):
    """Arma y somete una Sales Invoice de un solo concepto. Compartida por
    `crear_recurrente` (donde esta factura queda de molde) y `crear_factura`
    (donde es la factura completa). `emision` puede ser hoy o una fecha
    futura — ERPNext sí acepta `posting_date` futuro en Sales Invoice."""
    company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
        "Global Defaults", "default_company"
    )
    cuenta_ingreso = frappe.db.get_value(
        "Account", {"company": company, "root_type": "Income", "is_group": 0}, "name"
    )
    centro_costo = frappe.db.get_value("Company", company, "cost_center")

    inv = frappe.get_doc({
        "doctype": "Sales Invoice",
        "customer": customer,
        "company": company,
        "currency": "MXN",
        "conversion_rate": 1,
        "posting_date": str(emision),
        "due_date": str(frappe.utils.add_days(emision, frappe.utils.cint(dias_credito) or 15)),
        "set_posting_time": 1,
        "disable_rounded_total": 1,
        "remarks": marca,
        "items": [{
            "item_name": concepto[:140],
            "description": concepto[:255],
            "qty": 1,
            "rate": monto,
            "uom": "Unidad(es)",
            "income_account": cuenta_ingreso,
            "cost_center": centro_costo,
        }],
    })
    inv.set_missing_values()
    inv.insert()
    inv.submit()
    return inv


def _validar_monto_y_cliente(customer: str, monto) -> float:
    monto = round(float(monto), 2)
    if monto <= 0:
        frappe.throw("El monto debe ser mayor a cero")
    if not frappe.db.exists("Customer", customer):
        frappe.throw("El cliente no existe")
    return monto


@frappe.whitelist()
def crear_recurrente(customer: str, concepto: str, monto, frecuencia: str = "Monthly",
                     emision: str = None, dias_credito: int = 15, fin: str = None,
                     forzar: int = 0) -> dict:
    """Crea una recurrente desde cero.

    `emision` es la fecha exacta de la primera factura (el molde) — hoy por
    default, pero puede ser futura. No hace falta el ajuste de mes de
    `crear_desde_factura`: al no haber una factura previa que empuje el
    calendario, el `start_date` de la recurrente es la propia fecha de
    emisión, y la siguiente ya cae justo un período después.
    """
    validate_role()
    if frecuencia not in FRECUENCIAS:
        frappe.throw("Frecuencia inválida")
    monto = _validar_monto_y_cliente(customer, monto)

    hoy = frappe.utils.getdate(frappe.utils.today())
    emision = frappe.utils.getdate(emision) if emision else hoy
    if emision < hoy:
        frappe.throw("La fecha de emisión no puede ser anterior a hoy")

    if not frappe.utils.cint(forzar):
        dup = _duplicado_recurrente(customer, monto)
        if dup:
            nombre = frappe.db.get_value("Customer", customer, "customer_name")
            frappe.throw(
                f"{nombre} ya tiene una recurrente activa ({dup['name']}, "
                f"${dup['monto']:,.2f}). Cancélala primero o marca que sí quieres dos."
            )

    _asegurar_credito(customer, dias_credito)
    inv = _emitir_factura(customer, concepto, monto, emision, dias_credito, MARCA_RECURRENTE)

    name = _crear_auto_repeat(inv.name, frecuencia, emision.day, str(emision), fin)
    d = detalle_recurrente(name)
    d.update({"ok": True, "recurrente": name, "primera_factura": inv.name})
    return d


@frappe.whitelist()
def crear_factura(customer: str, concepto: str, monto, emision: str = None,
                  dias_credito: int = 15) -> dict:
    """Crea y emite una factura simple, sin recurrencia.

    Es la alta simplificada desde el CRM: 5 datos (cliente, concepto, monto,
    fecha de emisión, días de crédito) en vez del formulario completo del
    Desk de ERPNext (`/app/sales-invoice/new`), que expone impuestos, moneda,
    lista de precios y series que no aplican a este uso. Mismo alcance que el
    resto del módulo: sin timbrado CFDI ni envío al cliente (ver encabezado
    del archivo) — quien necesite eso sigue yendo al Desk.
    """
    validate_role()
    monto = _validar_monto_y_cliente(customer, monto)

    hoy = frappe.utils.getdate(frappe.utils.today())
    emision = frappe.utils.getdate(emision) if emision else hoy
    if emision < hoy:
        frappe.throw("La fecha de emisión no puede ser anterior a hoy")

    inv = _emitir_factura(customer, concepto, monto, emision, dias_credito, MARCA_FACTURA_CRM)
    d = detalle(inv.name)
    d["ok"] = True
    return d


@frappe.whitelist()
def pausar_recurrente(name: str, pausar: int = 1) -> dict:
    """Pausar/reanudar. GHL solo tiene cancelar, que es irreversible; aquí se
    puede detener un mes sin perder la plantilla ni su historial."""
    validate_role()
    doc = frappe.get_doc("Auto Repeat", name)
    doc.disabled = 1 if frappe.utils.cint(pausar) else 0
    doc.status = "Disabled" if doc.disabled else "Active"
    doc.save()
    return {"ok": True, **detalle_recurrente(name)}


@frappe.whitelist()
def editar_recurrente(name: str, frecuencia: str = None, dia=None, fin: str = None) -> dict:
    """Edita día del mes / frecuencia / fecha fin de una recurrente existente.

    `Auto Repeat` NO es submittable (a diferencia de la factura molde que sí
    lo es), así que aquí un `doc.save()` normal basta — Frappe recalcula
    `next_schedule_date` solo en `validate() -> set_dates()`.

    El monto y el cliente NO se editan aquí a propósito: viven en la factura
    molde (`reference_document`, submitted), y tocarlos ahí desincronizaría
    el libro contable. La vía ya existente para cambiar el monto es pausar
    esta y crear una nueva (`crear_recurrente`/`crear_desde_factura`) — el
    mismo criterio que ya aplica `_duplicado_recurrente`.
    """
    validate_role()
    doc = frappe.get_doc("Auto Repeat", name)

    if frecuencia:
        if frecuencia not in FRECUENCIAS:
            frappe.throw("Frecuencia inválida")
        doc.frequency = frecuencia
    if dia not in (None, ""):
        doc.repeat_on_day = frappe.utils.cint(dia)
    if fin is not None:
        doc.end_date = fin or None

    doc.save()
    return {"ok": True, **detalle_recurrente(name)}


@frappe.whitelist()
def eliminar_recurrente(name: str) -> dict:
    """Borra la plantilla. Las facturas que ya emitió NO se tocan: son
    documentos contables con asientos, y borrarlas descuadraría la cartera."""
    validate_role()
    emitidas = frappe.db.count("Sales Invoice", {"auto_repeat": name, "docstatus": 1})
    frappe.delete_doc("Auto Repeat", name)
    return {"ok": True, "facturas_conservadas": emitidas}


@frappe.whitelist()
def buscar_clientes(q: str = "", limit: int = 20) -> list[dict]:
    validate_role()
    q = (q or "").strip()
    filtros = {"disabled": 0}
    if len(q) >= 2:
        filtros["customer_name"] = ["like", f"%{q}%"]
    return frappe.get_all(
        "Customer", filters=filtros, fields=["name", "customer_name"],
        order_by="customer_name asc", limit_page_length=frappe.utils.cint(limit) or 20,
    )


@frappe.whitelist()
def facturas_candidatas(q: str = "", limit: int = 30) -> list[dict]:
    """Facturas emitidas que aún no son molde de ninguna recurrente — para el
    selector de 'convertir en recurrente'."""
    validate_role()
    condiciones = ["si.docstatus = 1", "COALESCE(si.auto_repeat,'') = ''"]
    valores = {"limite": frappe.utils.cint(limit) or 30}
    q = (q or "").strip()
    if len(q) >= 2:
        # También por descripción del concepto: el equipo busca "Estrublock" o
        # "Sofía GPT", que es el nombre del servicio en GHL, no el del contacto
        # con el que quedó nombrado el cliente en ERPNext.
        condiciones.append(
            "(si.customer_name LIKE %(q)s OR si.name LIKE %(q)s OR EXISTS ("
            " SELECT 1 FROM `tabSales Invoice Item` sii"
            " WHERE sii.parent = si.name AND sii.description LIKE %(q)s))"
        )
        valores["q"] = f"%{q}%"
    filas = frappe.db.sql(
        f"""
        SELECT si.name, si.customer, si.customer_name, si.posting_date,
               si.grand_total, si.currency
        FROM `tabSales Invoice` si
        WHERE {' AND '.join(condiciones)}
        ORDER BY si.posting_date DESC
        LIMIT %(limite)s
        """,
        valores, as_dict=True,
    )
    for f in filas:
        f["concepto"] = _concepto(f["name"])
    return filas


# ---------------------------------------------------------------------------
# Cartera del contacto — sección del panel lateral de Conversaciones
# ---------------------------------------------------------------------------
# POR QUÉ ES UN ENDPOINT APARTE Y NO PARTE DE `panel.get_contact_panel`
#     Ese panel lo abre TODO el equipo en cada hilo (su gate es el del CRM).
#     La cartera de la agencia no es del vendedor: lo que se le debe a la
#     empresa, cuánto lleva vencido y cuánto pagó cada quien es información de
#     contabilidad, y va con el mismo gate que el resto de este módulo.
#     Meterla en `get_contact_panel` obligaría a elegir entre romper ese panel
#     para quien no es contable, o filtrar dentro de una respuesta compartida.
#
#     Además se pide por separado: la apertura de un hilo no debe pagar dos
#     consultas más para un dato que la mayoría de las veces es "$0".
#
# EL VÍNCULO ES `Customer.ghl_contact_id`, NO EL NOMBRE
#     Ver `patches/vincular_customer_contacto.py`. Emparejar por nombre le
#     colgaría los adeudos de un cliente a otro en la pantalla que el equipo
#     mira MIENTRAS habla con él. Si el contacto no tiene Customer vinculado se
#     devuelve `vinculado: False` — explícito, nunca un saldo adivinado.

MAX_FACTURAS_PANEL = 5
MAX_PAGOS_PANEL = 3


@frappe.whitelist()
def cartera_de_contacto(contact: str) -> dict:
    """Saldo, últimas facturas y últimos pagos del contacto abierto en el hilo.

    Contrato de degradación (los tres casos son normales, ninguno es error):
      - sin permiso        -> {"permitido": False}
      - contacto sin alta  -> {"vinculado": False}
      - cliente sin adeudo -> saldo 0 con su historial
    """
    roles = frappe.get_roles(frappe.session.user)
    if not any(r in roles for r in ALLOWED_ROLES) and frappe.session.user != "Administrator":
        return {"permitido": False}

    ghl_id = frappe.db.get_value("Contact", contact, "ghl_contact_id")
    customer = None
    if ghl_id:
        customer = frappe.db.get_value("Customer", {"ghl_contact_id": ghl_id}, "name")
    if not customer:
        return {"permitido": True, "vinculado": False}

    agg = frappe.db.sql(
        """
        SELECT COUNT(*) n,
               SUM(grand_total) facturado,
               SUM(outstanding_amount) saldo,
               SUM(CASE WHEN status = 'Overdue' THEN outstanding_amount ELSE 0 END) vencido
        FROM `tabSales Invoice`
        WHERE customer = %s AND docstatus = 1
        """,
        customer, as_dict=True,
    )[0]

    moldes = _moldes()
    facturas = frappe.get_all(
        "Sales Invoice",
        filters={"customer": customer, "docstatus": 1},
        fields=["name", "posting_date", "due_date", "grand_total", "outstanding_amount",
                "status", "currency", "remarks", "auto_repeat"],
        order_by="posting_date desc, creation desc",
        limit=MAX_FACTURAS_PANEL,
    )
    hoy = frappe.utils.getdate()
    for f in facturas:
        f["folio"] = _folio_ghl(f.pop("remarks", None), f.get("auto_repeat"), f["name"], moldes)
        f["estado"] = ESTADO_ES.get(f["status"], f["status"])
        # Días de atraso: lo primero que se pregunta antes de escribirle a alguien
        # que debe. ERPNext no lo expone en ninguna lista.
        f["dias_atraso"] = (
            (hoy - frappe.utils.getdate(f["due_date"])).days
            if f["due_date"] and f["outstanding_amount"] and f["status"] == "Overdue"
            else 0
        )
        f["concepto"] = _concepto(f["name"])

    pagos = frappe.db.sql(
        """
        SELECT pe.name, pe.posting_date, pe.paid_amount, pe.mode_of_payment, pe.reference_no
        FROM `tabPayment Entry` pe
        WHERE pe.party_type = 'Customer' AND pe.party = %s AND pe.docstatus = 1
        ORDER BY pe.posting_date DESC, pe.creation DESC
        LIMIT %s
        """,
        (customer, MAX_PAGOS_PANEL), as_dict=True,
    )
    for p in pagos:
        p["modo"] = MODOS_ES.get(p.get("mode_of_payment"), p.get("mode_of_payment"))

    return {
        "permitido": True,
        "vinculado": True,
        "customer": customer,
        "total_facturas": agg.n or 0,
        "facturado": float(agg.facturado or 0),
        "saldo": float(agg.saldo or 0),
        "vencido": float(agg.vencido or 0),
        "facturas": facturas,
        "pagos": pagos,
    }


# ---------------------------------------------------------------------------
# CFDI emitido — paso 1 de `planes/facturacion-cfdi-de-detectar-a-ejecutar.md`
# ---------------------------------------------------------------------------
# La cuenta por cobrar no sabía si ya tenía CFDI: había que buscar el XML a mano
# en Gmail (lo que tuvo que hacer la sesión de `cfo` con Félix el 18-sep). Sin
# ese dato, cualquier automatismo que se acerque a facturar puede emitir un CFDI
# duplicado, y un CFDI timbrado es caro de cancelar.
#
# `cfdi_uuid` es una LISTA (una línea por CFDI) y no un campo simple porque el
# primer caso real ya lo rompe: ACC-SINV-2026-00025 ($59,400 de Félix) se
# facturó en dos mitades de $29,700. Un solo UUID habría dicho "ya facturada"
# con la mitad sin timbrar — el error opuesto y peor, porque deja de avisar.
#
# Formato por línea (ni el UUID ni la fecha ISO contienen `|`):
#     <uuid>|<fecha_timbrado ISO>|<total>|<PUE|PPD>

METODOS_CFDI = ("PUE", "PPD")

# 36 caracteres, 5 grupos hex. Se valida porque un UUID mal capturado es peor
# que ninguno: hace creer que la factura está protegida cuando no lo está.
RE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# Tolerancia al comparar timbrado contra total de la factura. Un CFDI puede
# diferir unos centavos del Sales Invoice por redondeo del subtotal (guatson
# calcula el IVA sobre el precio sin IVA, ERPNext parte del total).
TOLERANCIA_CFDI = 1.0


def _cfdi_lineas(texto: str | None) -> list[dict]:
    """Parsea el campo `cfdi_uuid`. Una línea ilegible se ignora en silencio
    para no romper la lectura de la factura entera — pero nunca se reescribe,
    así que no se pierde: sigue ahí para que un humano la vea."""
    salida = []
    for linea in (texto or "").splitlines():
        linea = linea.strip()
        if not linea:
            continue
        partes = linea.split("|")
        if not RE_UUID.match(partes[0].strip()):
            continue
        salida.append({
            "uuid": partes[0].strip().lower(),
            "fecha": partes[1].strip() if len(partes) > 1 else None,
            "total": frappe.utils.flt(partes[2]) if len(partes) > 2 else 0.0,
            "metodo": partes[3].strip().upper() if len(partes) > 3 else None,
        })
    return salida


def _cfdi_derivados(lineas: list[dict]) -> dict:
    """Los tres escalares que se guardan junto a la lista. Existen para poder
    filtrar y reportar sin parsear texto en cada consulta; la lista es la
    fuente de verdad.

    `metodo` = PPD si CUALQUIER CFDI de la factura es PPD: es el que obliga a
    emitir complemento de pago, y basta uno para que la obligación exista."""
    return {
        "cfdi_total": round(sum(l["total"] for l in lineas), 2),
        "cfdi_metodo_pago": ("PPD" if any(l["metodo"] == "PPD" for l in lineas)
                             else ("PUE" if lineas else None)),
        "cfdi_fecha_timbrado": max((l["fecha"] for l in lineas if l["fecha"]), default=None),
    }


def _cfdi_de(factura: str) -> dict:
    inv = frappe.db.get_value(
        "Sales Invoice", factura,
        ["name", "customer_name", "docstatus", "grand_total", "outstanding_amount",
         "cfdi_uuid", "cfdi_total", "cfdi_metodo_pago", "cfdi_fecha_timbrado"],
        as_dict=True)
    if not inv:
        frappe.throw("La factura no existe")

    lineas = _cfdi_lineas(inv.cfdi_uuid)
    timbrado = round(sum(l["total"] for l in lineas), 2)
    total = frappe.utils.flt(inv.grand_total)
    return {
        "factura": inv.name,
        "cliente": inv.customer_name,
        "grand_total": total,
        "tiene_cfdi": bool(lineas),
        "cfdi": lineas,
        "timbrado": timbrado,
        "sin_timbrar": round(max(total - timbrado, 0), 2),
        # Tres estados, no dos: "parcial" es el caso de Félix y es justo el que
        # un booleano `ya_facturada` habría reportado mal.
        "estado_cfdi": ("sin_cfdi" if not lineas
                        else "completo" if timbrado >= total - TOLERANCIA_CFDI
                        else "parcial"),
        "metodo_pago": inv.cfdi_metodo_pago,
        "ultimo_timbrado": inv.cfdi_fecha_timbrado,
    }


@frappe.whitelist()
def estado_cfdi(factura: str) -> dict:
    """¿Esta factura ya tiene CFDI, y por cuánto? Sin tocar Gmail."""
    validate_role()
    return _cfdi_de(factura)


@frappe.whitelist()
def marcar_cfdi(factura: str, uuid: str, total, fecha_timbrado: str = None,
                metodo_pago: str = "PUE", forzar: int = 0) -> dict:
    """Registra un CFDI ya timbrado contra una factura. Lo llama el skill
    `facturacion` al terminar su Paso 6, nunca un humano a mano.

    APPEND-ONLY, no set-once a secas: una factura puede tener varios CFDI
    (parcialidades). Lo que es set-once es cada UUID — nunca se edita ni se
    borra una línea ya escrita.

    Tres guardas, y las tres importan por separado:

    1. `uuid` repetido en ESTA factura -> no-op idempotente. Protege del
       reintento (el skill puede reejecutarse tras un fallo de red) sin
       necesitar que el caller lleve estado.
    2. `uuid` pegado a OTRA factura -> se rechaza. Es la guarda anti-doble
       contabilización: un mismo folio fiscal no puede amortizar dos cuentas
       por cobrar.
    3. timbrar más que el total de la factura -> se rechaza salvo `forzar=1`.
       Es la única señal automática de sobrefacturación que existe hoy; se deja
       forzable porque un caso legítimo (nota de crédito, refacturación por
       cancelación) no debe quedar trabado esperando código nuevo.

    Se escribe con `frappe.db.set_value` y no con `doc.save()` por lo mismo que
    `editar_factura`: la factura está presentada (docstatus=1) y `save()` la
    rechazaría. Estos campos no viven en ningún GL Entry — son metadata fiscal,
    no contable.
    """
    validate_role()

    uuid = (uuid or "").strip().lower()
    if not RE_UUID.match(uuid):
        frappe.throw("UUID inválido: se espera el folio fiscal de 36 caracteres del CFDI")

    metodo_pago = (metodo_pago or "PUE").strip().upper()
    if metodo_pago not in METODOS_CFDI:
        frappe.throw(f"metodo_pago debe ser {' o '.join(METODOS_CFDI)}")

    total = frappe.utils.flt(total)
    if total <= 0:
        frappe.throw("El total del CFDI debe ser mayor a cero")

    inv = frappe.db.get_value(
        "Sales Invoice", factura,
        ["name", "docstatus", "grand_total", "cfdi_uuid"], as_dict=True)
    if not inv:
        frappe.throw("La factura no existe")
    if inv.docstatus != 1:
        frappe.throw("La factura no está emitida")

    lineas = _cfdi_lineas(inv.cfdi_uuid)

    # (1) idempotencia
    if any(l["uuid"] == uuid for l in lineas):
        return {**_cfdi_de(factura), "accion": "sin_cambio"}

    # (2) el mismo folio fiscal en otra factura
    otra = frappe.db.sql(
        """SELECT name FROM `tabSales Invoice`
           WHERE cfdi_uuid LIKE %s AND name != %s AND docstatus = 1 LIMIT 1""",
        (f"%{uuid}%", factura))
    if otra:
        frappe.throw(
            f"Ese folio fiscal ya está registrado en {otra[0][0]}. "
            "Un CFDI no puede amortizar dos facturas — verifica cuál es la correcta.")

    # (3) sobrefacturación
    nuevo_timbrado = round(sum(l["total"] for l in lineas) + total, 2)
    limite = frappe.utils.flt(inv.grand_total) + TOLERANCIA_CFDI
    if nuevo_timbrado > limite and not frappe.utils.cint(forzar):
        frappe.throw(
            f"El CFDI dejaría timbrado ${nuevo_timbrado:,.2f} contra una factura de "
            f"${frappe.utils.flt(inv.grand_total):,.2f}. Si es correcto (refacturación, "
            "nota de crédito), repite con forzar=1.")

    fecha = fecha_timbrado or frappe.utils.now()
    linea = f"{uuid}|{frappe.utils.get_datetime(fecha).isoformat()}|{total:.2f}|{metodo_pago}"
    lineas_txt = ((inv.cfdi_uuid or "").rstrip() + "\n" + linea).strip()

    derivados = _cfdi_derivados(_cfdi_lineas(lineas_txt))
    frappe.db.set_value("Sales Invoice", factura,
                        {"cfdi_uuid": lineas_txt, **derivados}, update_modified=False)
    frappe.db.commit()
    return {**_cfdi_de(factura), "accion": "registrado"}


@frappe.whitelist()
def sin_cfdi(filtro: str = "todas", limit=200) -> list[dict]:
    """Facturas emitidas con saldo fiscal sin timbrar. Es el reverso de
    `estado_cfdi`: en vez de preguntar por una, lista las que faltan.

    Arranca devolviendo casi toda la cartera —nada tiene CFDI registrado
    todavía, el campo nace vacío— y se va vaciando conforme el skill
    `facturacion` escriba. Eso es lo correcto: vacío significa "no se sabe",
    no "no tiene". El backfill histórico es manual y va por goteo.
    """
    validate_role()
    if filtro not in FILTROS:
        frappe.throw("filtro inválido")

    condiciones = ["si.docstatus = 1",
                   "(si.cfdi_total IS NULL OR si.cfdi_total < si.grand_total - %(tol)s)"]
    valores = {"tol": TOLERANCIA_CFDI, "limite": frappe.utils.cint(limit) or 200}

    estados = FILTROS[filtro]
    if estados:
        condiciones.append("si.status IN %(estados)s")
        valores["estados"] = tuple(estados)

    filas = frappe.db.sql(
        f"""
        SELECT si.name, si.customer_name, si.posting_date, si.grand_total,
               si.outstanding_amount, si.status, si.cfdi_total, si.cfdi_metodo_pago
        FROM `tabSales Invoice` si
        WHERE {' AND '.join(condiciones)}
        ORDER BY si.posting_date DESC
        LIMIT %(limite)s
        """,
        valores, as_dict=True)

    for f in filas:
        f["estado_es"] = ESTADO_ES.get(f["status"], f["status"])
        f["timbrado"] = frappe.utils.flt(f.get("cfdi_total"))
        f["sin_timbrar"] = round(frappe.utils.flt(f["grand_total"]) - f["timbrado"], 2)
    return filas
