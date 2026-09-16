"""Renombra el campo trampa (honeypot) de `Solicitud Web`.

POR QUÉ (2026-09-16)
--------------------
El campo se llamaba `empresa_web` y estaba etiquetado "Empresa". El autofill del
navegador rellenó ese campo oculto en un envío REAL (SOL-2026-00029, Alexis
Solano, alexis.solano@lge.com — escribió "LG Electronics", su propio empleador)
y el handler descartó el lead como bot. Un lead de $17,500 perdido en silencio.

La trampa sigue siendo válida —un bot que rellena todo lo que encuentra la
llena— pero su nombre y su etiqueta eran un imán para el autofill de
Chrome/Safari, que ignora `autocomplete="off"` cuando huele a "empresa". Se
renombra a `referencia_adicional` (ningún token que dispare autofill) y se le
quita la etiqueta "Empresa".

Idempotente: si ya está renombrado, no hace nada.

Ejecutar dentro del contenedor:
    bench --site crm.lavendi.mx execute frappe_chatwoot.renombrar_campo_trampa.ejecutar
"""

import frappe
from frappe.model.utils.rename_field import rename_field

DOCTYPE = "Solicitud Web"
WEBFORM = "solicitar-cotizacion"
VIEJO = "empresa_web"
NUEVO = "referencia_adicional"


def _columnas(doctype):
    return {c[0] for c in frappe.db.sql(f"SHOW COLUMNS FROM `tab{doctype}`")}


def renombrar_doctype():
    doc = frappe.get_doc("DocType", DOCTYPE)
    campos = {f.fieldname for f in doc.fields}

    if NUEVO in campos and VIEJO not in campos:
        print(f"{DOCTYPE}: ya renombrado")
        return

    if VIEJO not in campos:
        print(f"{DOCTYPE}: no existe {VIEJO}; nada que hacer")
        return

    # 1. Agregar el campo nuevo (en la posición del viejo) y guardar: crea la columna.
    nuevo = {
        "fieldname": NUEVO, "fieldtype": "Data", "label": "Referencia",
        "hidden": 1, "insert_after": VIEJO,
    }
    doc.append("fields", nuevo)
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    if NUEVO not in _columnas(DOCTYPE):
        raise RuntimeError(f"la columna {NUEVO} no se creó al guardar el DocType")

    # 2. Copiar el valor y actualizar referencias (property setters, vistas, etc.).
    rename_field(DOCTYPE, VIEJO, NUEVO)
    frappe.db.commit()

    # 3. Quitar el campo viejo.
    doc.reload()
    doc.fields = [f for f in doc.fields if f.fieldname != VIEJO]
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    # 4. Frappe no borra la columna huérfana; se quita a mano.
    if VIEJO in _columnas(DOCTYPE):
        frappe.db.sql_ddl(f"ALTER TABLE `tab{DOCTYPE}` DROP COLUMN `{VIEJO}`")
        frappe.db.commit()

    print(f"{DOCTYPE}: {VIEJO} -> {NUEVO}")


def renombrar_webform():
    # El `name` del Web Form lleva acentos ("solicita-una-cotización-ahora");
    # se resuelve por su `route`, que es lo estable.
    nombre = frappe.db.get_value("Web Form", {"route": WEBFORM}, "name")
    if not nombre:
        raise RuntimeError(f"Web Form con route {WEBFORM} no encontrado")
    doc = frappe.get_doc("Web Form", nombre)
    cambiados = 0
    for f in doc.web_form_fields:
        if f.fieldname == VIEJO:
            f.fieldname = NUEVO
            cambiados += 1
    if cambiados:
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    print(f"Web Form {WEBFORM}: {cambiados} campo(s) renombrado(s)")


def ejecutar():
    renombrar_doctype()
    renombrar_webform()
    frappe.clear_cache(doctype=DOCTYPE)


if __name__ == "__main__":
    ejecutar()
