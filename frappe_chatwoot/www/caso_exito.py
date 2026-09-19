# Copyright (c) 2026, lavendi.mx
"""Página pública que renderiza un "Caso de éxito" (DocType Caso Exito) como
one-page HTML/PDF con el diseño aprobado por Alejandro el 2026-09-18.

No genera el PDF aquí: /root/scripts/render-caso-exito.py (Playwright, host)
navega a esta URL y le toma un PDF, igual que ya se hacía a mano con los
HTML sueltos de campana-nutricion/. Esta página es la fuente única de verdad
del diseño — nuevos casos se crean y editan desde el Desk, no a mano en HTML.

`?name=<docname>` es el único parámetro. Sin nombre o con nombre inexistente
se muestra un estado de error simple; no hay datos sensibles que proteger
(los casos de éxito están pensados para volverse material público de todas
formas), así que no se exige login.
"""

import frappe

no_cache = 1


def get_context(context):
    context.no_cache = 1

    name = (frappe.form_dict.get("name") or "").strip()
    if not name or not frappe.db.exists("Caso Exito", name):
        context.error = "Caso de éxito no encontrado."
        return context

    doc = frappe.get_doc("Caso Exito", name)
    context.doc = doc
    context.error = None
    return context
