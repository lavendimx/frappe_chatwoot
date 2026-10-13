# Añadido por lavendi.mx.
"""
Web Push (VAPID) para la PWA de Sofía — sin Firebase ni el notification_relay de Frappe:
las claves son propias, generadas una vez y guardadas en el Single "Sofia Push Settings"
(la privada como Password, nunca sale del servidor). El navegador solo necesita la pública
para suscribirse; el servidor firma cada envío con la privada vía pywebpush.

subscribe/unsubscribe los llama el frontend con la sesión normal del usuario (mismo gate de
roles que el resto de este módulo). notify_handover lo llama agente-ia (proceso Node fuera
de Frappe) con el API key/secret de servicio que ya usa para todo lo demás — por eso no
recibe un `user` objetivo: no hay hoy un dueño por conversación, así que notifica a todo el
equipo con permiso sobre conversaciones y deja que quien la vea primero la tome.

Enrutamiento por destinatario (2026-09-20, segunda pasada): `notify_user` ya era el
primitivo de "avisar a una persona concreta" — lo nuevo es que ahora otros módulos de
`frappe_chatwoot` (tareas, recordatorios, facturas vencidas) lo llaman DIRECTO con el
usuario correcto (deal_owner, assigned_to) en vez de pasar por el broadcast. `notify_roles`
es el primitivo equivalente para "avisar a un grupo de roles", parametrizado — antes estaba
hardcodeado a `ALLOWED_ROLES` de chatwoot.py (roles de conversaciones) dentro de
`notify_handover`; ahora cualquier caller interno puede avisar a un conjunto de roles
distinto (p. ej. cartera vencida = Accounts/System Manager, no todo el equipo de ventas).

Canal FCM (2026-10-08): `Sofia Push Subscription` ahora tiene `channel` (`web_push` por
default, o `fcm`). `subscribe_fcm` registra tokens nativos de Android (app Capacitor) con
el mismo patrón de upsert por hash que `subscribe`, pero hasheando el token en vez del
endpoint. `notify_user` bifurca por `sub.channel` — las filas viejas sin ese campo caen a
`web_push` por default, así que el canal existente no cambia de comportamiento. `firebase_admin`
se importa de forma perezosa DENTRO de `_send_fcm` (no al tope del módulo): mientras el
paquete no esté instalado en el entorno vivo, el resto de `push.py` (incluido el canal
web_push, que otros módulos importan directo) sigue funcionando sin romperse.

Preferencias por usuario×tipo de evento×canal (2026-10-09): hasta hoy el sistema era
todo-o-nada — cualquier dispositivo suscrito recibía cualquier push. `Sofia Notification
Preference` guarda, por (user, event_type), si ese tipo de aviso debe llegar por
`web_push`/`fcm`. `notify_user()` ahora acepta `event_type` opcional: si no se pasa (callers
viejos — tareas.py, recordatorios.py, facturas_vencidas.py, notify_handover/notify_roles),
el comportamiento es idéntico al de siempre, sin preferencia de por medio. `get_notification_preferences`/
`set_notification_preference` son los dos endpoints que usa el panel nuevo para leer/escribir
esa preferencia. Además, `notify_from_crm_notification` enganchado como `after_insert` de
`CRM Notification` (doctype stock de la app `crm`, hook en `hooks.py`) hace que los 4 tipos
que ya alimentan la campanita del CRM (Mention, Task, Assignment, WhatsApp) disparen push
automáticamente, sin tener que enganchar cada evento de negocio uno por uno.
"""

import hashlib
import json

import frappe
from frappe.utils import strip_html
from pywebpush import WebPushException, webpush

from frappe_chatwoot.frappe_chatwoot.api.chatwoot import ALLOWED_ROLES, validate_role

DEFAULT_URL = "/crm/conversaciones"

