"""Reemplaza los workflows 1.2 / 1.3 de GHL: qué pasa cuando alguien llena el
formulario del sitio.

En GHL esto era un árbol de 20 nodos por formulario. Lo que de verdad hacía,
medido sobre el árbol exportado (`workflows-ghl/rescate/workflows/`):

    acuse por correo → crear oportunidad → asignar vendedor → esperar 5 min
    (solo L-V 08:00-18:00) → WhatsApp "Saludo inicial" → WhatsApp del producto
    → esperar respuesta → "Retomar" a las 24 h

Aquí se replican los seis primeros. El "Retomar" a 24 h NO se replica: era el
arranque de la cola de seguimientos (workflow 4), que lleva en borrador desde
el 06-sep y cuyo reemplazo es el agente IA atendiendo el hilo. Replicar la
espera sin la cola dejaría un mensaje huérfano.

Dos diferencias deliberadas con GHL:

- **La conversación NO pausa al agente.** `panel.start_conversation` pausa
  porque ahí quien escribe es una persona que se está haciendo cargo. Aquí el
  saludo es automático y el agente debe poder seguir la conversación — es lo
  que hacía GHL, que en la rama PWP/EGT incluso activaba el agente explícito.
- **El lead nace con dueño.** En GHL el `assign_user` era un nodo aparte que
  podía no correr si el árbol fallaba antes; aquí es parte de la creación.
"""

import datetime
import json
import time
import urllib.error
import urllib.request

import frappe

from frappe_chatwoot.utils import telefono as tel

# Etiqueta y mensaje por producto. Los textos son los de las plantillas de GHL
# rescatadas (`plantillas-sms-formulario.txt`), literales salvo las imágenes:
# esas vivían en el storage de la subcuenta y se rehospedaron en lavendi.mx.
PRODUCTOS = {
    "producto_pvp": {
        "etiqueta": "Curso Taller de Ventas Premium",
        "mensaje": "Producto de interés: *Curso Taller de Ventas Premium*📊\nhttps://lavendi.mx/proceso-de-ventas/",
        "valor": 17500,
    },
    "producto_sgpt": {
        "etiqueta": "SofIA CRM + Agente IA",
        "mensaje": "Producto de interés: *SofÍA GPT — CRM + Agente IA*🤖\nhttps://lavendi.mx/sofia-gpt/",
        "valor": 0,
    },
    "producto_web": {
        "etiqueta": "Diseño/Desarrollo Web",
        "mensaje": "*Página Web Premium*\nhttps://lavendi.mx/transformaciondigital-2",
        "valor": 0,
    },
    "producto_ads": {
        "etiqueta": "Google Ads",
        "mensaje": "Google Ads 📈\nhttps://lavendi.mx/google-ads-2",
        "valor": 0,
    },
    "producto_otro": {"etiqueta": "Otro", "mensaje": None, "valor": 0},
}

SALUDO = (
    "Hola {nombre}, 👋🏼\n\n"
    "Mi nombre es Sofía, parte del equipo de lavendi.mx - Desarrolladores de "
    "modelos de negocio en línea y ventas.\n\n"
    "Estoy respondiendo a tu solicitud de contacto a través de nuestra página web, "
    "¿Es correcto? 🙂\n\n"
    "Producto de interés:\n{productos}"
)

# Alejandro es el propietario de todas las oportunidades de Sofía CRM (decisión
# suya, 2026-09-18). Importa más de lo que parece: al convertir un lead,
# `CRM Lead.LEAD_DEAL_FIELD_MAP` copia `lead_owner` a `deal_owner`, así que este
# default define también de quién es la oportunidad que nace de ese lead.
# Propiedad ≠ asignación: quién trabaja cada caso sigue siendo el `_assign`.
LEAD_OWNER_DEFAULT = "alejandro.moreno@lavendi.mx"


