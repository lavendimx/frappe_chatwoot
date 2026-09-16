# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Crea los doctypes de "Mensajes programados" (enviar más tarde).

Mismo patrón que `create_push_doctypes.py`: son doctypes "Custom", no parte del
app `frappe_chatwoot`. Idempotente — volver a correrlo no hace nada.

Ejecutar dentro del contenedor:
    bench --site crm.lavendi.mx execute frappe_chatwoot.create_programados_doctype.crear
"""

import frappe


def crear():
    crear_adjunto()
    crear_mensaje()


def crear_adjunto():
    if frappe.db.exists("DocType", "Mensaje Programado Adjunto"):
        print("Mensaje Programado Adjunto ya existe")
        return
    frappe.get_doc({
        "doctype": "DocType",
        "name": "Mensaje Programado Adjunto",
        "module": "Custom",
        "custom": 1,
        "istable": 1,
        "fields": [
            {"fieldname": "archivo", "fieldtype": "Data", "label": "Archivo",
             "reqd": 1, "in_list_view": 1,
             "description": "URL servible del archivo (/files/... o https). El envío lo lee de ahí."},
            {"fieldname": "nombre", "fieldtype": "Data", "label": "Nombre"},
            {"fieldname": "mime", "fieldtype": "Data", "label": "Tipo"},
        ],
    }).insert(ignore_permissions=True)
    print("Mensaje Programado Adjunto creado")


def crear_mensaje():
    if frappe.db.exists("DocType", "Mensaje Programado"):
        print("Mensaje Programado ya existe")
        return
    frappe.get_doc({
        "doctype": "DocType",
        "name": "Mensaje Programado",
        "module": "Custom",
        "custom": 1,
        "naming_rule": "Random",
        "autoname": "hash",
        "track_changes": 1,
        "title_field": "contenido",
        "fields": [
            {"fieldname": "conversation_id", "fieldtype": "Int", "label": "Conversación",
             "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
            {"fieldname": "inbox_id", "fieldtype": "Data", "label": "Inbox"},
            {"fieldname": "contenido", "fieldtype": "Long Text", "label": "Mensaje"},
            {"fieldname": "enviar_at", "fieldtype": "Datetime", "label": "Enviar a las",
             "reqd": 1, "in_list_view": 1},
            {"fieldname": "estado", "fieldtype": "Select", "label": "Estado",
             "options": "Pendiente\nEnviado\nCancelado\nError",
             "default": "Pendiente", "in_list_view": 1, "in_standard_filter": 1},
            {"fieldname": "enviado_at", "fieldtype": "Datetime", "label": "Enviado",
             "read_only": 1},
            {"fieldname": "motivo_cancelacion", "fieldtype": "Data",
             "label": "Motivo de cancelación", "read_only": 1},
            {"fieldname": "error", "fieldtype": "Small Text", "label": "Error",
             "read_only": 1},
            {"fieldname": "programado_por", "fieldtype": "Link", "options": "User",
             "label": "Programado por", "in_list_view": 1},
            {"fieldname": "adjuntos", "fieldtype": "Table", "label": "Adjuntos",
             "options": "Mensaje Programado Adjunto"},
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "Sales Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "Sales User", "read": 1, "write": 1, "create": 1},
        ],
    }).insert(ignore_permissions=True)
    print("Mensaje Programado creado")
