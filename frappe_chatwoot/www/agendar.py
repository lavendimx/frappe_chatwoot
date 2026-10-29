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

import re

import frappe
from frappe import _
from frappe.sessions import get_csrf_token

no_cache = 1

# Acento por defecto cuando el sitio no declaró un color de marca en su
# Website Theme (hoy ninguno de los 11 sitios lo tiene — ver `marca_del_sitio`
# abajo). Gris neutro de buen gusto en vez del índigo genérico que traía la
# plantilla original.
COLOR_ACENTO_DEFAULT = "#1f2937"
_HEX_VALIDO = re.compile(r"^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$")


def marca_del_sitio():
    """Nombre/logo/acento ya configurados por sitio — sin crear nada nuevo.

    Nombre y logo salen de `FCRM Settings`: el mismo singleton que ya usa el
    SPA del CRM (`BrandLogo.vue`) y que `utils/provisionamiento.py` llena con
    la marca de plataforma por default, reemplazable por sitio declarando
    `brand_app_name`/`brand_logo` en `site_config.json`
    (`branding_de_sitio()`). Se usa éste y no `Website Settings.app_name`/
    `app_logo` (que existen pero gobiernan el login/Desk de Frappe, no el
    SPA) para quedar consistente con lo que el cliente ya ve en su propio
    CRM.

    Hallazgo real (29-oct, verificado en 5 sitios vía `bench console`): los
    11 sitios siguen en el default de plataforma — ningún cliente declaró
    marca propia todavía — así que esta página pública muestra hoy "Sofía
    GPT by lavendi.mx" en todos. Sigue siendo mejor que la plantilla
    genérica sin ninguna marca de antes, y en cuanto un sitio declare su
    marca en `site_config.json` esta página la recoge sola, sin tocar código.

    El acento sale de `Website Theme.primary_color` si el sitio lo declaró
    (Link a la doctype `Color`, cuyo `name` es el hex). Ningún sitio lo tiene
    hoy (los 5 revisados están en el tema "Standard" de Frappe, sin color
    propio) — cae al gris neutro default.
    """
    fs = frappe.get_single("FCRM Settings")
    nombre = (fs.brand_name or "").strip() or None
    logo = (fs.brand_logo or "").strip() or None

    acento = COLOR_ACENTO_DEFAULT
    ws = frappe.get_single("Website Settings")
    if ws.website_theme:
        color = frappe.db.get_value("Website Theme", ws.website_theme, "primary_color")
        if color and _HEX_VALIDO.match(color.strip()):
            acento = color.strip()

    return nombre, logo, acento


def get_context(context):
    context.no_cache = 1
    slug = (frappe.form_dict.get("slug") or "").strip().strip("/").lower()
    context.slug = slug

    context.marca_nombre, context.marca_logo, context.color_acento = marca_del_sitio()

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
