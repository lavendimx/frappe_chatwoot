"""Onboarding de proyecto ganado — lado CRM.

Reemplaza el disparador del workflow 6 de GHL ("Activación de proyecto -
Onboarding"), que hoy corre cuando una oportunidad pasa a Ganada. Aquel usaba
el trigger nativo de GHL; aquí es un `doc_events` sobre `CRM Deal`.

Reparto con el servicio del host (`crm-onboarding`, :4352):

  - Aquí: detectar la transición a Ganada, marcar al contacto como cliente
    (equivale al workflow "Tipo de contacto (cliente/lead)") y mandar el
    WhatsApp de bienvenida. El WhatsApp sale de este lado porque
    `chatwoot.send_message` además PAUSA al agente IA en esa conversación —
    mandarlo desde el host llegaría igual, pero Sofía seguiría intentando
    venderle a alguien que ya compró.
  - En el host: Notion (Cliente + Proyecto + tarea de arranque), aviso interno
    al equipo y correo de bienvenida. Vive allá porque el service account de
    Google y el token de Notion no están dentro de este contenedor, y montarlos
    aquí daría acceso a todo el Workspace de la agencia.

Idempotencia: `onboarding_enviado_at` en el propio deal. Frappe dispara
`on_update` varias veces por guardado y el equipo puede reabrir y volver a
cerrar una oportunidad; sin esta marca el cliente recibiría la bienvenida cada
vez.
"""

import json
import urllib.error
import urllib.request

import frappe

TIMEOUT = 60  # Notion (varias llamadas) + Gmail; 10s se queda corto.

# El servicio del host no adivina el producto y este lado tampoco: si no se
# reconoce, se crea el proyecto pero NO se manda bienvenida. Mandarle el
# material de PVP a quien compró una página web es peor que no mandar nada.
MAPEO_PRODUCTO = {
    "curso taller de ventas premium": "PVP",
    "sofia crm + agente ia": "SGPT",
    "sofía crm + agente ia": "SGPT",
    "sofia gpt": "SGPT",
    "sofía gpt": "SGPT",
    "diseño/desarrollo web": "PWP",
    "diseno/desarrollo web": "PWP",
    "google ads": "EGT",
    "curso taller de ia para empresas": "TLP",
}


def _config():
    """URL y token del servicio de onboarding, como custom fields de Chatwoot
    Settings — mismo criterio que la config de agenda y de Evolution."""
    settings = frappe.get_single("Chatwoot Settings")
    return {
        "url": (getattr(settings, "onboarding_url", None) or "").rstrip("/"),
        "token": settings.get_password("onboarding_token", raise_exception=False)
        if getattr(settings, "onboarding_token", None)
        else "",
    }


def _productos(doc):
    """Lista de códigos (PVP/SGPT/...) del deal.

    Prefiere `producto_contratado`, que es lo que se vendió. Si está vacío cae
    a `producto_principal`, que es el campo de interés migrado de GHL y suele
    traer varios separados por coma ("Curso Taller de Ventas Premium, SofIA
    CRM + Agente IA"). Lo que no reconoce, lo descarta: es preferible quedarse
    corto y que una persona lo complete, a inventar un producto."""
    directo = (getattr(doc, "producto_contratado", None) or "").strip()
    if directo:
        return [p.strip() for p in directo.split(",") if p.strip()]

    crudo = (getattr(doc, "producto_principal", None) or "")
    codigos = []
    for parte in crudo.split(","):
        cod = MAPEO_PRODUCTO.get(parte.strip().lower())
        if cod and cod not in codigos:
            codigos.append(cod)
    return codigos


def _email(doc):
    """`CRM Deal.email` es un campo derivado del contacto primario y llega
    vacío cuando el contacto no tenía correo al crearse el deal — pasa seguido
    con los que entran por WhatsApp. Se cae al Contact, que es la fuente."""
    directo = (getattr(doc, "email", None) or "").strip()
    if directo:
        return directo
    if doc.contact and frappe.db.exists("Contact", doc.contact):
        return (frappe.db.get_value("Contact", doc.contact, "email_id") or "").strip()
    return ""


def _nombre_contacto(doc):
    nombre = " ".join(x for x in [(doc.first_name or "").strip(),
                                  (doc.last_name or "").strip()] if x).strip()
    return nombre or (doc.contact or "").strip()


def on_deal_update(doc, method=None):
    """Hook `CRM Deal.on_update`. Barato en el caso normal: sale en la primera
    condición para cualquier guardado que no sea una transición a Ganada."""
    if doc.status != "Won":
        return
    if getattr(doc, "onboarding_enviado_at", None):
        return
    # `status_change_log` es el registro nativo del CRM. Un deal que se guarda
    # estando ya en Won (por cualquier otro cambio) no debe disparar nada.
    anterior = doc.get_doc_before_save()
    if anterior is not None and anterior.status == "Won":
        return

    frappe.enqueue(
        "frappe_chatwoot.utils.onboarding.ejecutar",
        queue="long",
        timeout=300,
        deal_name=doc.name,
        enqueue_after_commit=True,
    )


