# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Mensajería Meta (Facebook) — conectar la página desde el CRM.

POR QUÉ EXISTE
    Para que el equipo conecte una página de Facebook a la bandeja de Chatwoot
    SIN entrar al admin crudo (chat.lavendi.mx/app), que expone TODAS las
    bandejas de la cuenta compartida (hueco de aislamiento entre clientes).

    Replica el flujo del propio frontend de Chatwoot (Facebook.vue +
    useFacebookPageConnect): el navegador carga el JS SDK de Facebook y hace
    `FB.login`; el `user_access_token` resultante viaja a este backend, que llama
    a los endpoints de Chatwoot:
        POST /callbacks/facebook_pages         -> lista de páginas del usuario
        POST /callbacks/register_facebook_page -> crea el inbox
    (contrato verificado en app/controllers/api/v1/accounts/callbacks_controller.rb
    y sus jbuilders: facebook_pages devuelve {"data": {page_details, user_access_token}};
    register_facebook_page devuelve el inbox desnudo.)

POR QUÉ UN PROXY Y NO LLAMAR A CHATWOOT DESDE EL NAVEGADOR
    El `api_token` de Chatwoot es un secreto de servidor y los endpoints de
    callbacks exigen un usuario administrador de la cuenta. El navegador nunca ve
    ese token; solo manda a este backend el token de Facebook, efímero y del
    propio usuario.

QUÉ NO HACE (a propósito)
    No registra el canal en el enrutamiento multicanal por sitio
    (`FRAPPE_SITES` / `default_inbox_id`): eso es el frente "canales multicanal
    por sitio", todavía pendiente. El inbox creado SÍ aparece en el selector de
    Conversaciones porque `list_inboxes` lista los inboxes de la cuenta.
"""

import frappe

from frappe_chatwoot.utils import chatwoot_client as cw


def _exigir_admin():
    """Conectar una página crea una bandeja en la cuenta COMPARTIDA de Chatwoot:
    es configuración, no navegación. Se exige System Manager o Sales Manager
    (mismo criterio que quien puede escribir `Chatwoot Settings`)."""
    roles = set(frappe.get_roles(frappe.session.user))
    if frappe.session.user != "Administrator" and not ({"System Manager", "Sales Manager"} & roles):
        frappe.throw("Solo un administrador de ventas puede conectar páginas.", frappe.PermissionError)


@frappe.whitelist()
def config() -> dict:
    """Config pública para el JS SDK. El App ID de Facebook es público; la clave
    secreta NO se expone — vive en Chatwoot y nunca sale de ahí."""
    _exigir_admin()
    cfg = frappe.get_cached_doc("Chatwoot Settings")
    return {
        "activo": bool(cfg.enabled),
        "fb_app_id": (cfg.get("fb_app_id") or "").strip(),
        "fb_api_version": (cfg.get("fb_api_version") or "v22.0").strip(),
    }


@frappe.whitelist()
def paginas_facebook(omniauth_token: str) -> dict:
    """Páginas que administra quien hizo el login de Facebook.

    Separa las que ya están conectadas (`exists: true` en Chatwoot) de las
    disponibles: conectar dos veces la misma página crea un inbox duplicado, así
    que se filtra aquí y no solo en la UI."""
    _exigir_admin()
    if not (omniauth_token or "").strip():
        frappe.throw("Falta el token de Facebook (omniauth_token).")

    data = cw._post("/callbacks/facebook_pages", {"omniauth_token": omniauth_token})
    payload = (data or {}).get("data") or {}
    paginas = payload.get("page_details") or []
    return {
        "disponibles": [p for p in paginas if not p.get("exists")],
        "ya_conectadas": [p for p in paginas if p.get("exists")],
        "user_access_token": payload.get("user_access_token") or omniauth_token,
    }


@frappe.whitelist()
def conectar_pagina(
    user_access_token: str,
    page_access_token: str,
    page_id: str,
    inbox_name: str,
) -> dict:
    """Crea el inbox de la página en Chatwoot. Devuelve el inbox creado."""
    _exigir_admin()
    faltantes = [
        nombre
        for nombre, valor in (
            ("user_access_token", user_access_token),
            ("page_access_token", page_access_token),
            ("page_id", page_id),
            ("inbox_name", inbox_name),
        )
        if not (valor or "").strip()
    ]
    if faltantes:
        frappe.throw(f"Faltan datos para conectar la página: {', '.join(faltantes)}")

    return cw._post(
        "/callbacks/register_facebook_page",
        {
            "user_access_token": user_access_token,
            "page_access_token": page_access_token,
            "page_id": str(page_id),
            "inbox_name": inbox_name.strip(),
        },
    ) or {}
