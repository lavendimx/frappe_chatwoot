"""Ajustes que un sitio nuevo necesita DESPUES de que corren los fixtures.

Los fixtures (`hooks.py`) hacen upsert: agregan y actualizan, pero NUNCA borran.
Eso deja dos huecos que este modulo cierra en `after_migrate`:

1. **Embudo**: frappe/crm instala 22 etapas en ingles y el fixture solo
   agrega/actualiza las 16 nuestras, asi que quedarian las 6 nativas que en
   crm.lavendi.mx se borraron (cierre 17). Misma lista que `crm/limpiar_embudo.py`.

2. **Configuracion que depende de erpnext**: frappe/utils/fixtures.py envuelve el
   ARCHIVO COMPLETO en un try/except y lo salta entero si UN doctype falta. Los
   campos de `Customer`, los property setters de `Customer` y los permisos de
   `Customer`/`Sales Invoice`/`Payment Entry` no pueden vivir en `fixtures/` o un
   sitio sin erpnext se quedaria sin NINGUN campo. Viven en `fixtures/erpnext/` y
   se importan aqui solo si erpnext esta instalado.

Todo es idempotente y cada paso va en su propio try/except con su propio commit:
si uno falla no debe revertir lo que ya hizo el otro (paso real — un rename que
choca hacia que se perdieran los borrados de la misma corrida).
"""

import os

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

WEB_FORMS_CON_DUPLICADOS = ["base-de-conocimiento", "solicita-una-cotización-ahora"]


def ajustar_sitio():
    """Punto de entrada de `after_migrate`. Nunca lanza."""
    for paso in (
        _borrar_etapas_nativas,
        _corregir_typo,
        aplicar_fixtures_erpnext,
        deduplicar_web_form_fields,
    ):
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


def aplicar_fixtures_erpnext():
    """Importa `fixtures/erpnext/` si erpnext esta instalado. `import_doc` de
    Frappe acepta un directorio y recorre los .json de adentro."""
    if "erpnext" not in frappe.get_installed_apps():
        return
    ruta = os.path.join(frappe.get_app_path("frappe_chatwoot"), "fixtures", "erpnext")
    if not os.path.isdir(ruta):
        return
    from frappe.core.doctype.data_import.data_import import import_doc

    import_doc(ruta)


def deduplicar_web_form_fields():
    """Las child tables NO se exportan como fixture: al importarlas Frappe las
    vuelve a insertar y duplica los campos del formulario (paso el 2026-09-16 en
    crm.lavendi.mx, 24 campos -> 48). Los campos viajan dentro de `web_form.json`.
    Esto limpia los duplicados que ya se hayan creado; es idempotente."""
    filas = frappe.db.sql(
        """SELECT parent, fieldname, MIN(name) keep, COUNT(*) n
           FROM `tabWeb Form Field`
           WHERE parent IN %(parents)s
           GROUP BY parent, fieldname HAVING n > 1""",
        {"parents": tuple(WEB_FORMS_CON_DUPLICADOS)},
        as_dict=True,
    )
    for fila in filas:
        sobrantes = frappe.db.sql(
            """SELECT name FROM `tabWeb Form Field`
               WHERE parent=%s AND fieldname=%s AND name<>%s""",
            (fila.parent, fila.fieldname, fila.keep),
            pluck=True,
        )
        for name in sobrantes:
            frappe.db.delete("Web Form Field", {"name": name})
