"""Migra el producto de la oportunidad desde GHL (2026-09-14). Idempotente.

    bench --site crm.lavendi.mx execute frappe_chatwoot.migrar_producto_oportunidad.ejecutar
    bench --site crm.lavendi.mx execute frappe_chatwoot.migrar_producto_oportunidad.ejecutar --kwargs "{'apply':1}"

POR QUÉ
    El workflow «4. Seguimientos» de GHL ramifica en 4 (PVP · SGPT · PWP · EGT)
    leyendo el custom field de OPORTUNIDAD `CqAxyMaOppRxwOiDOTT4`. Ese campo
    nunca llegó a Frappe: `migracion/load.py:87` mapeó `producto_principal`, que
    es el campo del FORMULARIO (vive en el contacto y lo llena el prospecto a
    mano — tiene valores como "curso venta Disney" y párrafos enteros). Son dos
    campos distintos; sin el de la oportunidad no se puede decidir a qué rama
    entra un deal, y una secuencia que le manda el copy de PWP a un prospecto de
    PVP es peor que no mandar nada.

QUÉ NO HACE
    No normaliza los 11 valores sueltos de texto libre ("PVP - ONLINE",
    "EGT con Google ads. ", "Curso Taller de Ventas Premium"…). Se guardan tal
    cual y quedan fuera de las 4 ramas — que es exactamente lo que hacía GHL con
    ellos: su if_else compara por igualdad exacta. Normalizarlos aquí metería a
    esos prospectos a una secuencia en la que su dueño nunca los puso.

    Tampoco toca las 205 oportunidades abiertas sin valor. En GHL no recibían
    seguimiento por la misma razón; inventarles un producto sería decidir por el
    vendedor a qué campaña entra su prospecto.
"""

import json
import os

import frappe

CAMPO_GHL = "CqAxyMaOppRxwOiDOTT4"

CAMPO = {
    "fieldname": "ghl_producto",
    "fieldtype": "Data",
    "label": "Producto (oportunidad)",
    "description": "PVP · SGPT · PWP · EGT. Viene del custom field de oportunidad de GHL; "
                   "es el que decide la rama de la secuencia de seguimiento.",
    "insert_after": "ghl_opp_name",
}

# El JSONL de la extracción vive fuera del contenedor; se copia al desplegar.
RUTA = os.path.join(os.path.dirname(__file__), "oportunidades.jsonl")


def _asegurar_campo(apply: bool) -> str:
    if frappe.db.exists("Custom Field", {"dt": "CRM Deal", "fieldname": CAMPO["fieldname"]}):
        return "ya existía"
    if not apply:
        return "se crearía"
    frappe.get_doc({"doctype": "Custom Field", "dt": "CRM Deal", **CAMPO}).insert(
        ignore_permissions=True
    )
    frappe.db.add_index("CRM Deal", ["ghl_producto"])
    return "creado"


def ejecutar(apply=0):
    apply = bool(frappe.utils.cint(apply))
    estado = _asegurar_campo(apply)
    print(f"CAMPO CRM Deal.ghl_producto: {estado}")
    if estado == "se crearía":
        print("DRY-RUN: corre con apply=1 para crear el campo y sembrar en una pasada.")
        return

    from collections import Counter

    escritos = ya = sin_deal = sin_valor = 0
    valores = Counter()
    with open(RUTA, encoding="utf-8") as fh:
        for linea in fh:
            o = json.loads(linea)
            valor = next(
                (f.get("fieldValueString") for f in (o.get("customFields") or [])
                 if f.get("id") == CAMPO_GHL),
                None,
            )
            if not valor:
                sin_valor += 1
                continue
            valor = valor.strip()
            deal = frappe.db.get_value("CRM Deal", {"ghl_opportunity_id": o["id"]}, "name")
            if not deal:
                sin_deal += 1
                continue
            actual = frappe.db.get_value("CRM Deal", deal, "ghl_producto")
            if actual == valor:
                ya += 1
                continue
            if apply:
                # `update_modified=False`: `modified` es la única señal que queda
                # de cuándo se tocó de verdad un deal, y un backfill masivo la
                # destruiría — es justo lo que ya pasó con las 226 facturas el
                # 10-sep y dejó `Sales Invoice.modified` inservible.
                frappe.db.set_value("CRM Deal", deal, "ghl_producto", valor,
                                    update_modified=False)
            escritos += 1
            valores[valor] += 1

    if apply:
        frappe.db.commit()
    print(json.dumps({
        "apply": apply,
        "escritos" if apply else "a_escribir": escritos,
        "ya_estaban": ya,
        "sin_deal_en_frappe": sin_deal,
        "sin_valor_en_ghl": sin_valor,
        "valores": valores.most_common(),
    }, ensure_ascii=False, indent=2))
