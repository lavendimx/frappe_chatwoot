"""Preparación de llamada v2 — reemplaza el script GHL `preparacion-llamadas/` de
`/root/projects/ventas` (retirado: 100% acoplado a GHL, y el insumo cambió de
naturaleza — no toda cita trae un hilo de WhatsApp que resumir, ver agenda
pública / alta manual).

Plan: /root/projects/lavendimx/nuevosofia/planes/preparacion-llamadas-v2-sofia-crm.md

Diferencias deliberadas con `recordatorios.py` (que sí es la plantilla base):

  - El disparo NO es una ventana antes de la cita — es "al detectar la cita
    nueva" (decisión de Alejandro, 2026-09-18): se genera la nota apenas se
    puede, sin importar si la cita es en 1 día o en 3 semanas.
  - El barrido es horario en horario hábil, no cada 15 min — el volumen real
    es ~8 citas en dos semanas, no hace falta más frecuencia.
  - La entrega es un `Comment` en la ficha (Deal → Lead → Contact), no un
    mensaje al cliente — nunca escribe al exterior.
"""

import json
import urllib.error
import urllib.request

import frappe

from . import chatwoot_client

TIMEOUT_PLANEACION = 60  # el LLM tarda más que un CRUD normal; 30s (agenda) se queda corto.


def _activo():
    try:
        return bool(frappe.db.get_single_value("Chatwoot Settings", "planeacion_llamadas_activo"))
    except Exception:
        return False


def _config_agenda():
    """Mismo secreto compartido host↔contenedor que usa `api/agenda.py` —
    no hace falta un token nuevo, es el mismo proceso `agente-ia`."""
    settings = frappe.get_single("Chatwoot Settings")
    return {
        "url": (getattr(settings, "agenda_url", None) or "").rstrip("/"),
        "token": settings.get_password("agenda_token", raise_exception=False)
        if getattr(settings, "agenda_token", None)
        else "",
    }


