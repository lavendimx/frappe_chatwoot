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
"""

import hashlib
import json

import frappe
from pywebpush import WebPushException, webpush

from frappe_chatwoot.frappe_chatwoot.api.chatwoot import ALLOWED_ROLES, validate_role

DEFAULT_URL = "/crm/conversaciones"


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


def _target_users() -> set[str]:
    users = set()
    for role in ALLOWED_ROLES:
        for row in frappe.get_all("Has Role", filters={"role": role, "parenttype": "User"}, fields=["parent"]):
            users.add(row.parent)
    return users


def notify_user(user: str, title: str, body: str, url: str = None, tag: str = None) -> None:
    """Server-side, sin @frappe.whitelist(): solo lo llama notify_handover, que ya validó
    la llamada. Manda a TODOS los dispositivos suscritos de ese usuario."""
    settings = _settings()
    vapid_private = settings.get_password("vapid_private_key", raise_exception=False)
    if not settings.vapid_public_key or not vapid_private:
        frappe.log_error("Sofia Push: VAPID sin configurar", "sofia_push")
        return

    subs = frappe.get_all(
        "Sofia Push Subscription",
        filters={"user": user},
        fields=["name", "endpoint", "p256dh", "auth"],
    )
    payload = json.dumps({"title": title, "body": body, "url": url or DEFAULT_URL, "tag": tag})
    for sub in subs:
        subscription_info = {
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims={"sub": settings.vapid_subject or "mailto:alejandro.moreno@lavendi.mx"},
            )
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                # El navegador ya descartó esta suscripción (PWA desinstalada, endpoint
                # rotado) — limpiar aquí evita reintentar contra un endpoint muerto en
                # cada notificación futura, indefinidamente.
                frappe.delete_doc("Sofia Push Subscription", sub.name, ignore_permissions=True)
            else:
                frappe.log_error(f"Sofia Push: fallo enviando a {sub.name}: {e}", "sofia_push")
    frappe.db.commit()


@frappe.whitelist()
def notify_handover(title: str, body: str, url: str = None, tag: str = None) -> dict:
    """Llamado por agente-ia cuando una conversación se escala o el agente se pausa —
    nunca por tráfico que el bot ya atiende solo. El gate real es el API key/secret de
    servicio: solo un caller autenticado llega hasta aquí (Guest se rechaza abajo)."""
    if not frappe.session.user or frappe.session.user == "Guest":
        frappe.throw("No autorizado", frappe.PermissionError)
    notified = 0
    for user in _target_users():
        if frappe.db.exists("Sofia Push Subscription", {"user": user}):
            notify_user(user, title, body, url=url, tag=tag)
            notified += 1
    return {"ok": True, "notified": notified}
