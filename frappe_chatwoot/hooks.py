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
        # GHL. Cada 15 min sobre una ventana asimétrica [-15, +3] min respecto
        # a los 60, para que una corrida saltada no pierda la cita sin que el
        # primer tick la marque a 75 min. Sale en la primera línea si el
        # interruptor está apagado.
        "*/15 * * * *": [
            "frappe_chatwoot.utils.recordatorios.enviar_recordatorios",
        ],
        # Motor de secuencias PVP — réplica de «4. Seguimientos» de GHL.
        # `avanzar()` sale en la primera línea si
        # `Chatwoot Settings.secuencias_activas` está en 0, así que registrar el
        # job NO enciende nada. Encender son DOS interruptores: el global y
        # `Secuencia.activa`. Habilitado por Alejandro el 2026-09-15 (WhatsApp
        # real a 28 deals).
        #
        # Una corrida por hora dentro de la ventana laboral 12-18 L-V (7 al día),
        # no una sola: con `Secuencia.max_por_corrida = 6` el tope real pasa de 6
        # a 42 mensajes diarios, que es lo que hace falta para drenar un atraso
        # sin mandar una ráfaga concentrada a la misma hora. Cambiado por
        # Alejandro el 2026-09-17, junto con la inserción del checklist de compra
        # como 2do seguimiento.
        #
        # OJO: este valor es la fuente de verdad. `bench migrate` resincroniza
        # `Scheduled Job Type` desde aquí — editar solo la fila de la DB se
        # revierte al siguiente migrate (fue lo que pasó con el intento del
        # 2026-09-16 de moverlo a `0 12`, que nunca llegó a estar vivo).
        "0 12-18 * * 1-5": [
            "frappe_chatwoot.utils.secuencias.avanzar",
        ],
        # Preparación de llamada v2 — reemplaza el script GHL de
        # /root/projects/ventas (retirado). El disparo NO es una ventana
        # antes de la cita: es "al detectar la cita nueva" (Alejandro,
        # 2026-09-18), así que basta un barrido horario en horario hábil —
        # el volumen real es ~8 citas en dos semanas. Sale en la primera
        # línea si `Chatwoot Settings.planeacion_llamadas_activo` está en 0.
        "0 7-21 * * *": [
            "frappe_chatwoot.utils.planeacion_llamadas.generar_planeaciones",
        ],
        # Campañas de email programadas — plan campanas-contenedor-y-cadencia.md.
        # Barrido diario en horario laboral, no por hora: la cadencia se mide
        # en días (espera_dias), no en minutos, así que una corrida diaria
        # basta. Sale en la primera línea si Chatwoot Settings.campanas_automaticas
        # está en 0 (nace apagado) — registrar el job NO enciende nada.
        "0 9 * * 1-5": [
            "frappe_chatwoot.utils.campana_email.avanzar",
        ],
        # Aviso push de facturas vencidas — no existía ningún escaneo proactivo,
        # solo la pantalla /crm/facturacion que había que abrir a mano. Una vez
        # al día (no cada hora: "Overdue" no cambia dentro del mismo día) y en
        # horario hábil. Sale en la primera línea si
        # Chatwoot Settings.aviso_facturas_vencidas_activo está en 0 (nace
        # apagado) — registrar el job NO enciende nada.
        "0 8 * * 1-5": [
            "frappe_chatwoot.utils.facturas_vencidas.avisar_vencidas",
        ],
        # Autofollowup — reactivación de hilos de prospecto dejados en visto,
        # réplica del bot action `advancedFollowup` de GHL Conversation AI.
        # Plan: nuevosofia/planes/autofollowup-reactivacion-conversaciones.md.
        # Cron propio (NO el de `secuencias.avanzar`, más arriba): el primer
        # paso dispara a +3h del silencio, así que necesita barrer cada 20
        # min dentro de la ventana 08-18 L-V — barrer con el cron de
        # secuencias multiplicaría su ritmo sobre el mismo canal Baileys.
        # `barrer()` sale en la primera línea de cada Agente IA con
        # `followup_activo` en 0 (nace apagado en los 5 existentes,
        # 2026-09-24) — registrar el job NO enciende nada.
        "*/20 8-18 * * 1-5": [
            "frappe_chatwoot.utils.autofollowup.barrer",
        ],
        # Auto-expirar pausas humanas de `Chatwoot Pausa` — hasta hoy solo se
        # levantaban a mano (botón "Reanudar agente"). `expirar_pausas()` sale
        # en la primera línea si `Chatwoot Settings.expirar_pausas_activo` está
        # en 0 (nace apagado — ver docstring de utils/pausas.py, hay pausas
        # reales acumuladas que no deben resucitar de golpe). Cada 5 min, no
        # menos: los minutos configurables (`Agente IA.minutos_pausa_humana`)
        # son enteros, un barrido más fino no aporta precisión real.
        #
        # Comparte la clave "*/5 * * * *" con la bienvenida del formulario de
        # abajo (`captacion.enviar_bienvenidas`) — 2026-09-25: se fusionaron
        # en una sola lista porque un dict literal de Python con la misma
        # clave repetida se sobrescribe en tiempo de parseo (gana la última),
        # lo que dejó sin registrar el job de bienvenida entre el 18-sep y el
        # 25-sep sin ningún error visible. NO volver a separar esta clave.
        "*/5 * * * *": [
            "frappe_chatwoot.utils.pausas.expirar_pausas",
            # Bienvenida a los leads del formulario del sitio — reemplaza la
            # espera de 5 min que los workflows 1.2/1.3 de GHL tenían dentro
            # del árbol. Barrido sin estado por la misma razón que el
            # recordatorio: una espera viva dentro de un proceso no sobrevive
            # a un reinicio.
            "frappe_chatwoot.utils.captacion.enviar_bienvenidas",
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
    # Tope de usuarios de Sofía Lite (site_config `sofia_lite_max_usuarios`, ver
    # utils/seat_cap.py) — inerte si el sitio no define esa llave.
    "User": {
        "before_insert": "frappe_chatwoot.utils.seat_cap.validar_tope_usuarios",
    },
    "CRM Deal": {
        # `ghl_status` es una proyección de `status`, no un campo que se mantenga
        # a mano: sin esto diverge sola cada vez que alguien mueve una
        # oportunidad a Ganada/Perdida desde la UI. Ver utils/estatus_deal.py.
        "validate": "frappe_chatwoot.utils.estatus_deal.sincronizar",
        "on_update": [
            "frappe_chatwoot.utils.onboarding.on_deal_update",
            "frappe_chatwoot.utils.nutricion.on_deal_update",
        ],
        # `_assign` -> `deal_owner`. Va en `on_change`, NO en `on_update`: la
        # `Assignment Rule` corre en el hook comodín `"*"` de `on_update`, y
        # Frappe ejecuta los handlers del doctype ANTES que los comodines, así
        # que un `on_update` propio leería un `_assign` viejo. `on_change` corre
        # después de `on_update` (por eso el `ToDo` ya escribió `_assign`). El
        # porqué completo — incluida la precedencia del dueño — en el docstring
        # de utils/dueno_desde_assign.py.
        "on_change": "frappe_chatwoot.utils.dueno_desde_assign.sincronizar",
    },
    # CRM Lead no tenía ningún handler propio. Misma pieza que el Deal: el Round
    # Robin de Estrublock está sobre los DOS doctypes.
    "CRM Lead": {
        "on_change": "frappe_chatwoot.utils.dueno_desde_assign.sincronizar",
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
        "on_update": [
            "frappe_chatwoot.utils.chatwoot_contactos.sincronizar_nombre",
            # Todo alta/edición de Contact debe tener su Customer de ERPNext —
            # sin esto el contacto queda invisible en Cobranza (2026-09-25,
            # caso Alma Cabello). Ver utils/cliente_erp.py.
            "frappe_chatwoot.utils.cliente_erp.crear_customer_si_falta",
        ],
        "after_insert": [
            "frappe_chatwoot.utils.chatwoot_contactos.sincronizar_nombre",
            "frappe_chatwoot.utils.cliente_erp.crear_customer_si_falta",
        ],
        # crm_organization (Link al catalogo, 2026-09-17) -> company_name.
        "validate": "frappe_chatwoot.utils.chatwoot_contactos.sincronizar_organizacion",
    },
    # CRM Task → contacto/organización/oportunidad: `CRM Task` solo trae un
    # vínculo, así que en el panel de Tareas se veían aisladas (2026-09-15).
    # after_insert + on_update → push al usuario asignado, en alta y en
    # reasignación (2026-09-20, ampliado 2026-09-21). El guard contra el doble
    # disparo del insert (`doc.flags.in_insert`) vive dentro de
    # `notificar_asignacion`, no aquí — ver su docstring.
    "CRM Task": {
        "validate": "frappe_chatwoot.utils.tareas.autollenar",
        "after_insert": "frappe_chatwoot.utils.tareas.notificar_asignacion",
        "on_update": "frappe_chatwoot.utils.tareas.notificar_asignacion",
    },
}

