# Copyright (c) 2026, lavendi.mx
# License: MIT
"""
Extensiones de lavendi.mx sobre la bandeja de Conversaciones — no son upstream.

Dos cosas que el equipo tenía en GoHighLevel y aquí faltaban:

1. `get_contact_panel` — el panel lateral del contacto: quién es, sus
   oportunidades y sus notas, sin salir del hilo. En GHL esto era la columna
   derecha de la conversación; sin ella hay que abrir el CRM en otra pestaña y
   buscar a mano, que es justo lo que hace que nadie mire el contexto antes de
   contestar.

2. `search_crm_contacts` / `start_conversation` — abrir una conversación nueva
   desde el CRM. Hasta ahora Sofía solo podía CONTESTAR: si el equipo quería
   escribirle primero a alguien, tenía que hacerlo desde el celular, y ese
   mensaje entraba al hilo sin que nadie lo viera en la bandeja.

Identidad del contacto: el teléfono, no el correo. El 79% de los contactos
migrados de GHL no tiene email (medido 2026-09-06), así que todo el match va por
los últimos 10 dígitos del número — mismo criterio que usa el agente en
`agente-ia/lib/opportunities.js`.
"""

import json
import re
import urllib.error
import urllib.request

import frappe

from frappe_chatwoot.utils import autoria
from frappe_chatwoot.utils import chatwoot_client as cw
from frappe_chatwoot.utils import telefono as tel
from frappe_chatwoot.utils.chatwoot_contactos import _contacto_chatwoot

from .chatwoot import _pause_conversation, validate_role

# Cuántas notas/oportunidades devuelve el panel. Es una vista de contexto, no un
# reporte: si alguien necesita el histórico completo abre el registro en el CRM.
MAX_DEALS = 20
MAX_NOTES = 20

# Espejo de `api/chatwoot.MAX_ADJUNTOS`: mismo tope para el primer mensaje.
MAX_ADJUNTOS = 5


def telefono_corto(telefono: str | None) -> str | None:
    """Últimos 10 dígitos. Absorbe lada de país, +, espacios y guiones: el mismo
    número llega como '+52 1 555 966 8622', '5215559668622' o '5559668622'
    según venga de WhatsApp, de la migración de GHL o capturado a mano.
    Delega en `utils.telefono.corto` para no repetir el criterio (2026-09-23)."""
    return tel.corto(telefono)


def _contacto_por_telefono(telefono: str | None) -> str | None:
    """Nombre del `Contact` de Frappe que corresponde a ese número, si existe.

    El LIKE con comodín inicial no usa índice, pero son consultas puntuales por
    conversación abierta, no un barrido de los 4,224 contactos."""
    corto = telefono_corto(telefono)
    if not corto:
        return None
    patron = f"%{corto}"
    for campo in ("mobile_no", "phone"):
        encontrados = frappe.get_all(
            "Contact",
            filters=[[campo, "like", patron]],
            fields=["name"],
            limit=1,
        )
        if encontrados:
            return encontrados[0].name
    return None


def _notas_de(referencias: list[tuple[str, str]]) -> list[dict]:
    """Notas de todos los registros del contacto (su ficha + sus oportunidades).

    En GHL la nota colgaba del contacto; aquí `FCRM Note` es un dynamic link que
    puede apuntar a Contact, CRM Deal o CRM Lead — el equipo las escribe donde
    esté trabajando, así que el panel las junta todas en una sola lista."""
    if not referencias:
        return []
    condiciones = []
    valores: list[str] = []
    for doctype, docname in referencias:
        condiciones.append("(reference_doctype = %s AND reference_docname = %s)")
        valores.extend([doctype, docname])
    filas = frappe.db.sql(
        f"""
        SELECT name, title, content, reference_doctype, reference_docname, owner, modified,
               ghl_note_id
        FROM `tabFCRM Note`
        WHERE {" OR ".join(condiciones)}
        ORDER BY modified DESC
        LIMIT {MAX_NOTES}
        """,
        valores,
        as_dict=True,
    )
    for f in filas:
        # Contenido y título son Text Editor (HTML). El panel los pinta como texto
        # plano: es una vista de lectura rápida, no un editor.
        f["content"] = _texto_plano(f.get("content"))
        titulo = _texto_plano(f.get("title"))
        # Las notas migradas de GHL traen como título el propio arranque del
        # contenido ("[GHL] --------- Summary --------- Denise Mariscal..."), así
        # que mostrarlo duplica el texto en pantalla sin aportar nada.
        if titulo.startswith("[GHL]") or (titulo and f["content"].startswith(titulo[:40])):
            titulo = ""
        f["title"] = titulo
        f["origen_ghl"] = bool(f.pop("ghl_note_id", None))
    return filas


