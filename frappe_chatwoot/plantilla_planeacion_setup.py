# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Doctype `Plantilla de Planeacion` — doctrina de preparación de llamada, como dato.

    bench --site crm.lavendi.mx execute frappe_chatwoot.plantilla_planeacion_setup.crear
    bench --site crm.lavendi.mx execute frappe_chatwoot.plantilla_planeacion_setup.sembrar --kwargs "{'apply':1}"

POR QUÉ UN DOCTYPE Y NO CÓDIGO
    "No todas las videollamadas son de ventas, pero todas requieren planeación"
    (Alejandro, 2026-09-18). El bloque 0 (contexto) y el objetivo/cierre cambian
    según el tipo de reunión, y la estructura completa (decisor, objeciones) es
    doctrina propia de lavendi.mx — para otro cliente del nuevo Sofía será otra.
    Un doctype la deja editable desde la UI sin deploy, igual que `Plantilla` y
    `Secuencia`. Ver `utils/planeacion_llamadas.py` para cómo se usa.

CÓMO SE ELIGE LA PLANTILLA
    `utils/planeacion_llamadas.py._elegir_plantilla()` decide por heurística
    (existencia de deal/lead abierto, `motivo`, `origen`) con desempate del LLM.
    Si sale poco confiable, agregar un campo explícito a `Reunion Agendada` y
    dejar de inferir — ver Supuestos abiertos del plan.
"""

import frappe

CAMPOS = [
    {"fieldname": "tipo_reunion", "fieldtype": "Select", "label": "Tipo de reunión",
     "options": "Venta\nCliente o Proyecto\nInterna", "reqd": 1,
     "in_list_view": 1, "in_standard_filter": 1},
    {"fieldname": "col_1", "fieldtype": "Column Break"},
    {"fieldname": "activa", "fieldtype": "Check", "label": "Activa", "default": "1",
     "in_list_view": 1},
    {"fieldname": "bloques", "fieldtype": "Text", "label": "Bloques que aplican",
     "description": "Documentación legible: qué secciones de la hoja 0-6 usa este "
                    "tipo de reunión y por qué. No lo lee el generador — orienta a "
                    "quien edite instrucciones_llm."},
    {"fieldname": "instrucciones_llm", "fieldtype": "Text", "label": "Instrucciones para el generador",
     "description": "Se concatena al prompt base de planeacion_llamadas.js. Aquí va "
                    "lo que cambia por tipo de reunión: qué preguntar, qué objetivo "
                    "buscar, qué NO aplica (ej. decisor/objeciones en una reunión "
                    "interna)."},
]

SEMILLA = [
    {
        "tipo_reunion": "Venta",
        "bloques": "0-6 completos. El bloque 6 (objeciones) sale como andamio vacío "
                   "para completar a mano — el generador nunca redacta manejo de "
                   "objeciones ni cifras de descuento.",
        "instrucciones_llm": (
            "Es una llamada de VENTA (cierre, seguimiento comercial o primera "
            "reunión con un prospecto). Bloque 1: confirma si hay declaración "
            "EXPLÍCITA de quién decide; si no la hay, \"No confirmado — verificar "
            "en la llamada\". Bloque 2: objetivo debe apuntar a avanzar o cerrar la "
            "venta. Bloque 3: mínimo 2 preguntas ancladas a lo que falta confirmar "
            "para cotizar o cerrar. Bloque 5: cierre esperado en términos de "
            "compromiso (pago, fecha de firma, segunda llamada de cierre)."
        ),
    },
    {
        "tipo_reunion": "Cliente o Proyecto",
        "bloques": "0-5. Sin bloque de objeciones — no es una negociación de "
                   "precio, es seguimiento de un proyecto ya contratado.",
        "instrucciones_llm": (
            "Es una llamada de SEGUIMIENTO con un cliente ya contratado (avance de "
            "proyecto, revisión de resultados, soporte). Bloque 1 no aplica como "
            "\"decisor\" sino como \"quién del equipo del cliente estará presente\". "
            "Bloque 2: objetivo en términos de qué se va a revisar o desbloquear. "
            "Bloque 3: preguntas sobre avance, pendientes o bloqueos. Bloque 5: "
            "cierre esperado en términos de próximos pasos del proyecto, no de "
            "venta. Omite cualquier mención a objeciones o precio."
        ),
    },
    {
        "tipo_reunion": "Interna",
        "bloques": "0, 2, 3, 5. Sin decisor externo (bloque 1) ni objeciones "
                   "(bloque 6) — es una reunión del propio equipo.",
        "instrucciones_llm": (
            "Es una reunión INTERNA del equipo de lavendi.mx (planeación, revisión "
            "de negocio, coordinación). No hay prospecto ni cliente externo "
            "tomando la decisión: omite el bloque 1 (decisor) y el 6 (objeciones) "
            "por completo, no los dejes vacíos. Bloque 0: contexto de qué se ha "
            "hablado antes sobre este tema, si lo hay. Bloque 2: objetivo concreto "
            "de la reunión. Bloque 3: puntos a resolver. Bloque 5: qué decisión o "
            "acuerdo se espera al terminar."
        ),
    },
]


def crear():
    if frappe.db.exists("DocType", "Plantilla de Planeacion"):
        print("Plantilla de Planeacion ya existe")
        return
    frappe.get_doc({
        "doctype": "DocType",
        "name": "Plantilla de Planeacion",
        "module": "Custom",
        "custom": 1,
        "naming_rule": "Random",
        "autoname": "hash",
        "track_changes": 1,
        "title_field": "tipo_reunion",
        "fields": CAMPOS,
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "Sales Manager", "read": 1, "write": 1, "create": 1},
            {"role": "Sales User", "read": 1, "write": 1, "create": 1},
        ],
    }).insert(ignore_permissions=True)
    print("Plantilla de Planeacion creada")


def sembrar(apply=0):
    apply = bool(frappe.utils.cint(apply))
    nuevas, ya = 0, 0
    for row in SEMILLA:
        if frappe.db.exists("Plantilla de Planeacion", {"tipo_reunion": row["tipo_reunion"]}):
            ya += 1
            continue
        if apply:
            frappe.get_doc({
                "doctype": "Plantilla de Planeacion",
                "activa": 1,
                **row,
            }).insert(ignore_permissions=True)
        nuevas += 1
    if apply:
        frappe.db.commit()
    print(f"Plantilla de Planeacion — semilla: {nuevas} nuevas · {ya} ya existían · apply={apply}")
    return {"nuevas": nuevas, "ya": ya, "apply": apply}
