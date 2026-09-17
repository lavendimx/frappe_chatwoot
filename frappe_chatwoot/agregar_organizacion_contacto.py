# Copyright (c) 2026, lavendi.mx
"""Organización del contacto como catálogo, no texto libre — idempotente.

POR QUÉ
    `Contact.company_name` es un campo Data (texto libre): al editar la
    organización de un contacto no había forma de ver el catálogo de
    `CRM Organization` ya registrado ni de crear una organización nueva desde
    ahí (Alejandro, 2026-09-17). El resto del CRM (el campo `organization` de
    CRM Deal, la sección "Contacts" del Deal) ya usa un Link real con
    buscador + crear-al-vuelo.

QUÉ HACE
    1. Crea el campo `crm_organization` (Link -> CRM Organization) en Contact,
       justo antes de `company_name` en los layouts Side Panel y Quick Entry.
    2. NO toca `company_name`: sigue ahí, visible, para lo que no esté en el
       catálogo. Un hook aparte (`chatwoot_contactos.sincronizar_organizacion`,
       doc_event `validate`) refleja el nombre de la organización elegida en
       `company_name` cuando se liga — así todo lo que ya lee ese campo como
       texto (buscadores, `getOrganization()` del front, campañas de email,
       listas) sigue funcionando sin tocar ese código.
    3. Backfill CONSERVADOR: solo enlaza contactos cuyo `company_name` ya
       coincide (sin acentos ni mayúsculas) con una `CRM Organization`
       existente. No crea organizaciones nuevas a partir de texto libre — con
       ~4,200 contactos migrados de GHL, buena parte de `company_name` es
       basura (nombre de persona, frase suelta, vacío); auto-crear infla el
       catálogo con ruido. Lo que no matchea queda como hoy (company_name
       visible, sin Link) hasta que alguien lo asocie a mano con el selector.

Uso: bench --site crm.lavendi.mx execute agregar_organizacion_contacto.run
     (con apply=0 es dry-run; solo informa cuántos matchearían)
"""

import json
import unicodedata

import frappe

CAMPO = "crm_organization"
ANCLA = "company_name"
LAYOUTS = ("Contact-Side Panel", "Contact-Quick Entry")


def _normalizar(texto):
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return texto.strip().lower()


def _crear_campo():
    if frappe.db.exists("Custom Field", {"dt": "Contact", "fieldname": CAMPO}):
        return False
    frappe.get_doc(
        {
            "doctype": "Custom Field",
            "dt": "Contact",
            "fieldname": CAMPO,
            "label": "Organización (catálogo)",
            "fieldtype": "Link",
            "options": "CRM Organization",
            "insert_after": ANCLA,
            "translatable": 0,
        }
    ).insert(ignore_permissions=True)
    return True


def _agregar_a_columna(nodo, anchor, fieldname):
    if isinstance(nodo, dict):
        fields = nodo.get("fields")
        if isinstance(fields, list) and anchor in fields and fieldname not in fields:
            fields.insert(fields.index(anchor), fieldname)
            return True
        return any(_agregar_a_columna(v, anchor, fieldname) for v in nodo.values())
    if isinstance(nodo, list):
        return any(_agregar_a_columna(v, anchor, fieldname) for v in nodo)
    return False


def _actualizar_layouts():
    tocados = []
    for nombre in LAYOUTS:
        if not frappe.db.exists("CRM Fields Layout", nombre):
            continue
        doc = frappe.get_doc("CRM Fields Layout", nombre)
        layout = json.loads(doc.layout)
        if CAMPO in json.dumps(layout):
            continue
        if _agregar_a_columna(layout, ANCLA, CAMPO):
            doc.layout = json.dumps(layout)
            doc.save(ignore_permissions=True)
            tocados.append(nombre)
    return tocados


def _backfill(apply):
    mapa = {_normalizar(o): o for o in frappe.get_all("CRM Organization", pluck="name")}
    contactos = frappe.get_all(
        "Contact",
        filters=[["company_name", "is", "set"], [CAMPO, "is", "not set"]],
        fields=["name", "company_name"],
    )
    matches = [(c.name, mapa[_normalizar(c.company_name)]) for c in contactos if _normalizar(c.company_name) in mapa]
    if apply:
        for contacto, org in matches:
            frappe.db.set_value("Contact", contacto, CAMPO, org, update_modified=False)
    return len(contactos), len(matches)


def run(apply=1):
    apply = int(apply)
    creado = _crear_campo()
    layouts = _actualizar_layouts()
    total_sin_link, matcheados = _backfill(apply=bool(apply))
    if apply:
        frappe.db.commit()
        frappe.clear_cache(doctype="Contact")
    return {
        "campo_creado": creado,
        "layouts_actualizados": layouts,
        "contactos_con_texto_sin_link": total_sin_link,
        "backfill_matcheados_al_catalogo": matcheados,
        "apply": bool(apply),
    }