def _texto_plano(html: str | None) -> str:
    limpio = re.sub(r"<[^>]+>", " ", html or "")
    limpio = limpio.replace("&nbsp;", " ").replace("&amp;", "&")
    # Las notas de GHL abren con una regla de guiones que en texto plano es ruido.
    limpio = re.sub(r"-{4,}", " ", limpio)
    return re.sub(r"\s+", " ", limpio).strip()


@frappe.whitelist()
def get_contact_panel(conversation_id: int = None, phone: str = None) -> dict:
    """Contexto de CRM de quien escribe: ficha, oportunidades y notas.

    Se puede llamar con `conversation_id` (la bandeja) o directo con `phone`.
    Degrada suave: si el contacto no está en el CRM devuelve `found: False` en
    vez de reventar — un número desconocido escribiendo por primera vez es un
    caso normal, no un error.
    """
    validate_role()

    telefono = phone
    contacto_chatwoot = {}
    if not telefono and conversation_id:
        conv = cw.get_conversation(frappe.utils.cint(conversation_id))
        sender = ((conv or {}).get("meta") or {}).get("sender") or {}
        contacto_chatwoot = {
            "name": sender.get("name"),
            "phone": sender.get("phone_number"),
            "email": sender.get("email"),
            "identifier": sender.get("identifier"),
        }
        telefono = sender.get("phone_number") or sender.get("identifier")

    contact_name = _contacto_por_telefono(telefono)
    if not contact_name:
        return {"found": False, "chatwoot": contacto_chatwoot, "phone": telefono}

    contacto = frappe.db.get_value(
        "Contact",
        contact_name,
        ["name", "first_name", "last_name", "company_name", "mobile_no", "phone", "email_id", "designation"],
        as_dict=True,
    ) or {}

    deals = frappe.get_all(
        "CRM Deal",
        filters={"contact": contact_name},
        fields=[
            "name", "organization", "status", "deal_value", "currency",
            "deal_owner", "ghl_stage", "ghl_status", "modified",
            # Denormalizados por el motor de secuencias (ver
            # utils/secuencias.py, `_reflejar_en_deal`): el panel muestra si el
            # contacto está enrolado y en qué paso, sin abrir otra pantalla.
            "secuencia_actual", "secuencia_paso", "secuencia_estado",
        ],
        order_by="modified desc",
        limit=MAX_DEALS,
    )
    leads = frappe.get_all(
        "CRM Lead",
        filters={"contact": contact_name},
        fields=["name", "status", "lead_owner", "organization", "modified"],
        order_by="modified desc",
        limit=MAX_DEALS,
    )

    referencias = [("Contact", contact_name)]
    referencias += [("CRM Deal", d.name) for d in deals]
    referencias += [("CRM Lead", l.name) for l in leads]

    return {
        "found": True,
        "chatwoot": contacto_chatwoot,
        "contact": contacto,
        "deals": deals,
        "leads": leads,
        "notes": _notas_de(referencias),
    }


@frappe.whitelist()
def search_crm_contacts(query: str = "", limit: int = 10) -> list[dict]:
    """Buscador para el diálogo de conversación nueva: por nombre o por teléfono.

    Devuelve solo contactos CON teléfono — sin número no hay a dónde escribirle,
    y ofrecerlos sería un callejón sin salida en la interfaz."""
    validate_role()
    query = (query or "").strip()
    if len(query) < 2:
        return []

    digitos = re.sub(r"\D", "", query)
    if digitos and len(digitos) >= 4:
        filtros = [["mobile_no", "like", f"%{digitos[-10:] if len(digitos) >= 10 else digitos}%"]]
    else:
        filtros = [["name", "like", f"%{query}%"]]

    contactos = frappe.get_all(
        "Contact",
        filters=filtros,
        fields=["name", "first_name", "last_name", "company_name", "mobile_no", "email_id"],
        order_by="modified desc",
        limit=frappe.utils.cint(limit) or 10,
    )
    return [c for c in contactos if c.get("mobile_no")]


# ---------------------------------------------------------------------------
# Fusionar contactos duplicados (T4, 2026-09-23)
#
# Un mismo cliente termina con dos `Contact` y dos conversaciones cuando su
# teléfono entró con y sin el "1" móvil (caso Anabel Osuna: `+524422196109` vs
# `5214422196109`). Chatwoot 4.17.1 ya trae la fusión nativa; antes había que
# hacerlo a mano contacto por contacto.
# ---------------------------------------------------------------------------


