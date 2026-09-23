# Copyright (c) 2026, lavendi.mx
"""F3 — SOMBRA (shadow) del lead del sitio de Estrublock hacia el CRM nuevo.

QUÉ ES Y POR QUÉ
----------------
El formulario/calculadora de estrublock.mx sigue entrando por GHL: ésa es la vía
viva y **no se toca**. Este endpoint crea la MISMA ficha en
`estrublock.lavendi.mx` en paralelo, para poder comparar lado a lado durante F5
("0 divergencias": contacto, oportunidad, etapa, notas, atribución) antes de
cortar el canal en F6.

DÓNDE VA Y CÓMO SE DESPLIEGA  (sin reinicio)
--------------------------------------------
Archivo NUEVO, y **nada más**:
    <app>/frappe_chatwoot/frappe_chatwoot/api/lead_web.py
        (método: frappe_chatwoot.frappe_chatwoot.api.lead_web.crear)

    docker cp estrublock_f3_lead_web.py \
      frappe-oa5gpbnleuzmbbybtxtgakcr:/home/frappe/frappe-bench/apps/frappe_chatwoot/frappe_chatwoot/frappe_chatwoot/api/lead_web.py

Un módulo NUEVO se importa la primera vez que se llama al método: **no hace
falta reiniciar el contenedor**, y por lo tanto este deploy **no arrastra los
cambios sin commitear de otras sesiones** que un reinicio sí desplegaría
(producción corre desde el working tree — ver el ⚠ del CLAUDE.md del frente).

Rollback: borrar el archivo. Nada lo llama hasta que se cablee el mu-plugin, así
que no deja estado ni toca GHL.

CABLEADO EN EL SITIO (2º paso, con su propio gate — NO incluido aquí)
--------------------------------------------------------------------
En `web/estrublock-ghl-leads.php`, al final de `eb_ghl_create_lead_and_sms()`,
una llamada best-effort, no bloqueante y con interruptor:

    if ( defined('EB_FRAPPE_SHADOW') && EB_FRAPPE_SHADOW ) {
        wp_remote_post( 'https://estrublock.lavendi.mx/api/method/'
            . 'frappe_chatwoot.frappe_chatwoot.api.lead_web.crear', [
            'headers' => [
                'Authorization' => 'token ' . EB_FRAPPE_KEY . ':' . EB_FRAPPE_SECRET,
                'Content-Type'  => 'application/json',
            ],
            'body'    => wp_json_encode( [ 'payload' => wp_json_encode( [
                'nombre' => $full_name, 'telefono' => $phone_fmt, 'email' => $email,
                'municipio' => $municipio, 'estado' => $estado, 'source' => $source,
                'nota' => $note_details, 'atribucion' => eb_attr_row(),
            ] ) ] ),
            'timeout' => 10, 'blocking' => false,   // <- no puede retrasar ni romper el flujo de GHL
        ] );
    }

`blocking => false` es deliberado: si Frappe está lento o caído, el lead sigue
entrando a GHL exactamente como hoy.

TRES DECISIONES QUE VALE LA PENA REGISTRAR
------------------------------------------
1. **El dueño NO es una constante de lavendi.mx.** `captacion._crear_ficha`
   hardcodea `LEAD_OWNER_DEFAULT = "alejandro.moreno@lavendi.mx"`, y ese usuario
   **no existe** en `estrublock.lavendi.mx`: el Link reventaría con 417 ("Could
   not find Deal Owner"), que es justo el error que sixgardens arrastra desde el
   2026-09-22. Aquí el dueño se resuelve por payload -> `frappe.conf`
   (`lead_web_deal_owner`) -> y si el usuario no existe/enabled cae a
   `Administrator` (existe en todo sitio), así que el insert NUNCA falla por eso.

2. **No reusa `_crear_ficha`, y es a propósito.** Ese insertor es la forma
   correcta a largo plazo, pero parametrizarle el dueño obliga a editar
   `utils/captacion.py`, que YA está importado en los workers de gunicorn: el
   cambio exigiría **reiniciar** el bench — y hoy reiniciar desplegaría los
   cambios sin commitear de otras sesiones. Se prefiere un archivo nuevo sin
   reinicio a un refactor que arrastra trabajo ajeno a medias. **Deuda
   registrada**: cuando el bench esté tranquilo, vale la pena agregar
   `deal_owner=None` a `_crear_ficha` (default = comportamiento actual) y volver
   este módulo a esa llamada.

3. **Se saltan las `Assignment Rule` a propósito.** Estrublock tiene activas las
   2 reglas del Round Robin (`disabled=0`, `assign_condition = "1 == 1"` sobre
   `CRM Lead` y `CRM Deal`). Sin bypass, cada lead web de la sombra abriría ToDo
   + notificación a las 3 asesoras — que todavía no usan el panel nuevo (eso es
   F5.5) —, el mismo bloqueante declarado para F7. El mecanismo es
   `frappe.flags.in_patch = True` alrededor del insert: es el flag que el propio
   Frappe consulta en `assignment_rule.apply()` para saltarse las reglas.
"""

