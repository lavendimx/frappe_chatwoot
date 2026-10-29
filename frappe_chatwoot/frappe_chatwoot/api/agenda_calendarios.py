# Copyright (c) 2026, lavendi.mx
"""Agendamiento público NATIVO por `Calendario Agendamiento` — sin Google Calendar.

POR QUÉ EXISTE Y POR QUÉ NO ES `agenda_publica.py`
----------------------------------------------------
`api/agenda_publica.py` + `www/agenda/*` son el sistema de agendamiento público
ACTUAL: exclusivo de lavendi.mx (3 perfiles hardcodeados), vhost single-tenant
`agenda.lavendi.mx`, y los slots los calcula un proceso Node externo
(`agente-ia`) contra Google Calendar. Ese camino NO se toca aquí.

Este módulo es el camino NUEVO, genérico y nativo: cualquier sitio puede crear
sus propios `Calendario Agendamiento` (doctype, no hardcodeado) y publicarlos
en `https://<su-dominio>/agendar/<slug>` (routing en `hooks.py`,
`website_route_rules`). La disponibilidad se calcula aquí mismo, en Python,
contra las citas ya guardadas en `Reunion Agendada` — mismo patrón "local-"
(sin Google) ya validado en Estrublock, generalizado a cualquier cliente.

CANDADO A NIVEL DE APLICACIÓN, NO DE BASE DE DATOS
----------------------------------------------------
`reservar()` vuelve a calcular los slots libres del día justo antes de
insertar y rechaza si el slot pedido ya no está en esa lista — es el mismo
criterio ya aceptado en el resto del sistema (ver `agenda_publica.py`): una
doble reserva en la misma fracción de segundo es un riesgo aceptado, no un
caso que amerite un `UNIQUE` compuesto en BD.
"""

import frappe
from frappe import _
from frappe.utils import (
    add_days,
    add_to_date,
    get_datetime,
    getdate,
    now_datetime,
)

# Índice 0 = Lunes (Python `date.weekday()`), igual que el Select del child
# doctype `Calendario Agendamiento Horario.dia_semana`.
DIAS_SEMANA = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]

# Mismo criterio de "candado a nivel de aplicación" que agenda_publica.py:
# límite de reservas por IP en una ventana de 1 hora, vía cache de Frappe
# (Redis). Namespace propio (`agenda_calendarios:`) para no compartir contador
# con el sistema de Google Calendar.
MAX_RESERVAS_POR_IP_HORA = 5


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _obtener_calendario_activo(slug: str):
    slug = (slug or "").strip().lower()
    if not slug:
        frappe.throw(_("Falta el slug del calendario."))

    nombre = frappe.db.get_value("Calendario Agendamiento", {"slug": slug}, "name")
    if not nombre:
        frappe.throw(_("No existe un calendario de agendamiento con ese identificador."))

    cal = frappe.get_doc("Calendario Agendamiento", nombre)
    if not cal.activo:
        frappe.throw(_("Este calendario de agendamiento está desactivado."))
    return cal


def _ip_cliente() -> str:
    return frappe.local.request_ip or "desconocida"


def _bajo_limite_reservas() -> bool:
    clave = f"agenda_calendarios:reservas:{_ip_cliente()}"
    hechas = frappe.cache().get_value(clave) or 0
    if int(hechas) >= MAX_RESERVAS_POR_IP_HORA:
        return False
    frappe.cache().set_value(clave, int(hechas) + 1, expires_in_sec=3600)
    return True


def _horarios_del_dia(cal, dia_semana: str):
    return [h for h in (cal.horario or []) if h.activo and h.dia_semana == dia_semana]


def _citas_existentes_del_dia(calendario_name: str, dia):
    """Rango completo del día, para cruzar contra `start_datetime`/`end_datetime`."""
    inicio_dia = get_datetime(f"{dia} 00:00:00")
    fin_dia = get_datetime(f"{dia} 23:59:59")
    filas = frappe.db.get_all(
        "Reunion Agendada",
        filters={
            "calendario": calendario_name,
            "start_datetime": ["<", fin_dia],
            "end_datetime": [">", inicio_dia],
        },
        fields=["start_datetime", "end_datetime"],
    )
    return [(get_datetime(f.start_datetime), get_datetime(f.end_datetime)) for f in filas]


def _se_solapan(ini_a, fin_a, ini_b, fin_b) -> bool:
    return ini_a < fin_b and fin_a > ini_b


