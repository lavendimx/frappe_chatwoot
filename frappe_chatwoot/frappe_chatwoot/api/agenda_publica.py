# Copyright (c) 2026, lavendi.mx
# License: MIT
"""
Agenda PÚBLICA — reemplazo de los 3 widgets de booking de GoHighLevel.

Qué se está sustituyendo (volcado de la subcuenta FG3KBslyUoNf3Ar9YVHJ el 2026-09-11):

    widget/bookings/lavendimx            -> /agenda            (leads, 50 min)
    widget/bookings/lavendimx/clientes   -> /agenda/clientes   (clientes, 50 min)
    widget/bookings/lavendimx/entrevista -> /agenda/entrevista (entrevista, 50 min)

El primero es el que cuelga del botón "VIDEOLLAMADA DEMO" del sitio (bloque reusable 8006,
presente en 33 páginas); el segundo lo manda `onboarding/mensajes.py` a cada cliente nuevo;
el tercero lo usa el proceso de venta. Los tres mueren el día que se pause la subcuenta.

Diferencia con `agenda.py` (alta manual desde el CRM): ahí el que agenda es una persona del
equipo ya autenticada y puede elegir cualquier hora. Aquí el que agenda es un desconocido
desde internet y solo puede tomar los huecos que el servidor le ofreció. Todo el cálculo de
disponibilidad vive en `agente-ia/lib/agenda-publica.js`; este módulo es la puerta pública y
el enganche con el CRM.

Por qué la cita la crea el proceso `agente-ia` y no Frappe: el contenedor no tiene las
librerías de Google ni el service account (mismo motivo documentado en `agenda.py`).
"""

import json
import re
import urllib.error
import urllib.request

import frappe

TIMEOUT_AGENDA = 30

# Tope de reservas por IP y por hora. No es antifraude —un bot con IPs rotativas lo pasa—
# sino un freno al caso realista: alguien recargando y mandando el formulario en bucle, o un
# scraper que llenaría la agenda del equipo de citas falsas con invitación de Google incluida.
MAX_RESERVAS_POR_IP_HORA = 3


def _config_agenda() -> dict:
    settings = frappe.get_single("Chatwoot Settings")
    return {
        "url": (getattr(settings, "agenda_url", None) or "").rstrip("/"),
        "token": settings.get_password("agenda_token", raise_exception=False)
        if getattr(settings, "agenda_token", None)
        else "",
    }


