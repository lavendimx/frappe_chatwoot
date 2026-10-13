# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Cron de sondeo (polling) de comentarios de Facebook/Instagram — ver el
docstring de `api/meta_comentarios.py` para por qué esto es polling y no
webhook (resumen: el objeto `page` de Meta solo admite UNA url de webhook por
app, y ya está tomada por Chatwoot/Messenger en producción).

Registrado en `hooks.py` cada 10 min. `sincronizar()` sale en la primera
línea si no hay token configurado — registrar el job no enciende nada."""

import frappe

from frappe_chatwoot.frappe_chatwoot.api.meta_comentarios import (
    _enviar_dm,
    _graph_get,
    _registrar_comentario,
    _responder_comentario,
    _settings,
)

POSTS_RECIENTES = 5  # suficiente para no perder comentarios entre corridas de 10 min;
# subir esto sale caro en llamadas a la Graph API sin ganar nada (los posts viejos no
# reciben comentarios nuevos en la práctica).


def sincronizar():
    settings = _settings()
    if not settings.fb_page_access_token:
        return  # nunca se conectó una página — nada que sondear

    token = settings.get_password("fb_page_access_token")

    try:
        _sincronizar_facebook(settings, token)
    except Exception as exc:  # noqa: BLE001 — un canal roto no debe tumbar el otro
        frappe.log_error(title="meta_comentarios_sync: error Facebook", message=str(exc))

    if settings.ig_business_account_id:
        try:
            _sincronizar_instagram(settings, token)
        except Exception as exc:  # noqa: BLE001
            frappe.log_error(title="meta_comentarios_sync: error Instagram", message=str(exc))

    settings.db_set("ultima_sincronizacion", frappe.utils.now_datetime(), update_modified=False)
    frappe.db.commit()


def _sincronizar_facebook(settings, token):
    posts = _graph_get(
        f"{settings.fb_page_id}/posts",
        {"fields": "id", "limit": POSTS_RECIENTES, "access_token": token},
    ).get("data", [])

    for post in posts:
        comentarios = _graph_get(
            f"{post['id']}/comments",
            {"fields": "id,message,from,created_time", "limit": 50, "access_token": token},
        ).get("data", [])
        for c in comentarios:
            remitente = c.get("from") or {}
            if str(remitente.get("id")) == str(settings.fb_page_id):
                continue  # la página comentando (ej. nuestra propia respuesta anterior)
            _procesar(settings, "Facebook", c["id"], post["id"], remitente.get("name", ""), c.get("message", ""))


def _sincronizar_instagram(settings, token):
    ig_id = settings.ig_business_account_id
    media = _graph_get(
        f"{ig_id}/media",
        {"fields": "id", "limit": POSTS_RECIENTES, "access_token": token},
    ).get("data", [])

    for item in media:
        comentarios = _graph_get(
            f"{item['id']}/comments",
            {"fields": "id,text,username,timestamp", "limit": 50, "access_token": token},
        ).get("data", [])
        for c in comentarios:
            # El endpoint de IG no expone un "id" de autor comparable a fb_page_id en este
            # payload — se filtra la auto-respuesta por username (guardado al conectar).
            # Sin este filtro, cada reply propio se leería como comentario nuevo en la
            # siguiente corrida -> bucle de auto-respuesta.
            if settings.ig_username and c.get("username") == settings.ig_username:
                continue
            _procesar(settings, "Instagram", c["id"], item["id"], c.get("username", ""), c.get("text", ""))


def _procesar(settings, canal, comment_id, post_id, remitente, texto):
    doc = _registrar_comentario(canal, comment_id, post_id, remitente, texto)
    if doc.estado == "Pendiente" and settings.activo:
        _responder_comentario(doc)
        # El DM va DESPUÉS y solo si la respuesta pública salió: el comentario dice
        # "Te enviamos mensaje privado", así que mandar el DM cuando la respuesta pública
        # falló dejaría al prospecto con un privado sin contexto; y prometerle el privado
        # sin mandarlo sería peor. Van juntos o no van.
        # Es best-effort: `_enviar_dm` se traga sus propios errores en `dm_estado`, nunca
        # tumba la corrida ni revierte el comentario ya respondido.
        if doc.estado == "Respondido":
            _enviar_dm(doc)