def _lead_owner_default():
    # `alejandro.moreno@lavendi.mx` no existe en todos los sitios de cliente
    # (ej. sixgardens/estrublock) — usarlo ahí revienta el Link con 417. Cada
    # sitio puede declarar su propio dueño en site_config.json
    # (`"lead_owner_default": "usuario@dominio"`); sin esa clave el
    # comportamiento es idéntico al de siempre (byte a byte).
    return frappe.conf.get("lead_owner_default") or LEAD_OWNER_DEFAULT

# Ventana en la que GHL dejaba salir el saludo: L-V 08:00-18:00. Un lead que
# llega un sábado a las 23:00 recibe su WhatsApp el lunes a las 08:00, no de
# madrugada.
VENTANA_DIAS = (0, 1, 2, 3, 4)
VENTANA_INICIO = datetime.time(8, 0)
VENTANA_FIN = datetime.time(18, 0)
ESPERA_MINUTOS = 5

# Pausa entre el saludo y cada mensaje de producto. Enviados en el mismo
# instante, Evolution/Baileys no garantiza el orden de entrega: el lead podía
# ver "Producto de interés: ..." antes del saludo de Sofía. Peor caso por
# corrida: LOTE_MAX * (1 saludo + 4 productos) * 3 s = 150 s, holgado dentro
# del cron de 5 min.
PAUSA_ENTRE_MENSAJES = 3
LOTE_MAX = 10


def _productos_de(doc):
    return [k for k in PRODUCTOS if getattr(doc, k, 0)]


def _etiquetas(doc):
    return [PRODUCTOS[k]["etiqueta"] for k in _productos_de(doc)]


def _telefono_corto(telefono):
    """Delega en `utils.telefono.corto` (2026-09-23): mismo criterio en todo el
    CRM para no duplicar la expresión regular."""
    return tel.corto(telefono)


# "Aquí ya no se trabaja". Un match contra uno de estos NO bloquea la creación
# del lead: un prospecto perdido que vuelve a llenar el formulario es una
# oportunidad nueva, no un duplicado. Medido en la base el 2026-09-21: de 3,773
# deals, 3,306 están en `Lost` (la base migrada de GHL completa), y **56
# teléfonos tienen a la vez un deal cerrado y uno abierto** — el equipo ya abría
# oportunidad nueva a mano cuando un perdido regresaba. El dedup existe para no
# duplicar trabajo ABIERTO, no para enterrar re-consultas.
#
# Se deriva del catálogo (`CRM Deal Status.type` / `CRM Lead Status.type`) y no
# de una lista fija, para que un estatus nuevo que el equipo agregue quede
# clasificado solo. Ojo con el mapeo, no es obvio:
#   · `type == "Lost"`  → cerrado en ambos (Deal `Lost`; Lead `Junk`/`Unqualified`).
#   · `type == "Won"`   → cerrado SOLO en Deal. En Lead, `Won` marca
#     `Converted` y `Qualified`, que son trabajo VIVO — un lead calificado con
#     el que el equipo está hablando (17 en la base) tratado como cerrado
#     generaría un duplicado, justo lo contrario de lo que se busca. Si está
#     `Converted` ya tiene su Deal, y el Deal gana por precedencia de todos modos.
TIPOS_CERRADOS = {"CRM Deal": ("Lost", "Won"), "CRM Lead": ("Lost",)}
DOCTYPE_ESTATUS = {"CRM Deal": "CRM Deal Status", "CRM Lead": "CRM Lead Status"}


def _cerrados(doctype):
    """Estatus cerrados de un doctype, leídos del catálogo en cada llamada.

    Sin caché a propósito: son 16 filas y esto corre una vez por solicitud del
    formulario (4 en los primeros 10 días del canal). Cachearlo solo abriría la
    puerta a que el catálogo cambie y el dedup siga con la lista vieja.
    """
    return [f.name for f in frappe.get_all(
        DOCTYPE_ESTATUS[doctype],
        filters={"type": ["in", TIPOS_CERRADOS[doctype]]}, fields=["name"])]