def _pedir(ruta: str, payload: dict) -> dict:
    cfg = _config_agenda()
    if not (cfg["url"] and cfg["token"]):
        frappe.throw("La agenda no está configurada.")

    req = urllib.request.Request(
        f"{cfg['url']}{ruta}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-sofia-token": cfg["token"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_AGENDA) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        cuerpo = exc.read().decode(errors="replace")
        try:
            return json.loads(cuerpo)
        except ValueError:
            frappe.log_error(f"{ruta} -> {exc.code}: {cuerpo}", "agenda-publica")
            return {"ok": False, "error": "error_agenda"}
    except Exception as exc:
        frappe.log_error(f"{ruta}: {exc}", "agenda-publica")
        return {"ok": False, "error": "agenda_no_responde"}


# ---------------------------------------------------------------------------
# Endpoints públicos
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def slots(perfil: str = "leads", dias: int = 14):
    """Huecos libres. Solo lectura: no crea nada y no toca el calendario."""
    if perfil not in ("leads", "clientes", "entrevista"):
        return {"ok": False, "error": "perfil_desconocido"}
    try:
        dias = max(1, min(int(dias), 60))
    except (TypeError, ValueError):
        dias = 14
    return _pedir("/agenda/slots", {"perfil": perfil, "dias": dias})


def _ip_cliente() -> str:
    return frappe.local.request_ip or "desconocida"


def _bajo_limite() -> bool:
    """Contador por IP con TTL de una hora en el cache de Frappe (Redis)."""
    clave = f"agenda_publica:reservas:{_ip_cliente()}"
    hechas = frappe.cache().get_value(clave) or 0
    if int(hechas) >= MAX_RESERVAS_POR_IP_HORA:
        return False
    frappe.cache().set_value(clave, int(hechas) + 1, expires_in_sec=3600)
    return True


@frappe.whitelist(allow_guest=True)
def reservar(
    perfil: str = "leads",
    nombre: str = "",
    email: str = "",
    telefono: str = "",
    empresa: str = "",
    mensaje: str = "",
    fecha: str = "",
    hora: str = "",
    pagina_origen: str = "",
    utm_source: str = "",
    utm_medium: str = "",
    utm_campaign: str = "",
    utm_term: str = "",
    gclid: str = "",
):
    if perfil not in ("leads", "clientes", "entrevista"):
        return {"ok": False, "error": "perfil_desconocido"}

    if not _bajo_limite():
        return {
            "ok": False,
            "error": "demasiados_intentos",
            "mensaje": "Ya reservaste varias citas desde aquí. Si necesitas otra, escríbenos por WhatsApp.",
        }

    resultado = _pedir(
        "/agenda/reservar",
        {
            "perfil": perfil,
            "nombre": (nombre or "").strip(),
            "email": (email or "").strip().lower(),
            "telefono": (telefono or "").strip(),
            "empresa": (empresa or "").strip(),
            "mensaje": (mensaje or "").strip(),
            "fecha": (fecha or "").strip(),
            "hora": (hora or "").strip(),
        },
    )

    if not resultado.get("ok"):
        return resultado

    # La cita ya existe. Lo que sigue (CRM) es mejor-esfuerzo: si falla, el cliente igual
    # tiene su videollamada y su invitación de Google. Romperle la confirmación por un error
    # de CRM sería cambiar un problema interno por uno de cara al cliente.
    try:
        resultado["crm"] = _enganchar_crm(perfil, resultado, {
            "pagina_origen": pagina_origen,
            "utm_source": utm_source,
            "utm_medium": utm_medium,
            "utm_campaign": utm_campaign,
            "utm_term": utm_term,
            "gclid": gclid,
        })
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "agenda-publica: enganche CRM")
        resultado["crm"] = {"ok": False, "error": str(exc)}

    return resultado


# ---------------------------------------------------------------------------
# Enganche con el CRM
# ---------------------------------------------------------------------------

def _enganchar_crm(perfil: str, resultado: dict, atribucion: dict) -> dict:
    """Contacto siempre; oportunidad solo en el perfil de leads.

    En GHL el calendario de leads traía el formulario `ObqR96LcFpiiX6UKwLTu` amarrado y por
    eso una reserva creaba contacto y oportunidad. Los otros dos calendarios eran para gente
    que YA es cliente: crearles una oportunidad nueva cada vez que agendan soporte llenaría
    el embudo de ruido. Ahí solo se asegura el contacto y se liga la cita.
    """
    from frappe_chatwoot.utils.captacion import _asegurar_contacto, _buscar_existente

    datos = resultado.get("contacto") or {}
    nombre = datos.get("nombre") or ""
    email = datos.get("email") or ""
    telefono = datos.get("telefono") or ""

    doctype_existente, name_existente = _buscar_existente(telefono, email)

    # Portador en memoria: `_asegurar_contacto` lee `nombre`/`email`/`telefono` de un doc.
    # NO se inserta — no hay `Solicitud Web` guardada, así que no se dispara el WhatsApp de
    # bienvenida que habla de "tu solicitud de cotización": quien agenda una videollamada no
    # pidió una cotización, y ese mensaje descolocaría al cliente.
    portador = frappe.new_doc("Solicitud Web")
    portador.nombre = nombre
    portador.email = email
    portador.telefono = telefono

    contacto = _asegurar_contacto(portador)

    ficha = None
    ficha_dt = None
    if perfil == "leads" and not name_existente:
        try:
            ficha_dt, ficha = _crear_oportunidad_agenda(portador, contacto, datos, atribucion, resultado)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "agenda-publica: crear oportunidad")
    elif name_existente:
        _nota_cita(doctype_existente, name_existente, datos, resultado, perfil)

    # La cita queda ligada al contacto para que aparezca en su ficha, no suelta en la lista.
    if contacto and resultado.get("reunion"):
        try:
            frappe.db.set_value("Reunion Agendada", resultado["reunion"], "crm_contacto", contacto)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "agenda-publica: ligar contacto")

    frappe.db.commit()
    return {
        "ok": True,
        "contacto": contacto,
        # `lead` se conserva porque es la llave que ya lee el consumidor; desde
        # 2026-09-22 el valor normal es una oportunidad (`ficha_doctype` dice cuál).
        "lead": ficha,
        "ficha": ficha,
        "ficha_doctype": ficha_dt,
        "ya_existia": bool(name_existente),
    }


