"""Autofollowup: reactivación de conversaciones dejadas en visto (2026-09-24).

Réplica del bot action `advancedFollowup` ("Auto Follow-Up") de GHL Conversation
AI. Plan: `nuevosofia/planes/autofollowup-reactivacion-conversaciones.md`.

Este módulo empieza SOLO con el clasificador (paso 3 del plan) — de solo
lectura, no escribe nada. Es el gate: antes de construir el motor de envío se
mide cuántos hilos mudos son de verdad elegibles (prospecto, sin secuencia, sin
humano reciente). Si el número es bajo, el módulo no compensa el riesgo de la
ráfaga sobre el canal Baileys y el plan se replantea.

CRITERIOS DE EXCLUSIÓN (en este orden, el primero que aplica manda):
  1. El último mensaje real del hilo es de un humano del equipo (marca
     `content_attributes` de `utils/autoria.py`, o heredado sin marca con
     `message_type=outgoing` que no sea del bot ni de un automatismo) — alguien
     ya está atendiendo esa conversación a mano.
  2. El contacto es CLIENTE ACTIVO: el contacto resuelve a un `Customer` de
     ERPNext por el `Dynamic Link` nativo (mismo vínculo que
     `facturacion.cartera_de_contacto` — ver `utils/cliente_erp.py`). Un
     cliente hablando de facturación
     o soporte no es un prospecto que reactivar. ⚠ Este filtro es **inerte en
     los sitios de cliente** (ninguno tiene erpnext → sin `Customer`): por eso
     el barrido se protege con la **allowlist fail-closed** de `barrer()`, no
     con este criterio (ver "Fase 2" en `_es_cliente`).
  3. Ya tiene una `Secuencia Inscripcion` en estado Activa — evita el doble
     mensaje con el motor de secuencias existente (p. ej. "4. Seguimientos —
     PVP").
  4. El deal ya está cerrado (`Won`/`Lost`).
  5. Ya se le confirmó al prospecto, en esta misma conversación, que NO hay
     disponibilidad para la fecha que preguntó (`_tiene_disponibilidad_cerrada`,
     2026-10-15) — insistir después de eso es falsa esperanza, no reactivación.
     Caso real que lo originó: Lizeth Guadarrama (conv 1076, deal
     `CRM-DEAL-2026-00045`, Six Gardens) recibiendo follow-ups sobre una fecha
     que el staff ya cerró por evento sin cupo. Distinto del criterio 2: ahí
     se excluye a quien ya ES cliente; aquí se excluye a quien ya recibió un
     NO para lo que pidió, sea o no cliente.

Sin deal ligado a la conversación (`chatwoot_conversation_id`), NO se excluye
por defecto — son leads nuevos aún sin oportunidad, que sí son candidatos.

GATE FAIL-CLOSED (2026-09-27): `barrer()` solo corre en los inbox declarados en
`site_config.json` (`autofollow_inboxes`). Si la clave falta o está vacía, no
barre ningún inbox. Hoy ningún sitio la declara → el barrido está apagado por
diseño; **encender autofollow es una acción explícita y aparte**.
"""

import json
import random
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import frappe

from . import autoria, chatwoot_client, telefono

# ---------------------------------------------------------------------------
# Fallback Fase 2 de `_es_cliente` para sitios SIN erpnext (2026-10-12)
# ---------------------------------------------------------------------------
#
# Origen: bug reportado por Six Gardens el 2026-10-12 — el guard `erpnext`
# deja el criterio "ya es cliente" inerte en todo sitio sin ERPNext (hoy,
# todos los de cliente), así que el autofollowup le mandaba mensajes a quien
# ya había reservado/pagado. Instalar ERPNext fue evaluado y descartado por
# Alejandro: el hook que crea `Customer` ahí es automático sobre cualquier
# `Contact` nuevo/editado, sin relación a venta ni pago — empeoraría el bug,
# no lo resolvería.
#
# Señal elegida: la etiqueta nativa "Cliente" que el staff del cliente ya
# puede poner a mano en su app de WhatsApp Business (viaja por Baileys, sin
# API oficial de Meta). Los webhooks de labels de Evolution siguen apagados a
# propósito (encenderlos es trabajo aparte, con su propio gate de
# producción) — y no hacen falta: Evolution persiste la asociación en su tabla
# `Chat` siempre que `DATABASE_SAVE_DATA_CHATS=true`; el webhook solo reenvía
# el evento a terceros (verificado en `whatsapp.baileys.service.ts`, el
# handler `LABELS_ASSOCIATION` escribe con `addLabel` antes de reenviar).
#
# Se lee por la **API REST** de Evolution, no por su Postgres (decidido el
# 2026-10-13 tras verificarlo): ese Postgres escucha solo en `127.0.0.1`, así
# que consultarlo desde el contenedor de Frappe exigía abrirlo y reiniciar un
# cluster compartido con otro cliente. El puerto REST ya es alcanzable desde
# aquí por la misma red docker que Chatwoot usa para hablarle (regla UFW
# preexistente) — cero cambios de infraestructura, cero reinicios.
#
# Genérico por diseño, no hardcodeado a Six Gardens: todas las claves son de
# `site_config.json`, mismo patrón plano que `CLAVE_INBOXES` de abajo. Un
# sitio sin estas claves cae a `False` sin intentar ninguna conexión — fail-
# closed, igual que el guard `erpnext` que reemplaza.
CLAVE_EVO_BASE_URL = "evolution_api_base_url"
CLAVE_EVO_API_KEY = "evolution_api_key"
CLAVE_EVO_INSTANCE = "evolution_api_instance"
CLAVE_EVO_ETIQUETA = "evolution_api_etiqueta_cliente"  # default "Cliente"

