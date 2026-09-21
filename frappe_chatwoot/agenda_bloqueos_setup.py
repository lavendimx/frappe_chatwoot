# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Doctype `Ausencia Asesora` — Pieza H del scope de agenda nativa
(planes/scope-agenda-nativa-frappe.md, gap G6 / riesgo R2), corre SOLO en el sitio de
Estrublock.

Idempotente. Mismo patrón que `create_programados_doctype.py` (doctype "Custom", no parte
del módulo `frappe_chatwoot` — no se sincroniza a otros sitios con `bench migrate`) y que
`agenda_estado_asesora_setup.py` (Pieza C, mismo criterio de "corre solo donde se necesita").
Se copia a `apps/frappe_chatwoot/frappe_chatwoot/agenda_bloqueos_setup.py` dentro del
contenedor y se corre con

    bench --site estrublock.lavendi.mx execute frappe_chatwoot.agenda_bloqueos_setup.ejecutar

No usar `bench console` con este archivo: el console de IPython pierde las definiciones de
función al leer el script por stdin y truena con NameError.

POR QUÉ (gap G6 / riesgo R2, §2.2 y §6 del scope)
    Con Google Calendar, una asesora bloqueaba un día o una franja en SU PROPIO calendario y
    `busyRanges` lo recogía gratis — sin código adicional, sin doctype adicional. La decisión
    #17 (agenda nativa en Frappe, sin Google) eliminó eso SIN reemplazo: es una regresión no
    declarada, no una funcionalidad nueva. Sin este doctype, si Larisa se va de vacaciones o
    toma un día económico, el bot (o el alta manual) le sigue agendando citas.

    La decisión #24 del plan de migración (2026-09-20) ya aprobó cerrar esto dentro del MVP.

POR QUÉ SOLO ESTRUBLOCK
    Igual que `agenda_estado_asesora_setup.py`: `frappe_chatwoot` corre en crm.lavendi.mx,
    sixgardens.lavendi.mx y estrublock.lavendi.mx, pero cada uno tiene su propia base de
    datos. Un doctype "Custom" (custom=1) es dato de sitio, no código de app — igual que un
    Custom Field, solo existe donde se crea explícitamente. Correrlo en los otros dos sitios
    no sería destructivo, pero sería ruido sin dueño: ninguno de los dos usa fuente "frappe"
    en AGENDA_CITAS.

DISEÑO DEL DOCTYPE (decisión de esta sesión, no había especificación más fina que el gap G6)
    `asesora` (Link -> User, requerido)
        La misma identidad que ya usa AGENDA_CITAS y que `Reunion Agendada.asesora` (Pieza C)
        ya trae. Es el campo por el que se filtra un bloqueo — NO por `calendar_id`: alguien
        capturando un bloqueo desde el Desk nativo elige una persona, no un id de calendario
        interno. La relación calendar_id -> asesora ya vive en AGENDA_CITAS
        (agente-ia/lib/agenda-calendarios.js) y se resuelve del lado de Node
        (agenda-fuente.js::asesoraDeCalendario) sin duplicar el mapeo aquí.
    `start_datetime` / `end_datetime` (Datetime, requeridos)
        Mismo par de campos y mismo criterio de solape que `Reunion Agendada` — no se
        inventa un segundo modelo de tiempo en el mismo módulo. NO hay un campo aparte
        "día completo": para bloquear un día entero se captura de 00:00:00 a 00:00:00 del
        día siguiente. Para una franja, el rango exacto de esa franja.
    `motivo` (Data, opcional)
        Texto libre ("Vacaciones", "Día económico", "Cita médica"). No se modela como
        catálogo cerrado (Select) porque no hay ninguna regla de negocio que dependa del
        valor — es solo la razón visible en el Desk para quien revisa el calendario.

    Se DESCARTÓ un campo `estado`/`activo` para poder "cancelar" un bloqueo sin borrarlo: a
    diferencia de `Reunion Agendada` (donde borrar pierde el baseline de no-show, Pieza C),
    aquí no hay ninguna métrica que dependa de que el bloqueo haya existido — borrar la fila
    desde el Desk nativo (permiso `delete` ya incluido) es suficiente para el MVP.

    Se DESCARTÓ un campo `inbox_id`: una ausencia de una asesora aplica sin importar por qué
    inbox/canal se intente agendar con ella — no hay ningún caso de negocio donde Larisa esté
    "de vacaciones" para un inbox y disponible para otro.

