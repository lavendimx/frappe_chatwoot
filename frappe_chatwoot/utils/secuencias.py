# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Motor de secuencias de seguimiento — réplica del workflow «4. Seguimientos» de GHL.

Se copia a `apps/frappe_chatwoot/frappe_chatwoot/utils/secuencias.py`.

CONTEXTO
    Ese workflow está en `draft` desde el 06-sep. Desde entonces **ninguna
    oportunidad recibe seguimiento automático**: al medir el 14-sep había 398
    abiertas ($6.58M) y 308 de ellas sin una sola tarea pendiente. La spec
    completa, con el copy resuelto, está en `workflows-ghl/spec/4-seguimientos.md`;
    el extractor que la genera es `workflows-ghl/extraer_spec.py`.

DECISIONES QUE NO SON OBVIAS LEYENDO EL CÓDIGO

1.  **El canal es WhatsApp, no SMS.** Los 35 nodos de GHL son de tipo `sms`, pero
    el LC Phone nunca fue SMS-capable (error 21661 de Twilio, documentado en
    `cobranza/automation/config.py:134`) y el único SMS que intentó salir, el
    11-sep, murió con *"no eligible SMS sender was available"*. El copy además
    está escrito en formato WhatsApp (`*negritas*`, "1️⃣ Si / 2️⃣ No").
    Confirmado por Alejandro el 14-sep. El texto se replica literal.

2.  **El envío NO pausa al agente IA**, a diferencia de `api.chatwoot.send_message`.
    Esa pausa existe para que el bot no compita con un humano que acaba de
    intervenir; aquí el que escribe es un automatismo, y dejar la conversación
    pausada significaría que cuando el prospecto conteste —que es el objetivo
    del seguimiento— no le responda nadie. Mismo criterio que el recordatorio
    de citas (10-sep).

3.  **Salida por respuesta, medida contra el último envío.** El `stopOnResponse`
    de GHL. Si el contacto escribió DESPUÉS de nuestro último mensaje, la
    secuencia se detiene: seguir empujando plantillas a alguien que ya contestó
    es la forma más rápida de que nos bloqueen.

4.  **Freno de ráfaga por corrida** (`max_por_corrida`). En GHL cada contacto
    entraba el día que le tocaba; acá, al encender, hay 142 oportunidades PVP
    vigentes esperando el paso 1. Sin freno serían 142 WhatsApp en un minuto
    desde el número de la agencia — un patrón de spam ante WhatsApp, y un
    incidente de reputación del número que nos costaría el canal entero.

5.  **`proximo_en` se calcula y se guarda**, no se recalcula. Un job que se salta
    una corrida no debe perder ni duplicar un envío. Misma razón por la que
    `recordatorios.py` usa ventana y no contador.

INTERRUPTOR
    `Chatwoot Settings → secuencias_activas`. Nace apagado: encenderlo empieza a
    escribirle a prospectos reales.

CORRECCIONES DE LA AUDITORÍA DE PARIDAD (2026-09-14, contra
workflows-ghl/spec/4-seguimientos.md y el JSON crudo `draft-4._Seguimientos.json`)

6.  **GHL no solo "detiene" la secuencia cuando el contacto responde.** El
    árbol real tiene 29 `internal_notification` colgados de la rama
    `wait_reply` (verificado en el JSON: 2-3 por cada `wait`, justo antes de
    `add_to_workflow`) — avisan al equipo que un prospecto frío reaccionó.
    Sin esto, "Salió por respuesta" quedaba tan silencioso como el handover
    sin notificar que costó el incidente del 09-sep. Se replica con
    `_avisar_respondio()`, reusando el único canal que este contenedor puede
    alcanzar hoy sin inventar infraestructura (ver punto 7). `add_to_workflow`
    (a qué workflow re-inscribe) NO se replica: la spec no captura el id de
    destino y es decisión de negocio, no de este motor.

7.  **El correo saliente estaba roto en silencio.** Verificado en producción
    (14-sep): `Email Account` tiene **0 registros** en este contenedor.
    `frappe.sendmail()` no truena sin uno — encola en `Email Queue` y ahí se
    queda para siempre (confirmado contra la tabla real: 5,122 `Expired`,
    40 `Not Sent`, **0 `Sent`**). El paso "Email" reportaba
    `"email a {destino}"` como si hubiera entregado algo. El correo saliente
    real de nuevosofia sale del HOST (`agente-ia`, Gmail API con service
    account), no de este contenedor — mismo patrón que Notion y Google
    Calendar (ver `agenda.py`).

    **Resuelto el 15-sep**: el paso Email ahora llama a `POST /correo/enviar`
    del host (`_enviar_correo`), que manda por Gmail API con la service account
    impersonando `contacto@lavendi.mx`. El HTML se limpia antes de salir
    (`_html_seguro`): las imágenes de GHL que están en `media-map.json` se
    sustituyen y las que no, se quitan (mejor sin imagen que con un roto), y el
    CTA al widget de agenda de GHL se reapunta a `agenda.lavendi.mx`. Con esto
    cada WhatsApp del seguimiento tiene su correo, como en GHL.

8.  **Los adjuntos nunca se sustituían.** El paso WhatsApp pegaba la URL de
    Firebase tal cual — y la subcuenta de GHL ya está apagada, esos links
    devuelven 404 hoy. `_adjunto_seguro()` los resuelve contra
    `media-map.json` (generado en paralelo por `workflows-ghl/rescate/`,
    contraparte de `media/INDICE.tsv`) y, si el mapeo aún no existe, omite el
    adjunto — nunca manda el link roto.

9.  **"Actualizar oportunidad" solo soportaba un campo.** El JSON crudo
    muestra que 2 de los 21 nodos reales mueven la etapa del pipeline **y**
    marcan `status: lost` en el mismo nodo. `campo_destino`/`valor_destino`
    ahora aceptan listas separadas por coma para ese caso; un solo campo
    sigue funcionando exactamente igual que antes.

10. **Las tareas usaban un status y una fecha que no son los que ya usa el
    resto del proyecto.** `migracion/load.py` y
    `workflows-ghl/rescate/rescatar_leads_congelados.py` crean `CRM Task` con
    `status="Todo"`; este archivo usaba `"Backlog"` (inconsistente, aunque
    válido). Los 9 `task-notification` reales de GHL traen `dueDate: "now"`,
    no mañana. Corregido para seguir el mismo patrón que el resto del
    proyecto, y se separó título (`asunto`) de cuerpo (`mensaje` →
    `description`) — antes todo se aplastaba en el título a 140 caracteres.

11. **Reply-check ciego antes del primer envío.** `_debe_salir()` solo
    comparaba contra `ultimo_envio_at`, que nacía vacío hasta el primer
    WhatsApp/Email real — un contacto que ya conversaba con Sofía durante la
    primera espera (p. ej. "Esperar 2 días") no se detectaba y aun así
    recibía el primer mensaje enlatado. `inscribir()` ahora siembra
    `ultimo_envio_at` con el instante de inscripción.

