"""Ajustes que un sitio nuevo necesita DESPUES de que corren los fixtures.

Los fixtures (`hooks.py`) hacen upsert: agregan y actualizan, pero NUNCA borran.
Eso deja un hueco de paridad: frappe/crm instala 22 etapas de embudo en ingles y
el fixture solo agrega/actualiza las 16 nuestras, asi que un sitio nuevo
quedaria con las 6 etapas nativas que en crm.lavendi.mx se borraron (cierre 17).

Esto corre en `after_migrate` (despues de los fixtures). Es idempotente y nunca
borra una etapa que tenga oportunidades: si algo la usa, la deja y anota el
motivo. Misma lista que `crm/limpiar_embudo.py`, el script con el que se limpio
produccion a mano.

Cada paso va en su propio try/except y su propio commit: si uno falla no debe
revertir lo que ya hizo el otro (paso real — un rename que choca hacia que se
perdieran los borrados de la misma corrida).
"""

import frappe

ETAPAS_A_BORRAR = [
    "Futuras",
    "Qualification",
    "Demo/Making",
    "Proposal/Quotation",
    "Negotiation",
    "Ready to Close",
]
VIEJO, NUEVO = "4to Seguiiento", "4to Seguimiento"


def ajustar_embudo():
    """Deja el embudo igual en todos los sitios. Nunca lanza: un fallo aqui no
    debe tumbar un migrate."""
    for paso in (_borrar_etapas_nativas, _corregir_typo):
        try:
            paso()
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"provisionamiento: {paso.__name__}")


def _borrar_etapas_nativas():
    for name in ETAPAS_A_BORRAR:
        if not frappe.db.exists("CRM Deal Status", name):
            continue
        if frappe.db.count("CRM Deal", {"status": name}):
            frappe.log_error(
                f"Etapa '{name}' tiene oportunidades; no se borro.", "provisionamiento"
            )
            continue
        frappe.delete_doc("CRM Deal Status", name, force=True, ignore_permissions=True)


def _corregir_typo():
    """En un sitio migrado de GHL pueden coexistir el typo y el nombre bueno.
    Si el bueno ya existe, el typo solo se borra si nadie lo usa."""
    if not frappe.db.exists("CRM Deal Status", VIEJO):
        return
    if frappe.db.exists("CRM Deal Status", NUEVO):
        if frappe.db.count("CRM Deal", {"status": VIEJO}):
            frappe.log_error(
                f"'{VIEJO}' tiene oportunidades y '{NUEVO}' tambien existe; no se toco.",
                "provisionamiento",
            )
            return
        frappe.delete_doc("CRM Deal Status", VIEJO, force=True, ignore_permissions=True)
        return
    frappe.rename_doc("CRM Deal Status", VIEJO, NUEVO, force=True)
