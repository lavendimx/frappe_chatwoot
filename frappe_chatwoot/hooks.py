from . import __version__ as app_version

app_name = "frappe_chatwoot"
app_title = "Frappe Chatwoot"
app_publisher = "Hypedrive"
app_description = "Live-read Chatwoot conversation integration for Frappe. Chatwoot stays the system of record — no message duplication."
app_email = "9.shivamgupta.6@gmail.com"
app_license = "MIT"

# No custom Desk-wide JS needed today — the CRM-side integration (if/when the
# tab-embedded contract lands) lives inside the CRM frontend bundle itself,
# exactly like frappe_whatsapp's own app_include_js is unrelated to CRM's
# WhatsApp tab (that tab is compiled into CRM's own frontend, not loaded via
# this hook). Left here, commented, as the documented extension point:
# app_include_js = "/assets/frappe_chatwoot/js/frappe_chatwoot.js"

# ---------------------------------------------------------------------------
# Scheduler events
# ---------------------------------------------------------------------------
# Chatwoot's ActionCable feed is the "live" signal (see utils/realtime_bridge.py
# for why a persistent WS connection is NOT run as a bench worker in this
# deployment). Short-interval cron poll is the fallback that keeps
# publish_realtime signals flowing without a long-lived process. See the
# module docstring in realtime_bridge.py for the full tradeoff writeup.
scheduler_events = {
    "cron": {
        # every minute — cheap: HEAD-weight conversations list call, cached
        # response compared by (conversation_id, updated_at) to detect deltas.
        "* * * * *": [
            "frappe_chatwoot.utils.realtime_bridge.poll_and_broadcast",
            # Mensajes programados ("enviar más tarde") — sale en la primera
            # línea si no hay nada vencido. Ver utils/programados.py.
            "frappe_chatwoot.utils.programados.enviar_programados",
        ],
        # Recordatorio de videollamada 1 h antes — reemplaza el workflow 5 de
        # GHL. Cada 15 min sobre una ventana de 30, para que una corrida
        # saltada no pierda la cita. Sale en la primera línea si el
        # interruptor está apagado.
        "*/15 * * * *": [
            "frappe_chatwoot.utils.recordatorios.enviar_recordatorios",
        ],
        # Bienvenida a los leads del formulario del sitio — reemplaza la espera
        # de 5 min que los workflows 1.2/1.3 de GHL tenían dentro del árbol.
        # Barrido sin estado por la misma razón que el recordatorio: una espera
        # viva dentro de un proceso no sobrevive a un reinicio.
        "*/5 * * * *": [
            "frappe_chatwoot.utils.captacion.enviar_bienvenidas",
        ],
        # Motor de secuencias PVP — réplica de «4. Seguimientos» de GHL.
        # UNA corrida diaria dentro de la ventana 12-18 L-V; `avanzar()` sale en
        # la primera línea si `Chatwoot Settings.secuencias_activas` está en 0,
        # así que registrar el job NO enciende nada. Encender son DOS
        # interruptores: el global y `Secuencia.activa`. Habilitado por
        # Alejandro el 2026-09-15 (WhatsApp real a 28 deals).
        "0 15 * * 1-5": [
            "frappe_chatwoot.utils.secuencias.avanzar",
        ],
    },
}

# ---------------------------------------------------------------------------
# doc_events
# ---------------------------------------------------------------------------
# KB Source is a site-level custom doctype (Sofia RAG pipeline), not owned by
# this app — hooked here anyway since frappe_chatwoot is the glue-code app for
# the Sofia platform. before_insert forces inbox_id from the user's own
# User Permission, ignoring whatever a client-facing Web Form submission sent —
# the real tenant-isolation guarantee lives here, not in the form config.
#
# CRM Deal → onboarding: reemplaza el trigger del workflow 6 de GHL
# ("Activación de proyecto - Onboarding"), que hasta hoy era quien reaccionaba
# a una oportunidad ganada. El handler sale en la primera condición para todo
# guardado que no sea una transición a Ganada, así que no encarece el guardado
# normal de un deal.
doc_events = {
    "KB Source": {
        "before_insert": "frappe_chatwoot.utils.kb_isolation.set_inbox_from_user_permission",
    },
    "CRM Deal": {
        "on_update": "frappe_chatwoot.utils.onboarding.on_deal_update",
    },
    # Solicitud Web → Contacto + CRM Lead. Reemplaza el trigger "Formulario
    # Recibido" de los workflows 1.2 (PVP) y 1.3 (PWP/EGT) de GHL. El handler
    # nunca lanza: un fallo deja el error escrito en la solicitud, que sigue
    # existiendo — en GHL un árbol roto se veía como "no hubo leads".
    "Solicitud Web": {
        "after_insert": "frappe_chatwoot.utils.captacion.on_solicitud_insert",
    },
    # Contact → nombre en Chatwoot. La bandeja pinta el nombre de Chatwoot, no el
    # de Frappe, así que editar el nombre en el CRM no se veía en Conversaciones
    # (reportado por Alejandro el 2026-09-15). Frappe es la fuente de verdad.
    "Contact": {
        "on_update": "frappe_chatwoot.utils.chatwoot_contactos.sincronizar_nombre",
        "after_insert": "frappe_chatwoot.utils.chatwoot_contactos.sincronizar_nombre",
    },
    # CRM Task → contacto/organización/oportunidad: `CRM Task` solo trae un
    # vínculo, así que en el panel de Tareas se veían aisladas (2026-09-15).
    "CRM Task": {
        "validate": "frappe_chatwoot.utils.tareas.autollenar",
    },
}