PENDIENTE DE DECISIÓN HUMANA (no se inventó, se documenta)
    El canal correcto para "avisos internos" (paso "Aviso al equipo" y el
    aviso de respuesta del punto 6) es ambiguo: el patrón dominante en el
    resto del proyecto es WhatsApp al grupo interno (`onboarding/app.py`,
    `migracion/salud_canal_salida.py`) para alertas urgentes y correo para
    avisos de rutina — pero NINGUNO de los dos es alcanzable desde este
    contenedor hoy (ni la API key de Evolution ni el Email Account existen
    aquí). Por eso ambos usan `CRM Notification` (in-app, Frappe nativo): es
    lo único que funciona sin construir un puente nuevo. Si Alejandro quiere
    que esto suene por WhatsApp de verdad, hace falta un endpoint nuevo en
    `agente-ia` (mismo patrón que `/calendario/agendar`) — no se construyó
    aquí porque no me corresponde decidir el canal ni tocar esa app.

    Tampoco se modeló el workflow "4.1 Reinicio de Seguimientos" (199 nodos,
    casi un clon de éste). La arquitectura ya lo soporta sin tocar código —
    basta con crear más registros `Secuencia`— pero la condición real que
    mueve a un contacto de "4. Seguimientos" a "4.1" vive en el
    `add_to_workflow` de GHL, cuyo id de destino la spec extraída no captura.
