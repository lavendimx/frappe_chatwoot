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

    # Un aviso por factura sería una avalancha: al escribir esto hay 11 vencidas,
    # o sea 11 notificaciones seguidas a las 8 de la mañana — el patrón que vuelve
    # ignorable el canal (misma razón por la que el push de conversaciones avisa
    # una sola vez por hilo, bitácora 2026-09-05). Se manda UN aviso agregado y el
    # detalle se ve en /crm/facturacion, que ya existe.
    nuevas = [f for f in vencidas if not frappe.cache().get_value(_CACHE_PREFIX + f.name)]
    ya_avisadas = [f.name for f in vencidas if f not in nuevas]

    if not nuevas:
        return {"activo": True, "avisadas": [], "ya_avisadas_hoy": ya_avisadas}

    total = frappe.utils.fmt_money(
        sum(f.outstanding_amount or 0 for f in vencidas), currency="MXN"
    )
    if len(vencidas) == 1:
        f = vencidas[0]
        monto = frappe.utils.fmt_money(f.outstanding_amount, currency="MXN")
        cuerpo = f"{f.customer} debe {monto} desde {f.due_date}"
    else:
        cuerpo = f"{len(vencidas)} facturas vencidas · {total} por cobrar"

    try:
        push.notify_roles(
            ROLES,
            title="Cobranza vencida",
            body=cuerpo,
            url="/crm/facturacion",
            # tag fijo: un aviso nuevo reemplaza al anterior en la pantalla en vez
            # de apilarse día tras día.
            tag="facturas-vencidas",
        )
    except Exception as exc:
        frappe.log_error(f"aviso de cobranza vencida: {exc}", "Facturas vencidas (push)")
        return {"activo": True, "avisadas": [], "error": str(exc)}

    for f in nuevas:
        frappe.cache().set_value(_CACHE_PREFIX + f.name, "1", expires_in_sec=_TTL_SEGUNDOS)

    return {
        "activo": True,
        "avisadas": [f.name for f in nuevas],
        "ya_avisadas_hoy": ya_avisadas,
        "total_vencido": total,
    }