# ---------------------------------------------------------------------------
# permission_query_conditions / has_permission — gate de plan (Lite/Pro/Enterprise)
# ---------------------------------------------------------------------------
# Complementa el gate ya puesto en los endpoints RPC (`plan.exigir_no_lite()` en
# utils/secuencias.py, api/campanas.py, api/campana_email.py) para el acceso
# genérico vía /api/resource/<doctype> que el SPA también usa para listar y leer.
# Sin esto, un Sales User de un sitio Lite podía entrar a Secuencias, Campañas
# de email, Llamadas y a la config del Agente IA con la URL directa — verificado
# en vivo el 2026-09-25 en Six Gardens. Ver utils/plan.py: el nivel del sitio
# vive en site_config.json (`sofia_plan`), default "enterprise" si no está
# definida — un sitio sin esa llave no queda afectado.
#
# "CRM Call Log" es el doctype nativo de Frappe CRM para el módulo "Llamadas"
# (app `crm`, no nuestro) — Enterprise-only en la tabla de planes nueva, por
# eso usa el par `_enterprise` en vez de `_no_lite`.
#
# Sales Invoice / Payment Entry (Cobranza y facturación, ERPNext) se incluyen
# a modo defensivo: hoy Six Gardens (único sitio Lite) no tiene ERPNext
# instalado, así que estos hooks son inertes ahí — pero si algún día se
# instala por error, el gate ya está puesto.
permission_query_conditions = {
    "Secuencia": "frappe_chatwoot.utils.plan.condicion_lista_no_lite",
    "Secuencia Inscripcion": "frappe_chatwoot.utils.plan.condicion_lista_no_lite",
    "Campana Email": "frappe_chatwoot.utils.plan.condicion_lista_no_lite",
    "Campana Email Paso": "frappe_chatwoot.utils.plan.condicion_lista_no_lite",
    "Agente IA": "frappe_chatwoot.utils.plan.condicion_lista_no_lite",
    "Sales Invoice": "frappe_chatwoot.utils.plan.condicion_lista_no_lite",
    "Payment Entry": "frappe_chatwoot.utils.plan.condicion_lista_no_lite",
    "CRM Call Log": "frappe_chatwoot.utils.plan.condicion_lista_enterprise",
}