"""

import json
import os
import random
import time

import frappe

from . import chatwoot_client as cw

MAX_CORRIDA_DEFAULT = 20

# Delay aleatorio entre envíos reales de WhatsApp dentro de una misma corrida
# (12-sep, auditoría de despliegue). Mitigación de riesgo de baneo del canal
# Baileys (no oficial) — mismo motivo que el freno `max_por_corrida` (punto 4
# del docstring del módulo): varios WhatsApp seguidos, sin espaciar, desde el
# número de la agencia es un patrón reconocible de spam/automatización ante
# WhatsApp, y un incidente de reputación del número nos costaría el canal
# entero (el mismo canal que usa `agente-ia-chatwoot` para atender clientes
# reales). No se aplica antes del PRIMER envío de la corrida, solo entre uno
# y el siguiente.
DELAY_ENTRE_ENVIOS_SEG = (60, 120)

# Estados de `CRM Deal.ghl_status` que sacan de la secuencia. Un deal ganado o
# perdido no debe seguir recibiendo mensajes de venta — en GHL lo hacía el
# workflow «8. Salida por Venta Ganada».
ESTADOS_QUE_SACAN = ("won", "lost", "abandoned")


def _activo() -> bool:
    return bool(
        frappe.db.get_single_value("Chatwoot Settings", "secuencias_activas")
    )


def _ahora():
    return frappe.utils.now_datetime()


# ---------------------------------------------------------------------------
# Ventana horaria
# ---------------------------------------------------------------------------

def _dentro_de_ventana(sec, cuando=None) -> bool:
    """¿`cuando` cae dentro de la ventana L-V 12:00-18:00 de la secuencia?"""
    if not sec.get("horario_habil"):
        return True
    cuando = cuando or _ahora()
    dias = [int(d) for d in (sec.get("ventana_dias") or "1,2,3,4,5").split(",") if d.strip()]
    if cuando.isoweekday() not in dias:
        return False
    ini = sec.get("ventana_inicio") or "12:00"
    fin = sec.get("ventana_fin") or "18:00"
    return ini <= cuando.strftime("%H:%M") < fin


def _siguiente_hueco(sec, desde):
    """El primer instante >= `desde` que cae dentro de la ventana.

    Se avanza minuto a minuto solo dentro del día y luego se salta al inicio del
    siguiente día hábil; sin este salto, un cálculo ingenuo iteraría 1,440 veces
    por cada fin de semana."""
    if not sec.get("horario_habil"):
        return desde
    ini = sec.get("ventana_inicio") or "12:00"
    dias = [int(d) for d in (sec.get("ventana_dias") or "1,2,3,4,5").split(",") if d.strip()]
    cand = desde
    for _ in range(14):  # 14 días es cota dura: ninguna ventana razonable excede eso
        if _dentro_de_ventana(sec, cand):
            return cand
        hhmm = cand.strftime("%H:%M")
        if cand.isoweekday() in dias and hhmm < ini:
            h, m = ini.split(":")
            cand = cand.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            continue
        cand = (cand + frappe.utils.datetime.timedelta(days=1)).replace(
            hour=int(ini.split(":")[0]), minute=int(ini.split(":")[1]),
            second=0, microsecond=0,
        )
    return cand


# ---------------------------------------------------------------------------
# Inscripción
# ---------------------------------------------------------------------------

def _conversacion_de(deal: str, contacto: str) -> str:
    """A qué hilo de Chatwoot se le escribe.

    Se prefiere la del propio deal; si no tiene, se busca en el contacto — los
    deals migrados de GHL no traen `chatwoot_conversation_id` porque el hilo
    nació después, del lado de Chatwoot."""
    directo = frappe.db.get_value("CRM Deal", deal, "chatwoot_conversation_id")
    if directo:
        return str(directo)
    if not contacto:
        return ""
    for dt in ("CRM Deal", "CRM Lead"):
        fila = frappe.get_all(
            dt, filters={"contact": contacto, "chatwoot_conversation_id": ["!=", ""]},
            fields=["chatwoot_conversation_id"], order_by="modified desc", limit=1,
        )
        if fila:
            return str(fila[0].chatwoot_conversation_id)
    return ""


def _asegurar_conversacion(ins: dict, sec: dict) -> str:
    """Abre la conversación de Chatwoot si la inscripción no tiene una.

    POR QUÉ EXISTE
        El seguimiento reactiva prospectos que cotizaron por WhatsApp en GHL. Al
        medir el 14-sep, **0 de los 142 PVP abiertos tenía `chatwoot_conversation_id`**:
        el hilo nació del lado de Chatwoot, después de la migración, así que los
        deals migrados no lo traen. Sin esto el motor no tiene a dónde escribir —
        `inscribir()` los salta a propósito y el paso de WhatsApp moriría con
        `int("")`. Es la diferencia entre "seguimiento" y "outreach frío": aquí
        se crea el contacto y la conversación en el inbox de la secuencia.

    Mismo camino que `panel.py` (el que ya usa el botón "nueva conversación" de
    Conversaciones): primero se BUSCA el contacto por teléfono y se reutiliza un
    hilo abierto si existe; crear un contacto duplicado ensuciaría la bandeja con
    dos fichas de la misma persona.

    Idempotente: el `conversation_id` se guarda en la inscripción en cuanto se
    abre, así que el segundo envío (y el segundo intento tras un error) lo reusa.
    """
    if ins.get("conversation_id"):
        return str(ins["conversation_id"])
    inbox = frappe.utils.cint(sec.get("inbox_id") or 0)
    if not inbox:
        return ""

    deal = frappe.db.get_value("CRM Deal", ins["deal"], "mobile_no") or ""
    contacto = frappe.db.get_value(
        "Contact", ins.get("contacto"),
        ["mobile_no", "phone", "first_name", "last_name", "company_name"], as_dict=True,
    ) or {}
    telefono = (deal or contacto.get("mobile_no") or contacto.get("phone") or "").strip()
    if not telefono:
        return ""
    digitos = "".join(c for c in telefono if c.isdigit())
    nombre = " ".join(
        x for x in [contacto.get("first_name"), contacto.get("last_name")] if x
    ).strip() or (contacto.get("company_name") or telefono)

    contacto_id, source_id = None, None
    for consulta in (telefono, digitos, digitos[-10:]):
        encontrados = cw.search_contacts(consulta) if consulta else []
        if encontrados:
            contacto_id = encontrados[0].get("id")
            break
    if contacto_id:
        detalle = cw.get_contact(contacto_id)
        source_id = next(
            (b.get("source_id") for b in (detalle.get("contact_inboxes") or [])
             if ((b.get("inbox") or {}).get("id")) == inbox), None,
        )
        if not source_id:
            creado = cw.create_contact_inbox(contact_id=contacto_id, inbox_id=inbox)
            source_id = (creado.get("payload") or creado).get("source_id")
        # ¿Ya hay hilo abierto en este inbox? Se escribe ahí, no se abre otro.
        for conv in cw.get_conversations_for_contact(contacto_id):
            if conv.get("inbox_id") == inbox and conv.get("status") in ("open", "pending"):
                frappe.db.set_value("Secuencia Inscripcion", ins["name"],
                                    "conversation_id", str(conv["id"]), update_modified=False)
                ins["conversation_id"] = str(conv["id"])
                return str(conv["id"])
    else:
        nuevo = cw.create_contact(inbox_id=inbox, name=nombre, phone_number=telefono)
        contacto_id = nuevo.get("id")
        source_id = next((b.get("source_id") for b in (nuevo.get("contact_inboxes") or [])
                          if b.get("source_id")), None)

    if not source_id:
        return ""
    conv = cw.create_conversation(source_id=source_id, inbox_id=inbox, contact_id=contacto_id)
    conv_id = conv.get("id") or (conv.get("payload") or {}).get("id")
    if not conv_id:
        return ""
    frappe.db.set_value("Secuencia Inscripcion", ins["name"], "conversation_id",
                        str(conv_id), update_modified=False)
    ins["conversation_id"] = str(conv_id)
    return str(conv_id)


# ---------------------------------------------------------------------------
# Reflejo del estado en el `CRM Deal` (campos denormalizados, 2026-09-16)
#
# Por qué: el detalle de la oportunidad y el panel lateral de Conversaciones
# leen el `CRM Deal`, no el `Secuencia Inscripcion`. Escribir el resumen en el
# deal evita forkear `Deal.vue` (upstream: un rebuild del contenedor lo revierte)
# y deja el dato consultable en vistas y reportes.
#
# ⚠ Segundo lugar donde vive la verdad: si un call site olvida reflejar el
# cambio, el campo miente en silencio. Por eso TODAS las escrituras de estado
# pasan por `_set_ins()`, que llama aquí, y el backfill es `reflejar_todas()`.
# ---------------------------------------------------------------------------

def _resumen_deal(deal: str) -> dict:
    """Resumen de la inscripción vigente de un deal, formateado para el CRM.

    Prefiere la inscripción `Activa`: un deal que salió de una secuencia y entró
    a otra debe mostrar la viva, no la última que se tocó.
    """
    vacio = {"secuencia_actual": "", "secuencia_paso": "", "secuencia_estado": ""}
    campos = ["name", "secuencia", "estado", "paso_actual"]
    ins = frappe.db.get_value(
        "Secuencia Inscripcion", {"deal": deal, "estado": "Activa"}, campos, as_dict=True
    )
    if not ins:
        ins = frappe.db.get_value(
            "Secuencia Inscripcion", {"deal": deal}, campos,
            as_dict=True, order_by="modified desc",
        )
    if not ins:
        return vacio

    titulo = frappe.db.get_value("Secuencia", ins.secuencia, "titulo") or ins.secuencia
    total = frappe.db.count("Secuencia Paso", {"parent": ins.secuencia})
    i = frappe.utils.cint(ins.paso_actual)
    if i < total:
        p = frappe.db.get_value(
            "Secuencia Paso", {"parent": ins.secuencia, "idx": i + 1},
            ["nombre_ghl", "tipo"], as_dict=True,
        ) or {}
        nombre = (p.get("nombre_ghl") or p.get("tipo") or "").strip()
        paso = f"Paso {i + 1} de {total}" + (f" · siguiente: {nombre}" if nombre else "")
    else:
        paso = f"Paso {total} de {total} · completada"
    return {
        "secuencia_actual": titulo,
        "secuencia_paso": paso,
        "secuencia_estado": ins.estado or "",
    }


def _reflejar_en_deal(deal: str | None) -> None:
    """Escribe el resumen de la secuencia en los 3 campos del `CRM Deal`."""
    if not deal:
        return
    frappe.db.set_value("CRM Deal", deal, _resumen_deal(deal), update_modified=False)


def _set_ins(ins_name: str, campos: dict, **kw) -> None:
    """Escribe en la inscripción Y refleja el resultado en su `CRM Deal`.

    Punto único de escritura de estado: así ningún call site puede cambiar la
    inscripción sin actualizar el campo que ve el equipo.
    """
    deal = frappe.db.get_value("Secuencia Inscripcion", ins_name, "deal")
    frappe.db.set_value("Secuencia Inscripcion", ins_name, campos, **kw)
    _reflejar_en_deal(deal)


@frappe.whitelist()
def reflejar_todas() -> dict:
    """Backfill: recalcula los 3 campos en todos los deals con inscripción.

    Se corre una vez tras crear los campos (los inscritos actuales quedarían
    vacíos hasta su próximo movimiento) y sirve de reparación si un call site
    llegara a fallar. Idempotente.
    """
    deals = [r[0] for r in frappe.db.sql(
        "SELECT DISTINCT deal FROM `tabSecuencia Inscripcion` WHERE deal IS NOT NULL"
    )]
    for d in deals:
        _reflejar_en_deal(d)
    frappe.db.commit()
    return {"deals": len(deals)}


@frappe.whitelist()
def inscribir(secuencia: str, apply: int = 0, limite: int = 0) -> dict:
    """Mete en la secuencia las oportunidades abiertas de su producto.

    Dry-run por defecto **a propósito**: inscribir es lo que decide a quién le va
    a escribir el sistema, y esa lista se revisa antes, no después.

    NO inscribe:
      - deals sin conversación de Chatwoot (no hay a dónde escribir),
      - deals ya inscritos en esta secuencia (`permitir_reingreso` aparte),
      - deals que no están `open`.
    """
    sec = frappe.get_doc("Secuencia", secuencia).as_dict()
    if not sec.get("producto"):
        frappe.throw("Esa secuencia no tiene producto (rama) definido")

    deals = frappe.get_all(
        "CRM Deal",
        filters={"ghl_status": "open", "ghl_producto": sec["producto"]},
        fields=["name", "contact", "deal_value", "organization"],
        order_by="modified desc",
        limit_page_length=frappe.utils.cint(limite) or 0,
    )

    nuevos, sin_conv, ya = [], [], []
    for d in deals:
        if frappe.db.exists("Secuencia Inscripcion", {"secuencia": secuencia, "deal": d.name}):
            ya.append(d.name)
            continue
        conv = _conversacion_de(d.name, d.contact)
        if not conv:
            sin_conv.append(d.name)
            continue
        nuevos.append((d, conv))

    if frappe.utils.cint(apply):
        # `proximo_en` = ahora dentro de la ventana. El escalonado real lo pone
        # `max_por_corrida` en el job: fijar aquí horas distintas por deal daría
        # una falsa sensación de control y se desincronizaría al primer reinicio.
        cuando = _siguiente_hueco(sec, _ahora())
        for d, conv in nuevos:
            frappe.get_doc({
                "doctype": "Secuencia Inscripcion",
                "secuencia": secuencia,
                "deal": d.name,
                "contacto": d.contact,
                "conversation_id": conv,
                "estado": "Activa",
                "paso_actual": 0,
                "proximo_en": cuando,
                # Sembrado al instante de inscripción, no vacío: así una
                # respuesta que llega ANTES del primer envío automático (p.
                # ej. durante la primera espera de 2 días) también saca al
                # contacto de la secuencia. Ver punto 11 del docstring.
                "ultimo_envio_at": cuando,
            }).insert(ignore_permissions=True)
            _reflejar_en_deal(d.name)
        frappe.db.commit()

    return {
        "apply": bool(frappe.utils.cint(apply)),
        "secuencia": secuencia,
        "producto": sec["producto"],
        "inscritos" if frappe.utils.cint(apply) else "a_inscribir": len(nuevos),
        "ya_inscritos": len(ya),
        "sin_conversacion": len(sin_conv),
        "ejemplos": [d.name for d, _ in nuevos[:5]],
    }


# ---------------------------------------------------------------------------
# Ejecución de un paso
# ---------------------------------------------------------------------------

def _resolver(texto: str, ctx: dict) -> str:
    """Los merge fields de GHL (`{{contact.first_name}}`) con datos reales.

    Si un campo no resuelve se deja **vacío**, no el literal: que a un prospecto
    le llegue "Hola {{contact.first_name}}" es peor que "Hola ," — y ya pasó en
    producción el 06-sep con el prompt del agente."""
    if not texto:
        return ""
    for clave, valor in ctx.items():
        texto = texto.replace("{{%s}}" % clave, valor or "")
    # Cualquier merge field que haya quedado sin mapear se borra.
    import re
    return re.sub(r"\{\{[^}]+\}\}", "", texto)


def _contexto(ins: dict) -> dict:
    contacto = frappe.db.get_value(
        "Contact", ins.get("contacto"),
        ["first_name", "last_name", "company_name", "email_id"], as_dict=True,
    ) or {}
    return {
        "contact.first_name": (contacto.get("first_name") or "").strip(),
        "contact.last_name": (contacto.get("last_name") or "").strip(),
        "contact.name": " ".join(
            x for x in [contacto.get("first_name"), contacto.get("last_name")] if x
        ),
        "contact.company_name": contacto.get("company_name") or "",
        "contact.email": contacto.get("email_id") or "",
    }


_MEDIA_MAP_CACHE_KEY = "frappe_chatwoot:secuencias:media_map"


def _media_map() -> dict:
    """URL original de GHL (Firebase) -> URL propia ya rehospedada.

    La genera `workflows-ghl/rescate/` en paralelo a esta auditoría (ver
    `media/INDICE.tsv`, que trae el archivo local + la URL original pero
    todavía no el destino público). El archivo se copia junto a este módulo
    al desplegar —mismo criterio que el resto de `patches/`: se copia a mano
    a `utils/`— porque el contenedor de Frappe no monta `/root/projects`.

    Cacheado 60s (no releer disco en cada paso de una corrida de 20 envíos) y
    tolerante a que el archivo no exista todavía: devuelve {} en vez de
    tronar, que es justo el caso esperado hasta que el rescate termine."""
    cache = frappe.cache()
    cached = cache.get_value(_MEDIA_MAP_CACHE_KEY)
    if cached is not None:
        try:
            return json.loads(cached)
        except (TypeError, ValueError):
            pass
    ruta = os.path.join(os.path.dirname(__file__), "media-map.json")
    mapa = {}
    try:
        with open(ruta, encoding="utf-8") as f:
            crudo = json.load(f)
        if isinstance(crudo, dict):
            mapa = crudo
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as exc:
        frappe.log_error(f"secuencias: media-map.json ilegible: {exc}", "Secuencias")
    cache.set_value(_MEDIA_MAP_CACHE_KEY, json.dumps(mapa), expires_in_sec=60)
    return mapa


_DOMINIOS_GHL = ("firebasestorage.googleapis.com", "storage.googleapis.com")


def _adjunto_seguro(url: str) -> str | None:
    """La URL segura para mandar por WhatsApp, o None si no hay ninguna.

    El campo `adjunto_url` (ver su `description` en `secuencias_setup.py`) ya
    está pensado para traer la URL FINAL rehospedada una vez que alguien
    cargue los pasos reales — en ese caso se manda tal cual, sin pasar por el
    mapa. El mapa solo entra si lo que trae el paso sigue siendo un link de
    GHL (Firebase/`storage.googleapis.com`, como lo extrae
    `extraer_spec.py` hoy): esos links mueren con la subcuenta ya apagada, y
    solo se mandan si `_media_map()` tiene con qué sustituirlos. Nunca se
    manda un link de GHL sin sustituir — mandarle al prospecto un adjunto
    roto es peor que no mandarle ninguno."""
    if not url:
        return None
    if not any(dominio in url for dominio in _DOMINIOS_GHL):
        return url
    return _media_map().get(url) or None


def _leer_adjunto(url: str) -> tuple[str, bytes, str] | None:
    """(nombre, bytes, mime) del adjunto, o None si no se puede leer.

    Se manda como archivo real, no como URL en el texto (ver
    `create_message_with_attachment`): con URL el cliente ve un enlace en vez
    de la imagen.

    Primero se lee del disco del sitio: los assets rehospedados viven en
    `public/files/` de este mismo servidor (`sofiav2.lavendi.mx` es el dominio
    público de este site), así que no hace falta dar la vuelta por la red. Si
    no está en disco (un asset servido desde otro host), cae a descargarlo por
    HTTPS. `mimetypes` resuelve el tipo por extensión; si no lo reconoce se
    manda como octet-stream y WhatsApp lo entrega igual."""
    import mimetypes
    from urllib.parse import unquote, urlparse

    try:
        ruta = unquote(urlparse(url).path or "")
    except ValueError:
        return None
    nombre = os.path.basename(ruta) or "adjunto"
    mime = mimetypes.guess_type(nombre)[0] or "application/octet-stream"

    datos = None
    if ruta.startswith("/files/"):
        local = frappe.get_site_path("public", "files", unquote(ruta[len("/files/"):]))
        try:
            with open(local, "rb") as f:
                datos = f.read()
        except OSError:
            datos = None

    if datos is None:
        import requests
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                datos = resp.content
                mime = resp.headers.get("Content-Type") or mime
        except requests.RequestException:
            datos = None

    if not datos:
        return None
    return nombre, datos, mime


def _correo_disponible() -> bool:
    """¿Se puede mandar correo desde el host?

    Verificado en producción el 14-sep: `Email Account` tiene **0 registros** en este
    contenedor, así que `frappe.sendmail()` encola en `Email Queue` y ahí se queda para
    siempre (5,122 `Expired`, 40 `Not Sent`, **0 `Sent`**). El correo saliente real de
    nuevosofia sale del HOST (`agente-ia`, Gmail API con service account) — mismo patrón
    que el calendario y Notion.

    Desde el 15-sep el paso Email llama a `POST /correo/enviar` del host (ver
    `_enviar_correo`). El gate ahora es que ese canal esté configurado (`agenda_url` +
    `agenda_token` en Chatwoot Settings, el secreto compartido host↔contenedor), no un
    `Email Account` que nunca va a existir aquí.

    ⚠ `agenda_token` es un campo **Password**: se lee con `get_password()`, no con
    `frappe.db.get_single_value()` — esa vía devuelve el valor **cifrado** y el host
    responde 401 (mismo detalle que `agenda.py`)."""
    s = frappe.get_single("Chatwoot Settings")
    return bool((s.get("agenda_url") or "").strip()
                and s.get_password("agenda_token", raise_exception=False))


def _enviar_correo(para: str, asunto: str, html: str, remitente: str = "contacto@lavendi.mx") -> None:
    """Manda el correo por el host (Gmail API con la service account). Lanza si falla."""
    import requests

    s = frappe.get_single("Chatwoot Settings")
    base = (s.get("agenda_url") or "").rstrip("/")
    token = s.get_password("agenda_token", raise_exception=False) or ""
    resp = requests.post(
        f"{base}/correo/enviar",
        json={"para": para, "asunto": asunto, "html": html, "remitente": remitente},
        headers={"x-sofia-token": token, "Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise Exception(f"host /correo/enviar -> {resp.status_code}: {resp.text[:200]}")


# El widget de agenda de GHL (dominio white-label de la subcuenta). Ya está cortado: el
# CTA de los correos de seguimiento apunta ahí y sin esto saldría muerto.
_URL_AGENDA_GHL = "https://api.sofia.lavendi.mx/widget/bookings/lavendimx"
_URL_AGENDA_PROPIA = "https://agenda.lavendi.mx/"


def _html_seguro(html: str) -> str:
    """Deja el HTML del correo apto para salir hoy.

    Los correos de seguimiento vienen de GHL con dos cosas que mueren con la subcuenta:
    imágenes en `storage.googleapis.com` (la mayoría NO está en `media-map.json`, así que
    no hay a qué sustituirlas) y el CTA al widget de agenda de GHL. Sin esto el correo sale
    con cuadros rotos y un botón muerto. Las imágenes que sí están mapeadas se sustituyen;
    las que no, se quitan (mejor sin imagen que con un roto)."""
    if not html:
        return ""
    import re

    mapa = _media_map()

    def _img(m):
        etiqueta, url = m.group(0), m.group(1)
        return etiqueta.replace(url, mapa[url]) if url in mapa else ""

    html = re.sub(
        r"<img[^>]*src=[\"'](https?://(?:firebasestorage|storage)\.googleapis\.com[^\"']+)[\"'][^>]*>",
        _img, html, flags=re.IGNORECASE,
    )
    # Imágenes de GHL que quedaron como src suelto (sin la etiqueta <img>).
    html = re.sub(
        r"https?://(?:firebasestorage|storage)\.googleapis\.com[^\"'\s<>)]+",
        lambda m: mapa.get(m.group(0), ""), html,
    )
    return html.replace(_URL_AGENDA_GHL, _URL_AGENDA_PROPIA)


def _avisar_respondio(ins: dict) -> None:
    """Avisa que un prospecto en seguimiento automático volvió a escribir.

    En GHL la rama `wait_reply` de cada `wait` no solo detiene la secuencia:
    dispara 2-3 `internal_notification` (verificado en
    `draft-4._Seguimientos.json`). Sin este aviso, "Salió por respuesta"
    quedaba tan silencioso como el handover sin notificar que costó el
    incidente del 09-sep. Reusa el mismo canal que ya usa el paso "Aviso al
    equipo" (`CRM Notification`, in-app) — ver la nota "PENDIENTE DE DECISIÓN
    HUMANA" en el docstring del módulo sobre por qué no es WhatsApp/correo
    todavía. Best-effort: una falla aquí nunca debe bloquear la salida de la
    inscripción."""
    try:
        deal_owner = frappe.db.get_value("CRM Deal", ins["deal"], "deal_owner")
        mensaje = (f"{ins['deal']} respondió a un mensaje de seguimiento automático "
                   "— revisar la conversación.")
        frappe.get_doc({
            "doctype": "CRM Notification",
            "from_user": frappe.session.user,
            "to_user": deal_owner or frappe.session.user,
            "type": "Mention",
            "message": mensaje,
            "notification_text": mensaje,
            "reference_doctype": "CRM Deal",
            "reference_name": ins["deal"],
        }).insert(ignore_permissions=True)
    except Exception as exc:
        frappe.log_error(f"secuencia {ins.get('name')}: aviso de respuesta: {exc}", "Secuencias")


def _ejecutar_paso(ins: dict, paso: dict, sec: dict) -> str:
    """Corre un paso. Devuelve una nota corta para la traza."""
    tipo = paso.get("tipo")
    ctx = _contexto(ins)

    if tipo == "Esperar":
        return "espera"

    if tipo == "WhatsApp":
        cuerpo = _resolver(paso.get("mensaje"), ctx)
        nota_adjunto = ""
        adjunto = None
        if paso.get("adjunto_url"):
            # El adjunto va como ARCHIVO real (multipart), no como URL pegada
            # al texto: con URL el cliente ve un enlace en vez de la imagen
            # (verificado en producción el 14-sep-2026: el mensaje de prueba
            # llegó con el link a `.../secuencia-4-seg-059.png` como texto y 0
            # adjuntos). La URL de GHL nunca se manda cruda — ver
            # `_adjunto_seguro` (punto 8 del docstring): esa subcuenta ya está
            # apagada y esos links devuelven 404 hoy.
            rehospedado = _adjunto_seguro(paso["adjunto_url"])
            if rehospedado:
                adjunto = _leer_adjunto(rehospedado)
            if not adjunto:
                nota_adjunto = " (adjunto no disponible, omitido)"
        # `private=False`: es un mensaje al cliente. Y NO se pausa al agente
        # (ver decisión 2 del encabezado).
        # La conversación puede no existir todavía (deals migrados de GHL): se
        # abre aquí, no en la inscripción, para que un fallo al crear el hilo no
        # deje la inscripción a medias.
        conv = _asegurar_conversacion(ins, sec)
        if not conv:
            return "whatsapp omitido (sin teléfono o sin inbox para abrir conversación)"
        if adjunto:
            nombre, datos, mime = adjunto
            cw.create_message_with_attachment(
                int(conv), cuerpo, filename=nombre, data=datos, content_type=mime)
        else:
            cw.create_message(int(conv), cuerpo)
        return f"whatsapp enviado{nota_adjunto}"

    if tipo == "Email":
        destino = ctx.get("contact.email")
        if not destino:
            # 79% de los contactos migrados no tiene correo (medido 06-sep).
            # Saltar es correcto; abortar dejaría la secuencia atorada para siempre.
            return "email omitido (contacto sin correo)"
        if not _correo_disponible():
            # Ver `_correo_disponible`: sin canal, reportar éxito sería la misma
            # falla silenciosa que ya costó Aerotec y Agri Star.
            return "email omitido (sin canal de correo saliente configurado)"
        asunto = _resolver(paso.get("asunto"), ctx) or "lavendi.mx"
        html = _html_seguro(_resolver(paso.get("html") or paso.get("mensaje"), ctx))
        try:
            _enviar_correo(destino, asunto, html)
        except Exception as exc:
            frappe.log_error(f"secuencia {ins['name']}: email a {destino}: {exc}", "Secuencias")
            return f"email omitido (error al enviar: {exc})"
        return f"email enviado a {destino}"

    if tipo == "Nota interna":
        cw.create_message(int(ins["conversation_id"]),
                          _resolver(paso.get("mensaje"), ctx), private=True)
        return "nota interna"

    if tipo == "Aviso al equipo":
        frappe.get_doc({
            "doctype": "CRM Notification",
            "from_user": frappe.session.user,
            "to_user": frappe.db.get_value("CRM Deal", ins["deal"], "deal_owner")
                       or frappe.session.user,
            "type": "Mention",
            "message": _resolver(paso.get("mensaje"), ctx),
            "notification_text": _resolver(paso.get("mensaje"), ctx),
            "reference_doctype": "CRM Deal",
            "reference_name": ins["deal"],
        }).insert(ignore_permissions=True)
        return "aviso al equipo"

    if tipo == "Tarea":
        titulo = (_resolver(paso.get("asunto"), ctx).strip()
                  or _resolver(paso.get("mensaje"), ctx)[:140].strip()
                  or "Seguimiento")
        frappe.get_doc({
            "doctype": "CRM Task",
            "title": titulo[:140],
            "description": _resolver(paso.get("mensaje"), ctx),
            "reference_doctype": "CRM Deal",
            "reference_docname": ins["deal"],
            # "Todo", no "Backlog": mismo patrón que ya usa el resto del
            # proyecto para crear CRM Task (migracion/load.py,
            # rescatar_leads_congelados.py) — punto 10 del docstring.
            "status": "Todo",
            "priority": "Medium",
            "assigned_to": frappe.db.get_value("CRM Deal", ins["deal"], "deal_owner"),
            # Los 9 `task-notification` reales de GHL traen `dueDate: "now"`,
            # no mañana (verificado en el JSON crudo).
            "due_date": frappe.utils.now_datetime(),
        }).insert(ignore_permissions=True)
        return "tarea creada"

    if tipo == "Actualizar oportunidad":
        # Comparado con GHL: 2 de los 21 nodos reales mueven la etapa del
        # pipeline Y marcan `status: lost` en el mismo nodo (verificado en
        # `draft-4._Seguimientos.json`) — un solo campo/valor no alcanza para
        # replicarlos. `campo_destino`/`valor_destino` aceptan listas
        # separadas por coma; un solo campo sigue funcionando igual que antes.
        campos = [c.strip() for c in (paso.get("campo_destino") or "").split(",") if c.strip()]
        if not campos:
            return "sin campo_destino"
        valores = [v.strip() for v in (paso.get("valor_destino") or "").split(",")]
        if len(valores) == 1 and len(campos) > 1:
            valores = valores * len(campos)
        meta = frappe.get_meta("CRM Deal")
        aplicados, faltantes = [], []
        for campo, valor in zip(campos, valores):
            if meta.get_field(campo):
                frappe.db.set_value("CRM Deal", ins["deal"], campo, valor)
                aplicados.append(f"{campo}={valor}")
            else:
                faltantes.append(campo)
        nota = "; ".join(aplicados) or "nada aplicado"
        if faltantes:
            nota += f" (campo inexistente: {', '.join(faltantes)})"
        return nota

    if tipo == "Etiqueta":
        return "etiqueta (no implementado)"

    return f"tipo desconocido: {tipo}"


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------

def _debe_salir(ins: dict) -> str | None:
    """¿Esta inscripción tiene que detenerse antes de ejecutar su paso?"""
    # Doble vocabulario de estatus: `ghl_status` (migración) y `status` (Frappe)
    # NO son consistentes — hay deals con `ghl_status=open` y `status=Lost`.
    # Mirar solo `ghl_status` dejaba que la secuencia le siguiera escribiendo a
    # una oportunidad perdida (medido 2026-09-16: Azael $200k y Harvey $50k,
    # ambos Lost, iban a recibir su siguiente mensaje). Basta que uno diga
    # perdida/ganada.
    estado_deal = frappe.db.get_value(
        "CRM Deal", ins["deal"], ["ghl_status", "status"], as_dict=True
    ) or {}
    for campo in ("ghl_status", "status"):
        valor = (estado_deal.get(campo) or "").lower()
        if valor in ESTADOS_QUE_SACAN:
            return f"la oportunidad pasó a {valor} ({campo})"

    sec = frappe.db.get_value("Secuencia", ins["secuencia"], "parar_si_responde")
    if sec and ins.get("ultimo_envio_at") and ins.get("conversation_id"):
        try:
            datos = cw.list_messages(int(ins["conversation_id"]))
            for m in reversed(datos.get("payload") or []):
                # message_type 0 = entrante (del cliente)
                if m.get("message_type") != 0:
                    continue
                creado = frappe.utils.get_datetime(
                    frappe.utils.datetime.datetime.fromtimestamp(m.get("created_at"))
                )
                if creado > frappe.utils.get_datetime(ins["ultimo_envio_at"]):
                    # Deja constancia de cuándo respondió (el campo existe en
                    # el doctype desde el diseño original pero nunca se
                    # escribía) y avisa al equipo — punto 6 del docstring.
                    frappe.db.set_value("Secuencia Inscripcion", ins["name"],
                                        "ultimo_mensaje_cliente_at", creado,
                                        update_modified=False)
                    _avisar_respondio(ins)
                    return "el contacto respondió"
                break
        except Exception as exc:
            # Que Chatwoot no responda NO debe sacar a nadie de la secuencia,
            # pero tampoco debe hacernos mandar el siguiente mensaje a ciegas:
            # se pospone.
            frappe.log_error(f"secuencia {ins['name']}: {exc}", "Secuencias")
            return "__posponer__"
    return None


def avanzar():
    """Scheduler. Ejecuta los pasos vencidos, respetando ventana y freno."""
    if not _activo():
        return {"activo": False}

    ahora = _ahora()
    pendientes = frappe.get_all(
        "Secuencia Inscripcion",
        filters={"estado": "Activa", "proximo_en": ["<=", ahora]},
        fields=["name", "secuencia", "deal", "contacto", "conversation_id",
                "paso_actual", "ultimo_envio_at"],
        order_by="proximo_en asc",
    )

    hechos, salidas, pospuestos = [], [], 0
    por_secuencia = {}
    # Cuenta envíos de WhatsApp ya despachados en ESTA corrida (todas las
    # secuencias juntas — hoy solo existe una, pero el freno de ráfaga es del
    # canal físico, no de la secuencia). Ver DELAY_ENTRE_ENVIOS_SEG arriba.
    envios_whatsapp_en_corrida = 0

    for ins in pendientes:
        sec = por_secuencia.get(ins.secuencia)
        if sec is None:
            sec = frappe.get_doc("Secuencia", ins.secuencia).as_dict()
            por_secuencia[ins.secuencia] = sec
        if not sec.get("activa"):
            continue

        tope = sec.get("max_por_corrida")
        tope = MAX_CORRIDA_DEFAULT if tope is None else tope
        if tope and sec.setdefault("_hechos", 0) >= tope:
            continue

        if not _dentro_de_ventana(sec, ahora):
            frappe.db.set_value("Secuencia Inscripcion", ins.name, "proximo_en",
                                _siguiente_hueco(sec, ahora), update_modified=False)
            pospuestos += 1
            continue

        motivo = _debe_salir(dict(ins))
        if motivo == "__posponer__":
            frappe.db.set_value("Secuencia Inscripcion", ins.name, "proximo_en",
                                frappe.utils.add_to_date(ahora, minutes=30),
                                update_modified=False)
            pospuestos += 1
            continue
        if motivo:
            _set_ins(ins.name, {
                "estado": "Salió por respuesta" if "respondió" in motivo
                          else "Salió por cambio de etapa",
                "motivo": motivo,
            }, update_modified=False)
            salidas.append((ins.name, motivo))
            continue

        pasos = sec.get("pasos") or []
        siguiente = ins.paso_actual  # base 0 sobre la lista = "el que sigue"
        if siguiente >= len(pasos):
            _set_ins(ins.name, {
                "estado": "Terminada", "motivo": "secuencia completa",
            }, update_modified=False)
            continue

        paso = pasos[siguiente]
        paso = paso if isinstance(paso, dict) else paso.as_dict()

        # Delay de ráfaga: solo antes de un WhatsApp real, y solo si ya salió
        # al menos uno en esta corrida — el primer envío nunca espera.
        if paso.get("tipo") == "WhatsApp" and envios_whatsapp_en_corrida > 0:
            time.sleep(random.uniform(*DELAY_ENTRE_ENVIOS_SEG))

        try:
            nota = _ejecutar_paso(dict(ins), paso, sec)
        except Exception as exc:
            _set_ins(ins.name, {
                "estado": "Error", "motivo": f"paso {siguiente + 1}: {exc}"[:500],
            }, update_modified=False)
            frappe.log_error(f"secuencia {ins.name} paso {siguiente + 1}: {exc}", "Secuencias")
            continue

        if paso.get("tipo") == "WhatsApp":
            envios_whatsapp_en_corrida += 1

        espera = frappe.utils.cint(paso.get("espera_minutos"))
        proximo = _siguiente_hueco(sec, frappe.utils.add_to_date(ahora, minutes=espera or 1))
        cambios = {"paso_actual": siguiente + 1, "proximo_en": proximo}
        if paso.get("tipo") in ("WhatsApp", "Email"):
            cambios["ultimo_envio_at"] = ahora
        _set_ins(ins.name, cambios, update_modified=False)

        sec["_hechos"] = sec.get("_hechos", 0) + 1
        hechos.append((ins.name, siguiente + 1, nota))

    frappe.db.commit()
    return {"activo": True, "ejecutados": len(hechos), "salidas": len(salidas),
            "pospuestos": pospuestos, "detalle": hechos[:20], "salio": salidas[:10]}


@frappe.whitelist()
def estado() -> dict:
    """Resumen para saber qué está corriendo sin abrir la lista."""
    filas = frappe.db.sql(
        """SELECT i.secuencia, s.titulo, s.activa, i.estado, COUNT(*) n
           FROM `tabSecuencia Inscripcion` i
           JOIN `tabSecuencia` s ON s.name = i.secuencia
           GROUP BY i.secuencia, s.titulo, s.activa, i.estado""",
        as_dict=True,
    )
    return {"motor_activo": _activo(), "resumen": filas}


@frappe.whitelist()
def listar_inscripciones(secuencia: str | None = None, estado: str | None = None) -> dict:
    """Inscripciones para la pantalla "Secuencias": quién está enrolado en qué
    secuencia y en qué paso va. Solo lectura — no pausa ni saca a nadie."""
    filtros = {}
    if secuencia:
        filtros["secuencia"] = secuencia
    if estado:
        filtros["estado"] = estado

    filas = frappe.get_all(
        "Secuencia Inscripcion", filters=filtros,
        fields=["name", "secuencia", "deal", "contacto", "conversation_id",
                "estado", "paso_actual", "proximo_en", "ultimo_envio_at", "motivo"],
        order_by="secuencia asc, paso_actual asc, proximo_en asc",
        limit_page_length=0,
    )

    # Los pasos viven en el doc padre; se cargan una vez por secuencia (no N consultas).
    pasos_por_sec = {}
    for ins in filas:
        sec_name = ins["secuencia"]
        if sec_name not in pasos_por_sec:
            pasos_por_sec[sec_name] = frappe.get_all(
                "Secuencia Paso", filters={"parent": sec_name},
                fields=["idx", "tipo", "nombre_ghl"], order_by="idx asc",
                limit_page_length=0,
            )
        pasos = pasos_por_sec[sec_name]
        i = frappe.utils.cint(ins["paso_actual"])
        ins["total_pasos"] = len(pasos)
        if i < len(pasos):
            p = pasos[i]
            ins["siguiente_paso"] = p.get("nombre_ghl") or p.get("tipo")
            ins["siguiente_tipo"] = p.get("tipo")
        else:
            ins["siguiente_paso"] = "Completada"
            ins["siguiente_tipo"] = None

    # Todas las secuencias (activas o no) con su tamaño, para el panel de la
    # pantalla. Se cuentan los pasos y los inscritos activos en dos agregados —
    # no una consulta por secuencia.
    pasos_total = dict(frappe.db.sql(
        "SELECT parent, COUNT(*) FROM `tabSecuencia Paso` GROUP BY parent"))
    activos = dict(frappe.db.sql(
        "SELECT secuencia, COUNT(*) FROM `tabSecuencia Inscripcion` "
        "WHERE estado='Activa' GROUP BY secuencia"))
    secuencias = frappe.get_all(
        "Secuencia", fields=["name", "titulo", "activa", "producto"],
        order_by="activa desc, titulo asc",
    )
    for s in secuencias:
        s["total_pasos"] = pasos_total.get(s["name"], 0)
        s["inscritos"] = activos.get(s["name"], 0)

    return {
        "motor_activo": _activo(),
        "secuencias": secuencias,
        "inscripciones": filas,
    }


# ---------------------------------------------------------------------------
# Gestión manual desde la pantalla "Secuencias" (2026-09-15)
#
# La pantalla nació de solo lectura (cierre 11). Alejandro pidió las 4 acciones
# que antes solo se podían hacer por consola: ver las secuencias que existen,
# sacar a un contacto, meter a otro y forzar el siguiente paso.
#
# ⚠ Dos de ellas mandan WhatsApp real (forzar un paso de tipo WhatsApp y meter
# a alguien, cuyo primer paso puede ser un envío): por eso van con `@frappe.whitelist`
# + gate de rol, y el frontend pide confirmación antes de dispararlas.
# ---------------------------------------------------------------------------

# Quién puede mover una secuencia a mano. Un Sales User ve la lista (es su
# pipeline) pero no manda mensajes en nombre del equipo ni reescribe el estado
# de una automatización. Mismo criterio que `facturacion.py`.
_ROLES_EDICION = ("System Manager", "Sales Manager")


def _exigir_edicion():
    roles = frappe.get_roles(frappe.session.user)
    if not any(r in roles for r in _ROLES_EDICION) and frappe.session.user != "Administrator":
        frappe.throw("Sin permiso para modificar secuencias", frappe.PermissionError)


@frappe.whitelist()
def buscar_deals(q: str = "", secuencia: str | None = None, limite: int = 20) -> list:
    """Busca oportunidades para inscribir a mano.

    Se busca por nombre de contacto (el caso normal: el humano piensa en la
    persona, no en el id del deal), por empresa, teléfono o correo. Devuelve
    `ya_inscrito` cuando se pide una secuencia, para que el diálogo no ofrezca
    meter dos veces a la misma persona.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return []
    filas = frappe.db.sql(
        """
        SELECT d.name, d.contact, d.organization, d.ghl_status, d.ghl_producto,
               d.chatwoot_conversation_id, d.deal_value,
               c.first_name, c.last_name, c.mobile_no, c.email_id, c.company_name
        FROM `tabCRM Deal` d
        LEFT JOIN `tabContact` c ON c.name = d.contact
        WHERE d.name LIKE %(q)s
           OR CONCAT(IFNULL(c.first_name, ''), ' ', IFNULL(c.last_name, '')) LIKE %(q)s
           OR c.company_name LIKE %(q)s
           OR c.mobile_no LIKE %(q)s
           OR c.email_id LIKE %(q)s
        ORDER BY d.modified DESC
        LIMIT %(lim)s
        """,
        {"q": f"%{q}%", "lim": frappe.utils.cint(limite) or 20},
        as_dict=True,
    )
    inscritos = set()
    if secuencia:
        inscritos = {
            r[0] for r in frappe.db.sql(
                "SELECT deal FROM `tabSecuencia Inscripcion` WHERE secuencia=%s",
                secuencia,
            )
        }
    for f in filas:
        f["contacto_nombre"] = " ".join(
            x for x in [f.get("first_name"), f.get("last_name")] if x
        ).strip() or (f.get("contact") or "")
        f["ya_inscrito"] = f["name"] in inscritos
    return filas


