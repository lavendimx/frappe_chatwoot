"""`ghl_status` vacío deja el deal fuera de las vistas guardadas.

`ghl_status` nació en la migración de GHL (2026-09-06) y las vistas del embudo
filtran por él. Todo `CRM Deal` creado después por otra vía (campaña COPARMEX,
alta manual, conversión de lead) nace con el campo vacío y NO aparece en "Solo
activas" — quedó invisible sin que nadie lo notara. Se corrige el backlog y se
le pone default al campo para que no vuelva a pasar.
"""

import frappe


def run(aplicar=0):
    aplicar = int(aplicar)
    rs = frappe.db.sql("""select name, deal_name, status from `tabCRM Deal`
        where ifnull(ghl_status,'')='' and status not in ('Won','Lost')""", as_dict=1)
    print("modo:", "APLICAR" if aplicar else "SIMULACION", "| deals a marcar open:", len(rs))
    for r in rs:
        print("   ", r.name, "|", r.deal_name, "|", r.status)
    # Won/Lost sin ghl_status: se alinean a su status real, no a open.
    cerrados = frappe.db.sql("""select name, status from `tabCRM Deal`
        where ifnull(ghl_status,'')='' and status in ('Won','Lost')""", as_dict=1)
    print("cerrados sin ghl_status:", len(cerrados))
    if not aplicar:
        return
    for r in rs:
        frappe.db.set_value("CRM Deal", r.name, "ghl_status", "open", update_modified=False)
    for r in cerrados:
        frappe.db.set_value("CRM Deal", r.name, "ghl_status", r.status.lower(), update_modified=False)
    cf = frappe.get_doc("Custom Field", "CRM Deal-ghl_status")
    if cf.default != "open":
        cf.default = "open"
        cf.save(ignore_permissions=True)
        print("default del campo -> open")
    frappe.db.commit()
    print("listo.")
