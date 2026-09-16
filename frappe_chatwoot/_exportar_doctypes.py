"""Exporta los doctypes propios del sitio a codigo dentro de esta app.

Los 15 doctypes nacieron como "Custom" en la DB del sitio (custom=1, app=NULL),
asi que no viajaban a un sitio nuevo. Este script los vuelca como archivos
`doctype/<scrub>/<scrub>.json` + `.py` con module="Frappe Chatwoot", que es lo
que `bench migrate` sincroniza en cualquier sitio donde la app este instalada.

Idempotente: sobrescribe los archivos. No toca la DB.

    bench --site crm.lavendi.mx execute frappe_chatwoot._exportar_doctypes.ejecutar
"""

import json
import os

import frappe

DOCTYPES = [
    "Agente IA",
    "Chatwoot Pausa",
    "KB Inbox",
    "KB Source",
    "Mensaje Programado",
    "Mensaje Programado Adjunto",
    "Plantilla",
    "Reunion Agendada",
    "Secuencia",
    "Secuencia Inscripcion",
    "Secuencia Paso",
    "Sofia Push Settings",
    "Sofia Push Subscription",
    "Solicitud Web",
    "Stripe Settings",
]

MODULE = "Frappe Chatwoot"
BASE = "/home/frappe/frappe-bench/apps/frappe_chatwoot/frappe_chatwoot/frappe_chatwoot/doctype"

VOLATILE = ("_user_tags", "_comments", "_assign", "_liked_by", "__islocal", "__unsaved")


def _limpiar(d):
    d.pop("custom", None)
    for k in VOLATILE:
        d.pop(k, None)
    for ft in ("fields", "permissions", "links", "actions", "states"):
        for row in d.get(ft) or []:
            for k in list(row.keys()):
                if k in VOLATILE or k in ("doctype", "parent", "parentfield", "parenttype"):
                    row.pop(k, None)
    return d


def ejecutar():
    frappe.flags.ignore_permissions = True
    os.makedirs(BASE, exist_ok=True)
    open(os.path.join(BASE, "__init__.py"), "a").close()

    resumen = []
    for name in DOCTYPES:
        doc = frappe.get_doc("DocType", name).as_dict()
        doc["module"] = MODULE
        _limpiar(doc)

        scrub = frappe.scrub(name)
        carpeta = os.path.join(BASE, scrub)
        os.makedirs(carpeta, exist_ok=True)
        open(os.path.join(carpeta, "__init__.py"), "a").close()

        with open(os.path.join(carpeta, scrub + ".json"), "w") as f:
            json.dump(doc, f, indent=1, sort_keys=True, default=str)

        controlador = os.path.join(carpeta, scrub + ".py")
        if not os.path.exists(controlador):
            clase = name.replace(" ", "").replace("-", "")
            with open(controlador, "w") as f:
                f.write(
                    "# Copyright (c) 2026, lavendi.mx\n"
                    "# MIT\n\n"
                    "from frappe.model.document import Document\n\n\n"
                    f"class {clase}(Document):\n\tpass\n"
                )
        resumen.append(
            (name, scrub, len(doc.get("fields") or []), len(doc.get("permissions") or []))
        )
    return resumen
