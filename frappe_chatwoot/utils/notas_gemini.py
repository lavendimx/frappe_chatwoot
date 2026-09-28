"""Notas de Gemini → CRM (frente "actividad y notas del contacto", 2026-09-27).

Las notas de videollamada NO viven en el evento de Calendar: llegan por correo
de `gemini-notes@google.com` y quedan como Google Doc en Drive (resumen
completo + "Próximos pasos sugeridos"). El agente-ia del host ya parsea esos
correos y los deja listos; este job los **trae por el mismo contrato
host↔contenedor** que `planeacion_llamadas` (`Chatwoot Settings.agenda_url` +
`agenda_token`) y los escribe al CRM:

  - resuelve la ficha de forma determinista por `event_id` (no por título) y,
    si no existe, por el contacto (email/nombre) → Deal/Lead más reciente;
  - publica una `FCRM Note` "Resumen — <fecha>" con el resumen;
  - deja un `Comment` corto con el link del Doc para el timeline;
  - crea una `CRM Task` por cada próximo paso sugerido;
  - si había fila de `Reunion Agendada`, guarda `notas_gemini_url/_msg_id/_at`.

Idempotencia: doctype `Chatwoot Notas Gemini` (msg_id unique). Piso de fecha
por defecto: últimos 30 días, para no ingerir el histórico de 443 correos.
"""

import json
import urllib.error
import urllib.request

import frappe

from frappe_chatwoot.utils.planeacion_llamadas import (
    _config_agenda,
    _contacto_por_email,
    _resolver_ficha,
)
from frappe_chatwoot.utils.resumen_contacto import _formatear_resumen

TIMEOUT_NOTAS = 60
DIAS_PISO = 3  # ventana por defecto del lector; configurable por argumento. Bajado de 30 a 3
# (2026-09-28) para que el primer barrido no meta ~20 notas + ~60 CRM Task de golpe: se acumula
# hacia adelante. El backfill histórico se corre a mano con `desde` si se quiere.


def _activo():
    try:
        return bool(frappe.db.get_single_value("Chatwoot Settings", "notas_gemini_activo"))
    except Exception:
        return False