def _buscar_existente(telefono, email):
    """Misma regla que `lib/opportunities.js` del agente: la identidad es el
    teléfono (el 79% de la base no tiene correo) y el Deal gana sobre el Lead,
    porque si ya hay oportunidad abierta ahí es donde el equipo trabaja.

    Devuelve `(doctype, name, abierto)`. Un match **cerrado** se devuelve igual
    —sirve para la nota cruzada— pero con `abierto=False`; el llamador crea el
    lead nuevo de todos modos. Prioridad: un registro abierto siempre gana sobre
    uno cerrado, sin importar en qué orden aparezcan.
    """
    corto = _telefono_corto(telefono)
    criterios = []
    if corto:
        criterios.append({"mobile_no": ["like", f"%{corto}"]})
    if email:
        criterios.append({"email": email})

    # Dos pasadas y no una: el abierto se pide con filtro explícito de estatus
    # para que un contacto con varios registros cerrados no lo deje fuera.
    for filtros in criterios:
        for doctype in ("CRM Deal", "CRM Lead"):
            abierto = frappe.get_all(
                doctype,
                filters=dict(filtros, status=["not in", _cerrados(doctype)]),
                fields=["name"], limit=1)
            if abierto:
                return doctype, abierto[0].name, True
    for filtros in criterios:
        for doctype in ("CRM Deal", "CRM Lead"):
            hallado = frappe.get_all(doctype, filters=filtros,
                                     fields=["name"], limit=1)
            if hallado:
                return doctype, hallado[0].name, False
    return None, None, False


def _asegurar_contacto(doc):
    corto = _telefono_corto(doc.telefono)
    if corto:
        ya = frappe.get_all("Contact", filters={"mobile_no": ["like", f"%{corto}"]},
                            fields=["name"], limit=1)
        if ya:
            return ya[0].name
    # Guardar el teléfono en su forma canónica E.164 (`+52` + 10) en vez del
    # texto crudo del formulario: es el origen del duplicado `+52...` vs
    # `521...` que dejó a un mismo cliente con dos fichas (2026-09-23).
    telefono_norm = tel.e164_mx(doc.telefono) or doc.telefono
    partes = (doc.nombre or "").strip().split(" ", 1)
    contacto = frappe.get_doc({
        "doctype": "Contact",
        "first_name": partes[0] or "Sin nombre",
        "last_name": partes[1] if len(partes) > 1 else "",
        "mobile_no": telefono_norm,
    })
    if doc.email:
        contacto.append("email_ids", {"email_id": doc.email, "is_primary": 1})
    if telefono_norm:
        contacto.append("phone_nos", {"phone": telefono_norm, "is_primary_mobile_no": 1})
    contacto.insert(ignore_permissions=True)
    return contacto.name


