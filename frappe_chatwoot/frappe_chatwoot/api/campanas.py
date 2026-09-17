# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Campañas de email (Newsletter) expuestas en el producto Sofía GPT.

Se copia a `apps/frappe_chatwoot/frappe_chatwoot/frappe_chatwoot/api/campanas.py`.

CONTEXTO
    El motor de Newsletter quedó listo y validado el 2026-09-16 (ver
    `patches/README.md` y `overrides/newsletter.py`): personalización por
    destinatario, correo saliente real por Resend y tracking de apertura. Pero
    **no había forma de usarlo desde el producto**: la única puerta era el Desk
    (`/app/newsletter`), que expone cientos de doctypes genéricos y rompe la
    identidad y el aislamiento del cliente.

    Este endpoint es la API de la sección "Campañas de email" del SPA. El
    aislamiento por cliente ya no es un filtro por `inbox_id`: cada cliente vive
    en **su propio sitio Frappe** (multi-sitio), así que sus `Newsletter` y
    `Email Group` son suyos por construcción. En el sitio compartido las
    campañas son de lavendi.mx.

    Gate de roles: enviar una campaña manda correo real a una lista completa, así
    que crear/probar/enviar exigen System Manager o Sales Manager (mismo criterio
    que `utils/secuencias.py` y `api/facturacion.py`). Ver la lista lo puede
    hacer cualquier usuario con acceso al CRM.

NOTA sobre `send_test_email` del core
    `Newsletter.send_test_email()` llama `validate_email_address()` (que devuelve
    un `str`) y lo pasa como `emails=` a `send_newsletter()`, que itera
    **caracteres** → no encola nada y no lanza error (falla en silencio). Por eso
    `probar()` no lo usa: manda con lista explícita.
"""

import frappe
from frappe import _

_ROLES_EDICION = ("System Manager", "Sales Manager")


def _puede_editar():
    roles = frappe.get_roles(frappe.session.user)
    return frappe.session.user == "Administrator" or any(r in roles for r in _ROLES_EDICION)


def _exigir_edicion():
    if not _puede_editar():
        frappe.throw(_("Sin permiso para administrar campañas"), frappe.PermissionError)
    # `Newsletter` es un doctype del core con un solo DocPerm ("Newsletter
    # Manager") — ni System Manager entra. El gate de arriba es la frontera de
    # seguridad de este endpoint, igual que en `api/facturacion.py`, así que las
    # operaciones sobre el documento van con `ignore_permissions`. Sin esto, crear
    # una campaña desde el SPA da 403 aunque el usuario sea Sales Manager.
    frappe.flags.ignore_permissions = True


def _remitente_default():
    return (
        frappe.db.get_value("Email Account", {"default_outgoing": 1, "enable_outgoing": 1}, "email_id")
        or "contacto@lavendi.mx"
    )


def _estado(c):
    if c.get("email_sent"):
        return "Enviada"
    if c.get("schedule_send"):
        return "Programada"
    return "Borrador"


@frappe.whitelist()
def listar():
    """Campañas con sus métricas, más recientes primero.

    `aperturas` = `total_views` (lo incrementa `newsletter_email_read`, el tracker
    de imagen del core). El motor **no** registra clics: no se inventa esa cifra.
    """
    campanas = frappe.get_all(
        "Newsletter",
        fields=[
            "name",
            "subject",
            "email_sent",
            "email_sent_at",
            "total_recipients",
            "total_views",
            "creation",
            "schedule_send",
            "content_type",
            "sender_email",
        ],
        order_by="creation desc",
        limit_page_length=0,
        # Ver nota en _exigir_edicion: `Newsletter` solo tiene DocPerm de
        # "Newsletter Manager", así que sin esto un Sales User/Manager vería la
        # lista VACÍA aunque `puede_editar` sea true.
        ignore_permissions=True,
    )
    for c in campanas:
        c["grupos"] = frappe.get_all(
            "Newsletter Email Group",
            filters={"parent": c.name},
            pluck="email_group",
            ignore_permissions=True,
        )
        c["estado"] = _estado(c)
        c["aperturas"] = c.get("total_views") or 0

    return {"campanas": campanas, "puede_editar": _puede_editar()}


@frappe.whitelist()
def grupos():
    """Listas de correo disponibles (doctype `Email Group`)."""
    return frappe.get_all(
        "Email Group",
        fields=["name", "title", "total_subscribers"],
        order_by="title asc",
        limit_page_length=0,
        ignore_permissions=True,  # ver nota en listar/_exigir_edicion
    )


@frappe.whitelist()
def detalle(name):
    frappe.flags.ignore_permissions = True  # ver nota en _exigir_edicion
    doc = frappe.get_doc("Newsletter", name)
    d = doc.as_dict()
    d["grupos"] = [g.email_group for g in doc.email_group]
    d["estado"] = _estado(d)
    d["aperturas"] = d.get("total_views") or 0
    try:
        d["cola"] = doc.get_sending_status()
    except Exception:
        d["cola"] = None
    return d


@frappe.whitelist()
def crear(
    subject,
    email_group,
    contenido,
    content_type="HTML",
    sender_email=None,
    sender_name="lavendi.mx",
    send_unsubscribe_link=1,
):
    """Crea una campaña en borrador (no envía)."""
    _exigir_edicion()

    subject = (subject or "").strip()
    contenido = contenido or ""
    email_group = (email_group or "").strip()

    if not subject:
        frappe.throw(_("El asunto es obligatorio"))
    if not email_group:
        frappe.throw(_("Elige una lista de correo"))
    if not contenido.strip():
        frappe.throw(_("El contenido no puede estar vacío"))
    if content_type not in ("HTML", "Rich Text", "Markdown"):
        frappe.throw(_("Tipo de contenido no válido"))
    if not frappe.db.exists("Email Group", email_group):
        frappe.throw(_("La lista {0} no existe").format(email_group))

    doc = frappe.new_doc("Newsletter")
    doc.subject = subject
    doc.sender_email = (sender_email or "").strip() or _remitente_default()
    doc.sender_name = (sender_name or "lavendi.mx").strip()
    doc.content_type = content_type
    if content_type == "HTML":
        doc.message_html = contenido
    elif content_type == "Markdown":
        doc.message_md = contenido
    else:
        doc.message = contenido
    doc.send_unsubscribe_link = frappe.utils.cint(send_unsubscribe_link)
    doc.append("email_group", {"email_group": email_group})
    doc.insert(ignore_permissions=True)

    frappe.db.commit()
    return {"name": doc.name, "subject": doc.subject}


@frappe.whitelist()
def probar(name, email):
    """Manda una copia de prueba a un correo, sin marcar la campaña como enviada."""
    _exigir_edicion()

    email = (email or "").strip()
    if not email:
        frappe.throw(_("Indica un correo de prueba"))
    frappe.utils.validate_email_address(email, throw=True)

    doc = frappe.get_doc("Newsletter", name)
    # Lista explícita — ver nota del docstring: `send_test_email()` del core está roto.
    doc.send_newsletter(emails=[email], test_email=True)
    frappe.db.commit()
    return {"ok": True, "email": email}


@frappe.whitelist()
def enviar(name):
    """Encola la campaña a toda la lista. No se puede deshacer."""
    _exigir_edicion()

    doc = frappe.get_doc("Newsletter", name)
    if doc.email_sent:
        frappe.throw(_("Esta campaña ya se envió"))
    doc.send_emails()  # valida, encola a los pendientes y guarda
    frappe.db.commit()
    return {"ok": True, "destinatarios": doc.total_recipients, "name": doc.name}
