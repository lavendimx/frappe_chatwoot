# Copyright (c) 2026, lavendi.mx
"""Página pública de agenda — perfil "leads". Ver `api/agenda_publica.py`."""

from frappe_chatwoot.frappe_chatwoot.api.agenda_publica import contexto_pagina


def get_context(context):
    return contexto_pagina(context, "leads")
