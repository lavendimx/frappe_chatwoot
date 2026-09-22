# Copyright (c) 2026, lavendi.mx
"""Campo `titulo_plantilla` en `Secuencia Paso` (idempotente).

POR QUÉ
    El catálogo de plantillas del composer (`utils/plantillas.listar`) sirve las
    plantillas WhatsApp de la secuencia "4. Seguimientos — PVP" como filas
    virtuales derivadas de `Secuencia Paso`, en vez de copias en el doctype
    `Plantilla` que había que sincronizar a mano (ver plan
    plantillas-virtuales-desde-secuencia.md, /root/projects/ventas/supervisor).
    `nombre_ghl` no sirve como título visible — trae el nombre del nodo
    original de GHL, ya desactualizado (ej. "Videollamada (1er Seg. PVP)").

Uso: bench --site sofiav2.lavendi.mx execute \
    frappe_chatwoot.agregar_titulo_plantilla_secuencia.run
"""

import frappe

TITULOS = {
    2: "1er Seg. PVP — Costo de un equipo no profesionalizado",
    7: "2do Seg. PVP — Checklist de compra",
    12: "3er Seg. PVP — CRM dormido",
    17: "4to Seg. PVP — Reseñas",
    22: "5to Seg. PVP — Podcast",
    27: "6to Seg. PVP — Voz",
}


def run():
    if not frappe.db.get_value("Custom Field", {"dt": "Secuencia Paso", "fieldname": "titulo_plantilla"}, "name"):
        frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "Secuencia Paso",
            "fieldname": "titulo_plantilla",
            "label": "Título en catálogo de plantillas",
            "fieldtype": "Data",
            "insert_after": "nombre_ghl",
            "depends_on": "eval:['WhatsApp','Email'].includes(doc.tipo)",
            "description": "Título que ve el equipo en el selector de plantillas del composer. Vacío = usa el nodo GHL.",
        }).insert(ignore_permissions=True)
        print("creado: titulo_plantilla")
    else:
        print("ya existe: titulo_plantilla")

    secuencia = frappe.get_doc("Secuencia", "4. Seguimientos — PVP")
    tocados = 0
    for paso in secuencia.get("pasos", []):
        titulo = TITULOS.get(paso.idx)
        if titulo and paso.titulo_plantilla != titulo:
            paso.titulo_plantilla = titulo
            tocados += 1
    if tocados:
        secuencia.save(ignore_permissions=True)
    frappe.db.commit()
    frappe.clear_cache(doctype="Secuencia Paso")
    print(f"sembrados: {tocados} pasos")