def _leer_agente(desde=None):
    """POST a `{agenda_url}/notas-gemini/leer`. `desde` es un piso ISO; el
    agente-ia devuelve `{"ok": true, "correos": [...]}` con los correos ya
    parseados. Mismo secreto compartido que `planeacion_llamadas`."""
    cfg = _config_agenda()
    if not (cfg["url"] and cfg["token"]):
        raise RuntimeError("agenda no configurada (Chatwoot Settings → URL y token del agente)")
    req = urllib.request.Request(
        f"{cfg['url']}/notas-gemini/leer",
        data=json.dumps({"desde": str(desde) if desde else None}).encode(),
        headers={"Content-Type": "application/json", "x-sofia-token": cfg["token"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_NOTAS) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        cuerpo = {}
        try:
            cuerpo = json.loads(exc.read().decode())
        except (ValueError, OSError):
            pass
        raise RuntimeError(cuerpo.get("mensaje") or str(exc)) from exc
    if not data.get("ok"):
        raise RuntimeError(data.get("mensaje") or "el lector no respondió ok")
    return data.get("correos") or []


def _campo(correo, *claves):
    """Primer valor no vacío entre varias claves posibles del correo parseado."""
    for clave in claves:
        valor = correo.get(clave)
        if valor:
            return str(valor).strip()
    return ""


def _contacto_por_nombre(nombre):
    nombre = (nombre or "").strip()
    if not nombre:
        return None
    try:
        return frappe.db.get_value("Contact", {"full_name": nombre}, "name")
    except Exception:
        # `full_name` no existe en Contact de todo sitio (lo agrega crm); se cae
        # a first/last.
        partes = nombre.split(" ", 1)
        return frappe.db.get_value(
            "Contact",
            {"first_name": partes[0], "last_name": partes[1] if len(partes) > 1 else ""},
            "name",
        )


def _ficha_por_evento(event_id):
    """(ficha_doctype, ficha_name, reunion_name) o (None, None, None).
    Ancla determinista: `Reunion Agendada.event_id` es unique."""
    if not event_id:
        return (None, None, None)
    cita = frappe.db.get_value(
        "Reunion Agendada", {"event_id": event_id},
        ["name", "crm_contacto", "email_participante", "nombre_participante"],
        as_dict=True,
    )
    if not cita:
        return (None, None, None)
    ficha_doctype, ficha_name = _resolver_ficha(cita, crear=False)
    return (ficha_doctype, ficha_name, cita["name"])


def _ficha_por_contacto(email, nombre):
    """(ficha_doctype, ficha_name, contacto) — el Deal/Lead más reciente del
    Contact (por email o por nombre)."""
    contacto = _contacto_por_email(email) or _contacto_por_nombre(nombre)
    if not contacto:
        return (None, None, None)
    for doctype in ("CRM Deal", "CRM Lead"):
        fila = frappe.get_all(doctype, filters={"contact": contacto}, fields=["name"],
                               order_by="modified desc", limit=1)
        if fila:
            return (doctype, fila[0].name, contacto)
    return (None, None, contacto)


def _resolver_ficha_gemini(correo, crear=True):
    """Deal/Lead por event_id → por contacto → crear. Devuelve
    `(ficha_doctype, ficha_name, reunion_name)`; `crear=False` en dry-run (no da
    de alta un Lead de paso)."""
    event_id = _campo(correo, "event_id")
    ficha_doctype, ficha_name, reunion = _ficha_por_evento(event_id)
    if ficha_doctype:
        return (ficha_doctype, ficha_name, reunion)

    email = _campo(correo, "email", "email_participante")
    nombre = _campo(correo, "nombre", "participante", "nombre_participante")
    ficha_doctype, ficha_name, contacto = _ficha_por_contacto(email, nombre)
    if ficha_doctype:
        return (ficha_doctype, ficha_name, reunion)

    if not crear:
        return (None, None, reunion)
    try:
        from frappe_chatwoot.utils.captacion import _crear_ficha
        ficha = _crear_ficha(contacto, nombre=nombre, email=email, source="Notas de Gemini")
        return (ficha[0], ficha[1], reunion)
    except Exception as exc:
        frappe.log_error(f"notas_gemini: no se pudo crear ficha para {email}: {exc}",
                          "Notas de Gemini")
        return (None, None, reunion)


def _owner_ficha(ficha_doctype, ficha_name):
    if ficha_doctype == "CRM Deal":
        return frappe.db.get_value("CRM Deal", ficha_name, "deal_owner")
    if ficha_doctype == "CRM Lead":
        return frappe.db.get_value("CRM Lead", ficha_name, "lead_owner")
    return None


def _parsear_pasos(valor):
    """Normaliza los próximos pasos: acepta lista (tal cual) o texto con viñetas
    /renglones y devuelve una lista limpia de strings."""
    if not valor:
        return []
    if isinstance(valor, (list, tuple)):
        out = []
        for p in valor:
            if isinstance(p, dict):
                titulo = str(p.get("accion") or p.get("title") or p.get("titulo") or "").strip()
                desc = str(p.get("descripcion") or p.get("description") or "").strip()
                resp = str(p.get("responsable") or p.get("assigned_to") or "").strip()
                if titulo or desc:
                    out.append({"titulo": titulo or desc, "descripcion": desc, "responsable": resp})
            else:
                s = str(p).strip()
                if s:
                    out.append({"titulo": s, "descripcion": "", "responsable": ""})
        return out
    pasos = []
    for linea in str(valor).split("\n"):
        limpia = linea.strip().lstrip("-•*").strip()
        if limpia:
            pasos.append({"titulo": limpia, "descripcion": "", "responsable": ""})
    return pasos


def _fecha_corta(fecha):
    if not fecha:
        return frappe.utils.formatdate(frappe.utils.now_datetime(), "d MMM")
    try:
        return frappe.utils.formatdate(frappe.utils.get_datetime(fecha), "d MMM")
    except Exception:
        return str(fecha)


def _fecha_dt(fecha):
    """El header `Date` del correo es RFC 2822 ('Mon, 28 Sep 2026 17:34:14 +0000');
    el campo `fecha` de `Chatwoot Notas Gemini` es Datetime, así que MySQL lo rechaza
    crudo (1292). Devuelve None si no parsea — el campo es opcional."""
    if not fecha:
        return None
    try:
        dt = frappe.utils.get_datetime(fecha)
        # El header trae offset ('+0000') → `get_datetime` devuelve un datetime
        # tz-aware y MySQL rechaza '2026-09-28 17:34:14+00:00' (1292). El campo
        # es naive: se descarta el tzinfo.
        if dt is not None and getattr(dt, "tzinfo", None) is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _titulo(fecha):
    return f"Resumen — {_fecha_corta(fecha)}"


def _publicar_comentario(ficha_doctype, ficha_name, doc_url):
    """`Comment` corto con el link del Doc — aparece en el timeline de
    actividad. Mismo patrón que `api/stripe_pagos.py` y `api/facturacion.py`."""
    frappe.get_doc({
        "doctype": "Comment",
        "comment_type": "Comment",
        "reference_doctype": ficha_doctype,
        "reference_name": ficha_name,
        "content": ('Notas de la videollamada (Gemini): '
                    f'<a href="{frappe.utils.escape_html(doc_url)}">abrir documento</a>'),
    }).insert(ignore_permissions=True)


def _crear_tarea(paso, ficha_doctype, ficha_name, assigned_to, doc_url):
    if isinstance(paso, dict):
        titulo = str(paso.get("titulo") or "").strip()
        desc = str(paso.get("descripcion") or "").strip()
        resp = str(paso.get("responsable") or "").strip()
    else:
        titulo, desc, resp = str(paso).strip(), "", ""
    detalle = " ".join(x for x in (
        f"Responsable: {resp}." if resp else "",
        desc,
        f"Documento: {doc_url}" if doc_url else "",
    ) if x) or "Próximo paso sugerido por Gemini."
    tarea = {
        "doctype": "CRM Task",
        "title": (titulo or "Próximo paso de la videollamada")[:140],
        "description": detalle,
        "status": "Todo",
        "priority": "Medium",
        "reference_doctype": ficha_doctype,
        "reference_docname": ficha_name,
    }
    if assigned_to:
        tarea["assigned_to"] = assigned_to
    frappe.get_doc(tarea).insert(ignore_permissions=True)


def generar_notas_gemini(dry_run=0, desde=None):
    """Job programado. `desde` es el piso de fecha del lector (por defecto:
    últimos 30 días). `dry_run=1` no escribe nada y devuelve previsualizaciones."""
    dry_run = bool(frappe.utils.cint(dry_run))
    if not dry_run and not _activo():
        return {"activo": False}

    if not desde:
        desde = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-DIAS_PISO)
    correos = _leer_agente(desde=desde)

    nuevos, saltadas, previsualizadas = [], [], []
    for correo in correos or []:
        msg_id = _campo(correo, "msg_id", "message_id")
        if not msg_id:
            saltadas.append(("(sin msg_id)", "correo sin msg_id"))
            continue
        try:
            if frappe.db.exists("Chatwoot Notas Gemini", {"msg_id": msg_id}):
                saltadas.append((msg_id, "ya procesado"))
                continue

            ficha_doctype, ficha_name, reunion = _resolver_ficha_gemini(correo, crear=not dry_run)
            resumen_texto = _campo(correo, "resumen_texto", "resumen")
            doc_url = _campo(correo, "doc_url")
            fecha = _campo(correo, "fecha")
            pasos = _parsear_pasos(correo.get("proximos_pasos")
                                    or correo.get("proximos_pasos_sugeridos"))

            if dry_run:
                previsualizadas.append({
                    "msg_id": msg_id,
                    "ficha": f"{ficha_doctype} {ficha_name}" if ficha_doctype else "(sin ficha resoluble)",
                    "reunion": reunion,
                    "titulo": _titulo(fecha),
                    "pasos": pasos,
                })
                continue

            if not ficha_doctype:
                saltadas.append((msg_id, "sin ficha resoluble"))
                continue

            ahora = frappe.utils.now_datetime()
            html = _formatear_resumen(resumen_texto)

            frappe.get_doc({
                "doctype": "FCRM Note",
                "title": _titulo(fecha),
                "content": html,
                "reference_doctype": ficha_doctype,
                "reference_docname": ficha_name,
            }).insert(ignore_permissions=True)

            if doc_url:
                _publicar_comentario(ficha_doctype, ficha_name, doc_url)

            assigned = _owner_ficha(ficha_doctype, ficha_name)
            for paso in pasos:
                _crear_tarea(paso, ficha_doctype, ficha_name, assigned, doc_url)

            if reunion:
                frappe.db.set_value(
                    "Reunion Agendada", reunion,
                    {"notas_gemini_url": doc_url, "notas_gemini_msg_id": msg_id,
                     "notas_gemini_at": ahora},
                    update_modified=False,
                )

            frappe.get_doc({
                "doctype": "Chatwoot Notas Gemini",
                "msg_id": msg_id,
                "event_id": _campo(correo, "event_id"),
                "ficha_doctype": ficha_doctype,
                "ficha_name": ficha_name,
                "doc_url": doc_url,
                "fecha": _fecha_dt(fecha),
            }).insert(ignore_permissions=True)

            frappe.db.commit()
            nuevos.append(msg_id)
        except Exception as exc:
            # Atomicidad por correo: sin este rollback, un fallo DESPUÉS de crear
            # la nota/comentario/tareas (p.ej. al insertar la marca de dedup) dejaría
            # escrituras parciales que el siguiente barrido duplicaría, porque la
            # fila de `Chatwoot Notas Gemini` (la marca) no quedaría.
            try:
                frappe.db.rollback()
            except Exception:
                pass
            try:
                # `frappe.log_error` reventó con CharacterLengthExceededError por un
                # mensaje largo dentro de este mismo except (mata el job entero).
                frappe.log_error(f"notas_gemini {msg_id}: {str(exc)[:300]}", "Notas de Gemini")
            except Exception:
                pass
            saltadas.append((msg_id, str(exc)[:200]))

    if dry_run:
        return {"activo": True, "dry_run": True, "previsualizadas": previsualizadas, "saltadas": saltadas}
    return {"activo": True, "nuevos": nuevos, "saltadas": saltadas}
