"""Agrega utm_content, gbraid y wbraid a Solicitud Web + su Web Form.

Frente 2 de la migracion GHL: el iframe de captacion ahora propaga estos 3
parametros (attr-iframe.js, sitio), pero el doctype/Web Form aun no los
declara -> Frappe los descarta en silencio. Idempotente: si un campo ya
existe, no lo duplica.

Ejecutar dentro del contenedor:
    bench --site crm.lavendi.mx execute frappe_chatwoot.agregar_campos_utm.ejecutar
"""

import frappe

DOCTYPE = "Solicitud Web"
WEBFORM = "solicita-una-cotizaci\u00f3n-ahora"

CAMPOS_DOCTYPE = [
    {"fieldname": "utm_content", "fieldtype": "Data", "label": "UTM Content",
     "insert_after": "utm_term"},
    {"fieldname": "gbraid", "fieldtype": "Data", "label": "GBRAID",
     "insert_after": "gclid"},
    {"fieldname": "wbraid", "fieldtype": "Data", "label": "WBRAID",
     "insert_after": "gbraid"},
]

NUEVO_CLIENT_SCRIPT = """
frappe.ready(function () {
  const p = new URLSearchParams(window.location.search);
  const mapa = {
    utm_source: 'utm_source', utm_medium: 'utm_medium',
    utm_campaign: 'utm_campaign', utm_term: 'utm_term', utm_content: 'utm_content',
    gclid: 'gclid', gbraid: 'gbraid', wbraid: 'wbraid',
  };
  for (const [param, campo] of Object.entries(mapa)) {
    const v = p.get(param);
    if (v) { frappe.web_form.set_value(campo, v.slice(0, 140)); }
  }
  const pagina = p.get('pagina') || document.referrer || '';
  if (pagina) { frappe.web_form.set_value('pagina_origen', pagina.slice(0, 140)); }
});
"""


def agregar_campos_doctype():
    doc = frappe.get_doc("DocType", DOCTYPE)
    existentes = {f.fieldname for f in doc.fields}
    agregados = []
    for campo in CAMPOS_DOCTYPE:
        if campo["fieldname"] in existentes:
            continue
        doc.append("fields", campo)
        agregados.append(campo["fieldname"])
    if agregados:
        doc.save(ignore_permissions=True)
        print(f"{DOCTYPE}: agregados {agregados}")
    else:
        print(f"{DOCTYPE}: ya tenía los 3 campos, sin cambios")


def agregar_campos_webform():
    doc = frappe.get_doc("Web Form", WEBFORM)
    existentes = {f.fieldname for f in doc.web_form_fields}
    agregados = []
    for campo in ("utm_content", "gbraid", "wbraid"):
        if campo in existentes:
            continue
        doc.append("web_form_fields", {
            "fieldname": campo, "fieldtype": "Data", "label": campo, "hidden": 1,
        })
        agregados.append(campo)
    doc.client_script = NUEVO_CLIENT_SCRIPT
    doc.save(ignore_permissions=True)
    print(f"Web Form {WEBFORM}: agregados {agregados}, client_script actualizado")


def ejecutar():
    agregar_campos_doctype()
    agregar_campos_webform()
    frappe.db.commit()


if __name__ == "__main__":
    ejecutar()
