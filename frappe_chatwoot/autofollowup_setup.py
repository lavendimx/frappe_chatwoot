# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Autofollowup: reactivación de conversaciones dejadas en visto (2026-09-24).

Réplica del bot action `advancedFollowup` de GHL Conversation AI. Plan:
`nuevosofia/planes/autofollowup-reactivacion-conversaciones.md` (paso 4).

QUÉ CREA
    1. `Chatwoot Followup` — doctype propio (nuevo). Un registro por
       conversación bajo reactivación: paso actual, próximo envío, estado.
       No reusa `Secuencia Inscripcion` (esa es para el motor de secuencias
       por producto, con su propia cadencia y cron) — mezclar los dos crearía
       una condición de carrera entre dos motores escribiendo la misma fila.
    2. `Agente IA Followup Paso` — tabla hija con la cadencia (espera + unidad
       por paso). Vive en `Agente IA` para que la configuración sea POR
       INBOX: cada cliente prende/apaga y ajusta cantidad/frecuencia sin tocar
       a los demás, y viaja por fixtures a cualquier sitio nuevo — decisión
       de Alejandro (2026-09-24): "debe estar disponible para todos, actuales
       y futuros".
    3. Custom Fields en `Agente IA`: interruptor, ventana horaria y tope por
       corrida. El CONTENIDO del mensaje NO se configura aquí — lo redacta el
       agente con el contexto real del hilo (decisión explícita de Alejandro).

POR QUÉ NO SE EXPONE "DÍAS DE LA SEMANA"
    La ventana es L-V estricta para todos por la misma razón de negocio (nadie
    decide una compra B2B el fin de semana; un mensaje comercial en domingo
    molesta) — es una regla del motor, no una preferencia por cliente. Vive
    hardcoded en `utils/autofollowup.py`, igual que el cron de `secuencias.py`
    hardcodea `1-5` en el crontab y no lo vuelve un campo.

Uso:
    bench --site crm.lavendi.mx execute frappe_chatwoot.autofollowup_setup.ejecutar