def _resolver_contacto_chatwoot(valor) -> tuple[int, str | None]:
    """Normaliza la entrada a `(chatwoot_id, contact_frappe)`.

    Acepta las tres formas con las que el frontend puede mandar un contacto:
      · un **id de Chatwoot** directo (numérico);
      · el **nombre de un `Contact` de Frappe** (se resuelve por su teléfono);
      · un **teléfono** (se busca el homólogo de Chatwoot por últimos 10 dígitos).
    """
    v = str(valor or "").strip()
    if not v:
        frappe.throw("Falta el contacto a fusionar")

    if not v.isdigit() and frappe.db.exists("Contact", v):
        tel = frappe.db.get_value("Contact", v, ["mobile_no", "phone"], as_dict=True) or {}
        cw_contact = _contacto_chatwoot(tel.get("mobile_no") or tel.get("phone"))
        if not cw_contact:
            frappe.throw(f"El contacto {v} no tiene homólogo en Chatwoot (se busca por teléfono).")
        return int(cw_contact["id"]), v

    if v.isdigit():
        return int(v), None

    cw_contact = _contacto_chatwoot(v)
    if not cw_contact:
        frappe.throw(f"No se encontró un contacto de Chatwoot para {v!r}.")
    return int(cw_contact["id"]), _contacto_por_telefono(v)


