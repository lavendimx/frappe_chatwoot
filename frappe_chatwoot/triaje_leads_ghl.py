# Copyright (c) 2026, lavendi.mx
"""Triaje de los 573 `CRM Lead` no convertidos — Bloque 2, paso 6 del plan
`bandeja-colapsable-oportunidades-cobranza.md`.

POR QUÉ
    Desde el 2026-09-22 la captación crea `CRM Deal` (etapa Lead), no `CRM Lead`
    (Bloque 2, paso 4). Quedan 573 leads del import de GHL del 06-sep que nadie
    trabajó: 1 solo editado desde el import, 0 comunicaciones. La pantalla
    "Clientes potenciales" salió del sidebar (paso 5).

CRITERIO (medido 2026-09-22; cuadra 88 / 420 / 65 sobre 573)
    A — señal real (88): tiene `FCRM Note` o `CRM Task` ligados, o `source` en
        un canal de intención declarada (Videollamada, Formulario de cotización,
        Chat Widget, Link de pago, Sitio lavendi.mx, Formulario web).
        → crear `CRM Deal` en etapa Lead, con dueño (nunca vacío).
    B — solo contactable (420): sin señal, pero con email o teléfono.
        → lista de nutrición (solo los que traen correo).
    C — sin contacto (65): sin señal y sin email ni teléfono.
        → `status = "Junk"`.

⚠ `source` NULL: `NULL IN (...)` es NULL, no FALSE. El conteo original sin
`COALESCE` perdía 390 leads en silencio (daba 88/95/0 en vez de 88/420/65). El
`COALESCE` no es cosmético.

⚠ De los 420 de B solo ~170 traen correo; el resto solo teléfono. No se pueden
agregar a una lista de email — se reportan aparte y NO se inventan correos.

DRY-RUN por defecto. Uso:
    bench --site crm.lavendi.mx execute frappe_chatwoot.triaje_leads_ghl.run
    bench --site crm.lavendi.mx execute frappe_chatwoot.triaje_leads_ghl.run --kwargs "{'apply': 1}"
"""

import frappe

FUENTES_INTENCION = (
    "Videollamada", "Formulario de cotización", "Chat Widget",
    "Link de pago", "Sitio lavendi.mx", "Formulario web",
)
GRUPO_NUTRICION = "Nutrición - Oportunidades perdidas"


def _clasificar():
    filas = frappe.db.sql(
        """
        SELECT l.name, l.lead_name, l.email, l.mobile_no, l.source, l.organization,
               l.lead_owner, l.contact,
               (EXISTS(SELECT 1 FROM `tabFCRM Note` n
                        WHERE n.reference_doctype='CRM Lead' AND n.reference_docname=l.name)
                OR EXISTS(SELECT 1 FROM `tabCRM Task` t
                        WHERE t.reference_doctype='CRM Lead' AND t.reference_docname=l.name)
                OR COALESCE(l.source,'') IN %(fuentes)s) AS senal,
               (COALESCE(l.email,'')!='' OR COALESCE(l.mobile_no,'')!='') AS contactable
        FROM `tabCRM Lead` l
        WHERE l.converted=0
        """,
        {"fuentes": FUENTES_INTENCION},
        as_dict=True,
    )
    a, b, c = [], [], []
    for f in filas:
        if f.senal:
            a.append(f)
        elif f.contactable:
            b.append(f)
        else:
            c.append(f)
    return a, b, c


def _ya_tiene_deal(f):
    """Idempotencia: no crear dos veces. Mira el vínculo `lead` y el contacto."""
    if frappe.db.exists("CRM Deal", {"lead": f.name}):
        return True
    if f.contact and frappe.db.exists("CRM Deal", {"contact": f.contact}):
        return True
    return False


def _crear_deal(f):
    from frappe_chatwoot.utils.captacion import LEAD_OWNER_DEFAULT

    campos = {
        "doctype": "CRM Deal",
        "lead": f.name,
        "status": "Lead",
        "deal_owner": f.lead_owner or LEAD_OWNER_DEFAULT,
        "deal_name": f.lead_name or f.name,
        "source": f.source,
    }
    if f.contact:
        campos["contact"] = f.contact
        campos["contacts"] = [{"contact": f.contact, "is_primary": 1}]
    if f.organization:
        campos["organization_name"] = f.organization
    deal = frappe.get_doc(campos)
    deal.insert(ignore_permissions=True)
    # Un lead que ya tiene oportunidad está convertido — así lo marca el propio
    # `crm_lead.convert_to_deal`. Sin esto el lead sigue apareciendo en el
    # listado (que filtra `converted=0`) y "Clientes potenciales" no se vacía.
    frappe.db.set_value("CRM Lead", f.name, "converted", 1, update_modified=False)
    return deal.name


def run(apply: int = 0, muestra: int = 5):
    apply = frappe.utils.cint(apply)
    a, b, c = _clasificar()
    b_email = [f for f in b if (f.email or "").strip()]
    b_sin = [f for f in b if not (f.email or "").strip()]
    a_dup = [f for f in a if _ya_tiene_deal(f)]

    print(f"A senal real       : {len(a):4d}  (ya con Deal: {len(a_dup)})")
    print(f"B solo contactable : {len(b):4d}  (con correo: {len(b_email)} / solo tel: {len(b_sin)})")
    print(f"C sin contacto     : {len(c):4d}")
    print(f"TOTAL              : {len(a) + len(b) + len(c):4d}")
    for f in a[:muestra]:
        print(f"  A {f.name} {f.lead_name!r} source={f.source!r} org={f.organization!r}")
    for f in b[:muestra]:
        print(f"  B {f.name} {f.lead_name!r} email={f.email!r} tel={f.mobile_no!r}")
    for f in c[:muestra]:
        print(f"  C {f.name} {f.lead_name!r}")

    if not apply:
        print("\nDRY-RUN. Nada escrito. Corre con apply=1 para aplicar.")
        return {"a": len(a), "b": len(b), "c": len(c), "dry_run": True}

    creados = saltados = 0
    fallidos = []
    for f in a:
        if _ya_tiene_deal(f):
            saltados += 1
            continue
        try:
            _crear_deal(f)
            creados += 1
        except Exception as exc:
            # Sin el rollback, el insert fallido deja la transacción sucia y el
            # propio `log_error` se pierde en silencio (pasó con el lead de
            # prueba CRM-LEAD-2026-00640, cuyo Contact ya no existía).
            frappe.db.rollback()
            frappe.log_error(f"triaje {f.name}: {exc}", "triaje_leads_ghl")
            fallidos.append(f.name)
    print(f"A: {creados} deals creados, {saltados} saltados (ya tenían), {len(fallidos)} fallidos")
    if fallidos:
        print(f"A fallidos: {fallidos}")

    from frappe_chatwoot.frappe_chatwoot.api.campanas import _agregar_uno

    baldes = {}
    for f in b_email:
        balde, _ = _agregar_uno(GRUPO_NUTRICION, f.email) or (None, None)
        baldes[balde] = baldes.get(balde, 0) + 1
    print(f"B: {len(b_email)} correos procesados en la lista de nutrición -> {baldes}")
    print(f"B: {len(b_sin)} leads solo con teléfono, sin canal de email (no tocados)")

    for f in c:
        frappe.db.set_value("CRM Lead", f.name, "status", "Junk", update_modified=False)
    print(f"C: {len(c)} leads marcados Junk")

    frappe.db.commit()
    print("Aplicado y commiteado.")
    return {"a": len(a), "b": len(b), "c": len(c), "creados": creados, "dry_run": False}
