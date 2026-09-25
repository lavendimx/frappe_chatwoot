# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Campaña de email como serie de N pasos con cadencia — orquestación.

Se copia a
`apps/frappe_chatwoot/frappe_chatwoot/frappe_chatwoot/api/campana_email.py`.

Plan: `planes/campanas-contenedor-y-cadencia.md`.

QUÉ ES Y QUÉ NO ES
    `Campana Email` es el contenedor (título, lista, remitente, interruptor de
    programación). Cada `Campana Email Paso` declara un correo de la serie —
    con o sin contenido todavía. El contenido, cuando existe, sigue siendo un
    `Newsletter` normal: personalización por destinatario, tracking de
    apertura y unsubscribe ya están resueltos ahí (2026-09-16) y no se
    duplican aquí.

    Esto NO reemplaza `api/campanas.py` (la lista/alta de Newsletters sueltos
    sigue viva para un envío de una sola vez) ni `utils/secuencias.py` (el
    seguimiento 1:1 a una oportunidad). Son tres cosas distintas a propósito
    — decisión de Alejandro, 2026-09-18: Campañas y Secuencias NO se unifican.

ESTADO DE UN PASO SE CALCULA, NO SE GUARDA
    "Enviado" ya lo sabe `Newsletter.email_sent`; "sin redactar" es
    `paso.newsletter` vacío. Guardar un tercer campo de estado sería un
    segundo lugar donde la verdad puede desincronizarse — el mismo riesgo que
    ya se documentó con `CRM Deal.secuencia_actual` (2026-09-16).

FECHA ESTIMADA
    Si el paso trae `programado_para`, esa es la fecha. Si no, se acumula
    `espera_dias` desde la fecha de activación de la campaña (o desde el envío
    real del paso anterior, si ya salió — para que la cadencia no se acumule
    en falso cuando un paso se manda tarde a mano).