def _pedir_planeacion(payload):
    cfg = _config_agenda()
    if not (cfg["url"] and cfg["token"]):
        raise RuntimeError("agenda no configurada (Chatwoot Settings → URL y token del agente)")
    req = urllib.request.Request(
        f"{cfg['url']}/planeacion/generar",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-sofia-token": cfg["token"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_PLANEACION) as resp:
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
    return data.get("bloques") or {}


def _contacto_por_email(email):
    """`crm_contacto` viene vacío en buena parte de las citas (3 de 8 medidas el
    2026-09-18: Estrublock, GAPESF, luz de luna) pero `email_participante` está
    en todas. Se busca el contacto por su correo antes de rendirse."""
    correo = (email or "").strip().lower()
    if "@" not in correo:
        return None
    fila = frappe.get_all("Contact Email", filters={"email_id": correo},
                           fields=["parent"], limit=1)
    return fila[0].parent if fila else None


def _crear_oportunidad_desde_cita(cita, contacto):
    """Ficha de una cita que no tiene ninguna: `CRM Deal` (etapa Lead) si hay
    contacto, `CRM Lead` de respaldo si no. Devuelve `(doctype, name)`.

    Ninguna ficha donde anotar significaba, hasta el 2026-09-18, tirar la hoja
    recién generada: un `Comment`/`FCRM Note` sobre `Contact` no se ve en el panel
    (`Contact.vue` solo tiene pestaña `Deals`) y sin contacto no hay dónde ponerla.

    Se crea la ficha, que además es lo correcto de negocio: un prospecto que agendó
    una videollamada pertenece al embudo, no suelto como contacto. Mismo insertor
    `_crear_ficha` que `agenda_publica._crear_oportunidad_agenda`."""
    from frappe_chatwoot.utils.captacion import _crear_ficha

    nombre = (cita.get("nombre_participante") or "").strip()
    doc_contacto = frappe.get_doc("Contact", contacto) if contacto else None
    if not nombre and doc_contacto:
        nombre = " ".join(x for x in (doc_contacto.first_name, doc_contacto.last_name) if x)
    if not nombre:
        nombre = (cita.get("email_participante") or "").split("@", 1)[0] or "Sin nombre"

    partes = nombre.split(" ", 1)
    org = getattr(doc_contacto, "company_name", None) if doc_contacto else None
    return _crear_ficha(
        contacto,
        first_name=partes[0],
        last_name=partes[1] if len(partes) > 1 else "",
        email=(cita.get("email_participante") or "").strip(),
        organization_name=org,
    )


def _resolver_ficha(cita, crear=True):
    """Deal → Lead → (crear Lead). Devuelve (doctype, name) o (None, None).

    `Contact` se dejó de usar como destino a propósito: el panel del CRM no
    muestra ni notas ni comentarios en la ficha de contacto, así que entregar
    ahí equivale a no entregar.

    `crear=False` en dry-run: un modo que dice "no escribe nada" no puede dar
    de alta un lead de paso."""
    contacto = (cita.get("crm_contacto") or "").strip()
    if not contacto:
        contacto = _contacto_por_email(cita.get("email_participante")) or ""
    if contacto:
        for doctype in ("CRM Deal", "CRM Lead"):
            fila = frappe.get_all(doctype, filters={"contact": contacto}, fields=["name"],
                                   order_by="modified desc", limit=1)
            if fila:
                return (doctype, fila[0].name)

    # Segunda vuelta por correo, incluso con `crm_contacto` poblado: los
    # contactos se duplican (`Alexis Solano` / `Alexis Solano-1`, creados con un
    # día de diferencia por el mismo correo) y la cita puede apuntar al gemelo
    # sin ficha mientras el lead real cuelga del otro. Buscar solo por contacto
    # creaba un lead duplicado — pasó el 2026-09-18 con esa misma persona.
    correo = (cita.get("email_participante") or "").strip().lower()
    if correo:
        for doctype in ("CRM Deal", "CRM Lead"):
            fila = frappe.get_all(doctype, filters={"email": correo}, fields=["name"],
                                   order_by="modified desc", limit=1)
            if fila:
                return (doctype, fila[0].name)

    if not crear:
        return (None, None)
    try:
        return _crear_oportunidad_desde_cita(cita, contacto)
    except Exception as exc:
        frappe.log_error(f"planeacion_llamadas: no se pudo crear ficha para {cita.get('name')}: {exc}",
                          "Preparación de llamada v2")
        return (None, None)


def _elegir_plantilla(cita, ficha_doctype, ficha_doc):
    """Heurística v1 (sin desempate por LLM, ver Supuestos abiertos del plan):
    un deal Ganado es seguimiento de cliente, no venta; todo lo demás cae a
    Venta por default — es más seguro pedir de más (decisor, objeciones) que
    de menos si la heurística se equivoca."""
    if ficha_doctype == "CRM Deal" and (ficha_doc or {}).get("status") == "Won":
        tipo = "Cliente o Proyecto"
    else:
        tipo = "Venta"
    plantilla = frappe.get_all(
        "Plantilla de Planeacion",
        filters={"tipo_reunion": tipo, "activa": 1},
        fields=["name", "tipo_reunion", "instrucciones_llm"],
        limit=1,
    )
    if not plantilla:
        # Fail-open a Venta si el tipo elegido no tiene plantilla activa —
        # nunca debe bloquear la generación por un dato de configuración.
        plantilla = frappe.get_all(
            "Plantilla de Planeacion",
            filters={"tipo_reunion": "Venta", "activa": 1},
            fields=["name", "tipo_reunion", "instrucciones_llm"],
            limit=1,
        )
    return plantilla[0] if plantilla else None


def _planeaciones_previas(ficha_doctype, ficha_name):
    """Últimas 2 notas de planeación ya escritas sobre esta misma ficha —
    para que el LLM ancle al histórico, como mejoró la de Valeria del
    15 al 17-sep (ver Contexto verificado del plan).

    Lee `FCRM Note` (la sección "Notas" del panel), que es donde vive tanto
    lo que genera este job como lo que escribe el equipo a mano — la nota
    "Planeación" de Valeria del 17-sep es de esa segunda clase."""
    if not (ficha_doctype and ficha_name):
        return []
    filas = frappe.get_all(
        "FCRM Note",
        filters={
            "reference_doctype": ficha_doctype,
            "reference_docname": ficha_name,
            "title": ["like", "%Planeaci%"],
        },
        fields=["content", "creation"],
        order_by="creation desc",
        limit_page_length=2,
    )
    return [frappe.utils.strip_html(f.content)[:600] for f in filas]


def _transcripcion(chatwoot_conversation_id):
    if not chatwoot_conversation_id:
        return ""
    try:
        data = chatwoot_client.list_messages(int(chatwoot_conversation_id))
    except Exception as exc:
        frappe.log_error(f"planeacion_llamadas: no se pudo leer conversación {chatwoot_conversation_id}: {exc}",
                          "Preparación de llamada v2")
        return ""
    lineas = []
    for m in (data.get("payload") or data.get("messages") or [])[-15:]:
        cuerpo = (m.get("content") or "").strip()
        if not cuerpo:
            continue
        quien = "CONTACTO" if m.get("message_type") == 0 else "LAVENDI"
        lineas.append(f"[{quien}] {cuerpo[:400]}")
    return "\n".join(lineas)


def _armar_contexto(cita, ficha_doctype, ficha_doc, plantilla):
    dominio = ""
    email = (cita.get("email_participante") or "").strip()
    if "@" in email:
        dominio = email.split("@", 1)[1]

    datos = {
        "cita": {
            "nombre_participante": cita.get("nombre_participante"),
            "email_participante": email,
            "motivo": cita.get("motivo"),
            "start_datetime": str(cita.get("start_datetime") or ""),
            "origen": cita.get("origen"),
            "dominio_correo": dominio,
        },
        "ficha": {"doctype": ficha_doctype, "name": ficha_doc.get("name") if ficha_doc else None,
                   **({k: v for k, v in (ficha_doc or {}).items()
                       if k in ("organization", "status", "deal_value", "lead_name", "company_name")})},
        "transcripcion_chatwoot": _transcripcion(cita.get("chatwoot_conversation_id")),
        "planeaciones_previas": _planeaciones_previas(ficha_doctype, (ficha_doc or {}).get("name")),
    }
    return {"plantilla": {"tipo_reunion": plantilla["tipo_reunion"],
                           "instrucciones_llm": plantilla["instrucciones_llm"]},
            "datos": datos}


ORDEN_BLOQUES = [
    ("contexto", "0. Contexto"),
    ("decisor", "1. ¿Es decisor / quién estará presente?"),
    ("objetivo", "2. Objetivo de la llamada"),
    ("preguntas", "3. Preguntas a realizar"),
    ("material", "4. Material de apoyo"),
    ("cierre", "5. Cierre esperado"),
]

# `FCRM Note.content` es un Text Editor (tiptap) con la extensión de tablas
# cargada (`frappe-ui/TextEditor.vue`) — el andamio va como `<table>` real,
# editable celda por celda. En markdown salía como renglones con pipes.
_FILA_VACIA = "<tr><td>{}</td><td></td><td></td><td></td></tr>"
ANDAMIO_OBJECIONES = (
    "<h4>6. Manejo de objeciones (completar antes de la llamada)</h4>"
    "<table><tbody>"
    "<tr><th>Objeción</th><th>Pregunta de aclaración</th>"
    "<th>Manejo</th><th>Segundo intento de cierre</th></tr>"
    + _FILA_VACIA.format("Precio")
    + _FILA_VACIA.format("")
    + _FILA_VACIA.format("")
    + "</tbody></table>"
)


def _html(texto):
    return frappe.utils.escape_html(str(texto))


def _titulo_nota(cita):
    cuando = frappe.utils.get_datetime(cita.get("start_datetime"))
    return f"Planeación — {frappe.utils.formatdate(cuando, 'd MMM')} {cuando.strftime('%H:%M')}"


def _formatear_nota(cita, bloques, plantilla_nombre):
    partes = [f"<p><i>Borrador automático — Sofía Ventas · plantilla: "
              f"{_html(plantilla_nombre)}</i></p>"]
    for clave, titulo in ORDEN_BLOQUES:
        valor = bloques.get(clave)
        if not valor:
            continue
        if clave == "preguntas" and isinstance(valor, list):
            cuerpo = "<ul>" + "".join(f"<li>{_html(p)}</li>" for p in valor) + "</ul>"
        else:
            cuerpo = "".join(f"<p>{_html(linea)}</p>"
                             for linea in str(valor).split("\n") if linea.strip())
        partes.append(f"<h4>{_html(titulo)}</h4>{cuerpo}")
    partes.append(ANDAMIO_OBJECIONES)
    return "".join(partes)


def _publicar_nota(ficha_doctype, ficha_name, titulo, html):
    """Va a `FCRM Note` — la sección "Notas" del panel — no a `Comment`.
    Razón (Alejandro, 2026-09-18): un `Comment` sobre `Contact` no se ve en
    ningún lado (`Contact.vue` solo tiene pestaña `Deals`), y aun sobre
    Deal/Lead la pestaña donde el equipo busca una planeación es Notas."""
    frappe.get_doc({
        "doctype": "FCRM Note",
        "title": titulo,
        "content": html,
        "reference_doctype": ficha_doctype,
        "reference_docname": ficha_name,
    }).insert(ignore_permissions=True)


def generar_planeaciones(dry_run=0):
    """Scheduler horario en horario hábil. Barre citas futuras sin
    `planeacion_generada_at` — no una ventana de tiempo: la nota se quiere al
    detectar la cita, sin importar cuándo es (decisión de Alejandro).

    `dry_run=1` (solo para validación manual vía `bench execute`): ignora el
    interruptor, genera y devuelve el texto de cada cita, pero NO publica la
    nota ni marca `planeacion_generada_at` — nada se escribe."""
    dry_run = bool(frappe.utils.cint(dry_run))
    if not dry_run and not _activo():
        return {"activo": False}

    ahora = frappe.utils.now_datetime()
    citas = frappe.get_all(
        "Reunion Agendada",
        filters={
            "start_datetime": [">", ahora],
            "planeacion_generada_at": ["is", "not set"],
        },
        fields=["name", "nombre_participante", "email_participante", "motivo",
                "start_datetime", "origen", "crm_contacto", "chatwoot_conversation_id"],
    )

    generadas, saltadas, previsualizadas = [], [], []
    for cita in citas:
        try:
            ficha_doctype, ficha_name = _resolver_ficha(cita, crear=not dry_run)
            ficha_doc = frappe.get_doc(ficha_doctype, ficha_name).as_dict() if ficha_doctype else None
            plantilla = _elegir_plantilla(cita, ficha_doctype, ficha_doc)
            if not plantilla:
                # Sin plantilla activa no hay con qué generar — no se marca:
                # que reintente la próxima corrida en cuanto se siembre/active una.
                saltadas.append((cita.name, "sin Plantilla de Planeacion activa"))
                continue

            contexto = _armar_contexto(cita, ficha_doctype, ficha_doc, plantilla)
            bloques = _pedir_planeacion(contexto)
            texto = _formatear_nota(cita, bloques, plantilla["tipo_reunion"])

            if dry_run:
                previsualizadas.append({"cita": cita.name, "ficha": f"{ficha_doctype} {ficha_name}"
                                         if ficha_doctype else "(sin ficha resoluble)",
                                         "plantilla": plantilla["tipo_reunion"], "texto": texto})
                continue

            if ficha_doctype:
                _publicar_nota(ficha_doctype, ficha_name, _titulo_nota(cita), texto)
            else:
                # Sin Deal/Lead/Contact resoluble (ej. agenda pública sin
                # crm_contacto aún) no hay ficha donde anotar — se marca igual:
                # nada cambiaría en corridas futuras sin intervención manual.
                saltadas.append((cita.name, "sin ficha (Deal/Lead/Contact) resoluble"))

            frappe.db.set_value("Reunion Agendada", cita.name, "planeacion_generada_at",
                                 ahora, update_modified=False)
            generadas.append(cita.name)
        except Exception as exc:
            frappe.log_error(f"planeacion_llamadas {cita.name}: {exc}", "Preparación de llamada v2")
            saltadas.append((cita.name, str(exc)[:200]))

    if dry_run:
        return {"activo": True, "dry_run": True, "previsualizadas": previsualizadas, "saltadas": saltadas}
    if generadas or saltadas:
        frappe.db.commit()
    return {"activo": True, "generadas": generadas, "saltadas": saltadas}
