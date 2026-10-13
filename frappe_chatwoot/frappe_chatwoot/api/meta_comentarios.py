# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Respuesta DETERMINISTA (sin IA) a comentarios públicos de Facebook/Instagram.

POR QUÉ EXISTE
    GHL respondía comentarios de publicaciones; Sofía CRM hoy no — Chatwoot no
    modela comentarios como conversación (confirmado en
    `planes/redes-mensajeria-chatwoot-meta.md`), así que este frente vive
    FUERA de Chatwoot por completo: almacenamiento propio (doctypes
    `Comentario Meta` / `Meta Comentarios Settings`, creados por
    `patches/create_comentarios_meta_doctypes.py`), token propio.

POR QUÉ SONDEO (POLLING) Y NO WEBHOOK — corregido 2026-10-09, versión anterior
rota
    La primera versión de este módulo usaba un webhook propio. Error real: la
    configuración de Webhooks de Meta es **una URL por objeto** (`page`,
    `instagram`), no por campo. El objeto `page` de la app "Sofía CRM -
    lavendi.mx" YA tiene su URL tomada por Chatwoot (`chat.lavendi.mx/bot`,
    Messenger en producción, inbox 12). Agregar el campo `feed` ahí habría
    mandado los eventos de comentarios al endpoint de CHATWOOT, no a este
    backend — el webhook nunca habría disparado nada, en silencio. Corregido
    ANTES de deployar nada (se detectó al revisar el plan de despliegue, no
    en producción).

    La alternativa correcta y más segura: sondear la Graph API cada 10 min
    (`utils/meta_comentarios_sync.py::sincronizar`, vía scheduler de Frappe)
    — mismo patrón que ya usa `agente-ia` con Chatwoot (polling, no
    websockets/webhooks). Cero riesgo de tocar el webhook de `page` que ya
    sirve Messenger en vivo. Latencia de hasta 10 min en vez de tiempo real —
    aceptable: esto es evidencia de respuesta, no un chat.

