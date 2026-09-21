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
            "serie",
            "serie_paso",
            "serie_total_pasos",
            "serie_intervalo_dias",
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
def serie_detalle(serie):
    """Los pasos de una serie, ordenados, para pintar la línea de tiempo.

    Solo lectura de lo ya declarado al crear cada paso (ver `crear`) — no
    calcula fechas ni infiere nada: si un paso todavía no se redacta, no
    aparece aquí (v1 es visibilidad, no un generador de borradores).
    """
    serie = (serie or "").strip()
    if not serie:
        return []
    pasos = frappe.get_all(
        "Newsletter",
        filters={"serie": serie},
        fields=[
            "name",
            "subject",
            "email_sent",
            "email_sent_at",
            "schedule_send",
            "total_recipients",
            "total_views",
            "serie_paso",
            "serie_total_pasos",
            "serie_intervalo_dias",
        ],
        order_by="serie_paso asc, creation asc",
        limit_page_length=0,
        ignore_permissions=True,
    )
    for p in pasos:
        p["estado"] = _estado(p)
    return pasos


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
    serie=None,
    serie_paso=None,
    serie_total_pasos=None,
    serie_intervalo_dias=None,
):
    """Crea una campaña en borrador (no envía).

    `serie_*` es declarativo (v1, solo visibilidad — ver
    `agregar_campos_serie_newsletter.py`): quien redacta el paso dice a qué
    serie pertenece, qué paso es y cada cuántos días va respecto al anterior.
    Nada se infiere ni se dispara solo.
    """
    _exigir_edicion()

    subject = (subject or "").strip()
    contenido = contenido or ""
    email_group = (email_group or "").strip()
    serie = (serie or "").strip()

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
    if serie:
        doc.serie = serie
        doc.serie_paso = frappe.utils.cint(serie_paso) or 1
        doc.serie_total_pasos = frappe.utils.cint(serie_total_pasos) or 1
        doc.serie_intervalo_dias = frappe.utils.cint(serie_intervalo_dias) or 0
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
    # Mismo motivo que en `campana_email.enviar_paso_ahora`: `send_emails()` termina en
    # `self.save()`, y `has_permission()` del core mira `self.flags`, no el global que
    # puso `_exigir_edicion()`. Sin esto, solo Administrator puede enviar.
    doc.flags.ignore_permissions = True
    doc.send_emails()  # valida, encola a los pendientes y guarda
    frappe.db.commit()
    return {"ok": True, "destinatarios": doc.total_recipients, "name": doc.name}


# --------------------------------------------------------------------------
# Suscriptores — sin esto, poblar una lista solo se puede desde el Desk.
# Mismo gate que crear/probar/enviar: administrar una lista es tan sensible
# como mandar la campaña (agregar en masa puede terminar mandándole correo a
# quien no debía estar ahí).
# --------------------------------------------------------------------------


@frappe.whitelist()
def crear_grupo(titulo):
    """Da de alta una lista (`Email Group`) nueva, vacía."""
    _exigir_edicion()

    titulo = (titulo or "").strip()
    if not titulo:
        frappe.throw(_("El nombre de la lista es obligatorio"))
    if frappe.db.exists("Email Group", titulo):
        frappe.throw(_("Ya existe una lista con ese nombre"))

    doc = frappe.new_doc("Email Group")
    doc.title = titulo
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"name": doc.name, "title": doc.title}


@frappe.whitelist()
def eliminar_grupo(email_group):
    """Borra la lista y a todos sus suscriptores.

    No basta `ignore_permissions=True` en el borrado del grupo: su propio
    `on_trash` (core) borra cada `Email Group Member` con una llamada A PARTE
    a `frappe.delete_doc`, **sin** heredar la bandera — y `Email Group Member`
    tiene el mismo DocPerm único de "Newsletter Manager" que el resto del
    módulo. Sin vaciar la lista primero, un Sales/System Manager real (no
    Administrator) truena aquí aunque `_exigir_edicion()` ya lo autorizó.
    `frappe.client.delete` genérico tampoco sirve, por la misma razón.
    """
    _exigir_edicion()

    if not frappe.db.exists("Email Group", email_group):
        frappe.throw(_("La lista {0} no existe").format(email_group))

    for miembro in frappe.get_all("Email Group Member", filters={"email_group": email_group}, pluck="name"):
        frappe.delete_doc("Email Group Member", miembro, ignore_permissions=True)

    frappe.delete_doc("Email Group", email_group, ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True}


