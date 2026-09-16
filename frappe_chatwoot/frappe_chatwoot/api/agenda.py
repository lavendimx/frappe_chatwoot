# Copyright (c) 2026, lavendi.mx
# License: MIT
"""
Alta y cancelación MANUAL de citas desde el CRM — no es upstream.

Hasta hoy la página Calendario era solo-lectura: mostraba únicamente lo que agendaba el
agente IA con su herramienta `agendar_reunion`, y su propio empty state decía "No se crean
manualmente". En GoHighLevel el equipo sí agregaba citas a mano al calendario, así que era
un hueco de paridad: cualquier reunión pactada por teléfono, por correo o en una junta
tenía que capturarse en Google Calendar aparte, fuera del CRM, donde nadie la ve al abrir
la ficha del cliente.

Por qué esto llama a otro proceso en vez de hablarle a Google directo: el contenedor de
Frappe no tiene las librerías de Google ni acceso al service account del host (verificado
2026-09-10). El motor de calendario —impersonación, Meet, color, invitaciones— ya está
resuelto en `agente-ia/lib/calendar.js`; duplicarlo aquí obligaría a montar dentro del
contenedor la credencial que abre todos los calendarios de la agencia. Frappe le pide la
cita al proceso `agente-ia` por HTTP (`10.0.3.1:8095`, cerrado a internet por UFW y con
token compartido) y ese proceso hace la parte de Google.

Consecuencia a tener presente: si `agente-ia` está caído, no se pueden agendar citas a
mano. Es el mismo acoplamiento que ya existe con Evolution para abrir conversaciones.
"""

import json
import urllib.error
import urllib.request

import frappe

from .chatwoot import validate_role

TIMEOUT_AGENDA = 30  # Google Calendar + Meet tarda; 10s (el de Evolution) se queda corto.


def _config_agenda() -> dict:
    """URL y token del proceso agente-ia, guardados como custom fields de Chatwoot
    Settings — mismo criterio que la config de Evolution: el contenedor de Frappe no
    comparte el .env del host."""
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
        frappe.throw("El agendamiento no está configurado (Chatwoot Settings → URL y token del agente).")

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
        # El agente contesta 400 con un motivo legible (horario ocupado, datos inválidos).
        # Se le muestra tal cual al usuario en vez de un "error del servidor" genérico.
        cuerpo = {}
        try:
            cuerpo = json.loads(exc.read().decode())
        except (ValueError, OSError):
            pass
        mensaje = cuerpo.get("mensaje") or cuerpo.get("error") or str(exc)
        frappe.log_error(title="agenda: el agente rechazó la cita", message=f"{ruta} -> {mensaje}")
        frappe.throw(mensaje)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        frappe.log_error(title="agenda: agente-ia no responde", message=f"{ruta} -> {exc}")
        frappe.throw(f"El servicio de agenda no responde: {exc}")


@frappe.whitelist()
def opciones_agenda() -> dict:
    """Lo que el diálogo necesita para armarse: en qué inboxes se puede agendar y a quién
    del equipo se puede poner de anfitrión.

    Solo se ofrecen inboxes con calendario configurado: sin `calendar_id` el alta fallaría
    hasta el último paso, después de que el usuario capturó todo."""
    validate_role()
    inboxes = frappe.get_all(
        "Agente IA",
        filters=[["calendar_id", "is", "set"]],
        fields=["name as inbox_id", "client_name", "calendar_id"],
        order_by="name asc",
    )
    # Orden: primero el inbox donde más se agenda. Los cuatro inboxes están activos y uno
    # de ellos se llama literalmente "Piloto (no usar en producción)" — ordenar por id
    # dejaría ese como opción preseleccionada. El conteo de citas ya agendadas es la única
    # señal real de cuál es el calendario que el equipo usa, y se corrige solo con el uso.
    conteos = dict(
        frappe.db.sql("SELECT inbox_id, COUNT(*) FROM `tabReunion Agendada` GROUP BY inbox_id")
    )
    for fila in inboxes:
        fila["citas"] = frappe.utils.cint(conteos.get(fila["inbox_id"], 0))
    inboxes.sort(key=lambda f: (-f["citas"], f["inbox_id"]))

    # Anfitriones: el equipo interno. Se filtra por dominio y no por rol a propósito —
    # los roles de ventas quedaron dispares tras la migración de GHL (a alejandro@ hubo que
    # agregárselos a mano el 06-sep) y una lista vacía dejaría el campo inútil.
    # Se descartan las cuentas de integración (las que tienen api_key: agente-ia@ es un
    # usuario de Frappe, no una persona a la que invitar a una videollamada).
    usuarios = frappe.get_all(
        "User",
        filters=[
            ["enabled", "=", 1],
            ["name", "like", "%@lavendi.mx"],
            ["user_type", "=", "System User"],
            ["api_key", "is", "not set"],
        ],
        fields=["name as email", "full_name"],
        order_by="full_name asc",
    )
    return {"inboxes": inboxes, "anfitriones": usuarios}


