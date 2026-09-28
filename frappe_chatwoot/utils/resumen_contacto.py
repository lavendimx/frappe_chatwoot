"""Quick summary IA por contacto — Fase 1 del frente "actividad y notas del
contacto" (propuesta registrada el 2026-09-27, sid 3ce9b38c).

Job **hermano** de `planeacion_llamadas.py`: mismo disparador (barrido de
`Reunion Agendada`), mismo destino (una `FCRM Note` en Deal→Lead) y mismo
contrato host↔contenedor (`Chatwoot Settings.agenda_url/agenda_token`), pero
una sola nota de resumen en vez de la hoja de planeación 0-6. Se reutilizan
las piezas comunes de `planeacion_llamadas` en lugar de reimplementarlas.

Disparo: `Reunion Agendada.end_datetime < now - 30min` sin
`resumen_generado_at` — la nota se quiere cuando la llamada ya ocurrió, no
antes. `dry_run=1` (solo validación manual vía `bench execute`) no escribe ni
marca ni crea Lead.

Ver `## Pendientes` de soporte: (c) quick summary IA por contacto.
"""

import json
import urllib.error
import urllib.request

import frappe

from . import chatwoot_client
from frappe_chatwoot.utils.planeacion_llamadas import (
    _config_agenda,
    _contacto_por_email,
    _resolver_ficha,
    _transcripcion,
)

TIMEOUT_RESUMEN = 60  # el LLM tarda más que un CRUD normal; 30s (agenda) se queda corto.

# Minutos que deben pasar del fin de la cita antes de intentar el resumen — el
# propio `end_datetime` ya quedó en el pasado, pero se deja un colchón para que
# la grabación/nota de Gemini esté disponible del lado del agente.
MINUTOS_ESPERA = 30


def _activo():
    try:
        return bool(frappe.db.get_single_value("Chatwoot Settings", "resumen_contacto_activo"))
    except Exception:
        return False


