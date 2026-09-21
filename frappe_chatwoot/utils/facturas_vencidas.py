"""Aviso push de facturas vencidas — hueco identificado en el plan de push
notifications (2026-09-20): existía la pantalla /crm/facturacion (`api/facturacion.py`,
filtro "vencidas") pero nadie la escaneaba proactivamente; había que abrirla a mano
para enterarse. Este job cierra ese hueco, sin tocar `facturacion.py` ni el esquema de
`Sales Invoice` (hay otro trabajo en curso sobre CFDI en ese archivo).

Dedup: cache de Redis por factura (TTL 20h, no un campo nuevo en `Sales Invoice` — es
metadata operativa nuestra, no un dato contable/CFDI). Una factura que sigue vencida
al día siguiente vuelve a avisar; una que se paga o se cae de "Overdue" deja de
aparecer en la consulta y simplemente no se vuelve a avisar de ella.

Sin dueño individual claro por factura (el `Customer` migrado de GHL no siempre
resuelve a un solo `CRM Deal`/vendedor) — se avisa por rol, no a una persona, mismo
patrón que `notify_handover`. Roles = los que ya pueden ver /crm/facturacion
(`api/facturacion.py::ALLOWED_ROLES`).
"""

import frappe

from ..frappe_chatwoot.api import push

ROLES = ["System Manager", "Accounts Manager", "Accounts User", "Sales Manager"]
_CACHE_PREFIX = "sofia_push:factura_vencida:"
_TTL_SEGUNDOS = 20 * 60 * 60


def _activo():
    try:
        return bool(frappe.db.get_single_value("Chatwoot Settings", "aviso_facturas_vencidas_activo"))
    except Exception:
        return False


def avisar_vencidas():
    """Cron diario. Sale en la primera línea si el interruptor está apagado —
    registrar el job no enciende nada, mismo criterio que el resto de los cron de
    esta app."""
    if not _activo():
        return {"activo": False}

    vencidas = frappe.get_all(
        "Sales Invoice",
        filters={"status": "Overdue", "docstatus": 1},
        fields=["name", "customer", "outstanding_amount", "due_date"],
    )

    avisadas, ya_avisadas = [], []
    for f in vencidas:
        cache_key = _CACHE_PREFIX + f.name
        if frappe.cache().get_value(cache_key):
            ya_avisadas.append(f.name)
            continue
        try:
            monto = frappe.utils.fmt_money(f.outstanding_amount, currency="MXN")
            push.notify_roles(
                ROLES,
                title="Factura vencida",
                body=f"{f.customer} debe {monto} desde {f.due_date}",
                url="/crm/facturacion",
                tag=f"factura-{f.name}",
            )
            frappe.cache().set_value(cache_key, "1", expires_in_sec=_TTL_SEGUNDOS)
            avisadas.append(f.name)
        except Exception as exc:
            frappe.log_error(f"factura vencida {f.name}: {exc}", "Facturas vencidas (push)")

    return {"activo": True, "avisadas": avisadas, "ya_avisadas_hoy": ya_avisadas}