def _generar_slots_del_dia(cal, dia) -> list[dict]:
    """Slots libres de un día concreto (objeto `date`), ya filtrados por:
    horario del día de la semana, anticipación mínima, ocupación existente y
    `max_citas_por_dia`.
    """
    dia_semana = DIAS_SEMANA[dia.weekday()]
    horarios = _horarios_del_dia(cal, dia_semana)
    if not horarios:
        return []

    ahora = now_datetime()
    minimo_inicio = add_to_date(ahora, minutes=cal.anticipacion_minima_minutos or 0)

    ocupadas = _citas_existentes_del_dia(cal.name, dia)

    max_por_dia = cal.max_citas_por_dia or 0
    if max_por_dia > 0 and len(ocupadas) >= max_por_dia:
        return []

    paso_minutos = (cal.duracion_minutos or 30) + (cal.buffer_minutos or 0)
    duracion_minutos = cal.duracion_minutos or 30

    libres = []
    cupo_restante = (max_por_dia - len(ocupadas)) if max_por_dia > 0 else None

    for h in horarios:
        cursor = get_datetime(f"{dia} {h.hora_inicio}")
        fin_ventana = get_datetime(f"{dia} {h.hora_fin}")

        while True:
            fin_slot = add_to_date(cursor, minutes=duracion_minutos)
            if fin_slot > fin_ventana:
                break
            if cursor < minimo_inicio:
                cursor = add_to_date(cursor, minutes=paso_minutos)
                continue

            choca = any(_se_solapan(cursor, fin_slot, oi, of) for oi, of in ocupadas)
            if not choca:
                if cupo_restante is not None and cupo_restante <= 0:
                    # Ya se ofrecieron tantos slots libres como cupo queda —
                    # no se sigue generando para este día (evita anunciar más
                    # huecos de los que `max_citas_por_dia` puede honrar).
                    return libres
                if cupo_restante is not None:
                    cupo_restante -= 1
                libres.append({
                    "inicio": cursor.strftime("%Y-%m-%d %H:%M:%S"),
                    "fin": fin_slot.strftime("%Y-%m-%d %H:%M:%S"),
                    "fecha": dia.strftime("%Y-%m-%d"),
                    "hora_inicio": cursor.strftime("%H:%M"),
                    "hora_fin": fin_slot.strftime("%H:%M"),
                })

            cursor = add_to_date(cursor, minutes=paso_minutos)

    return libres


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def calendario_info(slug: str = ""):
    cal = _obtener_calendario_activo(slug)
    return {
        "titulo": cal.titulo,
        "descripcion": cal.descripcion,
        "duracion_minutos": cal.duracion_minutos,
    }


@frappe.whitelist(allow_guest=True)
def slots(slug: str = "", fecha: str = ""):
    """Slots libres NATIVOS (sin Google, sin `agente-ia`) para el calendario `slug`.

    Si `fecha` (YYYY-MM-DD) viene, solo calcula ese día — igual debe caer
    dentro de la ventana de anticipación/alcance del calendario, si no
    devuelve lista vacía.
    """
    cal = _obtener_calendario_activo(slug)

    ventana_dias = max(1, int(cal.ventana_maxima_dias or 30))
    hoy = getdate(now_datetime())
    ultimo_dia = add_days(hoy, ventana_dias)

    if fecha:
        try:
            dias_a_revisar = [getdate(fecha)]
        except Exception:
            frappe.throw(_("Fecha inválida."))
        if dias_a_revisar[0] < hoy or dias_a_revisar[0] > ultimo_dia:
            return {"slug": cal.slug, "slots": []}
    else:
        dias_a_revisar = []
        cursor = hoy
        while cursor <= ultimo_dia:
            dias_a_revisar.append(cursor)
            cursor = add_days(cursor, 1)

    resultado = []
    for dia in dias_a_revisar:
        resultado.extend(_generar_slots_del_dia(cal, dia))

    return {"slug": cal.slug, "slots": resultado}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def reservar(
    slug: str = "",
    slot_inicio: str = "",
    nombre: str = "",
    telefono: str = "",
    email: str = "",
    notas: str = "",
):
    cal = _obtener_calendario_activo(slug)

    nombre = (nombre or "").strip()
    telefono = (telefono or "").strip()
    email = (email or "").strip().lower()
    notas = (notas or "").strip()
    slot_inicio = (slot_inicio or "").strip()

    if not nombre:
        frappe.throw(_("El nombre es obligatorio."))
    if not telefono and not email:
        frappe.throw(_("Se necesita al menos un teléfono o un correo de contacto."))
    if not slot_inicio:
        frappe.throw(_("Falta el slot a reservar."))

    if not _bajo_limite_reservas():
        frappe.throw(_("Demasiados intentos de reserva desde este origen. Intenta más tarde."))

    try:
        inicio = get_datetime(slot_inicio)
    except Exception:
        frappe.throw(_("El slot indicado no es una fecha/hora válida."))

    # Recheck inmediato: recalcula los slots libres de ESE día y exige que el
    # slot pedido siga en la lista — cubre horario, anticipación, ocupación y
    # `max_citas_por_dia` de una sola vez, con los datos más frescos posibles
    # justo antes de escribir.
    dia = getdate(inicio)
    libres_hoy = _generar_slots_del_dia(cal, dia)
    objetivo = inicio.strftime("%Y-%m-%d %H:%M:%S")
    coincidencia = next((s for s in libres_hoy if s["inicio"] == objetivo), None)
    if not coincidencia:
        frappe.throw(_("Ese horario ya no está disponible. Elige otro slot."))

    fin = get_datetime(coincidencia["fin"])

    doc = frappe.get_doc({
        "doctype": "Reunion Agendada",
        "event_id": f"nativo-{frappe.generate_hash(length=10)}",
        # `inbox_id` es `reqd=1` en el doctype porque nació pensado para citas
        # que SIEMPRE vienen de una conversación de Chatwoot. Un calendario
        # nativo no tiene inbox: se guarda un sentinel legible
        # ("agendar:<slug>") en vez de inventar un inbox_id numérico falso —
        # cualquier reporte que agrupe por inbox_id lo reconoce como "no es
        # Chatwoot" a simple vista, en vez de ensuciar las estadísticas de un
        # inbox real.
        "inbox_id": f"agendar:{cal.slug}",
        "calendar_id": "",
        "calendario": cal.name,
        "nombre_participante": nombre,
        "email_participante": email,
        "contacto_telefono": telefono,
        "notas": notas,
        "start_datetime": inicio,
        "end_datetime": fin,
    })
    doc.insert(ignore_permissions=True)

    return {
        "ok": True,
        "event_id": doc.event_id,
        "start_datetime": doc.start_datetime,
        "end_datetime": doc.end_datetime,
    }
