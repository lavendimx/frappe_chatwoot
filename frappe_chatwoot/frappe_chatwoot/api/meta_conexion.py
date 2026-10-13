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

QUÉ NO HACE (a propósito) — actualizado 2026-10-09
    Hasta el 2026-10-06, el inbox creado en Chatwoot no quedaba enrutado en
    ningún otro lado: aparecía solo en el selector de Conversaciones
    (`list_inboxes` lista la cuenta completa), pero alguien tenía que editar a
    mano `Chatwoot Settings.canales_bandeja`, `agente-ia/.env` (`FRAPPE_SITES`),
    sumar a `agente-ia@lavendi.mx` como colaborador del inbox en Chatwoot, y
    crear a mano el registro `Agente IA` clonando el prompt de otro inbox del
    mismo cliente — los 5 pasos documentados en `runbook-conectar-redes.md`.

    Ahora `completar_wiring()` agrupa los 4 pasos automatizables (el 5to,
    reiniciar `agente-ia-chatwoot`, ya no aplica — ver su docstring), TODOS
    best-effort — si cualquiera falla, el inbox de Chatwoot ya quedó creado y
    se devuelve igual; lo único que se pierde es la automatización de ese
    paso, no la conexión:
      1. Suma el inbox a `Chatwoot Settings.canales_bandeja` (ver
         `_agregar_canal_bandeja`) — local, en la misma base de datos, así que
         solo fallaría por un problema de permisos/validación del doctype.
      2. Avisa a `agente-ia` (proceso Node aparte, otro repo) con
         `POST /sitios/registrar-canal` (ver `_registrar_canal_en_agente`) —
         reusa la MISMA config que `api/agenda.py` (`Chatwoot Settings.agenda_url`
         / `.agenda_token`), que ya es el canal para que Frappe le hable a
         `agente-ia`. Esta llamada es la que de verdad puede fallar (el proceso
         puede estar caído, el token sin configurar, la red tardar) — por eso
         va envuelta aparte y nunca se deja que tumbe la respuesta de este
         endpoint ni reviertan el inbox que Chatwoot ya aceptó.
      3. Agrega a `agente-ia@lavendi.mx` como colaborador del inbox en
         Chatwoot (ver `_agregar_colaborador_inbox`) — tiene un riesgo abierto
         sin confirmar (rol del dueño del `api_token`), ver su docstring.
      4. Clona el registro `Agente IA` de un inbox origen del mismo cliente
         hacia uno nuevo (ver `_clonar_agente_ia`) — con una heurística simple
         de reemplazo de texto en el prompt, documentada ahí.

    `conectar_pagina()` (Messenger) llama `completar_wiring()` automáticamente
    porque sí tiene el `inbox_id` disponible. `autorizar_instagram()` NO puede
    (ver su docstring) — queda pendiente llamarlo aparte una vez que el inbox
    de Instagram ya exista.

    Si alguno de los 4 pasos falla, el inbox queda operativo en Chatwoot pero
    ese paso específico hay que completarlo a mano — el mismo hueco que
    existía antes de este cambio, no uno nuevo.