@frappe.whitelist()
def inscribir_deal(secuencia: str, deal: str) -> dict:
    """Inscribe UNA oportunidad a mano, sin el filtro por producto de `inscribir()`.

    `inscribir()` es la carga masiva por rama (todos los deals abiertos de un
    producto). Este es el caso humano: "mete a este contacto a esta secuencia".
    Reusa el mismo camino de conversación; si el deal no tiene hilo en Chatwoot,
    el paso de WhatsApp lo abrirá al ejecutarse (`_asegurar_conversacion`), así
    que se avisa `sin_conversacion` para que el humano no asuma que el primer
    envío será inmediato.
    """
    _exigir_edicion()
    if not frappe.db.exists("Secuencia", secuencia):
        frappe.throw("Esa secuencia no existe")
    d = frappe.db.get_value(
        "CRM Deal", deal, ["name", "contact", "ghl_status", "ghl_producto"], as_dict=True
    )
    if not d:
        frappe.throw("Esa oportunidad no existe")
    existente = frappe.db.get_value(
        "Secuencia Inscripcion", {"secuencia": secuencia, "deal": deal},
        ["name", "estado"], as_dict=True,
    )
    if existente and existente.estado == "Activa":
        frappe.throw("Esa oportunidad ya está inscrita en esa secuencia")

    sec = frappe.get_doc("Secuencia", secuencia).as_dict()
    conv = _conversacion_de(deal, d.contact)
    cuando = _siguiente_hueco(sec, _ahora())

    if existente:
        # Reingreso manual: se REACTIVA la misma inscripción en vez de crear una
        # fila nueva. Si no, alguien que salió (respondió, se sacó a mano) no
        # podría volver a entrar nunca, porque `inscribir()` rechaza cualquier
        # inscripción previa. Arranca de cero y limpia el motivo anterior.
        _set_ins(existente.name, {
            "estado": "Activa", "paso_actual": 0, "proximo_en": cuando,
            "ultimo_envio_at": cuando, "conversation_id": conv, "motivo": "",
        })
        frappe.db.commit()
        return {"ok": True, "name": existente.name, "reactivada": True,
                "sin_conversacion": not bool(conv), "proximo_en": str(cuando),
                "advertencia": None}

    doc = frappe.get_doc({
        "doctype": "Secuencia Inscripcion",
        "secuencia": secuencia,
        "deal": deal,
        "contacto": d.contact,
        "conversation_id": conv,
        "estado": "Activa",
        "paso_actual": 0,
        "proximo_en": cuando,
        # Sembrado, no vacío: una respuesta que llegue antes del primer envío
        # automático también saca al contacto (punto 11 del docstring).
        "ultimo_envio_at": cuando,
    }).insert(ignore_permissions=True)
    _reflejar_en_deal(deal)
    frappe.db.commit()

    advertencia = None
    if d.ghl_status and d.ghl_status != "open":
        advertencia = (f"La oportunidad está en estado '{d.ghl_status}': la secuencia "
                       "la sacará en su primera corrida.")
    elif sec.get("producto") and d.ghl_producto and d.ghl_producto != sec["producto"]:
        advertencia = (f"La oportunidad es de '{d.ghl_producto}' y la secuencia es de "
                       f"'{sec['producto']}'.")
    return {
        "ok": True, "name": doc.name, "sin_conversacion": not bool(conv),
        "proximo_en": str(cuando), "advertencia": advertencia,
    }


