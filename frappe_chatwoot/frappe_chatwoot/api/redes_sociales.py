"""Redes sociales (Ayrshare) — conectar Instagram/Facebook/etc. sin salir del CRM.

POR QUÉ EXISTE
    Objetivo: que un sitio (lavendi.mx u otro cliente) pueda conectar sus
    redes sociales vía Ayrshare desde el propio CRM, sin ir al dashboard de
    Ayrshare. Ayrshare modela cada cuenta que publica en redes como un
    "perfil" bajo una cuenta primaria de agencia (Business Plan): la cuenta
    primaria crea un perfil por cliente/sitio y cada perfil tiene su propio
    Profile Key para posteos/lecturas en su nombre.
    `generar_link_conexion()` es el flujo de onboarding HOSPEDADO de Ayrshare
    (`/api/profiles/generateJWT`): el usuario abre ese link y conecta sus
    cuentas de Instagram/Facebook/etc. sin que nosotros toquemos ninguna
    credencial suya.

⚠ VERIFICADO 2026-09-24 CONTRA LA API REAL (solo lecturas/intentos que
    Ayrshare rechazó antes de crear nada — sin efecto secundario, ver abajo):
    la API Key primaria registrada en credentials.md
    (`...ver credentials.md, no se expone aquí...`) hoy NO tiene el Business Plan
    activo. `GET /api/profiles` y `POST /api/profiles` devuelven
    `{"code":167,"action":"business plan","message":"The Business Plan is
    required to access this endpoint..."}`. Sin el Business Plan esa API Key
    es de UN SOLO perfil — el propio de lavendi.mx (facebook, gmb, linkedin,
    tiktok, twitter, confirmado vía `GET /api/user`) — y no puede crear
    perfiles nuevos por sitio/cliente.

    El código de abajo replica el modelo de "perfil por sitio" tal como lo
    pide el diseño (y tal como lo expone la propia API de Ayrshare). Hasta
    que se contrate el Business Plan, `crear_perfil`/`generar_link_conexion`
    van a fallar con ese mismo error de Ayrshare — capturado y relanzado con
    `frappe.throw`, nunca una excepción cruda de `requests`. `estado()` no
    llama a Ayrshare si todavía no hay `ayrshare_profile_key` guardado, así
    que no truena contra el gate del plan mientras nadie haya intentado
    conectar nada.

    NO se activó el Business Plan de Ayrshare en esta sesión — es un cargo
    recurrente nuevo y las compras/cargos requieren autorización explícita
    de Alejandro (doble confirmación, ver CLAUDE.md global). Verificado con
    2 llamadas de solo-lectura/rechazadas por Ayrshare antes de escribir
    nada (`GET /api/profiles`, `POST /api/profiles` con un título de prueba
    que Ayrshare rechazó por el gate del plan sin crear el perfil).

NO EXPONE LA API KEY AL FRONTEND
    El backend habla con Ayrshare; el frontend solo recibe estado (qué
    plataformas están conectadas) y URLs (el link hospedado de conexión). La
    API Key y el Profile Key nunca salen de Frappe — viven como `Password`
    en el Single `Redes Sociales`, igual que `Stripe Settings`.
"""

import frappe
import requests

from frappe_chatwoot.frappe_chatwoot.api.chatwoot import validate_role

API = "https://api.ayrshare.com/api"
TIMEOUT = 20


def _cfg():
    return frappe.get_cached_doc("Redes Sociales")


def _api_key() -> str:
    cfg = _cfg()
    key = cfg.get_password("ayrshare_api_key", raise_exception=False)
    if not key:
        frappe.throw("Redes Sociales: falta configurar la API Key de Ayrshare.")
    return key


def _headers(profile_key: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}
    if profile_key:
        headers["Profile-Key"] = profile_key
    return headers


def _ayrshare_error(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:300] or f"HTTP {resp.status_code}"
    return body.get("message") or body.get("error") or str(body)


@frappe.whitelist()
def estado() -> dict:
    """Qué plataformas están conectadas para el perfil de este sitio.

    No llama a Ayrshare si todavía no hay perfil creado (ayrshare_profile_key
    vacío) — no hay nada que consultar y así no se choca con el gate del
    Business Plan solo por abrir la pantalla."""
    validate_role()
    cfg = _cfg()
    if not cfg.enabled:
        return {"activo": False, "conectado": False, "plataformas": []}

    profile_key = cfg.get_password("ayrshare_profile_key", raise_exception=False)
    if not profile_key:
        return {
            "activo": True,
            "conectado": False,
            "plataformas": [],
            "mensaje": "Sin perfil creado todavía — usa generar_link_conexion().",
        }

    resp = requests.get(f"{API}/user", headers=_headers(profile_key), timeout=TIMEOUT)
    if resp.status_code != 200:
        frappe.throw(f"Ayrshare: {_ayrshare_error(resp)}")

    data = resp.json()
    activas = data.get("activeSocialAccounts") or []
    return {
        "activo": True,
        "conectado": bool(activas),
        "plataformas": activas,
        "detalle": data.get("displayNames") or [],
        "refId": data.get("refId"),
    }


def _crear_perfil_si_falta(cfg) -> str:
    """Crea el perfil de Ayrshare para este sitio la primera vez (requiere
    Business Plan en la cuenta primaria — ver docstring del módulo). Devuelve
    el Profile Key, existente o recién creado."""
    profile_key = cfg.get_password("ayrshare_profile_key", raise_exception=False)
    if profile_key:
        return profile_key

    titulo = (cfg.ayrshare_profile_title or frappe.local.site or "sitio").strip()
    resp = requests.post(
        f"{API}/profiles",
        headers=_headers(),
        json={"title": titulo},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        frappe.throw(f"Ayrshare (crear perfil): {_ayrshare_error(resp)}")

    body = resp.json()
    profile_key = body.get("profileKey")
    if not profile_key:
        frappe.throw("Ayrshare no devolvió un Profile Key al crear el perfil.")

    cfg.ayrshare_profile_key = profile_key
    cfg.ayrshare_ref_id = body.get("refId")
    if not cfg.ayrshare_profile_title:
        cfg.ayrshare_profile_title = titulo
    cfg.save(ignore_permissions=True)
    frappe.db.commit()
    return profile_key


@frappe.whitelist()
def generar_link_conexion() -> dict:
    """Devuelve el link hospedado de Ayrshare donde el usuario conecta sus
    cuentas de redes sociales para el perfil de este sitio. Crea el perfil
    en Ayrshare si todavía no existe (ver _crear_perfil_si_falta)."""
    validate_role()
    cfg = _cfg()
    if not cfg.enabled:
        frappe.throw("Redes Sociales está apagado — actívalo antes de generar el link.")

    profile_key = _crear_perfil_si_falta(cfg)

    resp = requests.post(
        f"{API}/profiles/generateJWT",
        headers=_headers(),
        json={"profileKey": profile_key, "redirect": True},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        frappe.throw(f"Ayrshare (link de conexión): {_ayrshare_error(resp)}")

    body = resp.json()
    url = body.get("url")
    if not url:
        frappe.throw("Ayrshare no devolvió el link de conexión.")
    return {"url": url}
