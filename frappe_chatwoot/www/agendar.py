# Copyright (c) 2026, lavendi.mx
"""`/agendar/<slug>` — página pública del agendamiento NATIVO (sin Google Calendar).

Cáscara de ruta, igual que `www/agenda/cita.py` y `www/f.py`: toda la lógica
real (slots, reserva) vive en `api/agenda_calendarios.py`. La ruta dinámica la
resuelve `website_route_rules` en `hooks.py` (`/agendar/<slug>` -> `agendar`,
mismo patrón que `/f/<ruta>` -> `f`), y el segmento dinámico llega aquí vía
`frappe.form_dict.get("slug")`.

Deliberadamente NO reemplaza ni toca `agenda.lavendi.mx` / `www/agenda/*` — es
un camino aditivo y paralelo, para cualquier sitio, no solo lavendi.mx.
"""

import frappe
from frappe import _
from frappe.sessions import get_csrf_token

no_cache = 1


def get_context(context):
    context.no_cache = 1
    slug = (frappe.form_dict.get("slug") or "").strip().strip("/").lower()
    context.slug = slug

    # `get_csrf_token()` genera uno si el visitante (guest) aún no tiene
    # sesión — mismo criterio ya documentado en `api/formularios.py` y
    # `api/agenda_publica.py`: leer el token sin generarlo da vacío para un
    # visitante nuevo, y un token vacío salta la validación de CSRF.
    context.csrf_token = get_csrf_token()

    context.error = None
    if not slug:
        context.error = _("Falta el identificador del calendario.")
        return context

    nombre = frappe.db.get_value(
        "Calendario Agendamiento", {"slug": slug, "activo": 1}, "name"
    )
    if not nombre:
        context.error = _("No existe (o está desactivado) un calendario de agendamiento con ese identificador.")

    return context
