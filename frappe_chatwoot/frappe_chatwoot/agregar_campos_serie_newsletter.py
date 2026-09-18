# Copyright (c) 2026, lavendi.mx
"""Campos de "serie" en `Newsletter` (idempotente).

POR QUÉ
    Alejandro (2026-09-18): en el panel de Campañas no hay forma de ver la
    estructura de una campaña que es en realidad una serie de varios correos
    (ej. COPARMEX: email 1 de 6) ni su cadencia ("cada campaña tiene una
    frecuencia diferente y una secuencia diferente"). `Newsletter` (core) no
    tiene ningún concepto de agrupación entre sí.

    Alcance v1 (decidido con Alejandro): solo VISIBILIDAD. Cada correo de la
    serie se sigue redactando y enviando a mano — no hay cron que dispare el
    siguiente paso solo. Automatizar el envío es justo el riesgo de reputación
    que ya quedó objetado en `planes/campanas-newsletter-secuencias.md`.

    Los 4 campos se declaran en cada `Newsletter` al crearlo (no se infieren):
    `serie` agrupa (mismo texto en los N correos), `serie_paso`/`serie_total_pasos`
    arman el badge "Paso 2 de 6", `serie_intervalo_dias` es la cadencia declarada
    respecto al paso anterior (dato informativo, no dispara nada).

Uso: bench --site sofiav2.lavendi.mx execute agregar_campos_serie_newsletter.run
"""

import frappe

CAMPOS = [
    # fieldname, label, fieldtype, insert_after
    ("serie", "Serie", "Data", "subject"),
    ("serie_paso", "Paso de la serie", "Int", "serie"),
    ("serie_total_pasos", "Total de pasos de la serie", "Int", "serie_paso"),
    ("serie_intervalo_dias", "Días desde el paso anterior", "Int", "serie_total_pasos"),
]


def run():
    for fieldname, label, fieldtype, insert_after in CAMPOS:
        if frappe.db.get_value("Custom Field", {"dt": "Newsletter", "fieldname": fieldname}, "name"):
            print(f"ya existe: {fieldname}")
            continue
        frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "Newsletter",
            "fieldname": fieldname,
            "label": label,
            "fieldtype": fieldtype,
            "insert_after": insert_after,
            "in_standard_filter": 1 if fieldname == "serie" else 0,
        }).insert(ignore_permissions=True)
        print(f"creado: {fieldname}")

    frappe.db.commit()
    frappe.clear_cache(doctype="Newsletter")
    print("listo")
