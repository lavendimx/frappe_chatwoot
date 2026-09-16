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
"""

import frappe


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

    return frappe.get_all(
        "Plantilla",
        filters=filtros,
        or_filters=or_filters,
        fields=["name", "titulo", "categoria", "texto", "adjunto"],
        order_by="categoria asc, titulo asc",
        limit_page_length=0,
    )
