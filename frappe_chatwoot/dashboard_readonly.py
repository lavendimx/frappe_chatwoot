"""Lectura completa del CRM para el dashboard propio (dashboard.lavendi.mx),
sin concederle permisos de escritura.

POR QUÉ EXISTE ESTE ARCHIVO
    El dashboard usa el usuario de servicio `dashboard@lavendi.mx` con el rol
    `Dashboard Cobranza (lectura)`. Darle `read` sobre `CRM Deal` **no alcanza**:
    la app `crm` instala un `permission_query_conditions`
    (`crm.permissions.org_hierarchy`) que recorta la consulta a los tratos donde
    el usuario es `deal_owner` o está asignado por ToDo. Como el usuario de
    servicio no es dueño de nada, la API REST devolvía `{"data": []}` —
    silenciosamente, sin 403 y sin error.

    El hook solo exime a `Administrator`, `System Manager` y `Sales Manager`, y
    los tres tienen `write` sobre `CRM Deal`. Concederle cualquiera de ellos al
    dashboard le daría permiso de MODIFICAR el pipeline de ventas para poder
    LEERLO — a cambio de nada.

    Este método invierte ese trato: lectura total, escritura imposible.

POR QUÉ AQUÍ Y NO EN `apps/frappe/frappe/`
    `frappe_chatwoot` es app propia de lavendi.mx (repo lavendimx/frappe_chatwoot),
    así que sobrevive a `bench update`. Los `migracion_*.py` que viven dentro de
    `apps/frappe/` son scripts one-off desechables; esto es un endpoint permanente.

GARANTÍAS
    · Solo lectura: `frappe.get_all` sin ningún `set_value`/`insert`.
    · Solo `CRM Deal`: el doctype está fijo, no es un parámetro.
    · Solo campos de una lista blanca: pedir un campo fuera de ella es un error,
      no un silencio — así un consumidor nuevo no puede extraer de aquí datos
      que este endpoint no fue pensado para exponer.
    · Solo roles autorizados: cualquier otro recibe `PermissionError`.
"""

import json

import frappe

ROLES_PERMITIDOS = {"Dashboard Cobranza (lectura)", "System Manager", "Administrator"}

# Lista blanca. Todo lo que el dashboard grafica hoy y nada más. Si un panel
# nuevo necesita otro campo, se agrega aquí a propósito.
CAMPOS_PERMITIDOS = {
    "name", "ghl_opportunity_id", "ghl_opp_name", "ghl_contact_id", "ghl_pipeline", "ghl_stage",
    "ghl_status", "ghl_opp_created_at", "ghl_opp_updated_at", "ghl_date_added",
    "status", "deal_value", "expected_deal_value", "contact", "organization",
    "mobile_no", "deal_owner", "lost_reason", "source", "creation", "modified",
    "closed_date", "gclid_ads", "gbraid", "wbraid", "utm_source", "utm_campaign",
    "utm_term", "ghl_session_source", "ghl_medium",
}

CAMPOS_DEFAULT = ["name", "ghl_opportunity_id", "ghl_status", "ghl_stage",
                  "deal_value", "contact", "modified"]


def _autorizar():
    if not (ROLES_PERMITIDOS & set(frappe.get_roles())):
        frappe.throw("Sin permiso para leer el CRM desde este endpoint",
                     frappe.PermissionError)


@frappe.whitelist()
def deals(fields=None, filters=None, limit_page_length=0):
    """`CRM Deal` completo, saltándose el filtro por dueño de la app `crm`.

    `ignore_permissions=True` es deliberado y es TODO el punto del método: la
    autorización ya se hizo arriba, por rol, y aquí no hay ninguna ruta de
    escritura que ese flag pueda abrir.
    """
    _autorizar()

    if isinstance(fields, str):
        fields = json.loads(fields)
    fields = fields or CAMPOS_DEFAULT
    fuera = set(fields) - CAMPOS_PERMITIDOS
    if fuera:
        frappe.throw(f"Campos no permitidos en este endpoint: {sorted(fuera)}")

    if isinstance(filters, str):
        filters = json.loads(filters)

    return frappe.get_all(
        "CRM Deal",
        fields=fields,
        filters=filters or None,
        limit_page_length=int(limit_page_length or 0),
        ignore_permissions=True,
    )