POR QUÉ UN TOKEN SEPARADO DEL DE CHATWOOT (y por qué el MISMO token sirve para
Facebook e Instagram)
    El token que Chatwoot guarda para el inbox de Messenger (`Channel::
    FacebookPage#page_access_token`) se pidió con scope
    `pages_show_list,pages_messaging,pages_manage_metadata,business_management`
    (ver `RedesSociales.vue`) — NUNCA incluyó `pages_manage_engagement`
    (verificado 2026-10-09 contra el token real vía `/debug_token`). Pedirlo
    aquí, en un login APARTE que nunca toca el inbox de Chatwoot, evita pisar
    el token de mensajería y evita el filtro de "página ya conectada" que
    tiene `RedesSociales.vue`.

    Instagram NO necesita un login ni un token propio: la cuenta de Instagram
    Business vinculada a la página se opera con el MISMO `page_access_token`
    (`GET /{page_id}?fields=instagram_business_account` descubre el ID una
    sola vez al conectar) — verificado contra la documentación de Meta
    2026-10-09. Por eso el bloqueo original de Instagram ("no hay forma de
    pedirle el scope a Chatwoot") estaba mal planteado: esto nunca necesitó
    pasar por el login nativo de Instagram de Chatwoot — es la MISMA conexión
    de Facebook, con más scopes.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

import frappe

GRAPH_API_VERSION = "v22.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# pages_manage_engagement -> responder comentarios de Facebook.
# pages_read_engagement   -> listar posts/comentarios de Facebook.
# instagram_manage_coments -> leer/responder comentarios de Instagram vía el page token
#   (producto "Instagram API with Facebook Login", cuenta vinculada a la página).
# instagram_business_basic -> quitado 2026-10-13: tronaba con "Invalid Scopes" en el login
#   real de ESTE flujo (login clásico de Facebook, facebook.com/dialog/oauth con scope=).
#   Investigado contra la documentación oficial de Meta (referencia de permisos de
#   Facebook Login): ese permiso está documentado para el producto "Instagram Login"
#   (Instagram API with Instagram Login, el login nativo directo que SÍ usa
#   `autorizar_instagram()`/Chatwoot para mensajería) — no para el login clásico de
#   Facebook que usa este archivo. Por eso ya estaba aprobado en App Review (mensajería)
#   pero sigue siendo "Invalid Scopes" aquí: no es el mismo producto de login, aunque sea
#   la misma app y el mismo permiso ya aprobado.
# ads_read -> quitado 2026-10-09: tronaba con "Invalid Scopes" (nunca se agregó a la app
#             en el dashboard de Meta) y era especulativo desde el inicio ("por si acaso"
#             para páginas vía Business Manager) — no bloquea lavendi.mx, que es admin
#             directo. Si una página de un socio (Estrublock/Ena Barrera/Esplendido) lo
#             necesita de verdad, hay que agregarlo primero en Permisos y características
#             del dashboard de Meta antes de volver a pedirlo aquí.
# instagram_basic -> necesario para DOS cosas en este flujo: (a) que la Graph API devuelva
#   el campo `instagram_business_account` de la página (sin él lo omite en silencio, sin
#   error — por eso en el primer login quedó conectado Facebook y Instagram no), y (b) los
#   endpoints de comentarios de Instagram (`/{ig-id}/media`, `/{media-id}/comments`), que
#   lo exigen junto con `instagram_manage_comments`.
#   NO confundir con `instagram_business_basic`: ese pertenece al producto "Instagram Login"
#   (el login nativo que usa Chatwoot para mensajería) y por eso tronaba aquí aunque esté
#   aprobado — ver arriba. Este archivo usa el login CLÁSICO de Facebook.
#   Historia: el 2026-10-13 el login real devolvió "Invalid Scopes: instagram_basic" porque
#   el permiso no existía en la app. Alejandro lo agregó el mismo día desde "Permisos y
#   funciones" del dashboard (deliberadamente NO con los botones de paquete de
#   "Configuración de la API con inicio de sesión de Facebook", que habrían metido
#   `instagram_content_publishing` e `instagram_manage_messages` — superficie que no se
#   necesita). Quedó en "Listo para la prueba" (Standard Access), suficiente para páginas
#   propias; Meta avisó que el permiso también queda listado en el caso de uso "Interactuar
#   con los clientes en Messenger" — aceptado, es solo un permiso más en la lista, no abre
#   revisión nueva.
#   Dato de respaldo por si el descubrimiento automático vuelve a fallar: el
#   `ig_business_account_id` de @lavendimx es 17841403673506557 (visto en Chatwoot,
#   tabla `channel_instagram`, inbox 22) y se puede cablear a mano — `conectar_...` ya no
#   lo sobrescribe con "" cuando Meta no lo devuelve.
# instagram_manage_messages -> agregado 2026-10-13 para el MENSAJE PRIVADO inicial
#   (Private Replies de Meta, ver `_enviar_dm`). Pregunta razonable de Alejandro: si la
#   revisión de mensajería ya aprobó `instagram_business_manage_messages`, ¿para qué este?
#   Respuesta: son dos familias de permisos distintas y este archivo usa el login CLÁSICO
#   de Facebook — `instagram_business_*` solo sirve con "Instagram Login"
#   (graph.instagram.com, el que usa Chatwoot). Para mandar el DM con el MISMO
#   `page_access_token` que ya usa este módulo hace falta `instagram_manage_messages`.
#   Facebook no necesita nada NUEVO en el dashboard: `pages_messaging` ya está aprobado
#   desde la revisión de mensajería (6-oct) — pero sí hay que PEDIRLO en este login, que no
#   lo incluía. Aprobado en la app ≠ horneado en el token: Meta hornea en cada
#   `page_access_token` únicamente los permisos que el scope del login pidió.
SCOPES_COMENTARIOS = (
    "pages_show_list,pages_manage_engagement,pages_read_engagement,pages_messaging,"
    "pages_read_user_content,instagram_basic,instagram_manage_comments,instagram_manage_messages"
)


def _exigir_admin():
    """Mismo gate que `meta_conexion.py::_exigir_admin` — configurar esto es
    tan sensible como conectar una página: cualquiera con este acceso puede
    publicar texto público a nombre de la página."""
    roles = set(frappe.get_roles(frappe.session.user))
    if frappe.session.user != "Administrator" and not ({"System Manager", "Sales Manager"} & roles):
        frappe.throw("Solo un administrador de ventas puede configurar respuesta a comentarios.", frappe.PermissionError)


def _settings():
    return frappe.get_single("Meta Comentarios Settings")


def _graph_get(path: str, params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{GRAPH_API_BASE}/{path}?{qs}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return json.loads(body)
        except ValueError:
            return {"error": {"message": body, "code": exc.code}}


def _graph_post(path: str, data: dict) -> dict:
    req = urllib.request.Request(
        f"{GRAPH_API_BASE}/{path}",
        data=urllib.parse.urlencode(data).encode(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"status": resp.status, "body": json.loads(resp.read())}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            body = json.loads(body)
        except ValueError:
            pass
        return {"status": exc.code, "body": body}


@frappe.whitelist()
def config_comentarios() -> dict:
    """Config pública para el JS SDK — mismo App ID que `meta_conexion.config()`,
    scope DISTINTO (el de comentarios: Facebook + Instagram en un solo login)."""
    _exigir_admin()
    cfg = frappe.get_cached_doc("Chatwoot Settings")
    settings = _settings()
    return {
        "fb_app_id": (cfg.get("fb_app_id") or "").strip(),
        "fb_api_version": GRAPH_API_VERSION,
        "scopes": SCOPES_COMENTARIOS,
        "ya_configurado": bool(settings.fb_page_access_token),
        "activo": bool(settings.activo),
        "fb_page_id": settings.fb_page_id or "",
        "ig_business_account_id": settings.ig_business_account_id or "",
    }


@frappe.whitelist()
def conectar_comentarios_facebook(user_access_token: str, page_id: str) -> dict:
    """Intercambia el `user_access_token` (scope `SCOPES_COMENTARIOS`) por el
    `page_access_token` de esa página, vía Graph API directo — NUNCA pasa por
    Chatwoot, no crea ni toca ningún inbox. Descubre de paso la cuenta de
    Instagram Business vinculada, si existe — un solo login cubre los dos
    canales.

    Deja `activo=0` siempre, aunque ya hubiera un token antes: encender la
    respuesta automática es una decisión aparte (ver `activar_comentarios`),
    para que guardar credenciales nunca dispare publicaciones por sorpresa.
    """
    _exigir_admin()
    if not (user_access_token or "").strip():
        frappe.throw("Falta el token de Facebook (user_access_token).")
    if not (page_id or "").strip():
        frappe.throw("Falta el page_id.")

    data = _graph_get(
        str(page_id),
        {"fields": "access_token,name,instagram_business_account", "access_token": user_access_token},
    )
    page_token = data.get("access_token")
    if not page_token:
        frappe.throw(
            "La Graph API no devolvió access_token de página — revisa que el login haya incluido "
            f"el scope completo ({SCOPES_COMENTARIOS}) y que seas admin/socio de esa página. "
            f"Respuesta de Meta: {json.dumps(data)[:300]}"
        )

    ig_account = (data.get("instagram_business_account") or {}).get("id")
    ig_username = ""
    if ig_account:
        ig_info = _graph_get(str(ig_account), {"fields": "username", "access_token": page_token})
        ig_username = ig_info.get("username") or ""

    settings = _settings()
    settings.fb_page_id = str(page_id)
    settings.fb_page_access_token = page_token
    # NO borrar un `ig_business_account_id` ya guardado cuando Meta no devuelve el campo:
    # sin el permiso `instagram_basic` (pendiente en el dashboard, ver SCOPES_COMENTARIOS)
    # la Graph API omite `instagram_business_account` en silencio, y sobrescribir con ""
    # borraría un id cableado a mano en cada reconexión de Facebook.
    if ig_account:
        settings.ig_business_account_id = str(ig_account)
        settings.ig_username = ig_username
    settings.activo = 0
    settings.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "ok": True,
        "page_id": str(page_id),
        "page_name": data.get("name"),
        "ig_business_account_id": ig_account,
        "siguiente_paso": (
            "Revisar/editar la plantilla de respuesta en Meta Comentarios Settings y llamar a "
            "activar_comentarios() cuando quede confirmado el primer caso real."
            if ig_account
            else "Esta página no tiene una cuenta de Instagram Business vinculada — solo Facebook "
            "quedó conectado. Vincularla en Meta Business Suite y volver a conectar aquí si se "
            "quiere cubrir Instagram también."
        ),
    }


@frappe.whitelist()
def activar_comentarios(activo: bool = True) -> dict:
    """Interruptor maestro, separado a propósito de `conectar_comentarios_facebook`
    — guardar el token nunca debe encender la publicación automática por sí
    solo."""
    _exigir_admin()
    settings = _settings()
    if not settings.fb_page_access_token:
        frappe.throw("No hay token de comentarios configurado todavía.")
    settings.activo = 1 if frappe.utils.cint(activo) else 0
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "activo": bool(settings.activo)}


def _registrar_comentario(canal: str, comment_id: str, post_id: str, remitente: str, texto: str):
    """Idempotente por `comment_id` (unique) — cada corrida del sondeo vuelve a
    leer los mismos posts recientes, así que el mismo comentario se ve muchas
    veces; solo la primera inserta."""
    if frappe.db.exists("Comentario Meta", comment_id):
        return frappe.get_doc("Comentario Meta", comment_id)
    doc = frappe.get_doc({
        "doctype": "Comentario Meta",
        "canal": canal,
        "comment_id": comment_id,
        "post_id": post_id,
        "remitente": remitente,
        "texto": texto,
        "estado": "Pendiente",
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc


def _responder_comentario(doc) -> None:
    """Nunca deja un `Comentario Meta` en Pendiente por una excepción propia —
    eso sería peor que no tener esta tabla: Alejandro pidió evidencia de que SÍ
    se intentó contestar, así que cualquier falla (token vacío, red caída,
    respuesta inesperada) se escribe como Error, nunca se traga en silencio."""
    try:
        settings = _settings()
        plantilla = settings.plantilla_respuesta or "Gracias por tu comentario."
        token = settings.get_password("fb_page_access_token") if settings.fb_page_access_token else None
        if not token:
            raise ValueError("Meta Comentarios Settings no tiene fb_page_access_token configurado.")
        endpoint = f"{doc.comment_id}/replies" if doc.canal == "Instagram" else f"{doc.comment_id}/comments"
        campo_mensaje = "message" if doc.canal == "Instagram" else "message"
        resultado = _graph_post(endpoint, {campo_mensaje: plantilla, "access_token": token})

        if resultado["status"] == 200 and isinstance(resultado["body"], dict) and resultado["body"].get("id"):
            doc.estado = "Respondido"
            doc.respuesta_enviada = plantilla
            doc.fecha_respondido = frappe.utils.now_datetime()
        else:
            doc.estado = "Error"
            doc.detalle_error = json.dumps(resultado["body"])[:500]
    except Exception as exc:  # noqa: BLE001 — a propósito: ver docstring, nunca Pendiente silencioso
        doc.estado = "Error"
        doc.detalle_error = str(exc)[:500]
    doc.save(ignore_permissions=True)
    frappe.db.commit()


def _dm_ya_entregado(cuerpo) -> bool:
    """¿El error de Meta significa "este comentario YA tiene su mensaje privado"?

    Meta admite un solo DM por comentario. Cuando se pide el segundo responde con
    `10900` ("Activity already replied to", Facebook) o el subcódigo `2534023`
    (Instagram). Ambos confirman la entrega del primero — por eso cuentan como éxito,
    no como fallo: así el reintento converge a `Enviado` en vez de quedarse atorado en
    `Error` reportando un problema que no existe.
    """
    if not isinstance(cuerpo, dict):
        return False
    err = cuerpo.get("error") or {}
    return err.get("code") == 10900 or err.get("error_subcode") in (2534023, 2534014)


def _enviar_dm(doc) -> None:
    """Manda el mensaje privado inicial al autor del comentario — "Private Replies" de Meta,
    la ÚNICA vía documentada para que una página escriba por DM a alguien que nunca le ha
    escrito (fuera de eso Meta lo bloquea: no hay ventana de mensajería abierta).

    POR QUÉ EXISTE, aparte de contestar el comentario
        Contestar en público solo deja evidencia; el objetivo real es abrir conversación de
        ventas. Este DM es el primer mensaje: cuando el prospecto responde, su respuesta
        entra al inbox normal de Chatwoot (12 Messenger / 22 Instagram) y de ahí la toma
        Sofía Ventas con todo su contexto. Por eso el texto de aquí es DETERMINISTA y corto
        — no intenta vender, solo abre la puerta; vender es trabajo de Sofía Ventas.

    POR QUÉ NO PASA POR CHATWOOT
        Chatwoot no tiene API de private reply (no modela comentarios). El envío tiene que
        ser Graph API directo. Costo asumido: este primer mensaje no queda en el historial
        de Chatwoot — la conversación arranca ahí con la respuesta del prospecto.

    EL MISMO ENDPOINT PARA LOS DOS CANALES (corregido 2026-10-12, probado en vivo)
        `POST /{page_id}/messages` con `recipient: {"comment_id": ...}` — el id de la PÁGINA
        de Facebook, incluso para comentarios de Instagram. Meta enruta solo según de dónde
        venga el `comment_id`.

        Las dos variantes que parecían correctas y fallan de verdad (medidas, no supuestas):
          - Facebook `POST /{comment_id}/private_replies` -> 100/33 "does not exist, cannot
            be loaded due to missing permissions, or does not support this operation". Es el
            endpoint que sale en los tutoriales viejos; con "new page experience" ya no.
          - Instagram `POST /{ig_business_account_id}/messages` -> (#3) "Application does not
            have the capability to make this API call". Ese endpoint es de la familia
            "Instagram Login" (graph.instagram.com, el que usa Chatwoot), no del Facebook
            Login clásico que usa este módulo.
        Los dos errores se leían como falta de permisos y NO lo eran: el token ya traía los 8
        scopes (verificado con `debug_token`). Era la ruta, no el permiso.

    LÍMITES DE META (los dos, verificados en su documentación)
        - 7 días desde el comentario. Pasado eso, Meta rechaza.
        - UN solo DM por comentario. Un segundo intento da error (subcódigo 2534014 en
          Instagram) — de ahí que `dm_estado` se marque siempre y nunca se reintente un
          `Enviado`.
    """
    if doc.dm_estado == "Enviado":
        return  # Meta solo admite uno por comentario — reintentar sería un error garantizado
    try:
        settings = _settings()
        mensaje = (settings.plantilla_dm or "").strip()
        if not mensaje:
            return  # plantilla vacía = DM desactivado a propósito, no es un error
        token = settings.get_password("fb_page_access_token") if settings.fb_page_access_token else None
        if not token:
            raise ValueError("Meta Comentarios Settings no tiene fb_page_access_token configurado.")

        if not settings.fb_page_id:
            raise ValueError("Meta Comentarios Settings no tiene fb_page_id configurado.")

        # El destinatario es el COMENTARIO, no el usuario: así es como Meta autoriza escribirle
        # a alguien sin ventana de mensajería previa. Mismo endpoint para Facebook e Instagram
        # — ver el bloque del docstring sobre las dos rutas que NO funcionan.
        resultado = _graph_post(
            f"{settings.fb_page_id}/messages",
            {
                "recipient": json.dumps({"comment_id": doc.comment_id}),
                "message": json.dumps({"text": mensaje}),
                "access_token": token,
            },
        )

        cuerpo = resultado["body"]
        ok = resultado["status"] == 200 and isinstance(cuerpo, dict) and not cuerpo.get("error")
        if ok or _dm_ya_entregado(cuerpo):
            # `_dm_ya_entregado`: Meta dice "ya respondiste este comentario". Eso NO es un
            # fallo — es la prueba de que el DM salió (en un intento anterior cuyo guardado
            # no alcanzó a persistir). Marcarlo Error dejaría el registro mintiendo para
            # siempre, porque el reintento va a dar exactamente el mismo error.
            doc.dm_estado = "Enviado"
            doc.dm_detalle_error = None
        else:
            doc.dm_estado = "Error"
            doc.dm_detalle_error = json.dumps(cuerpo)[:500]
    except Exception as exc:  # noqa: BLE001 — mismo criterio que _responder_comentario
        doc.dm_estado = "Error"
        doc.dm_detalle_error = str(exc)[:500]
    doc.save(ignore_permissions=True)
    frappe.db.commit()