has_permission = {
    "Secuencia": "frappe_chatwoot.utils.plan.permiso_doc_no_lite",
    "Secuencia Inscripcion": "frappe_chatwoot.utils.plan.permiso_doc_no_lite",
    "Campana Email": "frappe_chatwoot.utils.plan.permiso_doc_no_lite",
    "Campana Email Paso": "frappe_chatwoot.utils.plan.permiso_doc_no_lite",
    "Agente IA": "frappe_chatwoot.utils.plan.permiso_doc_no_lite",
    "Sales Invoice": "frappe_chatwoot.utils.plan.permiso_doc_no_lite",
    "Payment Entry": "frappe_chatwoot.utils.plan.permiso_doc_no_lite",
    "CRM Call Log": "frappe_chatwoot.utils.plan.permiso_doc_enterprise",
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

# Guard: /app (Frappe Desk) redirige a /crm salvo System Manager. Ver el
# docstring de utils/erp_guard.py para el porque (nadie usa el Desk, los 5
# usuarios son System User, tematizar el Desk se revierte con bench update).
# Administrator exceptuado dentro de la funcion, como llave de rescate.
before_request = [
    "frappe_chatwoot.utils.erp_guard.antes_de_la_peticion",
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
    # Single del app: sus 12 campos propios (evolution_*, onboarding_*,
    # inbox_formulario, recordatorio_citas_activo, secuencias_activas, agenda_*,
    # bienvenida_formulario_activa) no viajaban, asi que un sitio de cliente no
    # podia usar recordatorio de citas, bienvenida del formulario ni secuencias.
    # Viaja la DEFINICION del campo; el VALOR (tokens, URLs) es por sitio.
    "Chatwoot Settings",
    "Chatwoot Pausa",
    "KB Inbox",
    "KB Source",
    "Mensaje Programado",
    "Mensaje Programado Adjunto",
    "Plantilla",
    "Plantilla de Planeacion",
    "Reunion Agendada",
    "Secuencia",
    "Secuencia Inscripcion",
    "Secuencia Paso",
    "Sofia Push Settings",
    "Sofia Push Subscription",
    "Solicitud Web",
    "Stripe Settings",
    # Campañas de email programadas (2026-09-18, plan
    # campanas-contenedor-y-cadencia.md): contenedor + pasos con cadencia.
    "Campana Email",
    "Campana Email Paso",
    # Redes sociales vía Ayrshare (2026-09-24): perfil de Ayrshare de este
    # sitio (API key + Profile Key) para conectar Instagram/Facebook/etc.
    # sin salir del CRM. Single, como Stripe Settings.
    "Redes Sociales",
]

# Doctypes de terceros (crm) que extendimos con campos propios.
DOCTYPES_EXTENDIDOS = [
    "CRM Deal",
    "CRM Lead",
    "CRM Task",
    "CRM Organization",
    "Contact",
    # serie_* (obsoletos, ocultos) + futuros campos propios sobre el
    # Newsletter del core — sin esto no viajan a sitios de cliente.
    "Newsletter",
]

# Doctypes que solo existen si erpnext esta instalado. Sus campos, property
# setters y permisos NO pueden vivir en los fixtures normales:
# frappe/utils/fixtures.py envuelve el ARCHIVO COMPLETO en un try/except y lo
# salta entero si UN doctype falta ("Skipping fixture syncing from the file
# custom_field.json. Reason: DocType X not found"). Meter Customer ahi dejaria a
# un sitio sin erpnext sin NINGUN campo. Viven en fixtures/erpnext/ y los
# importa utils/provisionamiento.py solo si erpnext esta instalado.
DOCTYPES_ERPNext = ["Customer"]

fixtures = [
    {"dt": "Custom Field", "filters": [["dt", "in", DOCTYPES_PROPIOS + DOCTYPES_EXTENDIDOS]]},
    {
        "dt": "Property Setter",
        "filters": [["doc_type", "in", DOCTYPES_PROPIOS + DOCTYPES_EXTENDIDOS + ["Assignment Rule", "FCRM Note"]]],
    },
    {"dt": "Translation", "filters": [["language", "=", "es"]]},
    {"dt": "Custom DocPerm", "filters": [["parent", "in", ["CRM Deal", "User"]]]},
    {"dt": "Web Form", "filters": [["module", "=", "Custom"]]},
]

# Los catalogos del embudo (CRM Deal Status / Lead Source / Lost Reason) ESTUVIERON
# aqui hasta el 2026-09-22 y se sacaron a proposito. Eran contaminacion inversa:
# son datos de negocio de lavendi.mx, no producto, y al viajar como fixture se
# re-imponian en CADA `bench migrate` de CADA sitio. Medido en estrublock: 34
# etapas = sus 18 curadas de su propio GHL + nuestras 16 encima, visibles como
# columnas ajenas en su kanban ("Videollamada de cierre", "Seguimiento SGPT/Ads
# iniciado", "Temporal"). Un cliente que depurara su embudo lo veia volver al
# siguiente migrate.
#
# Ahora viven en fixtures/catalogos/ (fuera del barrido automatico de `fixtures/`,
# igual que fixtures/erpnext/) y los siembra
# utils/provisionamiento.sembrar_catalogos_una_vez() SOLO la primera vez por sitio.
# Un sitio nuevo sigue naciendo con el embudo curado; uno existente deja de ser
# pisado.
fixtures += [
    # Los layouts del CRM (que campos se ven en el alta, el tab Datos y el panel
    # lateral). El equipo los curo a mano en crm.lavendi.mx — cierre 17: ocultar
    # los campos muertos; cierre 19: "Valor de la oportunidad" en los 3 layouts
    # del deal. NO viajaban: un sitio nuevo se quedaba con los layouts crudos de
    # frappe/crm. El layout es UN campo JSON por registro, asi que el fixture lo
    # reemplaza completo — no aplica la trampa de las child tables.
    {"dt": "CRM Fields Layout"},
]

# ---------------------------------------------------------------------------
# after_migrate — cierra el hueco que los fixtures no pueden cerrar
# ---------------------------------------------------------------------------
# Los fixtures hacen upsert, no borran. frappe/crm instala 22 etapas de embudo
# en ingles; la siembra inicial agrega las 16 de lavendi.mx pero dejaria las
# 6 nativas que produccion borro. Ver utils/provisionamiento.py.
# OJO: la siembra de catalogos y el borrado de etapas nativas corren UNA SOLA VEZ
# por sitio (bandera `fc_catalogos_sembrados`), no en cada migrate.
after_migrate = [
    "frappe_chatwoot.utils.provisionamiento.ajustar_sitio",
]