def _pedir_resumen(payload):
    """POST a `{agenda_url}/resumen/generar` con el mismo secreto compartido
    host↔contenedor que `planeacion_llamadas._pedir_planeacion`. Devuelve el
    texto del resumen; el agente-ia arma el contexto del LLM."""
    cfg = _config_agenda()
    if not (cfg["url"] and cfg["token"]):
        raise RuntimeError("agenda no configurada (Chatwoot Settings → URL y token del agente)")
    req = urllib.request.Request(
        f"{cfg['url']}/resumen/generar",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-sofia-token": cfg["token"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_RESUMEN) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        cuerpo = {}
        try:
            cuerpo = json.loads(exc.read().decode())
        except (ValueError, OSError):
            pass
        raise RuntimeError(cuerpo.get("mensaje") or str(exc)) from exc
    if not data.get("ok"):
        raise RuntimeError(data.get("mensaje") or "el generador no respondió ok")
    return data.get("resumen") or ""


def _resumenes_previos(ficha_doctype, ficha_name):
    """Últimos 2 resúmenes ya escritos sobre esta misma ficha — para que el LLM
    ancle al histórico. Clone estructural de
    `planeacion_llamadas._planeaciones_previas`, con el título propio."""
    if not (ficha_doctype and ficha_name):
        return []
    filas = frappe.get_all(
        "FCRM Note",
        filters={
            "reference_doctype": ficha_doctype,
            "reference_docname": ficha_name,
            "title": ["like", "Resumen — %"],
        },
        fields=["content", "creation"],
        order_by="creation desc",
        limit_page_length=2,
    )
    return [frappe.utils.strip_html(f.content)[:600] for f in filas]


def _armar_contexto(cita, ficha_doctype, ficha_doc):
    """Paquete de datos para el agente-ia. Sin plantilla: el resumen no tiene
    tipos (Venta/Cliente) como la planeación."""
    dominio = ""
    email = (cita.get("email_participante") or "").strip()
    if "@" in email:
        dominio = email.split("@", 1)[1]

    return {
        "cita": {
            "nombre_participante": cita.get("nombre_participante"),
            "email_participante": email,
            "motivo": cita.get("motivo"),
            "start_datetime": str(cita.get("start_datetime") or ""),
            "end_datetime": str(cita.get("end_datetime") or ""),
            "origen": cita.get("origen"),
            "dominio_correo": dominio,
        },
        "ficha": {
            "doctype": ficha_doctype,
            "name": ficha_doc.get("name") if ficha_doc else None,
            **({k: v for k, v in (ficha_doc or {}).items()
                if k in ("organization", "status", "deal_value", "lead_name", "company_name")}),
        },
        "transcripcion_chatwoot": _transcripcion(cita.get("chatwoot_conversation_id")),
        "resumenes_previos": _resumenes_previos(ficha_doctype, (ficha_doc or {}).get("name")),
    }


def _titulo_resumen(cita):
    cuando = frappe.utils.get_datetime(cita["end_datetime"])
    return f"Resumen — {frappe.utils.formatdate(cuando, 'd MMM')}"


def _formatear_resumen(texto):
    """Convierte el texto plano del LLM en HTML para `FCRM Note`: viñetas `- `
    agrupadas en `<ul><li>` y el resto en `<p>`. Todo escapado (el contenido
    viene del LLM y no debe inyectar HTML)."""
    partes = []
    buffer = []
    for linea in str(texto or "").split("\n"):
        limpia = linea.strip()
        if not limpia:
            continue
        if limpia.startswith("- "):
            buffer.append(limpia[2:].strip())
            continue
        if buffer:
            partes.append("<ul>" + "".join(
                f"<li>{frappe.utils.escape_html(b)}</li>" for b in buffer) + "</ul>")
            buffer = []
        partes.append(f"<p>{frappe.utils.escape_html(limpia)}</p>")
    if buffer:
        partes.append("<ul>" + "".join(
            f"<li>{frappe.utils.escape_html(b)}</li>" for b in buffer) + "</ul>")
    return "".join(partes)


def _publicar_nota(ficha_doctype, ficha_name, titulo, html):
    """Clone literal de `planeacion_llamadas._publicar_nota`: va a `FCRM Note`
    (la pestaña Notas del panel), no a `Comment`."""
    frappe.get_doc({
        "doctype": "FCRM Note",
        "title": titulo,
        "content": html,
        "reference_doctype": ficha_doctype,
        "reference_docname": ficha_name,
    }).insert(ignore_permissions=True)


def generar_resumenes(dry_run=0):
    """Scheduler en horario hábil. Barre citas terminadas (fin hace ≥ 30 min)
    sin `resumen_generado_at`.

    `dry_run=1`: ignora el interruptor, genera y devuelve el texto de cada cita,
    pero NO publica la nota, NO marca `resumen_generado_at` y NO crea Lead
    (`crear=not dry_run`) — nada se escribe."""
    dry_run = bool(frappe.utils.cint(dry_run))
    if not dry_run and not _activo():
        return {"activo": False}

    ahora = frappe.utils.now_datetime()
    limite = frappe.utils.add_to_date(ahora, minutes=-MINUTOS_ESPERA)
    citas = frappe.get_all(
        "Reunion Agendada",
        filters={
            "end_datetime": ["<", limite],
            "resumen_generado_at": ["is", "not set"],
        },
        fields=["name", "nombre_participante", "email_participante", "motivo",
                "start_datetime", "end_datetime", "origen", "crm_contacto",
                "chatwoot_conversation_id"],
    )

    generados, saltadas, previsualizadas = [], [], []
    for cita in citas:
        try:
            ficha_doctype, ficha_name = _resolver_ficha(cita, crear=not dry_run)
            ficha_doc = frappe.get_doc(ficha_doctype, ficha_name).as_dict() if ficha_doctype else None

            contexto = _armar_contexto(cita, ficha_doctype, ficha_doc)
            resumen = _pedir_resumen(contexto)
            texto = _formatear_resumen(resumen)

            if dry_run:
                previsualizadas.append({
                    "cita": cita.name,
                    "ficha": f"{ficha_doctype} {ficha_name}" if ficha_doctype else "(sin ficha resoluble)",
                    "titulo": _titulo_resumen(cita),
                    "texto": texto,
                })
                continue

            if ficha_doctype:
                _publicar_nota(ficha_doctype, ficha_name, _titulo_resumen(cita), texto)
            else:
                # Sin Deal/Lead resoluble no hay dónde anotar — se marca igual:
                # nada cambiaría en corridas futuras sin intervención manual.
                saltadas.append((cita.name, "sin ficha (Deal/Lead) resoluble"))

            frappe.db.set_value("Reunion Agendada", cita.name, "resumen_generado_at",
                                 ahora, update_modified=False)
            generados.append(cita.name)
        except Exception as exc:
            frappe.log_error(f"resumen_contacto {cita.name}: {exc}", "Resumen de contacto")
            saltadas.append((cita.name, str(exc)[:200]))

    if dry_run:
        return {"activo": True, "dry_run": True, "previsualizadas": previsualizadas, "saltadas": saltadas}
    if generados or saltadas:
        frappe.db.commit()
    return {"activo": True, "generados": generados, "saltadas": saltadas}
