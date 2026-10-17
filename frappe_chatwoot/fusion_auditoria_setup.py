# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Backfill de `Chatwoot Fusion.mergee_contact_nombre` en filas existentes.

POR QUÉ
    La fusión de contactos por selección múltiple (`api/panel.py`,
    `fusionar_contactos_multiple`) agregó campos nuevos a `Chatwoot Fusion`
    (ver `doctype/chatwoot_fusion/chatwoot_fusion.json`) porque, en el nuevo
    flujo, `rename_doc(merge=True)` reescribe el Link `mergee_contact` hacia
    el `base` — sin un campo de texto plano aparte se pierde el rastro de qué
    `Contact` se borró. Las filas viejas (fusión solo-Chatwoot, sin tocar
    nunca el `Contact` de Frappe) todavía tienen `mergee_contact` intacto, así
    que el backfill es un simple snapshot: copiar el valor actual del Link al
    campo nuevo antes de que algún día se fusione también ese `Contact`.

    De paso, marca esas filas viejas como `estado="Chatwoot OK"` (ya hicieron
    la parte de Chatwoot, nunca la de Frappe) para que el reporte de la lista
    sea consistente entre filas viejas y nuevas.

    Dry-run por defecto — solo informa qué cambiaría.

Uso:
    bench --site crm.lavendi.mx execute frappe_chatwoot.fusion_auditoria_setup.ejecutar
    bench --site crm.lavendi.mx execute frappe_chatwoot.fusion_auditoria_setup.ejecutar --kwargs "{'apply': 1}"
"""

import frappe


def ejecutar(apply: int = 0):
    apply = frappe.utils.cint(apply)

    filas = frappe.get_all(
        "Chatwoot Fusion",
        filters={"mergee_contact": ["is", "set"], "mergee_contact_nombre": ["in", ["", None]]},
        fields=["name", "mergee_contact", "estado"],
    )

    for f in filas:
        print(f"{f.name}: mergee_contact_nombre <- {f.mergee_contact!r}"
              + ("" if f.estado else " (+ estado -> 'Chatwoot OK')"))

    if not apply:
        print(f"\nDRY-RUN: {len(filas)} filas cambiarían. Corre con apply=1 para aplicar.")
        return

    for f in filas:
        valores = {"mergee_contact_nombre": f.mergee_contact}
        if not f.estado:
            valores["estado"] = "Chatwoot OK"
        frappe.db.set_value("Chatwoot Fusion", f.name, valores, update_modified=False)
    frappe.db.commit()
    print(f"\nAplicado: {len(filas)} filas actualizadas (modified NO tocado).")