"""

import frappe
from frappe_chatwoot.utils import plan
from frappe import _

from .campanas import _exigir_edicion, _puede_editar, _remitente_default, _estado as _estado_newsletter

_ESTADO_PASO = ("Sin redactar", "Programado", "Enviado")


def _estado_paso(paso_row, newsletter_doc=None):
    if not paso_row.get("newsletter"):
        return "Sin redactar"
    if newsletter_doc is None:
        enviado = frappe.db.get_value("Newsletter", paso_row["newsletter"], "email_sent")
    else:
        enviado = newsletter_doc.get("email_sent")
    return "Enviado" if enviado else "Programado"


def _fecha_estimada(paso_row, fecha_base):
    """`fecha_base` es la fecha desde la que se cuenta: la activación de la
    campaña para el paso 1, o el envío real (o estimado) del paso anterior."""
    if paso_row.get("programado_para"):
        return frappe.utils.getdate(paso_row["programado_para"])
    return frappe.utils.add_days(fecha_base, frappe.utils.cint(paso_row.get("espera_dias") or 0))


def _enriquecer_pasos(doc):
    """Anota cada fila de `doc.pasos` con estado, fecha estimada y aperturas.
    Muta y regresa la lista de dicts lista para el frontend."""
    nombres = [p.newsletter for p in doc.pasos if p.newsletter]
    newsletters = {}
    if nombres:
        for n in frappe.get_all(
            "Newsletter", filters={"name": ["in", nombres]},
            fields=["name", "subject", "email_sent", "email_sent_at", "total_recipients", "total_views"],
            ignore_permissions=True,
        ):
            newsletters[n.name] = n

    fecha_base = frappe.utils.getdate(doc.creation)
    out = []
    for p in sorted(doc.pasos, key=lambda r: r.idx):
        fila = p.as_dict()
        nl = newsletters.get(p.newsletter)
        fila["estado"] = _estado_paso(fila, nl)
        if nl and nl.get("email_sent_at"):
            fecha_base = frappe.utils.getdate(nl["email_sent_at"])
            fila["fecha_estimada"] = fecha_base
        else:
            fila["fecha_estimada"] = _fecha_estimada(fila, fecha_base)
            if not fila.get("programado_para"):
                fecha_base = fila["fecha_estimada"]
        fila["subject"] = nl.get("subject") if nl else None
        fila["email_sent_at"] = nl.get("email_sent_at") if nl else None
        fila["total_recipients"] = nl.get("total_recipients") if nl else None
        fila["aperturas"] = (nl.get("total_views") or 0) if nl else 0
        out.append(fila)
    return out


@frappe.whitelist()
def listar_campanas():
    plan.exigir_no_lite()
    """Campañas con el resumen que hoy falta en pantalla: cuántos pasos,
    cuántos redactados/enviados, y el próximo pendiente."""
    campanas = frappe.get_all(
        "Campana Email",
        fields=["name", "titulo", "email_group", "activa", "creation"],
        order_by="creation desc",
        limit_page_length=0,
        ignore_permissions=True,
    )
    for c in campanas:
        doc = frappe.get_doc("Campana Email", c.name)
        pasos = _enriquecer_pasos(doc)
        c["total_pasos"] = len(pasos)
        c["pasos_redactados"] = sum(1 for p in pasos if p["estado"] != "Sin redactar")
        c["pasos_enviados"] = sum(1 for p in pasos if p["estado"] == "Enviado")
        pendiente = next((p for p in pasos if p["estado"] != "Enviado"), None)
        c["proximo_paso"] = pendiente["titulo_paso"] if pendiente else None
        c["proxima_fecha"] = pendiente["fecha_estimada"] if pendiente else None
        c["destinatarios"] = frappe.db.get_value("Email Group", c.email_group, "total_subscribers") or 0

    return {"campanas": campanas, "puede_editar": _puede_editar()}


@frappe.whitelist()
def detalle_campana(name):
    plan.exigir_no_lite()
    """La campaña completa con su línea de tiempo — lo que pediste ver:
    entrar a la campaña y ver los N correos y la cadencia entre cada uno."""
    doc = frappe.get_doc("Campana Email", name)
    d = doc.as_dict()
    d["pasos"] = _enriquecer_pasos(doc)
    d["destinatarios"] = frappe.db.get_value("Email Group", doc.email_group, "total_subscribers") or 0
    d["puede_editar"] = _puede_editar()
    return d


@frappe.whitelist()
def crear_campana(titulo, email_group, remitente_email=None, remitente_nombre=None, pasos=None):
    plan.exigir_no_lite()
    """Crea la campaña con todos sus pasos declarados de una vez (título +
    cadencia). Los pasos nacen sin `newsletter` — se redactan después."""
    _exigir_edicion()

    titulo = (titulo or "").strip()
    email_group = (email_group or "").strip()
    if not titulo:
        frappe.throw(_("El título de la campaña es obligatorio"))
    if not frappe.db.exists("Email Group", email_group):
        frappe.throw(_("La lista {0} no existe").format(email_group))

    if isinstance(pasos, str):
        pasos = frappe.parse_json(pasos)
    pasos = pasos or []

    doc = frappe.new_doc("Campana Email")
    doc.titulo = titulo
    doc.email_group = email_group
    doc.remitente_email = (remitente_email or "").strip()
    doc.remitente_nombre = (remitente_nombre or "lavendi.mx").strip()
    doc.activa = 0
    for p in pasos:
        titulo_paso = (p.get("titulo_paso") or "").strip()
        if not titulo_paso:
            continue
        doc.append("pasos", {
            "titulo_paso": titulo_paso,
            "espera_dias": frappe.utils.cint(p.get("espera_dias") or 0),
            "programado_para": p.get("programado_para") or None,
        })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"name": doc.name}


@frappe.whitelist()
def agregar_paso(campana, titulo_paso, espera_dias=0, programado_para=None):
    plan.exigir_no_lite()
    _exigir_edicion()
    doc = frappe.get_doc("Campana Email", campana)
    titulo_paso = (titulo_paso or "").strip()
    if not titulo_paso:
        frappe.throw(_("El título del paso es obligatorio"))
    doc.append("pasos", {
        "titulo_paso": titulo_paso,
        "espera_dias": frappe.utils.cint(espera_dias or 0),
        "programado_para": programado_para or None,
    })
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "paso": doc.pasos[-1].name}


@frappe.whitelist()
def editar_paso(campana, nombre_paso, titulo_paso=None, espera_dias=None, programado_para=None):
    plan.exigir_no_lite()
    _exigir_edicion()
    doc = frappe.get_doc("Campana Email", campana)
    fila = next((p for p in doc.pasos if p.name == nombre_paso), None)
    if not fila:
        frappe.throw(_("Ese paso no existe en esta campaña"))
    if fila.newsletter and frappe.db.get_value("Newsletter", fila.newsletter, "email_sent"):
        frappe.throw(_("Este paso ya se envió, no se puede editar"))
    if titulo_paso is not None:
        fila.titulo_paso = titulo_paso.strip()
    if espera_dias is not None:
        fila.espera_dias = frappe.utils.cint(espera_dias)
    if programado_para is not None:
        fila.programado_para = programado_para or None
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True}


@frappe.whitelist()
def eliminar_paso(campana, nombre_paso):
    plan.exigir_no_lite()
    _exigir_edicion()
    doc = frappe.get_doc("Campana Email", campana)
    fila = next((p for p in doc.pasos if p.name == nombre_paso), None)
    if not fila:
        frappe.throw(_("Ese paso no existe en esta campaña"))
    if fila.newsletter and frappe.db.get_value("Newsletter", fila.newsletter, "email_sent"):
        frappe.throw(_("Este paso ya se envió, no se puede eliminar"))
    doc.pasos.remove(fila)
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True}


@frappe.whitelist()
def reordenar_pasos(campana, orden):
    plan.exigir_no_lite()
    """`orden` = lista de `name` de filas de `pasos` en el orden deseado."""
    _exigir_edicion()
    if isinstance(orden, str):
        orden = frappe.parse_json(orden)
    doc = frappe.get_doc("Campana Email", campana)
    indice = {n: i for i, n in enumerate(orden)}
    faltantes = [p.name for p in doc.pasos if p.name not in indice]
    if faltantes:
        frappe.throw(_("El nuevo orden no incluye todos los pasos"))
    doc.pasos.sort(key=lambda p: indice[p.name])
    for i, p in enumerate(doc.pasos, start=1):
        p.idx = i
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True}


@frappe.whitelist()
def redactar_paso(campana, nombre_paso, subject, contenido, content_type="HTML"):
    plan.exigir_no_lite()
    """Crea el `Newsletter` del paso y lo liga — la vía normal para pasar un
    paso de 'Sin redactar' a 'Programado'. Reusa la misma validación y
    remitente de `api/campanas.crear`, apuntado a la lista de la campaña."""
    _exigir_edicion()
    doc = frappe.get_doc("Campana Email", campana)
    fila = next((p for p in doc.pasos if p.name == nombre_paso), None)
    if not fila:
        frappe.throw(_("Ese paso no existe en esta campaña"))
    if fila.newsletter:
        frappe.throw(_("Este paso ya tiene un correo ligado — edítalo desde ahí"))

    subject = (subject or "").strip()
    contenido = contenido or ""
    if not subject:
        frappe.throw(_("El asunto es obligatorio"))
    if not contenido.strip():
        frappe.throw(_("El contenido no puede estar vacío"))
    if content_type not in ("HTML", "Rich Text", "Markdown"):
        frappe.throw(_("Tipo de contenido no válido"))

    nl = frappe.new_doc("Newsletter")
    nl.subject = subject
    nl.sender_email = doc.remitente_email or _remitente_default()
    nl.sender_name = doc.remitente_nombre or "lavendi.mx"
    nl.content_type = content_type
    if content_type == "HTML":
        nl.message_html = contenido
    elif content_type == "Markdown":
        nl.message_md = contenido
    else:
        nl.message = contenido
    nl.send_unsubscribe_link = 1
    nl.append("email_group", {"email_group": doc.email_group})
    nl.insert(ignore_permissions=True)

    fila.newsletter = nl.name
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "newsletter": nl.name}


@frappe.whitelist()
def ligar_newsletter(campana, nombre_paso, newsletter):
    plan.exigir_no_lite()
    """Liga un `Newsletter` YA EXISTENTE a un paso — la vía de migración
    (ej. el email 1 de COPARMEX, ya redactado y validado antes de que
    existiera este contenedor)."""
    _exigir_edicion()
    if not frappe.db.exists("Newsletter", newsletter):
        frappe.throw(_("Ese correo no existe"))
    doc = frappe.get_doc("Campana Email", campana)
    fila = next((p for p in doc.pasos if p.name == nombre_paso), None)
    if not fila:
        frappe.throw(_("Ese paso no existe en esta campaña"))
    fila.newsletter = newsletter
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True}


@frappe.whitelist()
def activar_campana(campana, activa):
    plan.exigir_no_lite()
    _exigir_edicion()
    frappe.db.set_value("Campana Email", campana, "activa", frappe.utils.cint(activa))
    frappe.db.commit()
    return {"ok": True}


@frappe.whitelist()
def enviar_paso_ahora(campana, nombre_paso):
    plan.exigir_no_lite()
    """Disparo manual — el mismo botón que existe hoy para un Newsletter
    suelto, pero validado contra el paso de la campaña."""
    _exigir_edicion()
    doc = frappe.get_doc("Campana Email", campana)
    fila = next((p for p in doc.pasos if p.name == nombre_paso), None)
    if not fila:
        frappe.throw(_("Ese paso no existe en esta campaña"))
    if not fila.newsletter:
        frappe.throw(_("Este paso todavía no tiene contenido redactado"))

    nl = frappe.get_doc("Newsletter", fila.newsletter)
    if nl.email_sent:
        frappe.throw(_("Este paso ya se envió"))
    # `send_emails()` → `queue_all()` → `self.save()` → `check_permission("write")`, y
    # `Document.has_permission()` del core mira `self.flags`, NO el global
    # `frappe.flags.ignore_permissions` que pone `_exigir_edicion()`. Sin esta línea, un
    # Sales Manager con permiso de sobra (Valente, Zaira) recibe 403 y solo Administrator
    # puede disparar un paso — pasó de verdad con el email 1 de COPARMEX (2026-09-18).
    # La frontera de seguridad es `_exigir_edicion()`, que ya corrió arriba.
    nl.flags.ignore_permissions = True
    nl.send_emails()
    frappe.db.commit()
    return {"ok": True, "destinatarios": nl.total_recipients}
