# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Doctypes `Campana Email` + `Campana Email Paso` — contenedor de una campaña
como serie de N correos con cadencia, réplica de lo que ya resuelve
`Secuencia`/`Secuencia Paso` pero para un envío masivo a una lista, no un
seguimiento 1:1 a una oportunidad.

Plan: `planes/campanas-contenedor-y-cadencia.md` (paso 1).

POR QUÉ ESTOS DOS DOCTYPES Y NO CAMPOS SUELTOS EN `Newsletter`
    Los 4 campos `serie_*` (2026-09-18, ver `agregar_campos_serie_newsletter.py`)
    fueron el primer intento: texto declarativo repetido en cada `Newsletter`.
    No resuelven la queja real de Alejandro — "entrar a la campaña y ver cuántos
    emails y la secuencia entre cada uno" — porque un paso que todavía no se
    redacta **no existe en ningún `Newsletter`**, así que no hay dónde verlo.
    Quedan obsoletos: se ocultan del alta (Property Setter, no se borran).

    `Campana Email Paso` es tabla hija (`istable=1`) de `Campana Email`, mismo
    patrón que `Secuencia`/`Secuencia Paso` (ver `secuencias_setup.py`): el
    orden lo da el `idx` nativo de Frappe, no un campo propio.

`newsletter` VACÍO = TODAVÍA NO REDACTADO, A PROPÓSITO
    Es el punto central del plan: la campaña se ve completa (6 pasos) aunque
    solo el primero tenga contenido. El job que avanza la campaña se detiene
    en el primer paso sin `newsletter` ligado — nunca salta un hueco en
    silencio (ver `utils/campanas_email.py`).

ESTADO NO SE ALMACENA — SE CALCULA
    Un paso "enviado" ya lo sabe su `Newsletter.email_sent`; duplicarlo aquí
    crearía el mismo riesgo que ya se documentó con `secuencia_actual` en
    `CRM Deal` (denormalizar = un segundo lugar donde puede mentir la verdad).
    `api/campanas.py` calcula el estado al leer, no al escribir.

Uso:
    bench --site crm.lavendi.mx execute frappe_chatwoot.campana_email_setup.crear
"""

import frappe

CAMPOS_PASO = [
    {"fieldname": "titulo_paso", "fieldtype": "Data", "label": "Título del paso",
     "reqd": 1, "in_list_view": 1},
    {"fieldname": "espera_dias", "fieldtype": "Int", "label": "Días desde el paso anterior",
     "default": "0", "in_list_view": 1,
     "description": "0 en el primer paso = sale el día que se activa la campaña. "
                    "Ignorado si 'Fecha fija' tiene valor."},
    {"fieldname": "programado_para", "fieldtype": "Date", "label": "Fecha fija (opcional)",
     "in_list_view": 1,
     "description": "Si se declara, manda a esta fecha exacta en vez de calcular "
                    "por 'Días desde el paso anterior'."},
    {"fieldname": "newsletter", "fieldtype": "Link", "options": "Newsletter",
     "label": "Correo (Newsletter)", "in_list_view": 1,
     "description": "Vacío = paso declarado, contenido aún sin redactar. El "
                    "job de envío se detiene aquí y avisa."},
]

DOCTYPE_PASO = {
    "doctype": "DocType",
    "name": "Campana Email Paso",
    "module": "Frappe Chatwoot",
    "custom": 1,
    "istable": 1,
    "editable_grid": 1,
    "fields": CAMPOS_PASO,
}

CAMPOS_CAMPANA = [
    {"fieldname": "titulo", "fieldtype": "Data", "label": "Título de la campaña", "reqd": 1,
     "in_list_view": 1, "unique": 0},
    {"fieldname": "email_group", "fieldtype": "Link", "options": "Email Group",
     "label": "Lista de correo", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
    {"fieldname": "col_1", "fieldtype": "Column Break"},
    {"fieldname": "activa", "fieldtype": "Check", "label": "Programación activa",
     "default": "0", "in_list_view": 1,
     "description": "Si está encendida y el interruptor global "
                    "(Chatwoot Settings) también, el job manda cada paso solo "
                    "cuando le toca. Apagada = solo disparo manual por paso."},
    {"fieldname": "sec_remitente", "fieldtype": "Section Break", "label": "Remitente"},
    {"fieldname": "remitente_email", "fieldtype": "Data", "label": "Correo remitente",
     "description": "Vacío = el remitente por defecto del sitio."},
    {"fieldname": "col_2", "fieldtype": "Column Break"},
    {"fieldname": "remitente_nombre", "fieldtype": "Data", "label": "Nombre remitente",
     "default": "lavendi.mx"},
    {"fieldname": "sec_pasos", "fieldtype": "Section Break", "label": "Pasos"},
    {"fieldname": "pasos", "fieldtype": "Table", "options": "Campana Email Paso",
     "label": "Pasos"},
]

DOCTYPE_CAMPANA = {
    "doctype": "DocType",
    "name": "Campana Email",
    "module": "Frappe Chatwoot",
    "custom": 1,
    "naming_rule": "Random",
    "autoname": "hash",
    "track_changes": 1,
    "title_field": "titulo",
    "fields": CAMPOS_CAMPANA,
    "permissions": [
        {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
        {"role": "Sales Manager", "read": 1, "write": 1, "create": 1},
        {"role": "Sales User", "read": 1},
    ],
}


def crear():
    if frappe.db.exists("DocType", "Campana Email Paso"):
        print("Campana Email Paso ya existe")
    else:
        frappe.get_doc(DOCTYPE_PASO).insert(ignore_permissions=True)
        print("Campana Email Paso creado")

    if frappe.db.exists("DocType", "Campana Email"):
        print("Campana Email ya existe")
    else:
        frappe.get_doc(DOCTYPE_CAMPANA).insert(ignore_permissions=True)
        print("Campana Email creado")

    frappe.db.commit()
    frappe.clear_cache(doctype="Campana Email")
    frappe.clear_cache(doctype="Campana Email Paso")
    print("listo")