# ---------------------------------------------------------------------------
# override_doctype_class
# ---------------------------------------------------------------------------
# Newsletter personalizado POR DESTINATARIO. El core renderiza el HTML una sola
# vez para toda la lista (verificado 2026-09-16), así que `{{ contact.first_name }}`
# salía literal. El override vive en `overrides/newsletter.py` y solo cambia
# `send_newsletter()` / `get_message()` — sobrevive a `bench update` porque no
# toca el core. Es pieza del sistema replicable a clientes (sustituye a GHL).
override_doctype_class = {
    "Newsletter": ["frappe_chatwoot.overrides.newsletter.Newsletter"],
}

# ---------------------------------------------------------------------------
# Fixtures / boilerplate hook surface — none needed today.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Módulo de formularios — renderer propio sobre el motor de Web Form
# ---------------------------------------------------------------------------
# `/f/<ruta>` sirve CUALQUIER Web Form publicado con el sistema de diseño de
# Sofía. Sustituye la plantilla de Frappe, no su motor: la definición del
# formulario, la validación y la escritura siguen siendo suyas (el envío viaja
# por su `accept()` whitelisted). Ver `api/formularios.py` para el porqué de la
# ruta aparte en vez de sobrescribir la clase `Web Form`.
website_route_rules = [
    {"from_route": "/f/<ruta>", "to_route": "f"},
]

# El CSP de embebido: una página `www/` no tiene gancho para poner headers, y
# sin `frame-ancestors` el iframe de lavendi.mx sale en blanco.
after_request = [
    "frappe_chatwoot.frappe_chatwoot.api.formularios.csp_embebido",
]

# ---------------------------------------------------------------------------
# Fixtures — la configuracion que vive en la DB y que debe viajar a cada sitio
# ---------------------------------------------------------------------------
# Los 15 doctypes propios ya viajan como codigo (frappe_chatwoot/doctype/*).
# Lo que sigue es lo que NO es doctype y hasta hoy solo existia en la DB de
# crm.lavendi.mx: campos, property setters, traducciones, permisos y los Web
# Forms del modulo de formularios. Con esto, un sitio nuevo recibe todo con
# `bench --site <sitio> install-app frappe_chatwoot` + `bench migrate`.
#
# Los filtros son deliberadamente acotados: NO se exportan los custom fields /
# property setters que ya genera erpnext o la propia app crm al instalarse
# (p. ej. Contact.is_billing_contact, Print Settings.*, los ~96 property
# setters de doctypes de ERPNext), porque esos se recrean solos y duplicarlos
# como fixture haria que un sitio de cliente los pisara con nuestra copia.

DOCTYPES_PROPIOS = [
    "Agente IA",
    "Chatwoot Pausa",
    "KB Inbox",
    "KB Source",
    "Mensaje Programado",
    "Mensaje Programado Adjunto",
    "Plantilla",
    "Reunion Agendada",
    "Secuencia",
    "Secuencia Inscripcion",
    "Secuencia Paso",
    "Sofia Push Settings",
    "Sofia Push Subscription",
    "Solicitud Web",
    "Stripe Settings",
]

# Doctypes de terceros (crm) que extendimos con campos propios.
DOCTYPES_EXTENDIDOS = [
    "CRM Deal",
    "CRM Lead",
    "CRM Task",
    "CRM Organization",
    "Contact",
    "Customer",
]

fixtures = [
    {"dt": "Custom Field", "filters": [["dt", "in", DOCTYPES_PROPIOS + DOCTYPES_EXTENDIDOS]]},
    {
        "dt": "Property Setter",
        "filters": [["doc_type", "in", DOCTYPES_PROPIOS + DOCTYPES_EXTENDIDOS + ["Assignment Rule", "FCRM Note"]]],
    },
    {"dt": "Translation", "filters": [["language", "=", "es"]]},
    {"dt": "Custom DocPerm", "filters": [["parent", "in", ["CRM Deal", "Customer", "Payment Entry", "Sales Invoice", "User"]]]},
    {"dt": "Web Form", "filters": [["module", "=", "Custom"]]},
    {"dt": "Web Form Field", "filters": [["parent", "in", ["base-de-conocimiento", "solicita-una-cotización-ahora"]]]},
]

# Datos de catalogo del embudo y de origen/perdida — no son doctypes propios,
# son REGISTROS de doctypes de crm que el equipo curó (16 etapas, 25 origenes,
# 11 razones de perdida). Sin esto un sitio nuevo nace con las 22 etapas en
# ingles de frappe/crm y sin los origenes de lavendi.mx.
fixtures += [
    {"dt": "CRM Deal Status"},
    {"dt": "CRM Lead Source"},
    {"dt": "CRM Lost Reason"},
]

# ---------------------------------------------------------------------------
# after_migrate — cierra el hueco que los fixtures no pueden cerrar
# ---------------------------------------------------------------------------
# Los fixtures hacen upsert, no borran. frappe/crm instala 22 etapas de embudo
# en ingles; el fixture agrega/actualiza las 16 de lavendi.mx pero dejaria las
# 6 nativas que produccion borro. Ver utils/provisionamiento.py.
after_migrate = [
    "frappe_chatwoot.utils.provisionamiento.ajustar_embudo",
]