"""

import frappe

CAMPOS_PASO = [
    {"fieldname": "espera_valor", "fieldtype": "Int", "label": "Espera", "reqd": 1,
     "in_list_view": 1, "default": "1"},
    {"fieldname": "espera_unidad", "fieldtype": "Select", "label": "Unidad",
     "options": "Horas\nDías", "default": "Horas", "reqd": 1, "in_list_view": 1},
]

DOCTYPE_PASO = {
    "doctype": "DocType",
    "name": "Agente IA Followup Paso",
    "module": "Frappe Chatwoot",
    "custom": 1,
    "istable": 1,
    "editable_grid": 1,
    "fields": CAMPOS_PASO,
}

CAMPOS_FOLLOWUP = [
    {"fieldname": "conversation_id", "fieldtype": "Int", "label": "Conversación (Chatwoot)",
     "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
    {"fieldname": "inbox_id", "fieldtype": "Data", "label": "Inbox", "in_list_view": 1,
     "in_standard_filter": 1},
    {"fieldname": "col_1", "fieldtype": "Column Break"},
    {"fieldname": "deal", "fieldtype": "Link", "options": "CRM Deal", "label": "Oportunidad",
     "in_list_view": 1},
    {"fieldname": "contacto_nombre", "fieldtype": "Data", "label": "Contacto", "in_list_view": 1},
    {"fieldname": "sec_estado", "fieldtype": "Section Break", "label": "Estado"},
    {"fieldname": "estado", "fieldtype": "Select", "label": "Estado",
     "options": "Activa\nSalió por respuesta\nTerminada\nSalió a mano", "default": "Activa",
     "in_list_view": 1, "in_standard_filter": 1},
    {"fieldname": "paso_actual", "fieldtype": "Int", "label": "Paso actual", "default": "0",
     "in_list_view": 1},
    {"fieldname": "col_2", "fieldtype": "Column Break"},
    {"fieldname": "proximo_en", "fieldtype": "Datetime", "label": "Próximo envío",
     "in_list_view": 1},
    {"fieldname": "ultimo_out_at", "fieldtype": "Datetime", "label": "Último envío"},
    {"fieldname": "sec_detalle", "fieldtype": "Section Break", "label": "Detalle"},
    {"fieldname": "texto_ultimo", "fieldtype": "Small Text", "label": "Texto del último envío"},
    {"fieldname": "motivo", "fieldtype": "Data", "label": "Motivo (si salió)"},
]

DOCTYPE_FOLLOWUP = {
    "doctype": "DocType",
    "name": "Chatwoot Followup",
    "module": "Frappe Chatwoot",
    "custom": 1,
    "naming_rule": "Random",
    "autoname": "hash",
    "track_changes": 1,
    "sort_field": "modified",
    "sort_order": "DESC",
    "fields": CAMPOS_FOLLOWUP,
    "permissions": [
        {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
        {"role": "Sales Manager", "read": 1, "write": 1, "create": 1},
        {"role": "Sales User", "read": 1},
    ],
}

# Pasos de arranque para todo Agente IA nuevo: 3h / 1 día / 1 día — cadencia
# confirmada por Alejandro el 2026-09-24 para "reactivación rápida" (no la
# cola larga de hasta 5 pasos que permitía GHL). Cada inbox puede editarla.
PASOS_DEFAULT = [
    {"espera_valor": 3, "espera_unidad": "Horas"},
    {"espera_valor": 1, "espera_unidad": "Días"},
    {"espera_valor": 1, "espera_unidad": "Días"},
]

CAMPOS_AGENTE_IA = [
    {"fieldname": "sec_followup", "fieldtype": "Section Break",
     "label": "Seguimiento automático (reactivación)", "insert_after": "gate_intencion",
     "collapsible": 1},
    {"fieldname": "followup_activo", "fieldtype": "Check", "label": "Activo", "default": "0",
     "insert_after": "sec_followup",
     "description": "Manda hasta N mensajes de reactivación a hilos de prospecto "
                    "dejados en visto, dentro de la ventana horaria. Nunca a "
                    "clientes activos, hilos con humano reciente, deals cerrados "
                    "o ya inscritos en una Secuencia."},
    {"fieldname": "col_followup_1", "fieldtype": "Column Break", "insert_after": "followup_activo"},
    {"fieldname": "followup_ventana_inicio", "fieldtype": "Data", "label": "Ventana desde (HH:MM)",
     "default": "08:00", "insert_after": "col_followup_1"},
    {"fieldname": "followup_ventana_fin", "fieldtype": "Data", "label": "Ventana hasta (HH:MM)",
     "default": "18:00", "insert_after": "followup_ventana_inicio"},
    {"fieldname": "col_followup_2", "fieldtype": "Column Break", "insert_after": "followup_ventana_fin"},
    {"fieldname": "followup_max_por_corrida", "fieldtype": "Int", "label": "Máximo por corrida",
     "default": "4", "insert_after": "col_followup_2",
     "description": "Tope de mensajes de reactivación por corrida del job (anti-ban)."},
    {"fieldname": "sec_followup_pasos", "fieldtype": "Section Break", "label": "Pasos",
     "insert_after": "followup_max_por_corrida"},
    {"fieldname": "followup_pasos", "fieldtype": "Table", "options": "Agente IA Followup Paso",
     "label": "Cadencia de reactivación", "insert_after": "sec_followup_pasos"},
]


def _crear_doctype(spec: dict) -> None:
    if frappe.db.exists("DocType", spec["name"]):
        print(f"{spec['name']} ya existe")
        return
    frappe.get_doc(spec).insert(ignore_permissions=True)
    print(f"{spec['name']} creado")


def _agregar_campos(doctype: str, campos: list) -> None:
    for campo in campos:
        if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": campo["fieldname"]}):
            print(f"{doctype}.{campo['fieldname']} ya existe")
            continue
        frappe.get_doc({"doctype": "Custom Field", "dt": doctype, **campo}).insert(
            ignore_permissions=True)
        print(f"{doctype}.{campo['fieldname']} creado")


def ejecutar() -> None:
    _crear_doctype(DOCTYPE_PASO)
    _crear_doctype(DOCTYPE_FOLLOWUP)
    _agregar_campos("Agente IA", CAMPOS_AGENTE_IA)

    if not frappe.db.has_index("tabChatwoot Followup", "conversation_id_index"):
        frappe.db.add_index("Chatwoot Followup", ["conversation_id"], index_name="conversation_id_index")
        print("Índice conversation_id creado")

    # Siembra la cadencia default en cada Agente IA que aún no tenga pasos —
    # así el panel abre con algo editable en vez de vacío, y un inbox nuevo
    # (alta de cliente) nace con la reactivación lista para solo encenderla.
    for nombre in frappe.get_all("Agente IA", pluck="name"):
        doc = frappe.get_doc("Agente IA", nombre)
        if doc.get("followup_pasos"):
            continue
        for paso in PASOS_DEFAULT:
            doc.append("followup_pasos", paso)
        doc.save(ignore_permissions=True)
        print(f"Agente IA '{nombre}': cadencia default sembrada (3h/1d/1d)")

    frappe.db.commit()
    frappe.clear_cache(doctype="Agente IA")
    frappe.clear_cache(doctype="Chatwoot Followup")
    frappe.clear_cache(doctype="Agente IA Followup Paso")
    print("listo")