def _crear_ficha(contacto, *, nombre="", email="", telefono="", source="Formulario web",
                 valor=None, organization_name=None, atribucion=None,
                 first_name=None, last_name=None):
    """Inserta la ficha del embudo y devuelve `(doctype, name)`.

    Crea **`CRM Deal`** (etapa `Lead`) cuando hay contacto, que es el caso
    normal. Cae a **`CRM Lead`** solo cuando no lo hay: `CRM Deal` deriva
    `mobile_no`, `email` y `deal_name` del contacto primario
    (`set_primary_email_mobile_no` / `set_deal_name` en `crm_deal.py`), así que
    un Deal sin contacto nace sin teléfono y sin nombre visible — inservible
    para localizar al prospecto. `CRM Lead` sí tiene campos propios y sobrevive
    sin contacto.

    Existe desde el 2026-09-22 (Bloque 2, "Solo Oportunidades"): antes cada
    productor creaba un `CRM Lead` que había que convertir a mano para entrar al
    embudo y a las secuencias — de ahí los 603 leads acumulados y los prospectos
    sin oportunidad (caso Jorge Fonk).
    """
    atribucion = atribucion or {}
    if first_name is not None:
        completo = " ".join(x for x in (first_name, last_name) if x).strip()
    else:
        completo = (nombre or "").strip()
        partes = completo.split(" ", 1)
        first_name = partes[0] if partes else ""
        last_name = partes[1] if len(partes) > 1 else ""

    campos = {
        "first_name": first_name,
        "last_name": last_name,
        "lead_name": completo,
        "email": (email or "").strip() or None,
        "mobile_no": (telefono or "").strip() or None,
        "source": source,
    }
    for campo, valor_campo in (("utm_source", atribucion.get("utm_source")),
                               ("utm_campaign", atribucion.get("utm_campaign")),
                               ("utm_term", atribucion.get("utm_term")),
                               ("gclid_ads", atribucion.get("gclid"))):
        if valor_campo:
            campos[campo] = valor_campo

    if contacto:
        campos.update({
            "doctype": "CRM Deal",
            "contact": contacto,
            "contacts": [{"contact": contacto, "is_primary": 1}],
            "status": "Lead",
            "deal_owner": _lead_owner_default(),
            # El título de la oportunidad es la persona, no la empresa: sin este
            # `deal_name` explícito, `set_deal_name()` prefiere `organization_name`
            # y la lista mostraría el nombre de la empresa en vez del prospecto.
            "deal_name": completo,
        })
        if valor:
            campos["deal_value"] = valor
        if organization_name:
            # En Deal `organization` es un Link a `CRM Organization`; el nombre
            # suelto que trae una reserva o un formulario va en el campo de texto
            # `organization_name` para no inventar una organización.
            campos["organization_name"] = organization_name
        ficha = frappe.get_doc(campos)
        ficha.insert(ignore_permissions=True)
        return "CRM Deal", ficha.name

    campos.update({
        "doctype": "CRM Lead",
        "contact": None,
        "status": "New",
        "lead_owner": _lead_owner_default(),
    })
    if organization_name:
        campos["organization"] = organization_name
    ficha = frappe.get_doc(campos)
    ficha.insert(ignore_permissions=True)
    return "CRM Lead", ficha.name


def _crear_oportunidad(doc, cerrado=None):
    etiquetas = _etiquetas(doc)
    valor = max([PRODUCTOS[k]["valor"] for k in _productos_de(doc)] or [0])
    contacto = _asegurar_contacto(doc)
    ficha_dt, ficha = _crear_ficha(
        contacto,
        nombre=doc.nombre,
        email=doc.email,
        telefono=doc.telefono,
        valor=valor,
        atribucion={"utm_source": doc.utm_source, "utm_campaign": doc.utm_campaign,
                    "utm_term": doc.utm_term, "gclid": doc.gclid},
    )

    # Referencia cruzada cuando el prospecto ya había pasado por aquí y su único
    # registro estaba cerrado. Sin esto la ficha nueva parece un contacto virgen
    # y se pierde todo el historial de la vez anterior.
    historial = ""
    if cerrado and cerrado[0]:
        historial = (f"<p>⟲ Ya había estado en el CRM: <b>{cerrado[0]} {cerrado[1]}</b> "
                     "(cerrado). Se abrió oportunidad nueva porque volvió a "
                     "solicitar por el formulario.</p>")
        frappe.get_doc({
            "doctype": "FCRM Note",
            "title": "El prospecto volvió a solicitar por el formulario",
            "content": (
                f"<p>Nueva solicitud <b>{doc.name}</b> el {frappe.utils.now()}.</p>"
                f"<p>Se abrió <b>{ficha_dt} {ficha}</b>; este registro se deja "
                "como está.</p>"
                f"<p>Producto(s) de interés: {', '.join(etiquetas) or 'no especificado'}</p>"
            ),
            "reference_doctype": cerrado[0],
            "reference_docname": cerrado[1],
        }).insert(ignore_permissions=True)

    if etiquetas or historial:
        frappe.get_doc({
            "doctype": "FCRM Note",
            "title": "Solicitud desde el formulario del sitio",
            "content": (
                f"<p>Producto(s) de interés: <b>{', '.join(etiquetas) or 'no especificado'}</b></p>"
                f"<p>Página: {doc.pagina_origen or 'no registrada'}</p>"
                f"<p>Solicitud: {doc.name}</p>"
                f"{historial}"
            ),
            "reference_doctype": ficha_dt,
            "reference_docname": ficha,
        }).insert(ignore_permissions=True)
    return ficha_dt, ficha, contacto


