"""Nutrición por email de oportunidades perdidas.

Cuando un `CRM Deal` pasa a `Lost`, su contacto entra a la lista de email
"Nutrición - Oportunidades perdidas" (`Email Group`). No manda nada por sí
solo — solo puebla la lista; el envío de cada campaña sigue siendo manual vía
`api/campanas.py` (`enviar`), con el mismo gate de rol que cualquier otro
correo masivo.

Idempotencia: la propia `Email Group Member` es la marca. Si el deal se
reabre y se vuelve a perder, `_agregar_uno` ya lo encuentra y no hace nada —
no hace falta un campo nuevo en `CRM Deal` para esto.
"""

import frappe

GRUPO = "Nutrición - Oportunidades perdidas"


def _asegurar_grupo():
    if not frappe.db.exists("Email Group", GRUPO):
        frappe.get_doc({"doctype": "Email Group", "title": GRUPO}).insert(ignore_permissions=True)


def on_deal_update(doc, method=None):
    """Hook `CRM Deal.on_update`. Sale en la primera condición para cualquier
    guardado que no sea una transición a Lost — mismo criterio que
    `onboarding.on_deal_update` para no encarecer el guardado normal."""
    if doc.status != "Lost":
        return
    anterior = doc.get_doc_before_save()
    if anterior is not None and anterior.status == "Lost":
        return

    frappe.enqueue(
        "frappe_chatwoot.utils.nutricion.ejecutar",
        queue="short",
        timeout=60,
        deal_name=doc.name,
        enqueue_after_commit=True,
    )


def ejecutar(deal_name):
    from .onboarding import _email  # mismo fallback: CRM Deal.email -> Contact.email_id

    doc = frappe.get_doc("CRM Deal", deal_name)
    correo = _email(doc)
    if not correo:
        frappe.log_error(f"nutricion {deal_name}: el deal se perdió sin correo, no se pudo "
                         "agregar a la lista de nutrición", "CRM nutricion")
        return {"ok": False, "motivo": "sin correo"}

    _asegurar_grupo()
    from ..frappe_chatwoot.api.campanas import _agregar_uno

    balde, valor = _agregar_uno(GRUPO, correo) or (None, None)
    frappe.db.commit()
    return {"ok": True, "deal": deal_name, "correo": correo, "resultado": balde}