def _cuerpo_nota(datos: dict, resultado: dict, perfil: str) -> str:
    return (
        f"<p>Agendó una videollamada desde la <b>agenda del sitio</b> ({perfil}).</p>"
        f"<p>Cuándo: <b>{resultado.get('inicio', '')}</b> "
        f"({resultado.get('duracion_minutos', '')} min)</p>"
        f"<p>Meet: {resultado.get('meetLink') or 'sin liga'}</p>"
        f"<p>Empresa: {datos.get('empresa') or 'no indicada'}</p>"
    )


def _nota_cita(doctype: str, name: str, datos: dict, resultado: dict, perfil: str) -> None:
    """Un contacto conocido que agenda no genera oportunidad nueva, pero sí tiene que
    dejar rastro donde el equipo lo vea: sin esto, la cita solo existiría en el calendario
    y quien abra su ficha no sabría que hay una videollamada en pie."""
    try:
        frappe.get_doc({
            "doctype": "FCRM Note",
            "title": "Videollamada agendada desde el sitio",
            "content": _cuerpo_nota(datos, resultado, perfil),
            "reference_doctype": doctype,
            "reference_docname": name,
        }).insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "agenda-publica: nota en existente")


def _crear_oportunidad_agenda(portador, contacto, datos, atribucion, resultado):
    """Oportunidad de una reserva de videollamada.

    No reutiliza `_crear_oportunidad` de captación porque aquel gira alrededor de los
    checkboxes de producto del formulario (etiqueta, valor estimado) y una reserva de
    videollamada no declara producto: forzarlo dejaría la ficha con valor 0 y etiquetas
    vacías, y la nota apuntando a una `Solicitud Web` que no existe. Sí comparte el
    insertor `_crear_ficha`, que decide `CRM Deal` (etapa Lead) vs. `CRM Lead` de respaldo
    cuando no hay contacto."""
    from frappe_chatwoot.utils.captacion import _crear_ficha

    ficha_dt, ficha = _crear_ficha(
        contacto,
        nombre=portador.nombre,
        email=portador.email,
        telefono=portador.telefono,
        organization_name=datos.get("empresa"),
        atribucion=atribucion,
    )

    _nota_cita(ficha_dt, ficha, datos, resultado, "leads")
    return ficha_dt, ficha


# ---------------------------------------------------------------------------
# Contexto de las páginas (`www/agenda/*.html`)
# ---------------------------------------------------------------------------

TITULOS = {
    "leads": "Agenda una videollamada",
    "clientes": "Agenda una videollamada",
    "entrevista": "Agenda tu entrevista inicial",
}


# ---------------------------------------------------------------------------
# Gestión de la cita por el propio cliente (cancelar / reagendar)
# ---------------------------------------------------------------------------
# El visitante llega con el token que venía en la invitación de Google. No hay sesión ni
# login: el token ES la autorización, por eso es aleatorio de 128 bits y no el id del
# documento — con `REUNION-00042` cualquiera cancelaría la cita del siguiente.
#
# Estos tres endpoints solo pasan el token al proceso Node, que es el único que habla con
# Google Calendar. Aquí no se valida el token contra la base: hacerlo sería duplicar la
# regla en dos lugares y arriesgarse a que se separen.

