# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Gate de alcance por plan de Sofía GPT (Gratuito / Lite / Pro / Enterprise).

Plan: `nuevosofia/planes/...` (ver sofialite/planes/arquitectura-planes-sofia-gpt.md,
2026-09-25). Reemplaza el gate binario Lite/no-Lite de la versión anterior de este
archivo (huérfano, nunca importado) — ahora son 4 niveles, diferenciados por
features, sin límite de usuarios ni de registros en ninguno.

La señal de "en qué plan está este sitio" vive en `site_config.json`
(`sofia_plan` = "gratuito" | "lite" | "pro" | "enterprise"), NUNCA en código ni en un
doctype. El default es **fail-closed**: un sitio sin esa llave —o con un valor que no
está en `NIVELES`— se trata como **gratuito**, el nivel más bajo. Nunca sobre-otorga.

Cambio del 2026-09-27 (D-1 del registro de capacidades): antes el default era
**enterprise** (fail-open), "para que a nadie que ya paga se le bajara una función por
una llave que faltó" — pero eso restringía solo a quien se marcaba a mano, y los sitios
mal provisionados heredaban el nivel más permisivo. La precaución verificada antes de
invertirlo: ningún sitio existente dependía del default viejo (los 8 sitios de cliente
ya declaran `lite`, `crm`/`sofiav2` son `enterprise`, el único sin llave es
`erp-prueba.local`, laboratorio). `provisionamiento._plan_del_sitio` ahora escribe la
llave al alta con default restrictivo (D-5/O2) para que no vuelva a depender de que
alguien la ponga a mano.

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
    # Fail-closed (2026-09-27, D-1): sin llave o con valor desconocido, el nivel
    # más bajo. Antes era "enterprise", que sobre-otorgaba permiso a cualquier
    # sitio mal provisionado.
    return "gratuito"


def _es_agencia(user=None):
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    return "System Manager" in frappe.get_roles(user)


def _nivel_index(plan):
    try:
        return NIVELES.index(plan)
    except ValueError:
        # Valor desconocido en site_config: fail-closed, el nivel más bajo — no
        # sobre-otorgar por un typo de config. (Antes devolvía enterprise.)
        return NIVELES.index("gratuito")


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


# Campos de `FCRM Settings` que son de la plataforma, no del cliente: la marca del
# producto (los escribe Ajustes > Marca) y el menu de usuario (lo edita Ajustes >
# Home Actions, la MISMA tabla `dropdown_items` donde ocultamos "Apps"/"About").
CAMPOS_DE_PLATAFORMA = ("brand_name", "brand_logo", "favicon", "dropdown_items")


def proteger_campos_de_plataforma(doc, method=None):
    """doc_event `validate` de `FCRM Settings`: un cliente Lite/Gratuito no edita
    la marca ni el menu de usuario.

    Por que: `BrandSettings.vue` escribe `FCRM Settings.{brand_name,brand_logo,
    favicon}` y `HomeActions.vue` edita `dropdown_items` -- exactamente los campos
    que fija la plataforma. Un Sales Manager de un sitio Lite podia borrar el logo
    de Sofía o volver a meter los items de menu que ocultamos
    (`provisionamiento._dropdown_items_plataforma`). La UI ya los esconde por plan
    (`Settings.vue`, candado visual); esto es el candado REAL, por si se llama la
    API directa.

    Nunca estorba a la agencia (System Manager / `_es_agencia`), ni en migracion,
    install o import -- ahi corre el propio provisionamiento, que si toca esos
    campos a proposito. Reportado por Alejandro el 2026-09-26.
    """
    if (
        frappe.flags.in_migrate
        or frappe.flags.in_install
        or frappe.flags.in_patch
        or frappe.flags.in_import
    ):
        return
    if _es_agencia():
        return
    if _nivel_index(plan_de_este_sitio()) >= _nivel_index("pro"):
        return
    if _toco_campos_de_plataforma(doc):
        frappe.throw(
            _("Los ajustes de marca y el menú de usuario son parte de Sofía GPT y no se editan en tu plan actual."),
            frappe.PermissionError,
        )


def _firma_dropdown(doc):
    """Huella del menu de usuario (`dropdown_items`): que items hay, en que orden,
    con que etiqueta/ruta y si estan ocultos.

    No se compara la child table con `dict != dict`: `Document.__eq__` es por
    identidad, asi que dos cargas distintas del MISMO menu salen desiguales y el
    guard bloquearia cualquier guardado de `FCRM Settings`, aunque no tocara el
    menu (bug real detectado al probar: `service_provider` daba 403)."""
    return tuple(
        (d.name1, d.label, d.route, int(d.hidden or 0))
        for d in (doc.get("dropdown_items") or [])
    )


def _toco_campos_de_plataforma(doc):
    """Si ESTE guardado cambia marca o menu de usuario. Los escalares (marca) van
    por `has_value_changed`; la child table, por huella."""
    escalares = ("brand_name", "brand_logo", "favicon")
    if any(doc.has_value_changed(c) for c in escalares):
        return True
    antes = doc.get_doc_before_save()
    if not antes:
        return bool(doc.get("dropdown_items"))
    return _firma_dropdown(doc) != _firma_dropdown(antes)


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