@frappe.whitelist()
def fusionar_contactos(base: str, mergee: str) -> dict:
    """Fusiona dos contactos duplicados en Chatwoot (`base` absorbe a `mergee`).

    Idempotente por par: si la fusión ya se registró en `Chatwoot Fusion`, no se
    vuelve a llamar a Chatwoot (repetirla apuntaría a un `mergee` que ya no
    existe). El registro deja la auditoría de quién fusionó, cuándo y qué dos
    contactos de Frappe eran, porque la operación es irreversible desde Chatwoot.
    """
    validate_role()
    base_id, base_contact = _resolver_contacto_chatwoot(base)
    mergee_id, mergee_contact = _resolver_contacto_chatwoot(mergee)
    if base_id == mergee_id:
        frappe.throw("El contacto base y el duplicado son el mismo.")

    ya = frappe.db.exists(
        "Chatwoot Fusion",
        {"base_chatwoot_id": base_id, "mergee_chatwoot_id": mergee_id},
    )
    if ya:
        return {"ok": True, "ya_fusionado": True, "name": ya,
                "base_chatwoot_id": base_id, "mergee_chatwoot_id": mergee_id}

    cw.merge_contacts(base_id, mergee_id)

    doc = frappe.get_doc({
        "doctype": "Chatwoot Fusion",
        "base_chatwoot_id": base_id,
        "mergee_chatwoot_id": mergee_id,
        "base_contact": base_contact,
        "mergee_contact": mergee_contact,
        "fusionado_por": frappe.session.user,
        "fusionado_at": frappe.utils.now(),
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "ya_fusionado": False, "name": doc.name,
            "base_chatwoot_id": base_id, "mergee_chatwoot_id": mergee_id,
            "base_contact": base_contact, "mergee_contact": mergee_contact}


# ---------------------------------------------------------------------------
# Abrir conversación nueva
# ---------------------------------------------------------------------------


def _config_evolution() -> dict:
    """URL, instancia y api key de Evolution, guardadas como custom fields de
    Chatwoot Settings (no en el .env: el contenedor de Frappe no lo comparte)."""
    settings = frappe.get_single("Chatwoot Settings")
    return {
        "url": (getattr(settings, "evolution_url", None) or "").rstrip("/"),
        "instancia": getattr(settings, "evolution_instance", None) or "",
        "api_key": settings.get_password("evolution_api_key", raise_exception=False)
        if getattr(settings, "evolution_api_key", None)
        else "",
    }


def _e164_mx(telefono: str) -> tuple[str, str]:
    """(dígitos, E.164). 10 dígitos se asumen mexicanos: es lo que el equipo
    teclea. Con lada de país ya incluida se respeta tal cual."""
    digitos = re.sub(r"\D", "", telefono or "")
    if len(digitos) == 10:
        digitos = "52" + digitos
    if not (10 <= len(digitos) <= 15):
        frappe.throw("Número inválido: escribe 10 dígitos (México) o el número con lada de país.")
    return digitos, "+" + digitos


@frappe.whitelist()
def resolver_whatsapp(phone: str) -> dict:
    """¿Este número existe en WhatsApp, y con qué JID?

    Se le pregunta a WhatsApp a través de Evolution en vez de construir el JID
    a mano: los números mexicanos llevan un "1" después del 52 que no todos
    tienen, y adivinarlo abre una conversación con un destinatario inexistente
    — el mensaje se queda en el limbo sin error visible.

    Si Evolution no responde, devuelve `verificado: False` en vez de fallar: se
    puede seguir adelante sin JID (Chatwoot resuelve por número), solo sin la
    garantía previa. Nunca bloquea al usuario por una dependencia caída.
    """
    validate_role()
    digitos, e164 = _e164_mx(phone)
    cfg = _config_evolution()
    if not (cfg["url"] and cfg["instancia"] and cfg["api_key"]):
        return {"verificado": False, "existe": None, "jid": None, "e164": e164,
                "motivo": "Evolution no configurado en Chatwoot Settings"}
    try:
        req = urllib.request.Request(
            f"{cfg['url']}/chat/whatsappNumbers/{cfg['instancia']}",
            data=json.dumps({"numbers": [digitos]}).encode(),
            headers={"Content-Type": "application/json", "apikey": cfg["api_key"]},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            datos = json.loads(resp.read().decode())
        fila = (datos or [{}])[0]
        return {
            "verificado": True,
            "existe": bool(fila.get("exists")),
            "jid": fila.get("jid"),
            "e164": e164,
        }
    except (urllib.error.URLError, TimeoutError, ValueError, IndexError) as exc:
        frappe.log_error(title="panel: Evolution no responde", message=str(exc))
        return {"verificado": False, "existe": None, "jid": None, "e164": e164,
                "motivo": f"Evolution no responde: {exc}"}


@frappe.whitelist()
def start_conversation(inbox_id: int, phone: str, message: str = None, name: str = None,
                       force: int = 0, adjuntos=None) -> dict:
    """Abre (o reutiliza) la conversación con ese número y manda el primer mensaje.

    Deja el agente IA PAUSADO: quien abre la conversación desde el CRM es una
    persona que se está haciendo cargo del hilo.

    `adjuntos` (opcional): lista JSON de `{url, nombre, mime}` — archivos ya
    subidos al sitio por el composer de la bandeja. Se mandan como media nativa;
    sin esto la imagen llega como enlace pegado al texto.
    """
    validate_role()
    adjuntos = _parse_adjuntos(adjuntos)
    message = (message or "").strip()
    if not message and not adjuntos:
        frappe.throw("Escribe el mensaje con el que quieres abrir la conversación.")
    return abrir_conversacion(inbox_id=inbox_id, phone=phone, message=message,
                              name=name, force=force, pausar=True, adjuntos=adjuntos)


@frappe.whitelist()
def asegurar_conversacion(inbox_id: int, phone: str, name: str = None,
                          force: int = 0) -> dict:
    """Crea (o reutiliza) la conversación SIN mandar mensaje.

    Existe por el "programar" del primer mensaje: `programar_mensaje` exige un
    `conversation_id` y en modo "nueva conversación" todavía no hay ninguno. Se
    crea el hilo y ahí se agenda. Es el único caso que deja un hilo sin mensajes
    — y es una acción explícita del usuario, no un efecto colateral.
    """
    validate_role()
    return abrir_conversacion(inbox_id=inbox_id, phone=phone, message=None,
                              name=name, force=force, pausar=True)


def _parse_adjuntos(adjuntos) -> list:
    if isinstance(adjuntos, str):
        try:
            adjuntos = json.loads(adjuntos or "[]")
        except ValueError:
            return []
    adjuntos = adjuntos or []
    if len(adjuntos) > MAX_ADJUNTOS:
        frappe.throw(f"Máximo {MAX_ADJUNTOS} adjuntos por mensaje")
    return adjuntos


def _enviar(conversation_id: int, message: str | None, adjuntos: list | None) -> None:
    """Manda un mensaje al hilo, como media nativa si trae adjuntos.

    Mismo camino que `api/chatwoot.send_message`: con la URL pegada al `content`
    el cliente ve un enlace, no la imagen (ver `chatwoot_client.leer_adjunto`).
    Se marca `humano` (con el usuario de la sesión) para que el agente lea el
    mensaje como palabra del equipo y la burbuja muestre quién lo escribió.
    Sin texto ni adjuntos no hace nada: es el caso de `asegurar_conversacion`.
    """
    marca = autoria.marca_humana()
    adjuntos = adjuntos or []
    if adjuntos:
        archivos = []
        for a in adjuntos:
            leido = cw.leer_adjunto(a.get("url") or a.get("archivo"))
            if leido:
                archivos.append(leido)
        if archivos:
            cw.create_message_with_attachments(
                conversation_id, message or "", archivos=archivos,
                content_attributes=marca)
            return
        if not message:
            frappe.throw("No se pudieron leer los adjuntos")
    if message:
        cw.create_message(conversation_id, message, content_attributes=marca)


def abrir_conversacion(inbox_id: int, phone: str, message: str = None,
                       name: str = None, force: int = 0,
                       pausar: bool = True, adjuntos: list | None = None) -> dict:
    """Abre (o reutiliza) la conversación con ese número.

    Reutiliza a propósito: si el contacto ya tiene un hilo abierto en ese inbox,
    escribe ahí en vez de abrir uno nuevo. Dos hilos con la misma persona es el
    escenario que hace que un cliente reciba dos respuestas distintas del mismo
    equipo — el problema que ya conocemos por GHL.

    `pausar=False` es para los avisos automáticos (bienvenida del formulario):
    ahí el agente IA debe poder seguir atendiendo, porque nadie del equipo se
    hizo cargo todavía. Sin verificación de rol: la llaman jobs del sistema,
    no la UI.

    `message` y `adjuntos` son opcionales: sin ninguno de los dos solo se
    asegura que el hilo exista (ver `asegurar_conversacion`).
    """
    inbox_id = frappe.utils.cint(inbox_id)
    message = (message or "").strip()
    if not inbox_id:
        frappe.throw("Falta el inbox.")

    digitos, e164 = _e164_mx(phone)
    verificacion = resolver_whatsapp(phone)
    if verificacion.get("verificado") and not verificacion.get("existe") and not frappe.utils.cint(force):
        frappe.throw(f"El número {e164} no tiene WhatsApp. Verifícalo antes de escribir.")
    jid = verificacion.get("jid")

    # WhatsApp México usa `+521` + 10 dígitos, pero `_e164_mx` arma `+52` + 10
    # (no adivina el "1" a propósito). Evolution sí resuelve el número real y
    # devuelve el JID; de ahí se saca la forma correcta. Si el contacto queda
    # guardado como `+52` sin el "1", Evolution no lo encuentra y los mensajes
    # ENTRANTES se pierden (bug del 2026-09-10, que el vigilante reparaba hasta
    # 10 min después). Por eso el alta usa el número del JID cuando lo hay.
    e164_whatsapp = e164
    if jid and jid.endswith("@s.whatsapp.net"):
        e164_whatsapp = "+" + jid.split("@")[0]

    # Chatwoot busca por el número tal cual esté guardado; se prueban las formas
    # en que puede haber quedado (con y sin +, con y sin el 1 de México).
    contacto = None
    for consulta in dict.fromkeys((e164_whatsapp, e164, digitos, digitos[-10:])):
        encontrados = cw.search_contacts(consulta)
        if encontrados:
            contacto = encontrados[0]
            break

    if contacto:
        contacto_id = contacto.get("id")
        detalle = cw.get_contact(contacto_id)
        buzones = detalle.get("contact_inboxes") or []
        source_id = next(
            (b.get("source_id") for b in buzones if ((b.get("inbox") or {}).get("id")) == inbox_id),
            None,
        )
        if not source_id:
            creado = cw.create_contact_inbox(contact_id=contacto_id, inbox_id=inbox_id, source_id=jid)
            source_id = (creado.get("payload") or creado).get("source_id") or jid
        # ¿Ya hay hilo abierto? Se escribe ahí.
        for conv in cw.get_conversations_for_contact(contacto_id):
            if conv.get("inbox_id") == inbox_id and conv.get("status") in ("open", "pending"):
                _enviar(conv["id"], message, adjuntos)
                if pausar:
                    _pause_conversation(conv["id"], inbox_id)
                return {"conversation_id": conv["id"], "reutilizada": True, "contact_id": contacto_id}
    else:
        nuevo = cw.create_contact(
            inbox_id=inbox_id,
            name=(name or "").strip() or e164,
            phone_number=e164_whatsapp,
            identifier=jid,
        )
        contacto_id = nuevo.get("id")
        buzones = nuevo.get("contact_inboxes") or []
        source_id = (buzones[0].get("source_id") if buzones else None) or jid

    if not source_id:
        frappe.throw("Chatwoot no devolvió el canal del contacto; no se puede abrir la conversación.")

    conv = cw.create_conversation(source_id=source_id, inbox_id=inbox_id, contact_id=contacto_id)
    conversation_id = conv.get("id") or (conv.get("payload") or {}).get("id")
    if not conversation_id:
        frappe.throw("Chatwoot no devolvió el id de la conversación creada.")

    _enviar(conversation_id, message, adjuntos)
    if pausar:
        _pause_conversation(conversation_id, inbox_id)
    return {"conversation_id": conversation_id, "reutilizada": False, "contact_id": contacto_id}