MAX_ACCIONES_POR_IP_HORA = 10


def _bajo_limite_gestion() -> bool:
    """Freno al mismo caso realista que en `reservar`: alguien (o algo) probando tokens en
    bucle. Es más holgado porque aquí el visitante SÍ tiene motivo para repetir — mirar su
    cita, pensarlo, volver, cambiar la hora dos veces."""
    clave = f"agenda_publica:gestion:{_ip_cliente()}"
    hechas = frappe.cache().get_value(clave) or 0
    if int(hechas) >= MAX_ACCIONES_POR_IP_HORA:
        return False
    frappe.cache().set_value(clave, int(hechas) + 1, expires_in_sec=3600)
    return True


@frappe.whitelist(allow_guest=True)
def cita(token: str = ""):
    """Datos de la cita para pintar la página. Solo lectura."""
    if not _bajo_limite_gestion():
        return {"ok": False, "error": "demasiados_intentos",
                "mensaje": "Demasiados intentos. Espera un momento e inténtalo de nuevo."}
    return _pedir("/agenda/cita", {"token": token})


@frappe.whitelist(allow_guest=True, methods=["POST"])
def cancelar(token: str = ""):
    if not _bajo_limite_gestion():
        return {"ok": False, "error": "demasiados_intentos",
                "mensaje": "Demasiados intentos. Espera un momento e inténtalo de nuevo."}
    return _pedir("/agenda/cancelar", {"token": token})


@frappe.whitelist(allow_guest=True, methods=["POST"])
def reagendar(token: str = "", fecha: str = "", hora: str = ""):
    if not _bajo_limite_gestion():
        return {"ok": False, "error": "demasiados_intentos",
                "mensaje": "Demasiados intentos. Espera un momento e inténtalo de nuevo."}
    return _pedir("/agenda/reagendar", {"token": token, "fecha": fecha, "hora": hora})


def contexto_cita(context):
    """Página de gestión. El token viaja en la query (`?t=...`) porque es el único dato que
    el cliente tiene: llegó en el texto de su invitación de Google."""
    context.no_cache = 1
    context.title = "Tu videollamada con lavendi.mx"
    context.titulo_pagina = context.title
    token = (frappe.form_dict.get("t") or "").strip()
    from frappe.sessions import get_csrf_token

    context.csrf_token = get_csrf_token()
    context.token = token
    datos = cita(token) if token else {"ok": False, "error": "sin_token"}
    context.datos = datos
    context.cita_json = json.dumps(datos)
    context.error = None if datos.get("ok") else datos.get("error")
    # Los huecos para reagendar se piden con el perfil de la propia cita: sus reglas de
    # duración y horario son las que aplican, no las del perfil por default.
    disponibilidad = {"ok": False}
    if datos.get("ok"):
        disponibilidad = slots((datos.get("cita") or {}).get("perfil") or "leads", 21)
    context.disponibilidad = json.dumps(disponibilidad)
    return context


def contexto_pagina(context, perfil: str):
    context.no_cache = 1
    context.perfil = perfil
    # `context.title` lo reescribe Frappe prefijando el nombre del sitio ("Sofía - ...").
    # La página es de cara al cliente: ahí manda la marca de la agencia, no la del CRM.
    context.titulo_pagina = TITULOS.get(perfil, "Agenda una videollamada")
    context.title = context.titulo_pagina
    datos = slots(perfil, 21)
    # Frappe exige `X-Frappe-CSRF-Token` en todo POST de navegador, también en endpoints
    # `allow_guest`: la primera prueba E2E falló con 400 CSRFTokenError mientras la misma
    # llamada por curl pasaba. Leer `session.data.csrf_token` a secas devuelve vacío para un
    # visitante nuevo — `get_csrf_token()` lo genera si aún no existe.
    from frappe.sessions import get_csrf_token

    context.csrf_token = get_csrf_token()
    context.disponibilidad = json.dumps(datos)
    context.datos = datos
    context.error = None if datos.get("ok") else datos.get("error")
    return context