# Los 6 tipos de evento que hoy soporta el panel de preferencias. Los primeros
# 4 coinciden 1:1 con `CRM Notification.type` (campanita del CRM, app `crm`);
# Recordatorio y Factura son los eventos propios de frappe_chatwoot
# (recordatorios.py, facturas_vencidas.py) que ya llamaban notify_user() antes
# de que existiera este sistema de preferencias.
NOTIFICATION_EVENT_TYPES = ["Mention", "Task", "Assignment", "WhatsApp", "Recordatorio", "Factura"]


def _settings():
    return frappe.get_single("Sofia Push Settings")


def _hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()


@frappe.whitelist()
def get_vapid_public_key() -> str:
    validate_role()
    return _settings().vapid_public_key or ""


@frappe.whitelist()
def subscribe(endpoint: str, p256dh: str, auth: str) -> dict:
    validate_role()
    endpoint_hash = _hash(endpoint)
    existing = frappe.db.exists("Sofia Push Subscription", {"endpoint_hash": endpoint_hash})
    doc = (
        frappe.get_doc("Sofia Push Subscription", existing)
        if existing
        else frappe.new_doc("Sofia Push Subscription")
    )
    doc.endpoint = endpoint
    doc.endpoint_hash = endpoint_hash
    doc.p256dh = p256dh
    doc.auth = auth
    # Un mismo endpoint reasignado a otro usuario (dispositivo compartido, reinstalación)
    # debe apuntar al usuario actual, no acumular el histórico.
    doc.user = frappe.session.user
    doc.user_agent = (frappe.request.headers.get("User-Agent") or "")[:140] if frappe.request else ""
    doc.last_seen = frappe.utils.now_datetime()
    doc.channel = "web_push"
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True}


@frappe.whitelist()
def subscribe_fcm(token: str) -> dict:
    """Registro de un token de FCM (push nativo Android, app Capacitor). Mismo patrón de
    upsert que `subscribe`, pero el hash se calcula sobre el token en vez del endpoint —
    no hay endpoint/p256dh/auth en este canal."""
    validate_role()
    endpoint_hash = _hash(token)
    existing = frappe.db.exists("Sofia Push Subscription", {"endpoint_hash": endpoint_hash})
    doc = (
        frappe.get_doc("Sofia Push Subscription", existing)
        if existing
        else frappe.new_doc("Sofia Push Subscription")
    )
    doc.channel = "fcm"
    doc.fcm_token = token
    doc.endpoint_hash = endpoint_hash
    # Mismo criterio que subscribe(): un token reasignado a otro usuario debe apuntar
    # al usuario actual, no acumular el histórico.
    doc.user = frappe.session.user
    doc.user_agent = (frappe.request.headers.get("User-Agent") or "")[:140] if frappe.request else ""
    doc.last_seen = frappe.utils.now_datetime()
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True}


@frappe.whitelist()
def unsubscribe(endpoint: str) -> dict:
    validate_role()
    name = frappe.db.exists("Sofia Push Subscription", {"endpoint_hash": _hash(endpoint)})
    if name:
        frappe.delete_doc("Sofia Push Subscription", name, ignore_permissions=True)
        frappe.db.commit()
    return {"ok": True}


def _preference_enabled(user: str, event_type: str | None, channel: str) -> bool:
    """True si `user` quiere recibir avisos de `event_type` por `channel`
    (`web_push` o `fcm`). `event_type=None` preserva el comportamiento de
    siempre (ver docstring del módulo) — y una fila inexistente también
    cuenta como activado: el default es todo prendido hasta que el usuario
    apague algo explícito desde el panel, para no romper notificaciones de
    gente que nunca abrió esa pantalla."""
    if not event_type:
        return True
    name = frappe.db.exists("Sofia Notification Preference", {"user": user, "event_type": event_type})
    if not name:
        return True
    field = "enabled_web" if channel == "web_push" else "enabled_fcm"
    return bool(frappe.db.get_value("Sofia Notification Preference", name, field))


