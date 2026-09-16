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
               si.auto_repeat
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
    }


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
def crear_desde_factura(factura: str, frecuencia: str = "Monthly", dia=None,
                        inicio: str = None, fin: str = None, dias_credito: int = None,
                        forzar: int = 0) -> dict:
    """Convierte una factura ya emitida en la plantilla de una recurrente.

    Es la vía correcta para migrar las plantillas de GHL: la factura que GHL ya
    emitió este mes se vuelve el molde, y ERPNext emite la siguiente. No genera
    ningún cargo nuevo hoy — `next_schedule_date` nunca cae en el pasado, así
    que una fecha de inicio vieja no dispara facturas retroactivas.

    OJO CON EL CALENDARIO AL MIGRAR: Frappe empuja `start_date` a hoy si se le
    manda una fecha pasada (`Auto Repeat.before_insert`), así que la fecha de
    alta original de GHL no se puede conservar; lo que gobierna la cadencia es
    el día del mes. La consecuencia práctica es que **si el día de cobro de este
    mes todavía no llega, la primera emisión de ERPNext se va al mes siguiente**
    (migrar el día 10 una plantilla que cobra el 17 salta el 17 de este mes).
    Por eso cada plantilla se migra *después* de su emisión del mes, no antes, y
    la respuesta trae `proxima` para verificarlo antes de cancelar la de GHL.
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

    inicio = inicio or str(inv.posting_date)
    if dia in (None, "", 0):
        dia = frappe.utils.getdate(inv.posting_date).day

    # El plazo se hereda de la propia factura molde si no lo dicen: es el que ya
    # se le venía dando a ese cliente.
    if dias_credito in (None, ""):
        dias_credito = (
            frappe.utils.date_diff(inv.due_date, inv.posting_date)
            if inv.due_date and inv.posting_date else 15
        )
    plazo = _asegurar_credito(inv.customer, dias_credito)

    name = _crear_auto_repeat(factura, frecuencia, dia, inicio, fin)
    return {"ok": True, "recurrente": name, "plazo_pago": plazo, **detalle_recurrente(name)}


@frappe.whitelist()
def crear_recurrente(customer: str, concepto: str, monto, frecuencia: str = "Monthly",
                     dia=None, dias_credito: int = 15, fin: str = None,
                     forzar: int = 0) -> dict:
    """Crea una recurrente desde cero.

    Emite HOY la primera factura, que es la que queda de molde. ERPNext no acepta
    fecha de emisión futura en una Sales Invoice, así que no hay forma de dejar
    una recurrente "armada para empezar el mes que viene" sin un primer
    documento real. La interfaz lo dice antes de guardar.
    """
    validate_role()
    if frecuencia not in FRECUENCIAS:
        frappe.throw("Frecuencia inválida")
    monto = round(float(monto), 2)
    if monto <= 0:
        frappe.throw("El monto debe ser mayor a cero")
    if not frappe.db.exists("Customer", customer):
        frappe.throw("El cliente no existe")

    if not frappe.utils.cint(forzar):
        dup = _duplicado_recurrente(customer, monto)
        if dup:
            nombre = frappe.db.get_value("Customer", customer, "customer_name")
            frappe.throw(
                f"{nombre} ya tiene una recurrente activa ({dup['name']}, "
                f"${dup['monto']:,.2f}). Cancélala primero o marca que sí quieres dos."
            )

    _asegurar_credito(customer, dias_credito)

    company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
        "Global Defaults", "default_company"
    )
    cuenta_ingreso = frappe.db.get_value(
        "Account", {"company": company, "root_type": "Income", "is_group": 0}, "name"
    )
    centro_costo = frappe.db.get_value("Company", company, "cost_center")

    hoy = frappe.utils.today()
    inv = frappe.get_doc({
        "doctype": "Sales Invoice",
        "customer": customer,
        "company": company,
        "currency": "MXN",
        "conversion_rate": 1,
        "posting_date": hoy,
        "due_date": frappe.utils.add_days(hoy, frappe.utils.cint(dias_credito) or 15),
        "set_posting_time": 1,
        "disable_rounded_total": 1,
        "remarks": MARCA_RECURRENTE,
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

    if dia in (None, "", 0):
        dia = frappe.utils.getdate(hoy).day
    name = _crear_auto_repeat(inv.name, frecuencia, dia, hoy, fin)
    d = detalle_recurrente(name)
    d.update({"ok": True, "recurrente": name, "primera_factura": inv.name})
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
