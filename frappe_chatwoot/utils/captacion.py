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
    digitos = "".join(c for c in (telefono or "") if c.isdigit())
    return digitos[-10:] if len(digitos) >= 10 else None


def _buscar_existente(telefono, email):
    """Misma regla que `lib/opportunities.js` del agente: la identidad es el
    teléfono (el 79% de la base no tiene correo) y el Deal gana sobre el Lead,
    porque si ya hay oportunidad abierta ahí es donde el equipo trabaja."""
    corto = _telefono_corto(telefono)
    if corto:
        for doctype in ("CRM Deal", "CRM Lead"):
            hallado = frappe.get_all(doctype, filters={"mobile_no": ["like", f"%{corto}"]},
                                     fields=["name"], limit=1)
            if hallado:
                return doctype, hallado[0].name
    if email:
        for doctype in ("CRM Deal", "CRM Lead"):
            hallado = frappe.get_all(doctype, filters={"email": email},
                                     fields=["name"], limit=1)
            if hallado:
                return doctype, hallado[0].name
    return None, None


def _asegurar_contacto(doc):
    corto = _telefono_corto(doc.telefono)
    if corto:
        ya = frappe.get_all("Contact", filters={"mobile_no": ["like", f"%{corto}"]},
                            fields=["name"], limit=1)
        if ya:
            return ya[0].name
    partes = (doc.nombre or "").strip().split(" ", 1)
    contacto = frappe.get_doc({
        "doctype": "Contact",
        "first_name": partes[0] or "Sin nombre",
        "last_name": partes[1] if len(partes) > 1 else "",
        "mobile_no": doc.telefono,
    })
    if doc.email:
        contacto.append("email_ids", {"email_id": doc.email, "is_primary": 1})
    if doc.telefono:
        contacto.append("phone_nos", {"phone": doc.telefono, "is_primary_mobile_no": 1})
    contacto.insert(ignore_permissions=True)
    return contacto.name


def _crear_lead(doc):
    etiquetas = _etiquetas(doc)
    valor = max([PRODUCTOS[k]["valor"] for k in _productos_de(doc)] or [0])
    contacto = _asegurar_contacto(doc)
    lead = frappe.get_doc({
        "doctype": "CRM Lead",
        "first_name": doc.nombre,
        "lead_name": doc.nombre,
        "status": "New",
        "lead_owner": LEAD_OWNER_DEFAULT,
        "email": doc.email,
        "mobile_no": doc.telefono,
        "contact": contacto,
        "source": "Formulario web",
        # Campos de trazabilidad que existen desde la migración de GHL.
        "ghl_source": "Formulario web",
    })
    if valor:
        lead.deal_value = valor
    for campo, valor_campo in (("utm_source", doc.utm_source),
                               ("utm_campaign", doc.utm_campaign),
                               ("utm_term", doc.utm_term),
                               ("gclid_ads", doc.gclid)):
        if valor_campo and hasattr(lead, campo):
            setattr(lead, campo, valor_campo)
    lead.insert(ignore_permissions=True)

    if etiquetas:
        frappe.get_doc({
            "doctype": "FCRM Note",
            "title": "Solicitud desde el formulario del sitio",
            "content": (
                f"<p>Producto(s) de interés: <b>{', '.join(etiquetas)}</b></p>"
                f"<p>Página: {doc.pagina_origen or 'no registrada'}</p>"
                f"<p>Solicitud: {doc.name}</p>"
            ),
            "reference_doctype": "CRM Lead",
            "reference_docname": lead.name,
        }).insert(ignore_permissions=True)
    return lead.name, contacto


def on_solicitud_insert(doc, method=None):
    """Convierte la solicitud en Contacto + Lead. Nunca lanza: si algo falla,
    la solicitud queda en la bandeja con el error escrito y un humano la
    recupera — es justo el caso que el formulario de GHL no cubría, donde un
    fallo se veía como "no hubo leads"."""
    try:
        existente_dt, existente = _buscar_existente(doc.telefono, doc.email)
        if existente:
            doc.db_set("procesado", 1, update_modified=False)
            doc.db_set("error_proceso",
                       f"Ya existía {existente_dt} {existente}; no se creó lead nuevo",
                       update_modified=False)
            if existente_dt == "CRM Lead":
                doc.db_set("crm_lead", existente, update_modified=False)
        else:
            lead, contacto = _crear_lead(doc)
            doc.db_set("crm_lead", lead, update_modified=False)
            doc.db_set("crm_contacto", contacto, update_modified=False)
            doc.db_set("procesado", 1, update_modified=False)
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
        "lead": doc.crm_lead,
        # Sin lead pero ya reconocido (tenía Deal abierto) NO es un fallo: es el
        # caso bueno de no duplicar. El host solo debe alertar cuando la
        # solicitud quedó realmente huérfana.
        "vinculado_a": doc.error_proceso if doc.procesado and not doc.crm_lead else None,
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

    if doc.crm_lead and frappe.db.exists("CRM Lead", doc.crm_lead):
        frappe.db.set_value("CRM Lead", doc.crm_lead,
                            "chatwoot_conversation_id", str(conversation_id),
                            update_modified=False)
    doc.db_set("chatwoot_conversation_id", str(conversation_id),
               update_modified=False)