@frappe.whitelist()
def get_notification_preferences() -> list[dict]:
    """Para `frappe.session.user`: una fila por cada uno de los 6 `event_type`,
    aunque el usuario nunca haya tocado el panel — si no existe fila real en
    `Sofia Notification Preference`, se devuelve el default (todo activado)."""
    validate_role()
    user = frappe.session.user
    existentes = {
        row.event_type: row
        for row in frappe.get_all(
            "Sofia Notification Preference",
            filters={"user": user},
            fields=["event_type", "enabled_web", "enabled_fcm"],
        )
    }
    return [
        {
            "event_type": event_type,
            "enabled_web": bool(existentes[event_type].enabled_web) if event_type in existentes else True,
            "enabled_fcm": bool(existentes[event_type].enabled_fcm) if event_type in existentes else True,
        }
        for event_type in NOTIFICATION_EVENT_TYPES
    ]


@frappe.whitelist()
def set_notification_preference(event_type: str, channel: str, enabled: bool) -> dict:
    """Upsert de la fila `(frappe.session.user, event_type)`, prendiendo/apagando
    el campo del canal correspondiente (`channel="web"` -> `enabled_web`,
    `channel="fcm"` -> `enabled_fcm`; nombres cortos hacia el frontend, no los
    mismos que `Sofia Push Subscription.channel`, que usa `web_push`/`fcm`)."""
    validate_role()
    if event_type not in NOTIFICATION_EVENT_TYPES:
        frappe.throw(f"event_type inválido: {event_type}")
    if channel not in ("web", "fcm"):
        frappe.throw(f"channel inválido: {channel}")

    enabled = frappe.utils.cint(enabled) if not isinstance(enabled, bool) else enabled
    field = "enabled_web" if channel == "web" else "enabled_fcm"
    user = frappe.session.user

    name = frappe.db.exists("Sofia Notification Preference", {"user": user, "event_type": event_type})
    doc = (
        frappe.get_doc("Sofia Notification Preference", name)
        if name
        else frappe.new_doc("Sofia Notification Preference")
    )
    doc.user = user
    doc.event_type = event_type
    setattr(doc, field, 1 if enabled else 0)
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True}


def _target_users(roles: list[str] | None = None) -> set[str]:
    users = set()
    for role in (roles or ALLOWED_ROLES):
        for row in frappe.get_all("Has Role", filters={"role": role, "parenttype": "User"}, fields=["parent"]):
            users.add(row.parent)
    return users


def notify_roles(roles: list[str], title: str, body: str, url: str = None, tag: str = None) -> int:
    """Broadcast a quien tenga alguno de los roles dados. Para eventos sin un dueño
    individual claro (ej. cartera vencida: responsabilidad de cuentas/dirección, no de
    un solo vendedor). Server-side, sin @frappe.whitelist() — mismo criterio que
    notify_user: solo lo llaman callers ya autenticados/internos."""
    notified = 0
    for user in _target_users(roles):
        if frappe.db.exists("Sofia Push Subscription", {"user": user}):
            notify_user(user, title, body, url=url, tag=tag)
            notified += 1
    return notified