import json

import frappe

from frappe_chatwoot.utils.captacion import _asegurar_contacto

# Campos de atribución del payload -> campo del CRM nuevo. Son `Custom Field` de
# `CRM Deal` (verificados por API en estrublock.lavendi.mx el 2026-09-22).
# ⚠ OJO al auditar: la metadata del DocType NO lista los Custom Field, hay que
# consultar `/api/resource/Custom Field?filters=[[dt,=,CRM Deal]]` — con el
# DocType solo parecen "no existir" y se concluiría un falso bug.
ATRIBUCION_A_CAMPO = {
    "utm_source": "utm_source",
    "utm_campaign": "utm_campaign",
    "utm_term": "utm_term",
    "gclid": "gclid_ads",
    "gbraid": "gbraid",
    "wbraid": "wbraid",
}

# `Administrator` existe en todo sitio Frappe: el respaldo nunca falla el Link.
OWNER_FALLBACK = "Administrator"
ETAPA_DEAL = "Lead"   # `CRM Deal Status` (existe en estrublock, verificado)
ETAPA_LEAD = "New"    # `CRM Lead Status` (existe en estrublock, verificado)

# `source` es Link a `CRM Lead Source`. Los valores que manda el sitio ("Web
# Estrublock", "Cotizador Web Estrublock", "Presupuesto Web Estrublock") NO
# existen como fuente en Frappe, así que el Link reventaría el insert — mismo
# patrón que el dueño. Se cae a la fuente genérica y **el valor original se
# conserva en la nota**, para que F5 pueda compararlo igual.
SOURCE_FALLBACK = "Formulario web"


def _payload(crudo):
    """Acepta el JSON-string que manda PHP, un dict, o campos sueltos."""
    if isinstance(crudo, str):
        crudo = json.loads(crudo or "{}")
    return crudo or {}


def _owner(pedido):
    """Usuario del sitio que quedará como `deal_owner`, siempre uno existente."""
    for u in (pedido, frappe.conf.get("lead_web_deal_owner")):
        if u and frappe.db.get_value("User", u, "enabled"):
            return u
    return OWNER_FALLBACK


def _source(valor):
    """`CRM Lead Source` del sitio si el valor existe; si no, la fuente genérica."""
    valor = (valor or "").strip()
    if valor and frappe.db.exists("CRM Lead Source", valor):
        return valor
    return SOURCE_FALLBACK


def _normaliza_telefono(t):
    limpio = "".join(c for c in str(t or "") if c.isdigit())
    if len(limpio) == 10:
        return "+52" + limpio
    return "+" + limpio if limpio else ""