ALCANCE DE MVP — sin UI propia
    No hay vista de calendario ni diálogo nuevo en `patches/crm-frontend/` para esto (eso es
    Pieza E, aparte, y el gap G6 no lo pide). El Desk nativo de Frappe (list view estándar,
    con create/write/delete otorgados a System Manager, Sales Manager y Sales User) basta
    para que el equipo capture un bloqueo. Si más adelante se quiere un endpoint HTTP para
    declararlo desde fuera (p. ej. que el propio agente lo lea/escriba en conversación), es
    trabajo aparte — no se hizo aquí porque no era requisito del MVP (§8.7 del scope no lo
    menciona) y `frappe_chatwoot/api/agenda.py` ya tiene suficiente superficie sin tocar en
    este cambio.

RIPPLE en agente-ia (Node), YA HECHO en el mismo commit que este archivo:
    · `lib/agenda-fuente.js::busyRangesFrappe` ahora fusiona `Reunion Agendada` (citas) +
      `Ausencia Asesora` (bloqueos) en un solo array `{startMs,endMs}[]` — mismo shape que
      antes, así que `agenda-calendarios.js::calendariosLibres` no necesita ningún cambio.
    · `bookFrappe`/`updateFrappe` heredan la protección gratis: su candado ya llama a
      `busyRangesFrappe` antes de insertar/mover, así que un slot bloqueado por ausencia se
      rechaza con el mismo código `horario_ocupado` que un slot ocupado por otra cita.

⚠ Orden de despliegue: este patch tiene que correr ANTES de que `AGENDA_CITAS` declare
`"fuente": "frappe"` para cualquier inbox — si se declara antes, `busyRangesFrappe` fallaría
consultando un doctype que todavía no existe en ese sitio. Hoy ningún inbox en producción
tiene `fuente: "frappe"` (el gate está apagado), así que no hay carrera. Debe correr en el
mismo movimiento (o después) del patch de la Pieza C (`agenda_estado_asesora_setup.py`), que
tampoco se ha corrido contra el sitio vivo todavía.
"""

import frappe

DOCTYPE = "Ausencia Asesora"


def crear() -> None:
    if frappe.db.exists("DocType", DOCTYPE):
        print(f"{DOCTYPE} ya existe")
        return
    frappe.get_doc({
        "doctype": "DocType",
        "name": DOCTYPE,
        "module": "Custom",
        "custom": 1,
        "naming_rule": "Random",
        "autoname": "hash",
        "title_field": "asesora",
        "track_changes": 1,
        "fields": [
            {"fieldname": "asesora", "fieldtype": "Link", "options": "User",
             "label": "Asesora", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1,
             "description": "Dueña del calendario bloqueado (AGENDA_CITAS de agente-ia)."},
            {"fieldname": "start_datetime", "fieldtype": "Datetime", "label": "Inicio",
             "reqd": 1, "in_list_view": 1},
            {"fieldname": "end_datetime", "fieldtype": "Datetime", "label": "Fin",
             "reqd": 1, "in_list_view": 1,
             "description": "Para bloquear el día completo: 00:00:00 del día siguiente."},
            {"fieldname": "motivo", "fieldtype": "Data", "label": "Motivo",
             "description": "Vacaciones, día económico, cita médica, etc. Texto libre."},
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "Sales Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "Sales User", "read": 1, "write": 1, "create": 1, "delete": 1},
        ],
    }).insert(ignore_permissions=True)
    print(f"{DOCTYPE} creado")


def ejecutar() -> None:
    crear()
    frappe.db.commit()
    print("Listo")