def _registrar_resolicitud(doc, doctype, name):
    """El contacto ya tiene trabajo VIVO: no se duplica la oportunidad, pero la
    re-solicitud tiene que verse donde el equipo trabaja.

    Antes de 2026-09-21 esta rama no dejaba ningún rastro en el CRM — ni nota,
    ni tarea, ni aviso: el único registro era un texto dentro de la propia
    `Solicitud Web`, que nadie abre. Caso real: SOL-2026-00040, el deal quedó
    con `modified` intacto del día anterior.
    """
    etiquetas = _etiquetas(doc)
    frappe.get_doc({
        "doctype": "FCRM Note",
        "title": "Nueva solicitud del formulario (ya tenía oportunidad abierta)",
        "content": (
            f"<p>Producto(s) de interés: <b>{', '.join(etiquetas) or 'no especificado'}</b></p>"
            f"<p>Página: {doc.pagina_origen or 'no registrada'}</p>"
            f"<p>Solicitud: {doc.name}</p>"
            "<p>No se creó oportunidad nueva porque esta ya está abierta. "
            "Vale revisar si pide algo distinto a lo que ya se le está "
            "atendiendo.</p>"
        ),
        "reference_doctype": doctype,
        "reference_docname": name,
    }).insert(ignore_permissions=True)

    campo_dueno = "deal_owner" if doctype == "CRM Deal" else "lead_owner"
    dueno = frappe.db.get_value(doctype, name, campo_dueno) or _lead_owner_default()
    tarea = frappe.get_doc({
        "doctype": "CRM Task",
        "title": f"Volvió a llenar el formulario: {doc.nombre or 'sin nombre'}",
        "description": (
            f"Producto(s): {', '.join(etiquetas) or 'no especificado'} · "
            f"Página: {doc.pagina_origen or 'no registrada'} · Solicitud: {doc.name}"
        ),
        "status": "Backlog",
        "priority": "High",
        "assigned_to": dueno,
        "reference_doctype": doctype,
        "reference_docname": name,
    })
    tarea.insert(ignore_permissions=True)
    return tarea.name