@frappe.whitelist()
def sacar_inscripcion(inscripcion: str, motivo: str | None = None) -> dict:
    """Saca a un contacto de la secuencia, a mano.

    Deja el estado en "Salió a mano" en vez de borrar la fila: la inscripción es
    el registro de por qué se detuvo, y borrarla haría que `inscribir()` lo
    volviera a meter en la siguiente carga masiva.
    """
    _exigir_edicion()
    ins = frappe.db.get_value(
        "Secuencia Inscripcion", inscripcion, ["name", "estado"], as_dict=True
    )
    if not ins:
        frappe.throw("Esa inscripción no existe")
    if ins.estado != "Activa":
        return {"ok": False, "mensaje": f"Ya no está activa (estado: {ins.estado})"}
    _set_ins(inscripcion, {
        "estado": "Salió a mano",
        "motivo": (motivo or "").strip() or "Sacado a mano desde la pantalla de Secuencias",
    })
    frappe.db.commit()
    return {"ok": True, "estado": "Salió a mano"}


@frappe.whitelist()
def forzar_paso(inscripcion: str) -> dict:
    """Ejecuta YA el siguiente paso de una inscripción, saltándose la espera.

    "Forzar" es saltarse la ventana horaria y el `proximo_en`, **no** las
    salvaguardas: si la oportunidad ya se ganó/perdió, o el contacto respondió
    después de nuestro último envío, se detiene y lo dice (`_debe_salir`).
    Mandarle el siguiente mensaje de venta a quien ya contestó —o a un deal
    perdido— es justo lo que la secuencia existe para evitar.

    No aplica el delay de ráfaga (`DELAY_ENTRE_ENVIOS_SEG`): es un solo mensaje
    disparado por una persona, no una corrida de hasta 20.
    """
    _exigir_edicion()
    ins = frappe.db.get_value(
        "Secuencia Inscripcion", inscripcion,
        ["name", "secuencia", "deal", "contacto", "conversation_id",
         "estado", "paso_actual", "ultimo_envio_at"],
        as_dict=True,
    )
    if not ins:
        frappe.throw("Esa inscripción no existe")
    if ins.estado != "Activa":
        return {"ok": False, "mensaje": f"La inscripción no está activa ({ins.estado})"}

    sec = frappe.get_doc("Secuencia", ins.secuencia).as_dict()

    motivo = _debe_salir(dict(ins))
    if motivo == "__posponer__":
        return {"ok": False,
                "mensaje": "No se pudo verificar si el contacto respondió (Chatwoot no "
                           "respondió). Intenta de nuevo."}
    if motivo:
        _set_ins(ins.name, {
            "estado": "Salió por respuesta" if "respondió" in motivo
                      else "Salió por cambio de etapa",
            "motivo": motivo,
        }, update_modified=False)
        frappe.db.commit()
        return {"ok": False, "salio": True, "mensaje": f"Se detuvo: {motivo}"}

    pasos = sec.get("pasos") or []
    i = frappe.utils.cint(ins.paso_actual)
    if i >= len(pasos):
        _set_ins(ins.name, {"estado": "Terminada", "motivo": "secuencia completa"},
                 update_modified=False)
        frappe.db.commit()
        return {"ok": False, "mensaje": "La secuencia ya está completa"}

    paso = pasos[i]
    paso = paso if isinstance(paso, dict) else paso.as_dict()
    try:
        nota = _ejecutar_paso(dict(ins), paso, sec)
    except Exception as exc:
        _set_ins(ins.name, {
            "estado": "Error", "motivo": f"paso {i + 1}: {exc}"[:500],
        }, update_modified=False)
        frappe.db.commit()
        frappe.log_error(f"secuencia {ins.name} forzado paso {i + 1}: {exc}", "Secuencias")
        return {"ok": False, "error": str(exc)[:300],
                "mensaje": f"El paso {i + 1} falló: {exc}"}

    espera = frappe.utils.cint(paso.get("espera_minutos"))
    proximo = _siguiente_hueco(sec, frappe.utils.add_to_date(_ahora(), minutes=espera or 1))
    cambios = {"paso_actual": i + 1, "proximo_en": proximo}
    if paso.get("tipo") in ("WhatsApp", "Email"):
        cambios["ultimo_envio_at"] = _ahora()
    _set_ins(ins.name, cambios, update_modified=False)
    frappe.db.commit()

    siguiente = "Completada"
    if i + 1 < len(pasos):
        p = pasos[i + 1]
        p = p if isinstance(p, dict) else p.as_dict()
        siguiente = p.get("nombre_ghl") or p.get("tipo") or "Completada"
    return {
        "ok": True, "nota": nota, "paso": i + 1, "total_pasos": len(pasos),
        "siguiente_paso": siguiente,
    }