@frappe.whitelist()
def listar_suscriptores(email_group):
    """Suscriptores de una lista, con su estado de baja."""
    _exigir_edicion()

    if not frappe.db.exists("Email Group", email_group):
        frappe.throw(_("La lista {0} no existe").format(email_group))

    return frappe.get_all(
        "Email Group Member",
        filters={"email_group": email_group},
        fields=["name", "email", "unsubscribed"],
        order_by="email asc",
        limit_page_length=0,
        ignore_permissions=True,
    )


def _agregar_uno(email_group, correo):
    """Intenta agregar un correo suelto. Regresa en qué balde cayó, sin tocar
    la BD si ya existe (dado de baja o no) — nunca revive a quien se dio de
    baja, igual que el `add_subscribers` del core."""
    correo = (correo or "").strip()
    if not correo:
        return None
    valido = frappe.utils.validate_email_address(correo, throw=False)
    if not valido:
        return ("invalidos", correo)
    if frappe.db.exists("Email Group Member", {"email_group": email_group, "email": valido}):
        return ("ya_existian", valido)
    frappe.get_doc(
        {"doctype": "Email Group Member", "email_group": email_group, "email": valido}
    ).insert(ignore_permissions=True)
    return ("agregados", valido)


@frappe.whitelist()
def agregar_suscriptores(email_group, correos):
    """Agrega correos sueltos a una lista (pegados a mano o desde un archivo
    en el cliente — el parseo de CSV/texto ya lo hizo el navegador)."""
    _exigir_edicion()

    if not frappe.db.exists("Email Group", email_group):
        frappe.throw(_("La lista {0} no existe").format(email_group))

    if isinstance(correos, str):
        try:
            correos = frappe.parse_json(correos)
        except Exception:
            correos = correos.replace(",", "\n").split("\n")

    r = {"agregados": [], "ya_existian": [], "invalidos": []}
    for correo in correos or []:
        resultado = _agregar_uno(email_group, correo)
        if resultado:
            r[resultado[0]].append(resultado[1])

    frappe.get_doc("Email Group", email_group).update_total_subscribers()
    frappe.db.commit()
    return r


@frappe.whitelist()
def agregar_contactos(email_group, contactos):
    """Agrega a la lista el correo primario de cada `Contact` (selección
    humana explícita desde la pantalla de Contactos — nunca automática). Quien
    no tenga correo se reporta aparte, no se descarta en silencio."""
    _exigir_edicion()

    if not frappe.db.exists("Email Group", email_group):
        frappe.throw(_("La lista {0} no existe").format(email_group))

    if isinstance(contactos, str):
        contactos = frappe.parse_json(contactos)

    r = {"agregados": [], "ya_existian": [], "invalidos": [], "sin_correo": []}
    for contacto in contactos or []:
        email = frappe.db.get_value("Contact", contacto, "email_id")
        if not email:
            r["sin_correo"].append(contacto)
            continue
        resultado = _agregar_uno(email_group, email)
        if resultado:
            r[resultado[0]].append(resultado[1])

    frappe.get_doc("Email Group", email_group).update_total_subscribers()
    frappe.db.commit()
    return r


@frappe.whitelist()
def quitar_suscriptor(email_group, email):
    """Quita a alguien de la lista (borrado real, no marca de baja — para eso
    ya existe el link de unsubscribe del propio correo)."""
    _exigir_edicion()

    nombre = frappe.db.get_value("Email Group Member", {"email_group": email_group, "email": email})
    if not nombre:
        frappe.throw(_("Ese correo no está en la lista"))

    frappe.delete_doc("Email Group Member", nombre, ignore_permissions=True)
    frappe.get_doc("Email Group", email_group).update_total_subscribers()
    frappe.db.commit()
    return {"ok": True}
