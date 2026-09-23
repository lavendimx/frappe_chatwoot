"""Backfill puntual: mete a la lista de nutricion los correos de los leads
frios del import GHL (2026-09-06). No envia nada; solo puebla la lista.
Idempotente (usa _agregar_uno, que no revive dados de baja ni duplica).

Uso: bench --site crm.lavendi.mx execute frappe_chatwoot.backfill_nutricion_leads.ejecutar --kwargs "{'apply':0}"
     ... apply=1 para escribir.
"""
import frappe
from frappe_chatwoot.frappe_chatwoot.api.campanas import _agregar_uno
from frappe_chatwoot.utils.nutricion import GRUPO, _asegurar_grupo


def ejecutar(apply=0):
    apply = frappe.utils.cint(apply)
    filas = frappe.db.sql(
        """SELECT DISTINCT email FROM `tabCRM Lead`
           WHERE DATE(creation)='2026-09-06' AND email IS NOT NULL AND email <> ''
             AND (status IS NULL OR status <> 'Converted')""",
        as_dict=True,
    )
    correos = [(f.email or '').strip().lower() for f in filas if f.email]
    baldes = {'agregados': 0, 'ya_existian': 0, 'invalidos': 0}
    if apply:
        _asegurar_grupo()
        for c in correos:
            r = _agregar_uno(GRUPO, c)
            if r:
                baldes[r[0]] = baldes.get(r[0], 0) + 1
        frappe.db.commit()
    return {'apply': apply, 'total_correos': len(correos), 'baldes': baldes,
            'muestra': correos[:8]}
