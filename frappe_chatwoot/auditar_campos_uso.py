# Copyright (c) 2026, lavendi.mx
"""Auditoría de uso de campos por objeto (data-driven, no de opinión).

PARA QUÉ
    Decidir qué campos dejan de mostrarse no puede ser "creo que sobra": para
    cada campo mide **cuántos registros lo tienen lleno**. Un campo vacío en el
    100% de la base es candidato a ocultar/eliminar; uno con uso real se queda.

    También marca los campos que son **de sistema o los lee código** (los que no
    se pueden borrar aunque estén vacíos: los `ghl_*` los lee la migración,
    `chatwoot_conversation_id` lo usa el agente, `status` gobierna el kanban).

CÓMO
    Cuenta no-vacíos con un solo `SELECT COUNT(col)` por campo (una query por
    objeto, no una por campo). No lee ni escribe registros.

Uso: bench --site crm.lavendi.mx execute frappe_chatwoot.auditar_campos_uso.run
"""

import json

import frappe

# Objetos a auditar, en orden de importancia.
OBJETOS = ["CRM Deal", "CRM Lead", "Contact", "CRM Task", "CRM Organization"]

# Tipos que no son columnas reales en la base (layout, hijos, virtuales).
NO_COLUMNA = {
    "Section Break", "Column Break", "Tab Break", "Fold", "Heading",
    "HTML", "Button", "Image", "Table", "Table MultiSelect", "Geolocation",
    "Dynamic Link", "Attach", "Attach Image",
}

# Campos cuyo nombre delata que los lee código, no una persona: aunque estén
# vacíos no se pueden borrar sin romper algo. La columna es informativa —
# la decisión real se toma a mano en la revisión.
NUNCA_BORRAR = (
    "name", "status", "ghl_status", "ghl_stage", "ghl_producto", "ghl_contact_id",
    "ghl_opportunity_id", "ghl_pipeline", "ghl_task_id", "chatwoot_conversation_id",
    "chatwoot_inbox_id", "contact", "organization", "deal", "reference_docname",
    "reference_doctype", "mobile_no", "phone", "email", "email_id", "first_name",
    "last_name", "deal_owner", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "naming_series",
)


def _columnas(dt: str) -> list:
    meta = frappe.get_meta(dt)
    cols = []
    for f in meta.fields:
        if not f.fieldname or f.fieldtype in NO_COLUMNA:
            continue
        if f.fieldtype in ("Table", "Table MultiSelect"):
            continue
        cols.append(f)
    return cols


def _fill(dt: str, cols: list) -> dict:
    """% de registros con el campo no vacío. Una sola query por objeto."""
    tabla = f"`tab{dt}`"
    total = frappe.db.sql(f"SELECT COUNT(*) FROM {tabla}")[0][0] or 0
    if not total:
        return {"_total": 0}
    partes = ["COUNT(*)"]
    for f in cols:
        col = f"`{f.fieldname}`"
        partes.append(f"SUM(CASE WHEN {col} IS NULL OR {col} = '' THEN 0 ELSE 1 END)")
    fila = frappe.db.sql(f"SELECT {', '.join(partes)} FROM {tabla}")[0]
    res = {"_total": total}
    for i, f in enumerate(cols):
        res[f.fieldname] = int(fila[i + 1] or 0)
    return res


def run():
    salida = {}
    for dt in OBJETOS:
        cols = _columnas(dt)
        fill = _fill(dt, cols)
        total = fill.get("_total", 0)
        filas = []
        for f in cols:
            n = fill.get(f.fieldname, 0)
            filas.append({
                "fieldname": f.fieldname,
                "label": f.label,
                "fieldtype": f.fieldtype,
                "custom": bool(f.get("is_custom_field")),
                "read_only": bool(f.read_only),
                "llenado": n,
                "pct": round(100.0 * n / total, 1) if total else 0.0,
                "protegido": f.fieldname in NUNCA_BORRAR,
            })
        filas.sort(key=lambda x: (x["pct"], x["fieldname"]))
        salida[dt] = {"total_registros": total, "campos": filas}
        print(f"\n=== {dt} ({total} registros, {len(filas)} campos) ===")
        for x in filas:
            flag = "🔒" if x["protegido"] else ("·" if x["pct"] > 0 else "✗")
            print(f"  {flag} {x['pct']:>5}%  {x['fieldname']:<34} | {x['label']}")

    with open("/tmp/auditoria_campos.json", "w", encoding="utf-8") as fh:
        json.dump(salida, fh, ensure_ascii=False, indent=1)
    print("\nJSON -> /tmp/auditoria_campos.json")
    return {"objetos": list(salida), "json": "/tmp/auditoria_campos.json"}