def _crear_deal(contacto, nombre, email, telefono, source, valor, owner, atribucion):
    """Inserta el `CRM Deal` en etapa `Lead`.

    Réplica deliberada de la rama Deal de `captacion._crear_ficha` (ver decisión
    #2 del docstring). `deal_name`/`utm_*`/`gclid_ads` viven como Custom Field,
    por eso no aparecen en la metadata del doctype.
    """
    partes = nombre.split(" ", 1)
    campos = {
        "doctype": "CRM Deal",
        "contact": contacto,
        "contacts": [{"contact": contacto, "is_primary": 1}],
        "status": ETAPA_DEAL,
        "deal_owner": owner,
        "deal_name": nombre,
        "lead_name": nombre,
        "first_name": partes[0] if partes else "",
        "last_name": partes[1] if len(partes) > 1 else "",
        "email": email or None,
        "mobile_no": telefono or None,
        "source": source,
    }
    if valor:
        campos["deal_value"] = valor
    campos.update(atribucion or {})

    doc = frappe.get_doc(campos)
    doc.insert(ignore_permissions=True)
    return doc.name


@frappe.whitelist()
def crear(payload=None, **kw):
    """Crea la ficha espejo del lead web. Devuelve `{contacto, ficha_dt, ficha}`.

    Idempotente por teléfono/correo en el CONTACTO (se reusa si ya existe, igual
    que GHL), pero NO en la ficha: cada envío abre una ficha nueva, que es como
    se comporta el flujo vivo de GHL (una oportunidad/nota por envío).
    """
    pedido = _payload(payload)
    pedido.update({k: v for k, v in kw.items() if v is not None})

    telefono = _normaliza_telefono(pedido.get("telefono") or pedido.get("phone"))
    email = (pedido.get("email") or "").strip()
    nombre = (pedido.get("nombre") or pedido.get("name") or "").strip()

    if not telefono and not email:
        frappe.throw("lead_web.crear: hace falta teléfono o correo")

    contacto = _asegurar_contacto(
        frappe._dict(telefono=telefono, email=email, nombre=nombre)
    )

    atribucion = pedido.get("atribucion") or {}
    if isinstance(atribucion, str):
        atribucion = _payload(atribucion)
    campos_atribucion = {
        ATRIBUCION_A_CAMPO[k]: atribucion[k]
        for k in ATRIBUCION_A_CAMPO
        if atribucion.get(k)
    }

    source_ghl = (pedido.get("source") or "Web Estrublock").strip()
    try:
        valor = float(pedido.get("valor")) if pedido.get("valor") else None
    except (TypeError, ValueError):
        valor = None

    # Bypass de las Assignment Rule (decisión #3). El valor previo se restaura en
    # `finally` para no alterar el resto del request.
    previo = frappe.flags.get("in_patch")
    frappe.flags.in_patch = True
    try:
        ficha = _crear_deal(
            contacto=contacto,
            nombre=nombre,
            email=email,
            telefono=telefono,
            source=_source(source_ghl),
            valor=valor,
            owner=_owner(pedido.get("deal_owner")),
            atribucion=campos_atribucion,
        )
    finally:
        frappe.flags.in_patch = previo

    # La nota se crea SIEMPRE: además del detalle de la cotización, guarda la
    # fuente tal como viene de GHL (que no existe como `CRM Lead Source` aquí).
    nota = (pedido.get("nota") or "").strip()
    contenido = f"<p>Fuente en GHL: {frappe.utils.escape_html(source_ghl)}</p>"
    if nota:
        contenido += frappe.utils.sanitize_html(nota).replace("\n", "<br>")
    frappe.get_doc({
        "doctype": "FCRM Note",
        "title": "Solicitud desde el formulario del sitio",
        "content": contenido,
        "reference_doctype": "CRM Deal",
        "reference_docname": ficha,
    }).insert(ignore_permissions=True)

    return {"contacto": contacto, "ficha_dt": "CRM Deal", "ficha": ficha}