def notify_user(
    user: str, title: str, body: str, url: str = None, tag: str = None, event_type: str = None
) -> None:
    """Server-side, sin @frappe.whitelist(): lo llama cualquier módulo interno que ya
    sabe a qué usuario avisar (notify_handover/notify_roles para broadcast; tareas.py,
    recordatorios.py y facturas_vencidas.py para un destinatario puntual). Manda a
    TODOS los dispositivos suscritos de ese usuario.

    `event_type` (2026-10-09, opcional): si se pasa, cada suscripción se filtra contra
    `Sofia Notification Preference` antes de enviar — ver `_preference_enabled`. Los
    callers que todavía no lo pasan (`None`) no cambian de comportamiento.

    Sin `frappe.db.commit()` incondicional al final (2026-09-20, corregido al
    engancharlo dentro de un doc_event de `CRM Task`): un commit a mitad de una
    request normal de guardado le rompe el `check_if_latest()` del propio doc que se
    está guardando — `TimestampMismatchError` al primer `.save()` posterior al
    `after_insert`. `webpush()` es una llamada HTTP externa, sin escritura local; el
    único commit real que hace falta es el de limpiar una suscripción muerta."""
    settings = _settings()
    vapid_private = settings.get_password("vapid_private_key", raise_exception=False)
    vapid_ready = bool(settings.vapid_public_key and vapid_private)
    if not vapid_ready:
        # No se corta la función: filas `channel=fcm` no dependen de VAPID y deben
        # poder notificarse igual. Cada sub web_push se salta individualmente abajo.
        frappe.log_error("Sofia Push: VAPID sin configurar", "sofia_push")

    subs = frappe.get_all(
        "Sofia Push Subscription",
        filters={"user": user},
        fields=["name", "channel", "endpoint", "p256dh", "auth", "fcm_token"],
    )
    payload = {"title": title, "body": body, "url": url or DEFAULT_URL, "tag": tag}
    payload_json = json.dumps(payload)
    for sub in subs:
        # Filas creadas antes de que existiera el campo `channel` no tienen valor —
        # deben seguir tratándose como web_push para no perder su suscripción.
        channel = sub.channel or "web_push"

        if not _preference_enabled(user, event_type, channel):
            # El usuario apagó este tipo de aviso en este canal — se salta, no se
            # borra: no es una suscripción muerta, es una preferencia.
            continue

        if channel == "fcm":
            _send_fcm(sub, payload)
            continue

        if not vapid_ready:
            continue

        subscription_info = {
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload_json,
                vapid_private_key=vapid_private,
                vapid_claims={"sub": settings.vapid_subject or "mailto:alejandro.moreno@lavendi.mx"},
            )
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                # El navegador ya descartó esta suscripción (PWA desinstalada, endpoint
                # rotado) — limpiar aquí evita reintentar contra un endpoint muerto en
                # cada notificación futura, indefinidamente. Commit puntual: es la
                # única escritura local real de esta función.
                frappe.delete_doc("Sofia Push Subscription", sub.name, ignore_permissions=True)
                frappe.db.commit()
            else:
                frappe.log_error(f"Sofia Push: fallo enviando a {sub.name}: {e}", "sofia_push")
        except Exception as e:
            # 2026-09-21: hallado en producción — un `vapid_private_key` en formato
            # incorrecto (ej. DER/PKCS8 en vez del raw base64url de 32 bytes que espera
            # pywebpush) revienta DENTRO de la firma de `webpush()`, antes de tocar la
            # red: `cryptography` lanza `ValueError: Could not deserialize key data`, que
            # NO hereda de `WebPushException` y por eso no lo cachaba el except de arriba
            # — quedaba como excepción sin manejar en cada invocación. Es un problema de
            # configuración del SITIO (la llave), no de esta suscripción puntual: seguir
            # iterando `subs` fallaría idéntico en cada una web_push, así que se registra
            # UNA vez y se corta el loop en vez de ensuciar el log por cada suscriptor.
            # Las filas `fcm` restantes en `subs` ya se procesaron arriba en su propia
            # rama (o se procesan si vienen después — este `return` solo corta la rama
            # web_push, que es la que de verdad depende de esta llave).
            frappe.log_error(f"Sofia Push: vapid_private_key inválida o config rota: {e}", "sofia_push")
            vapid_ready = False
            continue