def ejecutar(deal_name):
    """Corre en background: nada de esto debe hacer esperar al vendedor que
    acaba de marcar la venta como ganada."""
    doc = frappe.get_doc("CRM Deal", deal_name)
    if getattr(doc, "onboarding_enviado_at", None):
        return {"ok": True, "saltado": "ya se había ejecutado"}

    productos = _productos(doc)
    contacto = _nombre_contacto(doc)
    empresa = (doc.organization or getattr(doc, "organization_name", "") or "").strip()
    resultado = {"deal": deal_name, "productos": productos}

    # 1. Marcar el contacto como cliente — es el workflow "Tipo de contacto
    #    (cliente/lead)" de GHL, que se replica aquí en vez de aparte.
    try:
        if doc.contact and frappe.db.exists("Contact", doc.contact):
            _marcar_cliente(doc.contact)
            resultado["contacto_marcado"] = True
    except Exception as exc:
        frappe.log_error(f"onboarding {deal_name}: no se pudo marcar el contacto como cliente: {exc}",
                         "CRM onboarding")

    # 2. WhatsApp de bienvenida (pausa al agente IA en esa conversación).
    resultado["whatsapp"] = _whatsapp_bienvenida(doc, productos)

    # 3. Host: Notion + aviso interno + correo de bienvenida.
    resultado["host"] = _avisar_al_host(doc, contacto, empresa, productos)

    frappe.db.set_value("CRM Deal", deal_name, "onboarding_enviado_at",
                        frappe.utils.now_datetime(), update_modified=False)
    frappe.db.commit()
    return resultado


def _marcar_cliente(contact_name):
    """El equivalente de `contact.type = customer` de GHL. En este CRM el
    marcador vivo es la etiqueta: los Contacts migrados no tienen un campo de
    tipo, y agregar uno solo para esto sería inventar esquema."""
    doc = frappe.get_doc("Contact", contact_name)
    tags = frappe.get_all("Tag Link", filters={"document_type": "Contact",
                                               "document_name": contact_name,
                                               "tag": "cliente actual"}, limit=1)
    if not tags:
        if not frappe.db.exists("Tag", "cliente actual"):
            frappe.get_doc({"doctype": "Tag", "__newname": "cliente actual"}).insert(
                ignore_permissions=True)
        frappe.get_doc({
            "doctype": "Tag Link",
            "parenttype": "Contact",
            "document_type": "Contact",
            "document_name": contact_name,
            "tag": "cliente actual",
            "title": doc.name,
        }).insert(ignore_permissions=True)


def _whatsapp_bienvenida(doc, productos):
    """Los dos mensajes que hoy manda GHL: la bienvenida del producto y el
    'Muchas gracias por confiar en nosotros'.

    Apagado mientras el workflow 6 de GHL siga publicado — si no, al cliente
    le llega todo por duplicado. El interruptor está del lado del host
    (`ENVIAR_AL_CLIENTE`), y este lado lo consulta para no tener dos
    interruptores que se puedan contradecir."""
    conv = getattr(doc, "chatwoot_conversation_id", None)
    if not conv:
        return "el deal no tiene conversación de Chatwoot"
    if not _host_envia_al_cliente():
        return "apagado (el workflow 6 de GHL sigue publicado)"

    from ..frappe_chatwoot.api import chatwoot as api_cw
    from . import autoria
    from . import onboarding_textos as textos

    key = next((p for p in productos if p in textos.PRODUCTOS), None)
    if not key:
        return "sin material para ese producto; lo atiende una persona"
    try:
        api_cw.cw.create_message(
            int(conv), textos.PRODUCTOS[key],
            content_attributes=autoria.marca_automatica(
                "onboarding", f"Bienvenida de onboarding — {key}"))
        api_cw.cw.create_message(
            int(conv), textos.SEGUNDO,
            content_attributes=autoria.marca_automatica(
                "onboarding", "Onboarding — segundo mensaje"))
        api_cw._pause_conversation(int(conv), getattr(doc, "chatwoot_inbox_id", None))
        return True
    except Exception as exc:
        frappe.log_error(f"onboarding {doc.name}: falló el WhatsApp de bienvenida: {exc}",
                         "CRM onboarding")
        return f"falló: {exc}"


def _host_envia_al_cliente():
    cfg = _config()
    if not cfg["url"]:
        return False
    try:
        with urllib.request.urlopen(f"{cfg['url']}/health", timeout=10) as resp:
            return bool(json.loads(resp.read().decode()).get("enviar_al_cliente"))
    except Exception:
        return False


def _avisar_al_host(doc, contacto, empresa, productos):
    cfg = _config()
    if not (cfg["url"] and cfg["token"]):
        frappe.log_error(f"onboarding {doc.name}: servicio no configurado "
                         "(Chatwoot Settings → onboarding_url / onboarding_token)",
                         "CRM onboarding")
        return "no configurado"

    # En GHL la oportunidad tenía nombre propio ("SGPT - LEEA", "TLP-Fragancia")
    # y ese nombre encabeza el título del proyecto en Notion. En Frappe CRM un
    # deal no tiene nombre: se identifica por su organización. Se usa el
    # producto contratado como encabezado, que es lo que hace legible el
    # título ("PVP - Aerotec - Alexis León"); si no se reconoció producto,
    # queda "<empresa> - <contacto>" y el host deduplica.
    payload = {
        "deal_id": doc.name,
        "deal_url": f"https://sofiav2.lavendi.mx/crm/deals/{doc.name}",
        "oportunidad": "/".join(productos) if productos else empresa,
        "empresa": empresa,
        "contacto": contacto,
        "email": _email(doc),
        "ghl_contact_id": (getattr(doc, "ghl_contact_id", None) or "").strip(),
        "productos": productos,
    }
    req = urllib.request.Request(
        f"{cfg['url']}/crm/proyecto-ganado",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-onboarding-token": cfg["token"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        cuerpo = ""
        try:
            cuerpo = exc.read().decode()[:300]
        except OSError:
            pass
        frappe.log_error(f"onboarding {doc.name}: el servicio respondió {exc.code}: {cuerpo}",
                         "CRM onboarding")
        return f"error {exc.code}"
    except Exception as exc:
        frappe.log_error(f"onboarding {doc.name}: no se pudo contactar el servicio: {exc}",
                         "CRM onboarding")
        return f"error: {exc}"
