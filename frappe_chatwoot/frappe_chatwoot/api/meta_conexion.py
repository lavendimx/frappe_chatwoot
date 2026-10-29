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
    """Indicador de solo lectura para el panel: qué páginas/números ya están
    conectados a Chatwoot para mensajería (Messenger, Instagram y WhatsApp
    Cloud API oficial), sin iniciar ningún login. Antes de esto, el panel solo
    mostraba "ya conectadas" después de repetir el login de Facebook, y
    Instagram no tenía ningún indicador aunque ya estuviera conectado
    (pendiente señalado en el CLAUDE.md del proyecto, 2026-10-09).

    Aislamiento por sitio (bug corregido 2026-10-13): `list_inboxes()` devuelve
    TODA la cuenta COMPARTIDA de Chatwoot, así que sin filtrar el panel de un
    cliente mostraba como "Conectada" la página de OTRO (Estrublock veía los
    inboxes 12/22 de lavendi.mx). Se filtra contra `_inboxes_del_sitio()` —
    la misma fuente de verdad que Conversaciones usa para "Todos los canales"
    (honra `canales_bandeja`; vacío = solo `default_inbox_id`).

    `whatsapp_cloud` — agregado junto con `conectar_whatsapp_cloud()` (ver su
    docstring). Se filtra por `channel_type == "Channel::Whatsapp"` Y
    `provider == "whatsapp_cloud"` a propósito: los números WhatsApp que ya
    existen hoy (los 4 de Estrublock vía Evolution) NO son `Channel::Whatsapp`
    en Chatwoot — llegan como `Channel::Api` genérico a través del bridge —
    así que no hay riesgo de que esta lista los mezcle o los duplique.
    `channel_type`, `provider` y `phone_number` vienen siempre en
    `GET /inboxes` (ver `_inbox.json.jbuilder` del contenedor de Chatwoot, sin
    gating por rol de administrador para esos 3 campos).

    `whatsapp_vinculacion_url` viaja aparte de las listas de arriba: no es un
    inbox de Chatwoot que se pueda consultar por API, es el link de
    `vincular.lavendi.mx/<token>` (panel de Evolution API, fuera de este
    proyecto — ver `/srv/equipo/whatsapp-bridge/`) que ya existía para dar de
    alta el WhatsApp Business normal del cliente (QR o número+código), guardado
    a mano en `Chatwoot Settings.whatsapp_vinculacion_url` por quien hizo el
    onboarding. Antes de esto el link solo se entregaba por fuera (WhatsApp/
    email) y no se podía volver a consultar dentro del CRM."""
    _exigir_admin()
    inboxes = cw.list_inboxes()
    sitio = set(_inboxes_del_sitio())
    return {
        "whatsapp_vinculacion_url": (
            frappe.get_cached_doc("Chatwoot Settings").get("whatsapp_vinculacion_url") or ""
        ).strip(),
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
        "whatsapp_cloud": [
            {"id": i.get("id"), "name": i.get("name"), "phone_number": i.get("phone_number")}
            for i in inboxes
            if i.get("channel_type") == "Channel::Whatsapp"
            and i.get("provider") == "whatsapp_cloud"
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


def _resolver_inbox_origen(inbox_origen: str = "") -> str:
    """Inbox del que se clona el `Agente IA` del canal nuevo.

    POR QUÉ NO ES "5" FIJO (bug real, no hipotético): "5" es el WhatsApp de
    lavendi.mx y solo existe en el sitio compartido. Cuando Estrublock conectó
    su página el 2026-10-13, `conectar_pagina` llamó a `completar_wiring` con
    ese default y `_clonar_agente_ia` murió con "no existe Agente IA origen
    para clonar" (ver Error Log de `estrublock.lavendi.mx` de ese día) — el
    inbox quedó creado pero sin agente, así que Sofía Ventas no habría
    contestado hasta que alguien creara el registro a mano. Lo mismo pasaría en
    cada cliente nuevo.

    El origen correcto por sitio es su `default_inbox_id` (el WhatsApp de ese
    cliente, que es de donde sale el prompt bueno). Se cae a "5" solo si el
    sitio no lo tiene configurado, para no cambiar el comportamiento del sitio
    compartido."""
    inbox_origen = str(inbox_origen or "").strip()
    if inbox_origen:
        return inbox_origen
    try:
        default = frappe.db.get_single_value("Chatwoot Settings", "default_inbox_id")
    except Exception:  # noqa: BLE001 — best-effort, igual que el resto del módulo
        default = None
    return str(default or "5").strip()


@frappe.whitelist()
def completar_wiring(inbox_id: str, inbox_origen: str = "", canal_nombre: str = "") -> dict:
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
    inbox_origen = _resolver_inbox_origen(inbox_origen)

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

    CÓMO SE DETECTA EL INBOX NUEVO (resuelto 2026-10-22, ver `instagram_ids_cuenta`):
    el frontend llama `instagram_ids_cuenta()` antes de abrir el popup y vuelve a
    llamarlo mientras sondea — en cuanto aparece un id que no estaba antes, llama
    `completar_wiring(inbox_id, canal_nombre="Instagram")` sola. Antes de este cambio
    el frontend diffeaba contra `estado_mensajeria()` (filtrado por
    `_inboxes_del_sitio()`, que depende de `canales_bandeja`) — un círculo vicioso
    real que nunca detectaba nada, porque el propio wiring es quien llena ese campo
    (bug confirmado con el caso real de EEPLV, 2026-10-22: Chatwoot sí creaba el
    inbox 28, pero nunca se enganchaba). `instagram_ids_cuenta()` existe
    exclusivamente para ese diff — sin filtrar por sitio, a propósito."""
    _exigir_admin()
    data = cw._post("/instagram/authorization", {}) or {}
    url = data.get("url")
    if not url:
        frappe.throw("Chatwoot no devolvió la URL de autorización de Instagram.")
    return {"url": url}


@frappe.whitelist()
def instagram_ids_cuenta() -> dict:
    """Ids (sin nombres, a propósito) de TODOS los inboxes `Channel::Instagram`
    de la cuenta COMPARTIDA de Chatwoot — sin filtrar por sitio.

    POR QUÉ SIN FILTRAR (y por qué eso es correcto aquí, no un hueco de
    aislamiento nuevo): `autorizar_instagram()` nunca recibe de vuelta el
    `inbox_id` del inbox que Chatwoot crea — la única forma de saber que
    "ya se conectó algo" es comparar un snapshot de antes/después. Si ese
    snapshot se filtra por `_inboxes_del_sitio()` (que depende de
    `canales_bandeja`), el inbox nuevo JAMÁS puede aparecer en el "después"
    porque ese mismo campo es lo que el wiring todavía no llenó — círculo
    vicioso confirmado con el caso real de EEPLV (inbox 28 creado en Chatwoot,
    nunca detectado, nunca wireado). Por eso este endpoint es deliberadamente
    de cuenta completa.

    POR QUÉ SOLO IDS, SIN `name` (mitiga la exposición entre clientes): el
    panel de un cliente no debe poder leer el nombre/usuario de Instagram de
    otro. Con solo ids, lo único que un cliente puede inferir es "apareció un
    id nuevo en la cuenta compartida" — ya expuesto hoy de facto por `/app`
    de Chatwoot para cualquiera con esas credenciales, que los clientes no
    tienen. El riesgo residual real es de concurrencia, no de privacidad: si
    DOS clientes conectan Instagram en la misma ventana de sondeo (~5 min),
    el frontend podría wirear el id equivocado al cliente equivocado. Mismo
    riesgo teórico que ya existía (sin resolverse) en el diff viejo; probabi-
    lidad baja dado el volumen de altas simultáneas, no se resuelve aquí."""
    _exigir_admin()
    inboxes = cw.list_inboxes()
    ids = [frappe.utils.cint(i.get("id")) for i in inboxes if i.get("channel_type") == "Channel::Instagram"]
    return {"ids": ids}


@frappe.whitelist()
def conectar_whatsapp_cloud(
    phone_number: str,
    phone_number_id: str,
    business_account_id: str,
    access_token: str,
    inbox_name: str = "",
) -> dict:
    """Conecta un número de WhatsApp Cloud API (oficial de Meta, Embedded
    Signup NO incluido) a Chatwoot — investigado y construido 2026-10-19 para
    el caso real de Estrublock (WABA `+1 555-322-1317`, hoy solo en GHL para
    plantillas; ver `/srv/clientes/estrublock/migracionsgpt/planes/
    readiness-migracion-estrublock.md` ítem 6 "Corte WABA" y
    `runbook-waba-plan-b.md`).

    POR QUÉ ES UN FORMULARIO (3 campos pegados a mano) Y NO UN BOTÓN DE LOGIN
        El botón de Facebook de arriba (`conectar_pagina`) funciona sin pedir
        nada a mano porque la app "Sofía CRM — lavendi.mx" (app_id
        2347475169339308) ya tiene aprobados `pages_show_list`,
        `pages_manage_metadata`, `pages_messaging`, `business_management`,
        etc. (ver CLAUDE.md del proyecto, "Meta App Review — APROBADO",
        6-oct-2026). El equivalente de un clic para WhatsApp es el "Embedded
        Signup for WhatsApp" de Meta, que exige los permisos
        `whatsapp_business_management` y `whatsapp_business_messaging` — NO
        están en esa lista de aprobados. Pedirlos abriría una ronda NUEVA de
        App Review (texto + video + revisión de Meta, mismo trámite ya vivido
        para comentarios en octubre) antes de poder ofrecer un botón real de
        auto-conexión. Este formulario NO depende de esa revisión: usa un
        token de **system user** generado a mano en Meta Business Manager
        (Business Manager > Usuarios del sistema), fuera del flujo OAuth de
        esta app — mismo principio que ya usa `Meta Comentarios Settings`
        para guardar un token generado por backend en vez de vía login.

    QUÉ VALIDA CHATWOOT AL CREAR EL INBOX (no hay que revalidar aquí)
        `Channel::Whatsapp#validate_provider_config?` (modelo nativo de
        Chatwoot, contenedor `chatwoot-pabuvqyduzevxbwnrcf3rozs`) llama
        `GET /{business_account_id}/message_templates` y, si cambia el
        `phone_number_id`, también `GET /{business_account_id}/phone_numbers`
        contra la Graph API real con el `access_token` dado — si cualquier
        dato está mal, Chatwoot responde 422 "Invalid Credentials" y
        `cw._post` lo propaga como `ChatwootAPIError` (mismo patrón que
        cualquier otro error de este archivo; el frontend ya sabe mostrar
        `e.messages[0]`). No se crea un inbox "roto" a medias.

    POR QUÉ NO HAY UN DOCTYPE PROPIO guardando estos 3 datos
        A diferencia de `Meta Comentarios Settings` (que vive FUERA de
        Chatwoot por diseño — ver api/meta_comentarios.py), aquí Chatwoot YA
        es el dueño de la verdad: los 3 datos quedan dentro de
        `channel_whatsapp.provider_config`/`business_management_token` de
        Chatwoot al crear el inbox. Duplicarlos en un doctype de Frappe sería
        una copia sin fuente de verdad clara y un secreto más que proteger.

    Reusa `completar_wiring()` igual que `conectar_pagina()` (Messenger):
    agrega el inbox a `canales_bandeja`, avisa a `agente-ia`, suma a
    `agente-ia@lavendi.mx` como colaborador del inbox y clona el `Agente IA`
    del inbox por defecto del sitio — los 4 pasos son best-effort; si alguno
    falla, el inbox ya quedó creado en Chatwoot (no se revierte), igual que
    el resto de este archivo.

    ⚠ NO EJECUTAR contra el número real de Estrublock sin haber desvinculado
    antes la app de GHL de ese WABA (decisión #10 / runbook-waba-plan-b.md):
    conectar el mismo número aquí mientras sigue atado a GHL puede causar
    downtime o conflicto de webhook — ese paso es un gate de producción,
    requiere doble confirmación de Alejandro, y es independiente de que este
    código ya esté listo."""
    _exigir_admin()
    faltantes = [
        nombre
        for nombre, valor in (
            ("phone_number", phone_number),
            ("phone_number_id", phone_number_id),
            ("business_account_id", business_account_id),
            ("access_token", access_token),
        )
        if not (valor or "").strip()
    ]
    if faltantes:
        frappe.throw(f"Faltan datos para conectar WhatsApp: {', '.join(faltantes)}")

    nombre_inbox = (inbox_name or f"WhatsApp Oficial {phone_number.strip()}").strip()

    inbox = cw._post(
        "/inboxes",
        {
            "name": nombre_inbox,
            "channel": {
                "type": "whatsapp",
                "phone_number": phone_number.strip(),
                "provider": "whatsapp_cloud",
                "provider_config": {
                    "api_key": access_token.strip(),
                    "phone_number_id": phone_number_id.strip(),
                    "business_account_id": business_account_id.strip(),
                },
            },
        },
    ) or {}

    inbox_id = inbox.get("id")
    if inbox_id:
        # inbox_origen se deja en su default (`_resolver_inbox_origen` cae al
        # `default_inbox_id` del sitio) — mismo criterio que `conectar_pagina`.
        completar_wiring(str(inbox_id), canal_nombre="WhatsApp")

    return inbox
