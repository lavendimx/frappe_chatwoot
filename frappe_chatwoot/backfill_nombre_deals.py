# Copyright (c) 2026, lavendi.mx
"""Backfill de first_name/last_name en CRM Deal desde el contacto primario.

POR QUÉ
    El buscador de la lista de oportunidades (`Deals.vue` smartSearch) solo
    consulta columnas de `tabCRM Deal` (name, deal_name, organization, email,
    mobile_no, first_name, last_name) — nunca la tabla hija `CRM Contacts`.
    9 deals creados nativamente (7 de agente-ia@ el 14-sep, 2 desde la UI el
    16-sep) tienen first_name/last_name NULL: el nombre de la persona solo
    vive en `CRM Contacts.full_name` del contacto primario. Resultado: buscar
    "Rocío Rivera" no encuentra `CRM-DEAL-2026-03773` (rotula como "Luz de
    luna"). Caso reportado por Alejandro, 2026-09-18.

QUÉ HACE
    Para cada CRM Deal con first_name Y last_name vacíos, toma el
    `full_name` del contacto hijo con is_primary=1 y lo divide en la primera
    palabra (first_name) y el resto (last_name). Si no hay espacio, todo va a
    first_name y last_name queda vacío.

    Dry-run por defecto — solo informa qué cambiaría.

Uso: bench --site sofiav2.lavendi.mx execute frappe_chatwoot_backfill_nombre_deals.run
     bench --site sofiav2.lavendi.mx execute frappe_chatwoot_backfill_nombre_deals.run --kwargs "{'apply': 1}"
"""

import frappe


def run(apply: int = 0):
    apply = frappe.utils.cint(apply)
    deals = frappe.db.sql(
        """
        SELECT d.name, c.full_name
        FROM `tabCRM Deal` d
        JOIN `tabCRM Contacts` c ON c.parent = d.name AND c.is_primary = 1
        WHERE (d.first_name IS NULL OR d.first_name = '')
          AND (d.last_name IS NULL OR d.last_name = '')
          AND c.full_name IS NOT NULL AND c.full_name != ''
        """,
        as_dict=True,
    )

    cambios = []
    for d in deals:
        partes = d.full_name.strip().split(" ", 1)
        first = partes[0]
        last = partes[1] if len(partes) > 1 else ""
        cambios.append((d.name, first, last))
        print(f"{d.name}: full_name={d.full_name!r} -> first_name={first!r} last_name={last!r}")

    if not apply:
        print(f"\nDRY-RUN: {len(cambios)} deals cambiarían. Corre con apply=1 para aplicar.")
        return

    for name, first, last in cambios:
        frappe.db.set_value("CRM Deal", name, {"first_name": first, "last_name": last}, update_modified=False)
    frappe.db.commit()
    print(f"\nAplicado: {len(cambios)} deals actualizados (modified NO tocado).")
