# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Doctype `Plantilla` + carga de las 108 plantillas rescatadas de GHL (2026-09-15).

    bench --site crm.lavendi.mx execute frappe_chatwoot.plantillas_setup.crear
    bench --site crm.lavendi.mx execute frappe_chatwoot.plantillas_setup.cargar
    bench --site crm.lavendi.mx execute frappe_chatwoot.plantillas_setup.cargar --kwargs "{'apply':1}"

POR QUÉ UN DOCTYPE PROPIO Y NO "RESPUESTAS PREDEFINIDAS" DE CHATWOOT
    Chatwoot trae canned responses, pero son **solo texto**. Las 108 plantillas de
    GHL traían adjunto (61 de las 95 de WhatsApp, ya rehospedadas en
    `sofiav2.lavendi.mx/files/`): mandarlas sin la imagen cambia el mensaje. Un
    doctype propio permite texto + adjunto + categoría, y lo llena el equipo desde
    el CRM sin depender de Chatwoot.

CÓMO SE CARGAN
    El JSON (`plantillas_ghl.json`, generado en el host desde
    `workflows-ghl/rescate/plantillas-ghl.md` cruzando los adjuntos contra
    `media-map.json`) se copia junto a este módulo. Idempotente por `ghl_id`.
"""

import json
import os

import frappe

BASE = os.path.dirname(os.path.abspath(__file__))


def crear():
    if frappe.db.exists("DocType", "Plantilla"):
        print("Plantilla ya existe")
        return
    frappe.get_doc({
        "doctype": "DocType",
        "name": "Plantilla",
        "module": "Custom",
        "custom": 1,
        "naming_rule": "Random",
        "autoname": "hash",
        "track_changes": 1,
        "title_field": "titulo",
        "fields": [
            {"fieldname": "titulo", "fieldtype": "Data", "label": "Título",
             "reqd": 1, "in_list_view": 1},
            {"fieldname": "categoria", "fieldtype": "Select", "label": "Producto",
             "options": "General\nPVP\nSGPT\nPWP\nEGT", "default": "General",
             "in_list_view": 1, "in_standard_filter": 1},
            {"fieldname": "canal", "fieldtype": "Select", "label": "Canal",
             "options": "WhatsApp\nEmail", "default": "WhatsApp",
             "in_list_view": 1, "in_standard_filter": 1},
            {"fieldname": "activa", "fieldtype": "Check", "label": "Activa", "default": "1",
             "in_list_view": 1},
            {"fieldname": "col_1", "fieldtype": "Column Break"},
            {"fieldname": "texto", "fieldtype": "Text", "label": "Mensaje"},
            {"fieldname": "adjunto", "fieldtype": "Data", "label": "Adjunto",
             "description": "URL servible (/files/... o https). Las de GHL se "
                            "rehospedaron en sofiav2.lavendi.mx/files/."},
            {"fieldname": "ghl_id", "fieldtype": "Data", "label": "Id en GHL",
             "read_only": 1, "description": "Trazabilidad; la plantilla original muere "
                                            "con la subcuenta."},
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "Sales Manager", "read": 1, "write": 1, "create": 1},
            {"role": "Sales User", "read": 1, "write": 1, "create": 1},
        ],
    }).insert(ignore_permissions=True)
    print("Plantilla creada")


def cargar(apply=0):
    apply = bool(frappe.utils.cint(apply))
    with open(os.path.join(BASE, "plantillas_ghl.json"), encoding="utf-8") as f:
        datos = json.load(f)["plantillas"]

    nuevas, ya = 0, 0
    for p in datos:
        if p.get("ghl_id") and frappe.db.exists("Plantilla", {"ghl_id": p["ghl_id"]}):
            ya += 1
            continue
        if apply:
            frappe.get_doc({
                "doctype": "Plantilla",
                "titulo": p["titulo"][:140],
                "categoria": p["categoria"],
                "canal": p["canal"],
                "texto": p["texto"],
                "adjunto": p["adjunto"],
                "ghl_id": p.get("ghl_id"),
                "activa": 1,
            }).insert(ignore_permissions=True)
        nuevas += 1
    if apply:
        frappe.db.commit()
    print(f"Plantillas: {nuevas} nuevas · {ya} ya existían · apply={apply}")
    return {"nuevas": nuevas, "ya": ya, "apply": apply}
