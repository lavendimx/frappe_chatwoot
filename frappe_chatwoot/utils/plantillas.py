# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Plantillas de mensaje (texto + adjunto) para el composer de Conversaciones.

Se copia a `apps/frappe_chatwoot/frappe_chatwoot/utils/plantillas.py`.

CONTEXTO
    Las 108 plantillas que el equipo usaba en GHL se rescataron el 2026-09-15 al
    doctype `Plantilla` (ver `patches/plantillas_setup.py`). Chatwoot trae
    "Respuestas predefinidas", pero son **solo texto**: 61 de las 95 de WhatsApp
    traen adjunto, así que mandarlas sin la imagen cambia el mensaje. De ahí el
    doctype propio y este endpoint para el selector del composer.

    El adjunto se guarda como URL servible (las de GHL se rehospedaron en
    `sofiav2.lavendi.mx/files/`). El envío lo lee del disco con `_leer_adjunto`
    (mismo camino que los programados y la secuencia): no hace falta resubirlo.

PLANTILLAS VIRTUALES (2026-09-21)
    Las 6 plantillas WhatsApp de "4. Seguimientos — PVP" se sincronizaron a mano
    al doctype `Plantilla` el 2026-09-21 y divergieron 2 veces en 3 días (el
    copy de la secuencia se edita más seguido que el catálogo). En vez de un
    espejo que hay que mantener, `listar()` sintetiza esas 6 filas en vivo desde
    `Secuencia Paso` — no hay copia que sincronizar. Las filas reales que las
    precedían quedaron con `activa=0` (se conservan, no se borran). Ver plan
    plantillas-virtuales-desde-secuencia.md (/root/projects/ventas/supervisor).
"""

import frappe


def _plantillas_virtuales(categoria=None):
    """Pasos WhatsApp con mensaje de secuencias activas, como filas de Plantilla."""
    filtros_secuencia = {"activa": 1}
    secuencias = frappe.get_all("Secuencia", filters=filtros_secuencia, fields=["name", "producto"])

    virtuales = []
    for sec in secuencias:
        sec_categoria = sec.producto or "PVP"
        if categoria and categoria != sec_categoria:
            continue
        pasos = frappe.get_all(
            "Secuencia Paso",
            filters={"parent": sec.name, "parenttype": "Secuencia", "tipo": "WhatsApp"},
            fields=["idx", "titulo_plantilla", "nombre_ghl", "mensaje", "adjunto_url"],
            order_by="idx asc",
        )
        for paso in pasos:
            if not paso.mensaje:
                continue
            virtuales.append({
                "name": f"seq:{sec.name}:{paso.idx}",
                "titulo": paso.titulo_plantilla or paso.nombre_ghl or f"Paso {paso.idx}",
                "categoria": sec_categoria,
                "texto": paso.mensaje,
                "adjunto": paso.adjunto_url,
            })
    return virtuales


@frappe.whitelist()
def listar(categoria=None, q=None, canal="WhatsApp"):
    """Plantillas activas para el selector. `q` busca en título y texto."""
    filtros = {"activa": 1}
    if canal:
        filtros["canal"] = canal
    if categoria:
        filtros["categoria"] = categoria

    or_filters = None
    if q and str(q).strip():
        like = f"%{str(q).strip()}%"
        or_filters = [["titulo", "like", like], ["texto", "like", like]]

    reales = frappe.get_all(
        "Plantilla",
        filters=filtros,
        or_filters=or_filters,
        fields=["name", "titulo", "categoria", "texto", "adjunto"],
        order_by="categoria asc, titulo asc",
        limit_page_length=0,
    )

    if not canal or canal == "WhatsApp":
        virtuales = _plantillas_virtuales(categoria=categoria)
        if q and str(q).strip():
            needle = str(q).strip().lower()
            virtuales = [
                v for v in virtuales
                if needle in (v["titulo"] or "").lower() or needle in (v["texto"] or "").lower()
            ]
        return virtuales + reales

    return reales