"""

import json
import urllib.error
import urllib.request

import frappe

from frappe_chatwoot.utils import chatwoot_client as cw

from .agenda import _config_agenda
from .chatwoot import _inboxes_del_sitio

TIMEOUT_REGISTRO_CANAL = 10  # best-effort: más corto que el de agenda.py (30s, Google+Meet).


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
def estado_mensajeria() -> dict:
    """Indicador de solo lectura para el panel: qué páginas ya están conectadas
    a Chatwoot para mensajería (Messenger e Instagram), sin iniciar ningún
    login. Antes de esto, el panel solo mostraba "ya conectadas" después de
    repetir el login de Facebook, y Instagram no tenía ningún indicador
    aunque ya estuviera conectado (pendiente señalado en el CLAUDE.md del
    proyecto, 2026-10-09).

    Aislamiento por sitio (bug corregido 2026-10-13): `list_inboxes()` devuelve
    TODA la cuenta COMPARTIDA de Chatwoot, así que sin filtrar el panel de un
    cliente mostraba como "Conectada" la página de OTRO (Estrublock veía los
    inboxes 12/22 de lavendi.mx). Se filtra contra `_inboxes_del_sitio()` —
    la misma fuente de verdad que Conversaciones usa para "Todos los canales"
    (honra `canales_bandeja`; vacío = solo `default_inbox_id`)."""
    _exigir_admin()
    inboxes = cw.list_inboxes()
    sitio = set(_inboxes_del_sitio())
    return {
        "facebook": [
            {"id": i.get("id"), "name": i.get("name")}
            for i in inboxes
            if i.get("channel_type") == "Channel::FacebookPage"
            and frappe.utils.cint(i.get("id")) in sitio
        ],
        "instagram": [
            {"id": i.get("id"), "name": i.get("name")}
            for i in inboxes
            if i.get("channel_type") == "Channel::Instagram"
            and frappe.utils.cint(i.get("id")) in sitio
        ],
    }


@frappe.whitelist()
def desconectar_inbox(inbox_id: str) -> dict:
    """Desconecta del panel una página/red ya conectada — de forma REVERSIBLE.

    ⚠ RIESGO (por esto NO se borra el inbox de Chatwoot):
        Borrar un inbox de Facebook/Instagram con `DELETE /inboxes/:id` hace que
        Chatwoot des-suscriba la PÁGINA de Meta del webhook COMPARTIDO — la misma
        página que sostiene los demás inbox/productos del sitio. Ya ocurrió una
        vez en este proyecto al borrar un canal (cuenta demo de Meta App Review,
        2026-10-12/13): hubo que invalidar el token ANTES del destroy justamente
        porque borrar la bandeja tumbaba la suscripción compartida de
        Messenger/Instagram de producción. Por eso aquí NO se llama a DELETE.

    DECISIÓN — qué sí hace (la opción segura y reversible):
        Desvincula el inbox de ESTE sitio quitándolo de `Chatwoot Settings
        .canales_bandeja`. Es exactamente lo que `_inboxes_del_sitio()` lee para
        armar "Todos los canales" y para el filtro de `estado_mensajeria`, así
        que la red deja de mostrarse como "Conectada" en el panel y deja de
        mezclarse en Conversaciones. Es 100% reversible: reconectar la página (o
        volver a agregar el id a `canales_bandeja`) la restaura.
        LÍMITE CONOCIDO: el inbox sigue existiendo en Chatwoot y `agente-ia`
        puede seguir enrutándolo — esto es una desconexión de panel, no una baja
        definitiva. Para borrar el canal de verdad hay que hacerlo a mano desde
        el admin de Chatwoot, con el cuidado del riesgo de arriba (invalidar el
        token de página antes).

    Aislamiento: exige que el inbox pertenezca a ESTE sitio
        (`_inboxes_del_sitio()`) antes de tocarlo — así el panel de un cliente no
        puede desconectar el canal de otro pasando un `inbox_id` ajeno."""
    _exigir_admin()
    try:
        inbox_id_int = int(str(inbox_id).strip())
    except (TypeError, ValueError):
        frappe.throw("inbox_id inválido.")

    if inbox_id_int not in _inboxes_del_sitio():
        frappe.throw(
            f"El inbox {inbox_id_int} no pertenece a este sitio — no se puede desconectar desde aquí.",
            frappe.PermissionError,
        )

    settings = frappe.get_single("Chatwoot Settings")
    declarados = [p.strip() for p in (settings.canales_bandeja or "").split(",") if p.strip()]
    if str(inbox_id_int) not in declarados:
        # Solo llegó por el fallback a `default_inbox_id` (canales_bandeja vacío):
        # es el inbox por DEFECTO del sitio (p. ej. WhatsApp). Quitarlo de aquí
        # exigiría tocar `default_inbox_id`, no este flujo — se rechaza para no
        # dejar el sitio sin ningún canal.
        frappe.throw(
            "Ese inbox es el canal por defecto del sitio (no está listado en canales_bandeja); "
            "no se puede desconectar por esta vía."
        )

    restantes = [p for p in declarados if p != str(inbox_id_int)]
    settings.canales_bandeja = ",".join(restantes)
    settings.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "inbox_id": str(inbox_id_int),
        "canales_bandeja": settings.canales_bandeja,
        "metodo": "desvinculado_del_sitio",
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


def _agregar_canal_bandeja(inbox_id: str) -> None:
    """Suma el inbox nuevo a `Chatwoot Settings.canales_bandeja` sin duplicar.

    Formato del campo (Data): ids de inbox separados por coma, ej. "5,12" — ver
    `api/chatwoot.py::_inboxes_del_sitio`, que es quien lo lee para armar "Todos
    los canales" en Conversaciones. Vacío ahí significa "solo el inbox por
    defecto" (`default_inbox_id`); si hoy está vacío y solo agregáramos el
    inbox nuevo, "Todos los canales" dejaría de incluir el canal por defecto
    (WhatsApp) — por eso, al declarar la lista por primera vez, se siembra con
    el default explícito antes de sumar el nuevo."""
    settings = frappe.get_single("Chatwoot Settings")
    actuales = [p.strip() for p in (settings.canales_bandeja or "").split(",") if p.strip()]
    if not actuales:
        default_id = frappe.utils.cint(settings.default_inbox_id or 0)
        if default_id:
            actuales = [str(default_id)]
    if inbox_id not in actuales:
        actuales.append(inbox_id)
    nuevo_valor = ",".join(actuales)
    if nuevo_valor != (settings.canales_bandeja or ""):
        settings.canales_bandeja = nuevo_valor
        settings.save(ignore_permissions=True)
        frappe.db.commit()


def _registrar_canal_en_agente(inbox_id: str) -> None:
    """Avisa a `agente-ia` (proceso Node aparte, `POST /sitios/registrar-canal`)
    que hay un inbox nuevo para este sitio — para que enrute mensajes sin que
    alguien edite `FRAPPE_SITES` en su `.env` a mano y reinicie.

    Best-effort A PROPÓSITO: reusa la config de `api/agenda.py::_config_agenda`
    (`Chatwoot Settings.agenda_url` / `.agenda_token`), que ya es el canal para
    que Frappe le hable a `agente-ia` — no se inventa un secreto nuevo. Pero a
    diferencia de `agenda._pedir`, aquí una falla NUNCA debe propagarse: el
    inbox ya existe en Chatwoot (lo creó `conectar_pagina` antes de llamar
    esta función) y no hay nada que revertir. Si esto falla, queda registrado
    con `frappe.log_error` y el enrutamiento se completa a mano, como se hacía
    antes de este cambio."""
    try:
        cfg = _config_agenda()
        if not (cfg["url"] and cfg["token"]):
            frappe.log_error(
                title="meta_conexion: agente-ia sin configurar (canal no registrado)",
                message=(
                    f"inbox_id={inbox_id} site={frappe.local.site} — Chatwoot Settings no tiene "
                    "agenda_url/agenda_token configurados. El inbox ya existe en Chatwoot; falta "
                    "sumarlo a mano a FRAPPE_SITES en el .env de agente-ia y reiniciar."
                ),
            )
            return

        req = urllib.request.Request(
            f"{cfg['url']}/sitios/registrar-canal",
            data=json.dumps({"inboxId": str(inbox_id), "site": frappe.local.site}).encode(),
            headers={"Content-Type": "application/json", "x-sofia-token": cfg["token"]},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_REGISTRO_CANAL) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001 — best-effort a propósito, ver docstring
        frappe.log_error(
            title="meta_conexion: agente-ia no respondió al registrar el canal",
            message=(
                f"inbox_id={inbox_id} site={frappe.local.site} -> {exc}\n\n"
                "El inbox ya existe en Chatwoot (no se revierte). Falta sumarlo a mano a "
                "FRAPPE_SITES en el .env de agente-ia y reiniciar para que el agente responda."
            ),
        )


def _agregar_colaborador_inbox(inbox_id: str) -> None:
    """Agrega a `agente-ia@lavendi.mx` como colaborador (inbox member) del
    inbox nuevo en Chatwoot — sin esto, Sofía Ventas no tiene permiso para
    postear ahí (401 al intentar responder, como pasó a mano con el inbox 22
    el 2026-10-09 antes de agregarlo).

    `POST /inbox_members` con `{"inbox_id", "user_ids"}` es ADITIVO (suma a la
    lista de miembros existente; a diferencia de `PATCH` en la misma ruta, que
    la reemplaza/remueve) — contrato verificado contra el código fuente real
    del contenedor `chatwoot-pabuvqyduzevxbwnrcf3rozs` en esta sesión. Por eso
    se usa `cw._post`, nunca un hipotético `cw._patch`. `cw._post` ya antepone
    `/api/v1/accounts/{account_id}`, así que aquí solo va el sufijo de la ruta
    (mismo patrón que `_agregar_canal_bandeja`/`_registrar_canal_en_agente` no
    usan esa ruta, pero sí reusan `cw._post` igual).

    RIESGO ABIERTO (no se pudo confirmar en esta sesión, dejar explícito en
    vez de asumir): el controlador de Chatwoot exige que el usuario dueño del
    `api_token` configurado en `Chatwoot Settings` sea `administrator` de la
    cuenta — si no, Chatwoot responde 403. El token es un campo Password
    (no se puede leer su valor desde código sin exponerlo) y confirmar el rol
    del usuario requeriría pegarle a la API real de Chatwoot, fuera de alcance
    de esta tarea (no se permite llamar APIs reales). Si esto falla con 403 en
    el primer uso real, ese es el motivo más probable a revisar primero.

    Best-effort A PROPÓSITO, mismo criterio que el resto del archivo: nunca
    propaga — si falla, el inbox y el aviso a `agente-ia` ya quedaron hechos;
    solo falta sumar al colaborador a mano desde Chatwoot (bandeja > Agentes)."""
    try:
        user_id = cw.agent_id_for_email("agente-ia@lavendi.mx")
        if not user_id:
            frappe.log_error(
                title="meta_conexion: agente-ia@lavendi.mx no es agente de Chatwoot",
                message=(
                    f"inbox_id={inbox_id} site={frappe.local.site} — no se encontró ese correo "
                    "en GET /agents. Sin esto Sofía Ventas no puede postear en el inbox nuevo "
                    "(401 al responder). Falta agregarlo a mano como colaborador desde Chatwoot."
                ),
            )
            return
        cw._post("/inbox_members", {"inbox_id": int(inbox_id), "user_ids": [user_id]})
    except Exception as exc:  # noqa: BLE001 — best-effort a propósito, ver docstring
        frappe.log_error(
            title="meta_conexion: no se pudo agregar a agente-ia como colaborador del inbox",
            message=(
                f"inbox_id={inbox_id} site={frappe.local.site} -> {exc}\n\n"
                "El inbox ya existe en Chatwoot (no se revierte). Falta agregar a mano a "
                "agente-ia@lavendi.mx como colaborador desde Chatwoot (bandeja > Agentes) — si "
                "el error es 403, revisar primero si el api_token de Chatwoot Settings "
                "pertenece a un usuario administrator de la cuenta (riesgo abierto, ver "
                "docstring de _agregar_colaborador_inbox)."
            ),
        )


def _clonar_agente_ia(inbox_id: str, inbox_origen: str, canal_nombre: str) -> None:
    """Clona el registro `Agente IA` de `inbox_origen` (ej. "5", WhatsApp)
    hacia uno nuevo con `name=inbox_id`, para que el canal recién conectado
    tenga agente desde el primer mensaje en vez de quedarse sin responder
    hasta que alguien lo cree a mano en el Desk (paso 3 del runbook manual).

    Usa `frappe.copy_doc` (no se reconstruye campo por campo) precisamente
    porque limpia `name`/`owner`/`creation`/`modified`/`docstatus` pero
    conserva TODO lo demás, incluyendo los custom fields de follow-up
    (`followup_activo` y los que agregó `autofollowup_setup.py` después del
    doctype base) si existen en el doc origen — una lista fija de campos se
    desactualizaría cada vez que se agregue un custom field nuevo.

    HEURÍSTICA SIMPLE en el prompt — documentado a propósito, no asumir que
    siempre queda perfecto: el `system_prompt` del origen se ajusta con un
    `.replace("WhatsApp", canal_nombre)` ciego (reemplazo textual, sin
    entender el prompt). Cubre las 2 menciones sueltas de "WhatsApp" dentro de
    párrafos ya verificadas en el prompt del inbox 5 (433 líneas, ninguna en
    una línea dedicada tipo "Canal: X"), pero vale la pena leer el prompt
    generado a mano la primera vez que el canal nuevo se use de verdad.

    `followup_activo` se fuerza a 0 si el campo existe en el doc clonado, sin
    importar lo que tuviera el origen: un canal recién conectado no debe
    heredar follow-up automático sin validarlo primero (mismo criterio que ya
    se aplicó a mano para los inbox 22/12 — ver pendientes del CLAUDE.md del
    proyecto).

    Idempotente a propósito: si ya existe un registro con `name=inbox_id`
    (conexión repetida, o `completar_wiring` llamado dos veces para el mismo
    inbox), no se sobreescribe ni se falla ruidosamente — se loggea y se sale
    en silencio. Best-effort, mismo criterio que el resto del archivo: nunca
    propaga la excepción."""
    try:
        if frappe.db.exists("Agente IA", inbox_id):
            frappe.log_error(
                title="meta_conexion: Agente IA ya existía, no se clona de nuevo",
                message=f"inbox_id={inbox_id} inbox_origen={inbox_origen} site={frappe.local.site}",
            )
            return

        if not frappe.db.exists("Agente IA", inbox_origen):
            frappe.log_error(
                title="meta_conexion: no existe Agente IA origen para clonar",
                message=(
                    f"inbox_origen={inbox_origen} inbox_id={inbox_id} site={frappe.local.site} — "
                    "no se encontró el doc a clonar. Falta crear el Agente IA del inbox nuevo a "
                    "mano en el Desk."
                ),
            )
            return

        origen = frappe.get_doc("Agente IA", inbox_origen)
        nuevo = frappe.copy_doc(origen)
        nuevo.name = inbox_id

        canal = (canal_nombre or "").strip()
        if canal and nuevo.system_prompt:
            nuevo.system_prompt = nuevo.system_prompt.replace("WhatsApp", canal)

        if nuevo.meta.has_field("followup_activo"):
            nuevo.followup_activo = 0

        nuevo.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as exc:  # noqa: BLE001 — best-effort a propósito, ver docstring
        frappe.log_error(
            title="meta_conexion: no se pudo clonar el Agente IA para el inbox nuevo",
            message=(
                f"inbox_id={inbox_id} inbox_origen={inbox_origen} site={frappe.local.site} -> "
                f"{exc}\n\nEl inbox ya existe en Chatwoot (no se revierte). Falta crear a mano el "
                f"registro Agente IA (name={inbox_id}) clonando el prompt de inbox_origen."
            ),
        )


@frappe.whitelist()
def completar_wiring(inbox_id: str, inbox_origen: str = "5", canal_nombre: str = "") -> dict:
    """Completa el wiring de un inbox ya creado en Chatwoot (pasos 2 y 3 del
    `runbook-conectar-redes.md`): agrega a `agente-ia@lavendi.mx` como
    colaborador del inbox (`_agregar_colaborador_inbox`) y clona el registro
    `Agente IA` de `inbox_origen` hacia uno nuevo con `name=inbox_id`
    (`_clonar_agente_ia`).

    También repite los pasos 1 y el aviso a `agente-ia` que `conectar_pagina`
    ya hacía inline (`_agregar_canal_bandeja`, `_registrar_canal_en_agente`),
    reusándolos tal cual — así esta función es un solo punto de entrada que
    deja completos los pasos 1-4 del runbook (todo lo automatizable). El paso
    5 (reiniciar `agente-ia-chatwoot`) ya NO aplica: `agente-ia` adoptó
    hot-reload de `FRAPPE_SITES`/`SITIO_DEFAULT_INBOXES` sin necesitar
    restart (confirmado en esta sesión por el agente que investigó ese repo
    en paralelo).

    POR QUÉ EXISTE APARTE de `conectar_pagina`: Instagram (`autorizar_instagram`)
    no tiene forma de llamar esto automáticamente — Chatwoot resuelve todo el
    OAuth en su propio dominio y nunca nos devuelve el `inbox_id` del inbox
    creado. Alguien (hoy, probablemente el frontend, comparando el listado de
    `list_inboxes()` antes/después de la autorización; o la consola de Frappe
    como fallback manual) tiene que llamar `completar_wiring` por separado una
    vez que el inbox de Instagram ya exista. Conectar ese punto (detectar el
    inbox nuevo tras el callback de Instagram) queda pendiente como trabajo de
    frontend, fuera del alcance de este cambio.

    Idempotente a propósito: no falla si ya se corrió antes para este
    `inbox_id` — cada paso interno es best-effort y no vuelve a duplicar nada
    (`_agregar_canal_bandeja` no repite el id en la lista, `_clonar_agente_ia`
    se sale en silencio si el doc ya existe)."""
    _exigir_admin()
    inbox_id = str(inbox_id or "").strip()
    if not inbox_id:
        frappe.throw("Falta inbox_id.")
    inbox_origen = str(inbox_origen or "5").strip()

    try:
        _agregar_canal_bandeja(inbox_id)
    except Exception as exc:  # noqa: BLE001 — el inbox ya existe en Chatwoot, no revertir
        frappe.log_error(
            title="meta_conexion: no se pudo sumar el inbox a canales_bandeja",
            message=f"inbox_id={inbox_id} site={frappe.local.site} -> {exc}",
        )

    _registrar_canal_en_agente(inbox_id)  # best-effort interno, nunca lanza — ver su docstring
    _agregar_colaborador_inbox(inbox_id)  # idem
    _clonar_agente_ia(inbox_id, inbox_origen, canal_nombre)  # idem

    return {"inbox_id": inbox_id, "inbox_origen": inbox_origen}


@frappe.whitelist()
def conectar_pagina(
    user_access_token: str,
    page_access_token: str,
    page_id: str,
    inbox_name: str,
) -> dict:
    """Crea el inbox de la página en Chatwoot y lo registra para el resto del
    stack. Devuelve el inbox creado (igual que antes; `completar_wiring` es un
    efecto secundario best-effort, no cambia lo que se devuelve)."""
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

    inbox = cw._post(
        "/callbacks/register_facebook_page",
        {
            "user_access_token": user_access_token,
            "page_access_token": page_access_token,
            "page_id": str(page_id),
            "inbox_name": inbox_name.strip(),
        },
    ) or {}

    inbox_id = inbox.get("id")
    if inbox_id:
        # inbox_origen se deja en su default ("5") — es el id del inbox de
        # WhatsApp de lavendi.mx hoy, no un valor genérico por cliente. Mismo
        # hueco de mapeo estático ya señalado en el CLAUDE.md del proyecto
        # ("Evaluar si el mapeo... necesita volverse dinámico antes de
        # escalar multicanal a más clientes") — no se resuelve aquí.
        completar_wiring(str(inbox_id), canal_nombre="Messenger de Facebook")

    return inbox


@frappe.whitelist()
def autorizar_instagram() -> dict:
    """URL de autorización nativa de Instagram, generada por el propio Chatwoot.

    A diferencia de `conectar_pagina`, aquí Chatwoot resuelve TODO el flujo en
    su propio dominio: Instagram tiene un canal nativo separado
    (`Channel::Instagram`, OAuth directo contra Instagram, NO el JS SDK de
    Facebook) cuyo controlador (`Api::V1::Accounts::Instagram::AuthorizationsController`)
    devuelve `{success, url}` con el `state` ya firmado (JWT con el account_id,
    que el propio `instagram/callback` de Chatwoot verifica al volver). Este
    backend solo pide esa URL para que el botón arranque desde el panel de
    Sofía en vez de mandar al admin crudo — el resto (login de Instagram,
    intercambio de token, alta del inbox) lo hace Chatwoot sin volver a
    llamarnos.

    Por lo mismo, el inbox resultante NO pasa por `completar_wiring` (que
    agrupa `_agregar_canal_bandeja`, `_registrar_canal_en_agente`,
    `_agregar_colaborador_inbox` y `_clonar_agente_ia`): este backend nunca
    recibe el control de vuelta a media conexión (al revés de
    `conectar_pagina`, que sí crea el inbox él mismo y por eso puede llamar
    `completar_wiring` directo).

    PENDIENTE (fuera de alcance de este cambio, trabajo de frontend): alguien
    tiene que llamar `completar_wiring(inbox_id, ...)` por separado una vez
    que el inbox de Instagram ya exista en Chatwoot — por ejemplo comparando
    `list_inboxes()` antes/después de que el usuario vuelva del OAuth, o
    a mano desde la consola de Frappe como fallback. Hasta que eso se
    construya, enrutar ese inbox sigue siendo 100% manual, igual que hoy."""
    _exigir_admin()
    data = cw._post("/instagram/authorization", {}) or {}
    url = data.get("url")
    if not url:
        frappe.throw("Chatwoot no devolvió la URL de autorización de Instagram.")
    return {"url": url}
