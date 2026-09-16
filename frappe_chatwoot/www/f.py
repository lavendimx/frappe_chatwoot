# Copyright (c) 2026, lavendi.mx
"""`/f/<ruta>` — cualquier Web Form publicado, con el diseño de Sofía.

Toda la lógica vive en `api/formularios.py`; esto es la cáscara de ruta, igual
que `www/agenda/cita.py`. La ruta dinámica la resuelve `website_route_rules`
en `hooks.py`.
"""

from frappe_chatwoot.frappe_chatwoot.api.formularios import contexto_formulario

no_cache = 1


def get_context(context):
    return contexto_formulario(context)