def on_solicitud_insert(doc, method=None):
    """Convierte la solicitud en Contacto + Oportunidad. Nunca lanza: si algo
    falla, la solicitud queda en la bandeja con el error escrito y un humano la
    recupera — es justo el caso que el formulario de GHL no cubría, donde un
    fallo se veía como "no hubo leads"."""
    try:
        existente_dt, existente, abierto = _buscar_existente(doc.telefono, doc.email)
        if existente and abierto:
            doc.db_set("procesado", 1, update_modified=False)
            doc.db_set("nota_proceso",
                       f"Vinculada a {existente_dt} {existente}, que ya está "
                       "abierto; no se duplicó la oportunidad",
                       update_modified=False)
            if existente_dt == "CRM Lead":
                doc.db_set("crm_lead", existente, update_modified=False)
            elif existente_dt == "CRM Deal":
                doc.db_set("crm_deal", existente, update_modified=False)
            _registrar_resolicitud(doc, existente_dt, existente)
        else:
            # Sin match, o con match solo en registros CERRADOS: en ambos casos
            # se crea la ficha. Un prospecto perdido que vuelve a llenar el
            # formulario es una oportunidad nueva, no un duplicado — así lo
            # hacía el equipo a mano en GHL (56 teléfonos con un deal cerrado y
            # uno abierto a la vez, medido el 2026-09-21).
            cerrado = (existente_dt, existente) if existente else None
            ficha_dt, ficha, contacto = _crear_oportunidad(doc, cerrado=cerrado)
            doc.db_set("crm_deal" if ficha_dt == "CRM Deal" else "crm_lead",
                       ficha, update_modified=False)
            doc.db_set("crm_contacto", contacto, update_modified=False)
            doc.db_set("procesado", 1, update_modified=False)
            if cerrado:
                doc.db_set("nota_proceso",
                           f"Ya había estado en el CRM ({cerrado[0]} {cerrado[1]}, "
                           f"cerrado); se abrió oportunidad nueva",
                           update_modified=False)
    except Exception as exc:
        frappe.log_error(f"captación {doc.name}: {exc}", "Solicitud Web")
        doc.db_set("error_proceso", str(exc)[:500], update_modified=False)

    # Trampa anti-bot: YA NO DESCARTA, solo marca para revisión. El 15-sep el
    # autofill del navegador la llenó en un lead real (SOL-2026-00029, Alexis
    # Solano) y se perdió en silencio. Un bot que llega ocasionalmente se revisa
    # y se borra; un lead perdido cuesta $17,500. El lead se crea igual y entra
    # al flujo normal; la marca solo pide que un humano lo mire.
    if doc.referencia_adicional:
        marca = "⚠ Revisar: campo trampa lleno (posible bot o autofill del navegador)"
        previo = frappe.db.get_value("Solicitud Web", doc.name, "error_proceso") or ""
        doc.db_set("error_proceso", f"{marca} · {previo}" if previo else marca,
                   update_modified=False)

    # Correo de acuse + aviso interno salen del host: el service account de
    # Google no está dentro de este contenedor.
    _avisar_al_host(doc)