TIMEOUT_EVO = 8


def _config_evolution() -> dict | None:
    """Datos de acceso a la API REST de la instancia Evolution del sitio, leídos
    de `site_config.json`. Devuelve `None` si falta cualquiera, para que
    `_es_cliente_por_etiqueta` corte antes de tocar la red (sin consultas
    muertas, mismo criterio que el guard `erpnext`)."""
    conf = frappe.conf
    base = conf.get(CLAVE_EVO_BASE_URL)
    api_key = conf.get(CLAVE_EVO_API_KEY)
    instancia = conf.get(CLAVE_EVO_INSTANCE)
    if not (base and api_key and instancia):
        return None
    return {
        "base": str(base).rstrip("/"),
        "api_key": api_key,
        "instancia": instancia,
        "etiqueta": conf.get(CLAVE_EVO_ETIQUETA) or "Cliente",
    }


def _pedir_evolution(cfg: dict, ruta: str, cuerpo: dict | None = None):
    """GET (o POST si `cuerpo`) a la API de Evolution. Devuelve el JSON, o
    `None` ante cualquier error — el llamador siempre trata `None` como "no
    sé", que aquí equivale a "no es cliente" (fail-closed)."""
    url = f"{cfg['base']}{ruta}"
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(
        url,
        data=datos,
        method="POST" if datos is not None else "GET",
        headers={"apikey": cfg["api_key"], "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_EVO) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        frappe.log_error(
            title="autofollowup._pedir_evolution",
            message=f"{ruta}: {e}",
        )
        return None


def _cache_barrido(clave: str, calcular):
    """Memo por job/request (`frappe.local`): el barrido consulta decenas de
    contactos y el catálogo de chats y etiquetas es el mismo para todos. No se
    usa `frappe.cache()` a propósito — una etiqueta recién puesta debe verse en
    el barrido siguiente (cada 20 min), no cuando expire un TTL."""
    almacen = getattr(frappe.local, "_autofollowup_evo_cache", None)
    if almacen is None:
        almacen = {}
        frappe.local._autofollowup_evo_cache = almacen
    if clave not in almacen:
        almacen[clave] = calcular()
    return almacen[clave]


def _label_ids_cliente(cfg: dict) -> set:
    """Ids de etiqueta cuyo nombre es la etiqueta configurada.

    Comparación laxa (sin espacios sobrantes, sin distinguir mayúsculas): el
    staff la teclea a mano en su teléfono y Evolution guarda el nombre tal
    cual, con espacio final incluido — en SIGARDENS existe literal
    `"SEGUIMIENTO "`.

    ⚠ El id viene en `id`, NO en `labelId` (verificado contra la API real el
    2026-10-13: `/label/findLabels` serializa la columna `labelId` del Postgres
    como `id`). Se aceptan ambos por si otra versión de Evolution lo expone
    con el nombre de la columna."""

    def calcular():
        etiquetas = _pedir_evolution(cfg, f"/label/findLabels/{cfg['instancia']}")
        if not isinstance(etiquetas, list):
            return set()
        buscada = str(cfg["etiqueta"]).strip().casefold()
        encontradas = set()
        for e in etiquetas:
            if not isinstance(e, dict):
                continue
            ident = e.get("id") or e.get("labelId")
            if ident and str(e.get("name") or "").strip().casefold() == buscada:
                encontradas.add(str(ident))
        return encontradas

    # La etiqueta va en la clave del memo: si no, un cambio de
    # `evolution_api_etiqueta_cliente` dentro del mismo job devolvería el
    # resultado de la etiqueta anterior.
    return _cache_barrido(f"label_ids::{cfg['etiqueta']}", calcular)


def _remote_jid_de_telefono(cfg: dict, corto: str) -> str | None:
    """`Chat.remoteJid` del chat 1:1 de ese teléfono, cubriendo las DOS formas
    de direccionamiento que esta cuenta usa a la vez (medido el 2026-10-13 en
    SIXGARDENS: de 174 chats 1:1, 120 van por LID y 54 por JID de teléfono):

      · **JID de teléfono** (`5212851122710@s.whatsapp.net`): el número está en
        el propio `remoteJid`, match directo.
      · **LID** (`54271340990484@lid`): el `remoteJid` no contiene el número;
        el teléfono real viaja en `key.remoteJidAlt` del último mensaje del
        chat, que `findChats` ya devuelve.

    ⚠ Cubrir solo el camino LID era un hueco real: habría ignorado la etiqueta
    en ~1 de cada 3 chats recientes.

    `findChats` solo devuelve chats con actividad reciente (verificado el
    2026-10-13: 186 de los que la cuenta acumula). No es limitante para este
    uso — un contacto en follow-up activo tiene mensajes recientes por
    definición, y los 13 activos de SIXGARDENS resolvieron 13/13 — pero por si
    acaso, si el índice falla se intenta el JID de teléfono canónico directo."""

    def calcular():
        chats = _pedir_evolution(cfg, f"/chat/findChats/{cfg['instancia']}", {})
        if not isinstance(chats, list):
            return {}
        indice = {}
        for chat in chats:
            jid = chat.get("remoteJid") or ""
            if not jid or jid.endswith("@g.us"):
                continue
            candidatos = [jid.split("@", 1)[0]]
            alt = ((chat.get("lastMessage") or {}).get("key") or {}).get("remoteJidAlt")
            if alt:
                candidatos.append(str(alt).split("@", 1)[0])
            for candidato in candidatos:
                llave = telefono.corto(candidato)
                if llave:
                    indice.setdefault(llave, jid)
        return indice

    jid = _cache_barrido("indice_chats", calcular).get(corto)
    if jid:
        return jid
    directo = f"{telefono.canonico_whatsapp(corto)}@s.whatsapp.net"
    chat = _pedir_evolution(
        cfg, f"/chat/findChatByRemoteJid/{cfg['instancia']}?remoteJid={directo}"
    )
    return directo if isinstance(chat, dict) and chat.get("remoteJid") else None


def _es_cliente_por_etiqueta(contact_name: str) -> bool:
    """Fallback Fase 2: ¿el chat de WhatsApp Business de este `Contact` trae la
    etiqueta nativa "Cliente"? Se resuelve contra la API REST de la instancia
    Evolution del sitio.

    Camino del dato:
      `Contact.mobile_no`/`phone` (por los últimos 10 dígitos, el criterio de
      identidad de todo el CRM — `utils/telefono.corto`)
        --`/chat/findChats`-->       `Chat.remoteJid` del chat 1:1 (LID o JID
                                     de teléfono, ver `_remote_jid_de_telefono`)
        --`/chat/findChatByRemoteJid`--> `labels` (lista de `labelId`)
        --`/label/findLabels`-->     se cruza contra el `labelId` cuyo nombre
                                     es la etiqueta configurada.

    Fail-closed en cada paso — perder un follow-up es más seguro que mandárselo
    a quien ya es cliente, mismo sesgo que el resto del módulo: sin claves en
    `site_config.json`, sin teléfono en el `Contact`, sin chat, sin etiqueta
    creada en la cuenta, o ante cualquier error de red → `False`.

    ⚠ LIMITACIÓN SIN VERIFICAR (no bloqueante para el código, sí para confiar
    en el resultado): medido el 2026-10-13, la tabla `Chat` de SIXGARDENS tiene
    sus 89 filas creadas en el mismo segundo (sync inicial del 14-sep) y solo
    UNA se actualizó después, y no hay ni un evento `labels association` en los
    logs disponibles. El código de Evolution sí hace `INSERT ... ON CONFLICT`
    al etiquetar (crearía la fila aunque no exista), pero **no está confirmado
    con un caso real** que el evento llegue desde el teléfono. Validar con un
    chat de prueba etiquetado a mano antes de confiar en este fallback."""
    if not contact_name:
        return False
    cfg = _config_evolution()
    if not cfg:
        return False
    datos = frappe.db.get_value("Contact", contact_name, ["mobile_no", "phone"], as_dict=True) or {}
    corto = telefono.corto(datos.get("mobile_no") or datos.get("phone"))
    if not corto:
        return False
    ids_cliente = _label_ids_cliente(cfg)
    if not ids_cliente:
        return False
    jid = _remote_jid_de_telefono(cfg, corto)
    if not jid:
        return False
    chat = _pedir_evolution(
        cfg, f"/chat/findChatByRemoteJid/{cfg['instancia']}?remoteJid={jid}"
    )
    if not isinstance(chat, dict):
        return False
    etiquetas_del_chat = chat.get("labels") or []
    if not isinstance(etiquetas_del_chat, list):
        return False
    return bool({str(x) for x in etiquetas_del_chat} & {str(x) for x in ids_cliente})


def _es_cliente(contact_name: str | None) -> bool:
    """Mismo vínculo que `facturacion.cartera_de_contacto`: `Dynamic Link`
    Contact -> Customer. Sin vínculo, NO se asume cliente (falso negativo
    es más seguro aquí: perdemos un follow-up, no arriesgamos molestar a un
    cliente que paga).

    Guard `erpnext` (2026-09-27, Fase 2 implementada 2026-10-12): sin el
    doctype `Customer` — ningún sitio de cliente tiene erpnext hoy — el
    vínculo original no puede existir. En vez de `False` directo, se delega a
    `_es_cliente_por_etiqueta` (fallback por la etiqueta nativa "Cliente" de
    WhatsApp Business, ver docstring de esa función). Si el sitio no tiene las
    claves de acceso en `site_config.json` (`evolution_api_*`), ese fallback
    también es `False` sin consultar nada — mismo resultado que antes, cero
    regresión en sitios que no lo configuren.

    Sigue siendo cierto que quien de verdad protege el barrido es la allowlist
    fail-closed de `barrer()` (`autofollow_inboxes`), no este criterio — este
    fallback reduce falsos negativos, no es la única defensa.
    """
    if not contact_name:
        return False
    if "erpnext" not in frappe.get_installed_apps():
        return _es_cliente_por_etiqueta(contact_name)
    # Vínculo nativo (`Dynamic Link`), no el viejo `ghl_contact_id`: ver
    # `utils/cliente_erp.py`. Con el campo de texto, un contacto fusionado
    # podía resolver al Customer equivocado (o a ninguno).
    from frappe_chatwoot.utils import cliente_erp

    return bool(cliente_erp.customer_de_contacto(contact_name))


def _deal_de(conversation_id) -> dict | None:
    return frappe.db.get_value(
        "CRM Deal",
        {"chatwoot_conversation_id": str(conversation_id)},
        ["name", "status", "contact", "deal_owner"],
        as_dict=True,
    )


def _tiene_secuencia_activa(deal_name: str | None) -> bool:
    if not deal_name:
        return False
    return bool(frappe.db.exists("Secuencia Inscripcion", {"deal": deal_name, "estado": "Activa"}))


def _ultimo_es_humano(last_msg: dict) -> bool:
    """El último mensaje saliente lo escribió una persona, no el bot ni un
    automatismo. Reusa las marcas de `utils/autoria.py`; sin marca (mensajes
    anteriores a ese cambio) se asume humano — el sesgo correcto es no
    reactivar de más, no reactivar de menos."""
    if last_msg.get("message_type") not in (1, "outgoing"):
        return False
    ca = last_msg.get("content_attributes") or {}
    if ca.get("sofia_bot"):
        return False
    if ca.get("automatico"):
        return False
    return True


# ---------------------------------------------------------------------------
# Guard "disponibilidad cerrada" (2026-10-15)
# ---------------------------------------------------------------------------
#
# Origen: hallazgo del 2026-10-13 en Six Gardens (conv 1076 inbox 7, deal
# `CRM-DEAL-2026-00045`) — el autofollow-up seguía insistiéndole a Lizeth
# Guadarrama sobre una fecha (18-20 dic) que el staff YA había cerrado por
# falta de cupo, preguntándole si quería que "un asesor revise si se liberó
# algo". Bug DISTINTO al de `_es_cliente_por_etiqueta` (ese excluye a quien ya
# ES cliente; este excluye a quien ya recibió un NO para lo que pidió, sea o
# no cliente — Lizeth es un prospecto rechazado, deal en "Lead", nunca pagó
# ni reservó, no aplica el criterio 2).
#
# Mismo patrón que el guard de salida `lib/guard-disponibilidad.js` del
# proyecto hermano `agente-ia` (Sofía Lite) — ahí se NEUTRALIZA en vivo la
# frase que el modelo intenta decir; aquí se LEE el historial ya enviado para
# decidir si vale la pena seguir insistiendo. Se portan solo las frases de
# NEGACIÓN de ese archivo: que el agente haya dicho "sí tengo disponibilidad"
# no es señal de que haya que parar el follow-up, es la señal contraria.
# Mantener las dos listas sincronizadas a mano si se edita una — no hay build
# compartido entre JS y Python.
PATRONES_DISPONIBILIDAD_CERRADA = [
    re.compile(r"\bno tengo disponibilidad\b", re.IGNORECASE),
    re.compile(r"\bno hay disponibilidad\b", re.IGNORECASE),
    re.compile(r"\bsin disponibilidad\b", re.IGNORECASE),
    re.compile(r"\bno est[áa]\s+disponible\b", re.IGNORECASE),
]

# Cuántos mensajes de la página más reciente se revisan. `list_messages` sin
# `before`/`after` ya devuelve solo una página (máx. 100, orden ascendente) —
# este tope es defensivo, no una paginación real: si la respuesta "ya no hay
# disponibilidad" quedó más atrás que esto, es una conversación vieja que de
# todos modos cayó fuera de `dias_max` en `clasificar()`.
LIMITE_MENSAJES_DISPONIBILIDAD = 30


def _tiene_disponibilidad_cerrada(conversation_id) -> bool:
    """¿Ya se le dijo a este prospecto, en esta misma conversación, que NO
    hay disponibilidad para la fecha que preguntó? Si sí, un follow-up
    ("¿ya pudiste confirmar...?") solo le da falsa esperanza — caso real:
    Lizeth Guadarrama, ver bloque de arriba.

    Revisa los mensajes SALIENTES (del bot o del staff — ambos cuentan: el
    prospecto ya recibió la respuesta sin importar quién la escribió) de la
    página más reciente de la conversación.

    Fail-closed INVERTIDO a propósito respecto al resto del módulo: ante
    cualquier error de red o respuesta vacía se asume que NO está cerrada
    (`False`) — perder esta exclusión una corrida y mandar un follow-up de
    más es más seguro que la inversa, porque `clasificar()` se vuelve a
    correr en cada barrido (no es una ventana que se cierra para siempre)."""
    if not conversation_id:
        return False
    try:
        pagina = chatwoot_client.list_messages(conversation_id)
    except Exception as e:
        frappe.log_error(
            title="autofollowup._tiene_disponibilidad_cerrada", message=str(e)
        )
        return False
    mensajes = (pagina or {}).get("payload") or []
    for m in mensajes[-LIMITE_MENSAJES_DISPONIBILIDAD:]:
        if m.get("message_type") not in (1, "outgoing"):
            continue
        texto = m.get("content") or ""
        if any(p.search(texto) for p in PATRONES_DISPONIBILIDAD_CERRADA):
            return True
    return False


@frappe.whitelist()
def clasificar(inbox_id, dias_max: int = 30) -> dict:
    """Solo lectura. Devuelve, por cada conversación abierta y muda del inbox
    (dentro de `dias_max` días de silencio), el veredicto elegible/excluida.

    `dias_max` acota el barrido a hilos recientes — no tiene sentido reactivar
    algo de hace 3 meses; por defecto 30 días cubre de sobra el backlog actual
    (máximo medido: 17 días, 2026-09-23)."""
    convs = []
    for pagina in range(1, 41):  # 40 x 25 = 1000, techo generoso; corta antes por bandeja vacía
        lote = chatwoot_client.list_conversations(inbox_id=int(inbox_id), status="open", page=pagina)
        if not lote:
            break
        convs.extend(lote)
    ahora = datetime.now(timezone.utc)
    resultado = []
    for c in convs:
        last = c.get("last_non_activity_message") or {}
        if not last or last.get("message_type") not in (1, "outgoing"):
            continue  # no muda: el último real es del prospecto, o no hay mensajes
        ts = last.get("created_at")
        if not ts:
            continue
        cuando = datetime.fromtimestamp(ts, tz=timezone.utc)
        horas_mudo = (ahora - cuando).total_seconds() / 3600
        if horas_mudo > dias_max * 24:
            continue

        deal = _deal_de(c["id"]) or {}
        motivo = None
        if _ultimo_es_humano(last):
            motivo = "último mensaje es de un humano del equipo"
        elif _es_cliente(deal.get("contact")):
            motivo = "es cliente activo (Customer vinculado)"
        elif _tiene_secuencia_activa(deal.get("name")):
            motivo = "ya está en una Secuencia activa"
        elif deal.get("status") in ("Won", "Lost"):
            motivo = f"deal cerrado ({deal.get('status')})"
        elif _tiene_disponibilidad_cerrada(c["id"]):
            motivo = "ya se confirmó sin disponibilidad para la fecha consultada"

        sender = (c.get("meta") or {}).get("sender") or {}
        resultado.append({
            "conversation_id": c.get("id"),
            "nombre": sender.get("name"),
            "horas_mudo": round(horas_mudo, 1),
            "deal": deal.get("name"),
            "status": deal.get("status"),
            "elegible": motivo is None,
            "motivo": motivo,
        })

    resultado.sort(key=lambda r: -r["horas_mudo"])
    elegibles = [r for r in resultado if r["elegible"]]
    return {
        "inbox_id": int(inbox_id),
        "total_mudas": len(resultado),
        "elegibles": len(elegibles),
        "detalle": resultado,
    }


# ---------------------------------------------------------------------------
# Motor de envío (paso 6 del plan)
# ---------------------------------------------------------------------------
#
# Cron propio, NO el de `secuencias.py` — ese corre `0 12-18 L-V` con tope 4;
# barrer cada ~20 min para un paso a +3h multiplicaría el ritmo de esa
# secuencia (34 inscritos, mismo canal Baileys). Ver objeción del plan.
#
# Ventana L-V estricta HARDCODED (no es un campo por inbox): es una regla de
# negocio pareja para todos los clientes (B2B, nadie decide un lunes-viernes
# el fin de semana), no una preferencia — igual que `secuencias.py` hardcodea
# `1-5` en el crontab en vez de volverlo un campo.
DIAS_HABILES = (1, 2, 3, 4, 5)
DELAY_ENTRE_ENVIOS_SEG = (60, 120)

# Allowlist fail-closed de inboxes elegibles (site_config `autofollow_inboxes`).
#
# Nombre elegido: `autofollow_inboxes` — sigue la convención de claves planas por
# sitio ya usadas (`sofia_plan`, `sofia_lite_max_usuarios`, `lead_owner_default`,
# `brand_app_name`) y nombra explícitamente la capacidad a la que pertenece.
#
# Semántica (fail-closed): si la clave FALTA o queda VACÍA, `barrer()` no corre
# en ningún inbox. Hoy equivale a "nadie declaró → no se escribe a nadie", aunque
# un `Agente IA` tuviera `followup_activo=1` por error. Si existe, es la lista de
# `inbox_id` (ints) elegibles; cualquier otro inbox queda fuera, **incluso con
# `forzar_inbox`** (el bypass manual NO se salta la allowlist).
#
# Valor sugerido por sitio (solo propuesta, NO se escribe aquí): el inbox real
# del agente del cliente — p. ej. `"autofollow_inboxes": [7]` en sixgardens
# (inbox 7) y `[18]` en grupogardenias (inbox 18). Un sitio NO se enciende solo
# por declarar la clave: encender autofollow (`followup_activo=1`) es una acción
# explícita y aparte.
CLAVE_INBOXES = "autofollow_inboxes"


def _parse_inboxes(crudo) -> set[int]:
    """Normaliza el valor crudo de `autofollow_inboxes` a un set de ints.

    Acepta lista/tupla de ints (`[5, 7]`) o string con ints separados por coma o
    espacio (`"5, 7"`). Lo que no parsea a int se ignora en vez de reventar — un
    valor mal escrito deja la allowlist más chica (fail-closed), nunca más
    grande."""
    if not crudo:
        return set()
    if isinstance(crudo, str):
        partes = crudo.replace(",", " ").split()
    elif isinstance(crudo, (list, tuple, set)):
        partes = list(crudo)
    else:
        partes = [crudo]
    permitidos = set()
    for p in partes:
        try:
            permitidos.add(int(p))
        except (TypeError, ValueError):
            continue
    return permitidos


def _inboxes_permitidos() -> set[int]:
    """Allowlist efectiva del sitio (lee `site_config.json` vía `frappe.conf`)."""
    return _parse_inboxes(frappe.conf.get(CLAVE_INBOXES))


def _permitido(inbox_id, permitidos: set) -> bool:
    """¿El inbox está en la allowlist ya normalizada? Un valor no numérico cae
    a `False` (no se barre por accidente)."""
    try:
        return int(inbox_id) in permitidos
    except (TypeError, ValueError):
        return False


def _config_agente() -> dict:
    """Mismo canal HTTP que `/planeacion/generar` y `/agenda/*`: reusa
    `Chatwoot Settings.agenda_url`/`agenda_token`, no credenciales propias."""
    settings = frappe.get_single("Chatwoot Settings")
    return {
        "url": (getattr(settings, "agenda_url", None) or "").rstrip("/"),
        "token": settings.get_password("agenda_token", raise_exception=False)
        if getattr(settings, "agenda_token", None) else "",
    }


def _pedir_texto(conversation_id: int, paso: int) -> str:
    cfg = _config_agente()
    if not (cfg["url"] and cfg["token"]):
        raise RuntimeError("agente no configurado (Chatwoot Settings → agenda_url/agenda_token)")
    req = urllib.request.Request(
        f"{cfg['url']}/followup/redactar",
        data=json.dumps({"conversationId": conversation_id, "paso": paso}).encode(),
        headers={"Content-Type": "application/json", "x-sofia-token": cfg["token"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"redactor HTTP {e.code}: {e.read().decode()[:300]}")
    if not data.get("ok"):
        raise RuntimeError(f"redactor: {data.get('error')} — {data.get('mensaje')}")
    return data["texto"]


def _hhmm(cuando: datetime) -> str:
    return cuando.strftime("%H:%M")


def _dentro_de_ventana(agente, cuando: datetime) -> bool:
    if cuando.isoweekday() not in DIAS_HABILES:
        return False
    ini = agente.get("followup_ventana_inicio") or "08:00"
    fin = agente.get("followup_ventana_fin") or "18:00"
    return ini <= _hhmm(cuando) < fin


def _siguiente_hueco(agente, desde: datetime) -> datetime:
    """El primer instante >= `desde` dentro de la ventana L-V del agente.
    Mismo algoritmo que `secuencias._siguiente_hueco` (salta al inicio del
    siguiente día hábil en vez de iterar minuto a minuto sobre un fin de
    semana)."""
    ini = agente.get("followup_ventana_inicio") or "08:00"
    h_ini, m_ini = (int(x) for x in ini.split(":"))
    cand = desde
    for _ in range(14):
        if _dentro_de_ventana(agente, cand):
            return cand
        if cand.isoweekday() in DIAS_HABILES and _hhmm(cand) < ini:
            cand = cand.replace(hour=h_ini, minute=m_ini, second=0, microsecond=0)
            continue
        cand = (cand + timedelta(days=1)).replace(
            hour=h_ini, minute=m_ini, second=0, microsecond=0)
    return cand


def _delta_paso(paso_row) -> timedelta:
    valor = paso_row.get("espera_valor") or 1
    if paso_row.get("espera_unidad") == "Días":
        return timedelta(days=valor)
    return timedelta(hours=valor)


def _crear_followups_nuevos(agente, inbox_id: int) -> int:
    """Sincroniza `clasificar()` contra `Chatwoot Followup`: crea una fila por
    cada elegible que todavía no tiene ninguna (activa o cerrada) — no
    reabre una que ya salió por respuesta o se agotó."""
    pasos = agente.get("followup_pasos") or []
    if not pasos:
        return 0
    r = clasificar(inbox_id)
    creados = 0
    for h in r["detalle"]:
        if not h["elegible"]:
            continue
        if frappe.db.exists("Chatwoot Followup", {"conversation_id": h["conversation_id"]}):
            continue
        proximo = _siguiente_hueco(agente, _ahora() + _delta_paso(pasos[0]))
        frappe.get_doc({
            "doctype": "Chatwoot Followup",
            "conversation_id": h["conversation_id"],
            "inbox_id": str(inbox_id),
            "deal": h.get("deal"),
            "contacto_nombre": h.get("nombre"),
            "estado": "Activa",
            "paso_actual": 0,
            "proximo_en": proximo,
        }).insert(ignore_permissions=True)
        creados += 1
    return creados


def _ahora():
    return frappe.utils.now_datetime()


def _respondio_desde(conversation_id: int, desde) -> bool:
    """¿Llegó un mensaje del prospecto después de `desde`? Un solo GET a la
    conversación (trae `last_non_activity_message`), no una relectura del
    historial completo."""
    conv = chatwoot_client.get_conversation(conversation_id)
    last = conv.get("last_non_activity_message") or {}
    if last.get("message_type") not in (0, "incoming"):
        return False
    ts = last.get("created_at")
    if not ts:
        return False
    cuando = datetime.fromtimestamp(ts, tz=timezone.utc)
    return cuando > (desde.replace(tzinfo=timezone.utc) if desde.tzinfo is None else desde)


def _sigue_elegible(fila) -> str | None:
    """Re-chequeo al momento de enviar (no solo al inscribir): un humano pudo
    haber tomado el hilo, o el deal pudo cerrarse, desde que se creó la fila."""
    conv = chatwoot_client.get_conversation(fila.conversation_id)
    last = conv.get("last_non_activity_message") or {}
    if last and _ultimo_es_humano(last):
        return "un humano del equipo ya escribió en el hilo"
    if fila.deal:
        status = frappe.db.get_value("CRM Deal", fila.deal, "status")
        if status in ("Won", "Lost"):
            return f"el deal se cerró ({status})"
        contacto = frappe.db.get_value("CRM Deal", fila.deal, "contact")
        if _es_cliente(contacto):
            return "el contacto ya es cliente activo"
    if _tiene_disponibilidad_cerrada(fila.conversation_id):
        return "ya se confirmó sin disponibilidad para la fecha consultada"
    return None


def _enviar_paso(fila, agente, pasos: list, dry_run: bool) -> None:
    paso_num = fila.paso_actual + 1  # 1-based para el redactor y el humano
    texto = _pedir_texto(fila.conversation_id, paso_num)

    if dry_run:
        chatwoot_client.create_message(
            fila.conversation_id,
            f"[DRY-RUN autofollowup, paso {paso_num}] {texto}",
            private=True,
        )
    else:
        chatwoot_client.create_message(
            fila.conversation_id, texto, private=False,
            content_attributes=autoria.marca_automatica("followup", f"paso {paso_num}"),
        )

    fila.paso_actual = paso_num
    fila.ultimo_out_at = _ahora()
    fila.texto_ultimo = texto
    if paso_num >= len(pasos):
        fila.estado = "Terminada"
    else:
        fila.proximo_en = _siguiente_hueco(agente, _ahora() + _delta_paso(pasos[paso_num]))
    fila.save(ignore_permissions=True)
    frappe.db.commit()


def _procesar_vencidos(agente, inbox_id: int, tope: int, dry_run: bool) -> dict:
    pasos = agente.get("followup_pasos") or []
    pendientes = frappe.get_all(
        "Chatwoot Followup",
        filters={"inbox_id": str(inbox_id), "estado": "Activa", "proximo_en": ["<=", _ahora()]},
        fields=["name", "conversation_id", "deal", "paso_actual", "ultimo_out_at", "creation"],
        order_by="proximo_en asc",
        limit_page_length=tope,
    )
    enviados, saltados, errores = 0, 0, 0
    for i, row in enumerate(pendientes):
        fila = frappe.get_doc("Chatwoot Followup", row.name)
        try:
            desde = fila.ultimo_out_at or fila.creation
            if _respondio_desde(fila.conversation_id, desde):
                fila.estado = "Salió por respuesta"
                fila.motivo = "el prospecto respondió"
                fila.save(ignore_permissions=True)
                frappe.db.commit()
                saltados += 1
                continue
            motivo_exclusion = _sigue_elegible(fila)
            if motivo_exclusion:
                fila.estado = "Salió a mano"
                fila.motivo = motivo_exclusion
                fila.save(ignore_permissions=True)
                frappe.db.commit()
                saltados += 1
                continue
            _enviar_paso(fila, agente, pasos, dry_run)
            enviados += 1
            if i < len(pendientes) - 1:
                time.sleep(random.uniform(*DELAY_ENTRE_ENVIOS_SEG))
        except Exception as e:
            frappe.log_error(title="autofollowup._procesar_vencidos", message=str(e))
            errores += 1
    return {"enviados": enviados, "saltados": saltados, "errores": errores}


@frappe.whitelist()
def barrer(forzar_inbox=None, dry_run: int = 0) -> dict:
    """Job del cron. Para cada `Agente IA` con `followup_activo=1`: sincroniza
    elegibles nuevos y envía los pasos vencidos (tope + delay anti-ban).

    Allowlist fail-closed (2026-09-27): solo corre en los inbox declarados en
    `site_config.autofollow_inboxes`. Sin la clave (o vacía) NO barre ningún
    inbox; un inbox fuera de la lista se omite aunque su `Agente IA` esté
    activo.

    `forzar_inbox` + `dry_run=1`: SOLO para validación manual (paso 8 del
    plan) — procesa ese inbox aunque `followup_activo` esté en 0, y escribe
    el texto como nota privada en vez de mandarlo. Nunca se llama así desde
    el cron. **No se salta la allowlist**: un `forzar_inbox` fuera de la
    lista no corre (si no, el bypass manual sería la puerta trasera del gate)."""
    dry_run = bool(int(dry_run))
    permitidos = _inboxes_permitidos()
    if not permitidos:
        return {"omitido": "sin inboxes declarados en site_config.autofollow_inboxes (fail-closed)"}
    if forzar_inbox is not None and not _permitido(forzar_inbox, permitidos):
        return {"omitido": f"inbox {forzar_inbox} fuera de autofollow_inboxes"}

    resultado = {}
    agentes = frappe.get_all("Agente IA", filters={"active": 1}, pluck="name")
    if forzar_inbox is not None and str(forzar_inbox) not in agentes:
        agentes = [str(forzar_inbox)]
    for inbox_id in agentes:
        if not _permitido(inbox_id, permitidos):
            resultado[inbox_id] = {"omitido": "fuera de autofollow_inboxes"}
            continue
        agente = frappe.get_doc("Agente IA", inbox_id).as_dict()
        forzado = forzar_inbox is not None and str(forzar_inbox) == str(inbox_id)
        if not agente.get("followup_activo") and not forzado:
            continue
        ahora = _ahora()
        if not forzado and not _dentro_de_ventana(agente, ahora):
            resultado[inbox_id] = {"omitido": "fuera de ventana"}
            continue
        creados = _crear_followups_nuevos(agente, inbox_id)
        tope = agente.get("followup_max_por_corrida") or 4
        r = _procesar_vencidos(agente, inbox_id, tope, dry_run or forzado)
        resultado[inbox_id] = {"nuevos": creados, **r}
    return resultado


# ---------------------------------------------------------------------------
# Panel (paso 7 del plan) — un solo endpoint de guardado
# ---------------------------------------------------------------------------
# `frappe.client.set_value` no maneja bien un campo Table (child table); en
# vez de pelear con eso desde el front, un endpoint propio hace el guardado
# completo (escalares + tabla de pasos) en una sola llamada atómica.

_ROLES_EDICION = ("System Manager", "Sales Manager")


def _exigir_edicion():
    roles = frappe.get_roles(frappe.session.user)
    if frappe.session.user != "Administrator" and not any(r in roles for r in _ROLES_EDICION):
        frappe.throw("Sin permiso para editar la configuración de reactivación")


@frappe.whitelist()
def guardar_config(inbox_id, activo, ventana_inicio, ventana_fin, max_por_corrida, pasos) -> dict:
    """Guarda la config de autofollowup de un `Agente IA` (interruptor, ventana,
    tope y cadencia). El contenido del mensaje NO se configura aquí — lo
    redacta el agente con el contexto (decisión de Alejandro, 2026-09-24)."""
    _exigir_edicion()
    if isinstance(pasos, str):
        pasos = json.loads(pasos)
    doc = frappe.get_doc("Agente IA", inbox_id)
    doc.followup_activo = 1 if int(activo) else 0
    doc.followup_ventana_inicio = ventana_inicio
    doc.followup_ventana_fin = ventana_fin
    doc.followup_max_por_corrida = int(max_por_corrida)
    doc.set("followup_pasos", [])
    for p in pasos:
        doc.append("followup_pasos", {
            "espera_valor": int(p["espera_valor"]),
            "espera_unidad": p["espera_unidad"],
        })
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True}


@frappe.whitelist()
def listar_activos(inbox_id) -> list:
    """Filas de `Chatwoot Followup` de un inbox, para la pantalla de Agente IA:
    quién está en re-enganche y en qué paso, para poder excluir a mano."""
    _exigir_edicion()
    return frappe.get_all(
        "Chatwoot Followup",
        filters={"inbox_id": str(inbox_id)},
        fields=["name", "conversation_id", "contacto_nombre", "deal", "estado",
                "paso_actual", "proximo_en", "ultimo_out_at", "motivo", "creation"],
        order_by="creation desc",
        limit_page_length=200,
    )


@frappe.whitelist()
def excluir_a_mano(conversation_id, motivo=None) -> dict:
    """Saca una conversación del autofollow a mano (botón del panel).

    Si no tiene fila todavía, la crea ya cerrada — `_crear_followups_nuevos`
    nunca la vuelve a meter (comprueba `frappe.db.exists` por
    `conversation_id`, sin importar el estado). Igual que
    `secuencias.sacar_inscripcion`: no se borra, se marca — el rastro de que
    alguien lo excluyó a propósito importa más que el espacio que ocupa."""
    _exigir_edicion()
    conversation_id = frappe.utils.cint(conversation_id)
    motivo = (motivo or "Excluido a mano desde el panel")[:139]
    existente = frappe.db.get_value("Chatwoot Followup", {"conversation_id": conversation_id}, "name")
    if existente:
        doc = frappe.get_doc("Chatwoot Followup", existente)
        doc.estado = "Salió a mano"
        doc.motivo = motivo
        doc.save(ignore_permissions=True)
    else:
        deal = _deal_de(conversation_id) or {}
        doc = frappe.get_doc({
            "doctype": "Chatwoot Followup",
            "conversation_id": conversation_id,
            "inbox_id": str(_inbox_de(conversation_id) or ""),
            "deal": deal.get("name"),
            "estado": "Salió a mano",
            "paso_actual": 0,
            "motivo": motivo,
        })
        doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "name": doc.name}


def _inbox_de(conversation_id) -> int | None:
    conv = chatwoot_client.get_conversation(conversation_id)
    return (conv.get("inbox_id"))