def _send_fcm(sub, payload: dict) -> None:
    """Envía un push nativo Android vía Firebase Cloud Messaging. Import perezoso de
    `firebase_admin` (no al tope del módulo): si el paquete no está instalado todavía en
    el entorno vivo, esto falla solo, sin tumbar el import de todo `push.py` (del que
    dependen tareas.py, recordatorios.py, facturas_vencidas.py para el canal web_push)."""
    try:
        import firebase_admin
        from firebase_admin import credentials, messaging
    except ImportError:
        frappe.log_error("Sofia Push: firebase_admin no instalado", "sofia_push")
        return

    settings = _settings()
    cred_path = settings.get("fcm_service_account_path")
    if not cred_path:
        frappe.log_error("Sofia Push: fcm_service_account_path sin configurar", "sofia_push")
        return

    # Gunicorn corre varios workers; cada uno importa este módulo por separado, así que
    # el guard es obligatorio — sin él, el segundo `initialize_app()` en el mismo proceso
    # lanza ValueError("The default Firebase app already exists").
    if not firebase_admin._apps:
        try:
            firebase_admin.initialize_app(credentials.Certificate(cred_path))
        except Exception as e:
            frappe.log_error(f"Sofia Push: no se pudo inicializar firebase_admin: {e}", "sofia_push")
            return

    message = messaging.Message(
        token=sub.fcm_token,
        notification=messaging.Notification(title=payload["title"], body=payload["body"]),
        data={"url": payload["url"] or DEFAULT_URL},
    )
    try:
        messaging.send(message)
    except messaging.UnregisteredError:
        # Equivalente al 404/410 de web_push: el token ya no es válido (app desinstalada,
        # token rotado) — borrar evita reintentar contra un token muerto indefinidamente.
        frappe.delete_doc("Sofia Push Subscription", sub.name, ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        code = getattr(e, "code", None) or ""
        if "UNREGISTERED" in str(code).upper() or "NOT_FOUND" in str(code).upper():
            frappe.delete_doc("Sofia Push Subscription", sub.name, ignore_permissions=True)
            frappe.db.commit()
        else:
            frappe.log_error(f"Sofia Push: fallo FCM enviando a {sub.name}: {e}", "sofia_push")


@frappe.whitelist()
def notify_handover(title: str, body: str, url: str = None, tag: str = None) -> dict:
    """Llamado por agente-ia cuando una conversación se escala o el agente se pausa —
    nunca por tráfico que el bot ya atiende solo. El gate real es el API key/secret de
    servicio: solo un caller autenticado llega hasta aquí (Guest se rechaza abajo)."""
    if not frappe.session.user or frappe.session.user == "Guest":
        frappe.throw("No autorizado", frappe.PermissionError)
    notified = notify_roles(ALLOWED_ROLES, title, body, url=url, tag=tag)
    return {"ok": True, "notified": notified}


def notify_from_crm_notification(doc, method=None) -> None:
    """Hook `after_insert` de `CRM Notification` (doctype stock de la app `crm`, wiring
    en `hooks.py`): engancha push a los 4 tipos que ya alimentan la campanita del CRM
    (Mention, Task, Assignment, WhatsApp) sin tener que enganchar cada evento de negocio
    de origen uno por uno — la campanita ya centraliza esa decisión.

    Envuelto en try/except amplio a propósito: esto corre dentro del `after_insert` de
    un doctype AJENO. Un error aquí jamás debe tumbar la creación de la notificación
    in-app (que ya insertó exitosamente antes de llegar a este hook)."""
    try:
        if not doc.to_user or doc.to_user == doc.from_user:
            # Mismo criterio que crm_notification.py: notify_user() (owner ==
            # assigned_to -> no hay nada que avisar) y sin destinatario no hay a
            # quién mandarle el push.
            return

        from_user_label = frappe.get_cached_value("User", doc.from_user, "full_name") or doc.from_user
        title = f"{from_user_label} — {doc.type}"
        body = strip_html(doc.notification_text or doc.message or "")

        # Misma lógica de ruteo que crm/api/notifications.py:get_notifications():
        # CRM Deal -> "deal", cualquier otro valor (normalmente CRM Lead) -> "lead".
        if not doc.reference_name:
            url = DEFAULT_URL
        elif doc.reference_doctype == "CRM Deal":
            url = f"/crm/deals/{doc.reference_name}"
        else:
            url = f"/crm/leads/{doc.reference_name}"

        notify_user(doc.to_user, title, body, url=url, tag=doc.type, event_type=doc.type)
    except Exception as e:
        frappe.log_error(f"Sofia Push: fallo en notify_from_crm_notification: {e}", "sofia_push")