def _avisar_al_host(doc):
    settings = frappe.get_single("Chatwoot Settings")
    url = getattr(settings, "onboarding_url", None)
    token = (settings.get_password("onboarding_token", raise_exception=False)
             if getattr(settings, "onboarding_token", None) else None)
    if not (url and token):
        frappe.log_error(f"captación {doc.name}: sin onboarding_url/token, "
                         "no se mandó acuse ni aviso interno", "Solicitud Web")
        return
    payload = {
        "solicitud": doc.name,
        "nombre": doc.nombre,
        "email": doc.email,
        "telefono": doc.telefono,
        "productos": _etiquetas(doc),
        "pagina": doc.pagina_origen,
        # `lead` se conserva por compatibilidad con el host; desde 2026-09-22 la
        # ficha normal es `deal` y `lead` solo se llena en el fallback sin contacto.
        "lead": doc.crm_lead,
        "deal": doc.crm_deal,
        # Sin ficha pero ya reconocido (tenía oportunidad ABIERTA) NO es un fallo:
        # es el caso bueno de no duplicar. El host no lo trata como avería, pero
        # desde 2026-09-21 tampoco lo silencia — se avisa distinto, porque una
        # re-solicitud de alguien a quien ya se le está atendiendo es
        # información comercial, no ruido.
        "vinculado_a": (frappe.db.get_value("Solicitud Web", doc.name, "nota_proceso")
                        if doc.procesado and not (doc.crm_lead or doc.crm_deal) else None),
        "utm_source": doc.utm_source,
        "gclid": doc.gclid,
    }
    req = urllib.request.Request(
        f"{url}/crm/solicitud-web",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-onboarding-token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            resp.read()
    except Exception as exc:
        frappe.log_error(f"captación {doc.name}: el host no recibió el aviso: {exc}",
                         "Solicitud Web")


# ---------------------------------------------------------------------------
# WhatsApp de bienvenida — job cada 5 min
# ---------------------------------------------------------------------------

def _en_ventana(ahora=None):
    ahora = ahora or frappe.utils.now_datetime()
    return (ahora.weekday() in VENTANA_DIAS
            and VENTANA_INICIO <= ahora.time() <= VENTANA_FIN)


def enviar_bienvenidas():
    """Manda el saludo inicial y el mensaje del producto a las solicitudes que
    ya cumplieron los 5 minutos de espera, dentro de la ventana hábil.

    Barrido sin estado, igual que `recordatorios.py`: sobrevive a reinicios,
    que es exactamente donde se rompía la espera dentro de un workflow."""
    if not _en_ventana():
        return

    settings = frappe.get_single("Chatwoot Settings")
    if not frappe.utils.cint(getattr(settings, "bienvenida_formulario_activa", 0)):
        return

    limite = frappe.utils.add_to_date(frappe.utils.now_datetime(),
                                      minutes=-ESPERA_MINUTOS)
    pendientes = frappe.get_all(
        "Solicitud Web",
        filters={"procesado": 1, "bienvenida_enviada_at": ["is", "not set"],
                 "creation": ["<=", limite]},
        fields=["name"], limit=LOTE_MAX, order_by="creation asc")

    for fila in pendientes:
        doc = frappe.get_doc("Solicitud Web", fila.name)
        try:
            _enviar_bienvenida(doc)
            doc.db_set("bienvenida_enviada_at", frappe.utils.now(), update_modified=False)
        except Exception as exc:
            frappe.log_error(f"bienvenida {doc.name}: {exc}", "Solicitud Web")
            # Se marca igual para que un número inválido no reintente para
            # siempre; el error queda escrito para revisión.
            doc.db_set("bienvenida_enviada_at", frappe.utils.now(), update_modified=False)
            doc.db_set("error_proceso", f"bienvenida: {exc}"[:500], update_modified=False)
    frappe.db.commit()


def _enviar_bienvenida(doc):
    from ..frappe_chatwoot.api import panel

    inbox_id = frappe.utils.cint(getattr(frappe.get_single("Chatwoot Settings"),
                                         "inbox_formulario", 0)) or 5
    etiquetas = _etiquetas(doc)
    saludo = SALUDO.format(nombre=(doc.nombre or "").split(" ")[0],
                           productos="\n".join(etiquetas) or "No especificado")

    conv = panel.abrir_conversacion(inbox_id=inbox_id, phone=doc.telefono,
                                    name=doc.nombre, pausar=False)
    conversation_id = conv["conversation_id"]
    from ..utils import autoria
    from ..utils import chatwoot_client as cw
    cw.create_message(conversation_id, saludo,
                      content_attributes=autoria.marca_automatica(
                          "bienvenida", "Bienvenida del formulario web"))
    for clave in _productos_de(doc):
        mensaje = PRODUCTOS[clave]["mensaje"]
        if mensaje:
            time.sleep(PAUSA_ENTRE_MENSAJES)
            cw.create_message(conversation_id, mensaje,
                              content_attributes=autoria.marca_automatica(
                                  "bienvenida", "Bienvenida del formulario web — producto"))

    # La liga al hilo de bienvenida se escribe en la ficha (Deal normal, Lead en
    # el fallback sin contacto). Es la marca que impide que `agente-ia` abra una
    # oportunidad duplicada cuando el prospecto conteste por primera vez.
    for doctype, name in (("CRM Deal", doc.crm_deal), ("CRM Lead", doc.crm_lead)):
        if name and frappe.db.exists(doctype, name):
            frappe.db.set_value(doctype, name,
                                "chatwoot_conversation_id", str(conversation_id),
                                update_modified=False)
    doc.db_set("chatwoot_conversation_id", str(conversation_id),
               update_modified=False)
