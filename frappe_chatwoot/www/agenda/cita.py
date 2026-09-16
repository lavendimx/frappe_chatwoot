# Copyright (c) 2026, lavendi.mx
"""Página donde el cliente ve, cancela o reagenda su cita. Ver `api/agenda_publica.py`.

El token llega en `?t=...` — es lo único que el visitante tiene, porque viene escrito en la
invitación de Google que recibió al reservar. Sin token la página no consulta nada.
"""

from frappe_chatwoot.frappe_chatwoot.api.agenda_publica import contexto_cita


def get_context(context):
    return contexto_cita(context)