@frappe.whitelist()
def buscar_contactos(query: str = "", limit: int = 10) -> list[dict]:
    """Buscador de contactos para prellenar la cita.

    A diferencia de `panel.search_crm_contacts`, NO exige teléfono: aquí lo que sirve es el
    correo (es a donde va la invitación de Google Calendar). También devuelve contactos sin
    ninguno de los dos — el nombre basta para dejar la cita registrada, y el 79% de los
    contactos migrados de GHL no tiene email."""
    validate_role()
    query = (query or "").strip()
    if len(query) < 2:
        return []

    digitos = "".join(c for c in query if c.isdigit())
    if digitos and len(digitos) >= 4:
        filtros = [["mobile_no", "like", f"%{digitos[-10:] if len(digitos) >= 10 else digitos}%"]]
    else:
        filtros = [["name", "like", f"%{query}%"]]

    return frappe.get_all(
        "Contact",
        filters=filtros,
        fields=["name", "first_name", "last_name", "company_name", "mobile_no", "email_id"],
        order_by="modified desc",
        limit=frappe.utils.cint(limit) or 10,
    )


@frappe.whitelist()
def crear_cita(
    inbox_id: str,
    nombre_participante: str,
    fecha: str,
    hora: str,
    duracion_minutos: int = 30,
    email_participante: str = None,
    motivo: str = None,
    empresa: str = None,
    anfitriones: str = None,
    contacto: str = None,
) -> dict:
    """Crea el evento real en Google Calendar (con Meet) y su registro en el CRM.

    `anfitriones` llega como JSON desde el front. Se agregan como invitados para que la
    cita caiga también en el calendario personal de quien la va a atender: el calendario
    de Sofía lo mira el equipo de vez en cuando, el propio lo miran todos los días.
    """
    validate_role()

    if isinstance(anfitriones, str):
        try:
            anfitriones = json.loads(anfitriones)
        except ValueError:
            anfitriones = [anfitriones] if anfitriones else []
    anfitriones = [a for a in (anfitriones or []) if a]

    resultado = _pedir(
        "/calendario/agendar",
        {
            "inbox_id": str(inbox_id),
            "nombre_participante": (nombre_participante or "").strip(),
            "email_participante": (email_participante or "").strip() or None,
            "fecha": fecha,
            "hora": hora,
            "duracion_minutos": frappe.utils.cint(duracion_minutos) or 30,
            "motivo": (motivo or "").strip(),
            "empresa": (empresa or "").strip(),
            "anfitriones": anfitriones,
            "contacto": contacto or "",
        },
    )
    if not resultado.get("ok"):
        frappe.throw(resultado.get("mensaje") or "No se pudo agendar la cita.")
    return resultado


@frappe.whitelist()
def cancelar_cita(name: str) -> dict:
    """Borra el evento en Google Calendar (avisando a los invitados) y su registro.

    Se permite cancelar también las citas del agente: si el cliente reagenda por WhatsApp,
    quien lo atiende necesita poder quitar la vieja sin entrar a la consola — que es como
    hubo que borrar las 3 citas de prueba que estuvieron un mes en el calendario."""
    validate_role()
    resultado = _pedir("/calendario/cancelar", {"name": name})
    if not resultado.get("ok"):
        frappe.throw(resultado.get("mensaje") or "No se pudo cancelar la cita.")
    return resultado


@frappe.whitelist()
def editar_cita(
    name: str,
    fecha: str,
    hora: str,
    duracion_minutos: int = 30,
    motivo: str = None,
) -> dict:
    """Mueve una cita ya creada (fecha/hora/duración) y actualiza su motivo.

    Igual que crear y cancelar: el trabajo real lo hace el proceso `agente-ia`
    (que tiene Google Calendar); aquí solo se valida el rol y se le pasa la
    petición. El evento se mueve con `events.patch`, no se borra y recrea, para
    que conserve su id, su Meet y sus invitados.
    """
    validate_role()
    resultado = _pedir(
        "/calendario/editar",
        {
            "name": name,
            "fecha": fecha,
            "hora": hora,
            "duracion_minutos": frappe.utils.cint(duracion_minutos) or 30,
            "motivo": (motivo or "").strip(),
        },
    )
    if not resultado.get("ok"):
        frappe.throw(resultado.get("mensaje") or "No se pudo editar la cita.")
    return resultado
