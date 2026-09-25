# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Gate de alcance por plan de Sofía GPT (Gratuito / Lite / Pro / Enterprise).

Plan: `nuevosofia/planes/...` (ver sofialite/planes/arquitectura-planes-sofia-gpt.md,
2026-09-25). Reemplaza el gate binario Lite/no-Lite de la versión anterior de este
archivo (huérfano, nunca importado) — ahora son 4 niveles, diferenciados por
features, sin límite de usuarios ni de registros en ninguno.

La señal de "en qué plan está este sitio" vive en `site_config.json`
(`sofia_plan` = "gratuito" | "lite" | "pro" | "enterprise"), NUNCA en código ni en un
doctype: un sitio sin esa llave (crm.lavendi.mx, estrublock.lavendi.mx a la fecha de
este archivo) se trata como **enterprise** — el default es el más permisivo a
propósito, para que ningún cliente que ya paga por una función deje de verla porque
le faltó una llave de config. Solo se restringe a quien se marca explícitamente.

"Gratuito" hereda todo lo que bloquea "lite" (Secuencias, Campañas, Agente IA,
Cobranza) por estar más abajo en `NIVELES` — no necesita gates propios. Además,
un sitio Gratuito nunca tiene infraestructura de agente (sin instancia Evolution,
sin inbox de Chatwoot, sin registro `Agente IA`): el bloqueo por ausencia de
infraestructura es la primera capa; este gate es la segunda, por si algún día se
factura de más o se reconfigura un sitio a mano.

Se exime System Manager a propósito: cubre a la agencia (nosotros) Y al usuario de
servicio del agente (`agente-ia@lavendi.mx`, System Manager en todo sitio) — el
agente sigue leyendo su propia config normal, esto solo bloquea lo que un usuario
del cliente (Sales User / Sales Manager) puede ver o tocar desde el SPA.
"""

import frappe
from frappe import _

NIVELES = ["gratuito", "lite", "pro", "enterprise"]


def plan_de_este_sitio():
    plan = frappe.conf.get("sofia_plan")
    if plan in NIVELES:
        return plan
    return "enterprise"


def _es_agencia(user=None):
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    return "System Manager" in frappe.get_roles(user)


def _nivel_index(plan):
    try:
        return NIVELES.index(plan)
    except ValueError:
        # Valor desconocido en site_config: no bloquear por un typo de config.
        return len(NIVELES) - 1


def exigir_plan_minimo(minimo, mensaje=None, user=None):
    """Llamar al inicio de cualquier endpoint whitelisted que requiera un
    plan igual o superior a `minimo` ("pro" o "enterprise"). No-op para
    usuarios de agencia o si el sitio ya cumple el mínimo."""
    if _es_agencia(user):
        return
    if _nivel_index(plan_de_este_sitio()) < _nivel_index(minimo):
        frappe.throw(
            mensaje
            or _("Este módulo no está incluido en tu plan actual de Sofía GPT."),
            frappe.PermissionError,
        )


def exigir_no_lite(mensaje=None):
    """Secuencias, Campañas de email, Agente IA (config), Cobranza — todo lo
    que Lite no incluye pero Pro y Enterprise sí."""
    exigir_plan_minimo("pro", mensaje)


def exigir_enterprise(mensaje=None):
    """Llamadas / voz IA y migración de histórico — exclusivo de Enterprise."""
    exigir_plan_minimo("enterprise", mensaje)


def _condicion_nivel(minimo, user):
    if _es_agencia(user):
        return ""
    if _nivel_index(plan_de_este_sitio()) < _nivel_index(minimo):
        return "1=0"
    return ""


def condicion_lista_no_lite(user):
    """Para `permission_query_conditions`: sin filas si el sitio es Lite y el
    usuario no es de agencia. "" (sin restricción) en cualquier otro caso."""
    return _condicion_nivel("pro", user)


def condicion_lista_enterprise(user):
    """Igual que `condicion_lista_no_lite` pero para módulos exclusivos de
    Enterprise (Llamadas)."""
    return _condicion_nivel("enterprise", user)


def _permiso_doc(minimo, doc, ptype=None, user=None):
    if _es_agencia(user):
        return None
    if _nivel_index(plan_de_este_sitio()) < _nivel_index(minimo):
        return False
    return None


def permiso_doc_no_lite(doc, ptype=None, user=None):
    """Para `has_permission`: deniega un documento puntual si el sitio es
    Lite. None (sin opinión, sigue el resto del stack normal de permisos) en
    cualquier otro caso."""
    return _permiso_doc("pro", doc, ptype, user)


def permiso_doc_enterprise(doc, ptype=None, user=None):
    """Igual que `permiso_doc_no_lite` pero exige Enterprise (Llamadas)."""
    return _permiso_doc("enterprise", doc, ptype, user)
