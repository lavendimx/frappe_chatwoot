"""Permisos de lectura del calendario para el equipo (2026-09-14).

    bench --site crm.lavendi.mx execute frappe_chatwoot.permisos_agenda.ejecutar
    bench --site crm.lavendi.mx execute frappe_chatwoot.permisos_agenda.ejecutar --kwargs "{'apply':1}"

POR QUÉ EXISTE
    Zaira (contacto@lavendi.mx) reportó que una cita que creó no le aparecía en la
    pantalla de Calendario. La cita SÍ se creó — en el CRM y en Google Calendar —,
    pero `Reunion Agendada` nació (doctype Custom, creado a mano en el panel) con un
    solo permiso: **System Manager**. Ni Zaira ni Valente lo tienen (tienen Sales
    User / Sales Manager), así que `createListResource` de `Calendario.vue` les
    devolvía una lista vacía.

    El alta y la cancelación no dependen de esto: pasan por los endpoints
    `api.agenda.crear_cita` / `cancelar_cita`, que le piden el trabajo al proceso
    `agente-ia` (que usa `agente-ia@`, System Manager). Por eso el bug era invisible
    al crear y evidente al listar.

CRITERIO DE PERMISOS
    Todo el equipo de ventas ve todas las citas (el calendario de Google está
    compartido con ellos por diseño). Sales User solo lee; Sales Manager además
    escribe, que es el patrón del resto del CRM. Sin `if_owner`: una cita agendada
    por un vendedor tiene que verla cualquiera que atienda al cliente.

Idempotente: si el rol ya tiene permiso, no lo duplica.
"""

import frappe

DOCTYPE = "Reunion Agendada"
PERMS = [
    {"role": "Sales User", "read": 1},
    {"role": "Sales Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
]


def ejecutar(apply=0):
    apply = bool(frappe.utils.cint(apply))
    doc = frappe.get_doc("DocType", DOCTYPE)
    por_rol = {p.role: p for p in doc.permissions}

    for p in PERMS:
        rol = p["role"]
        if rol not in por_rol:
            if not apply:
                print(f"{rol}: se agregaría {p}")
                continue
            doc.append("permissions", {"role": rol, "if_owner": 0, "permlevel": 0})
            por_rol[rol] = doc.permissions[-1]
            print(f"{rol}: agregado")

    # Forzar los valores exactos: `doc.save()` de un DocType reparte los flags de
    # un permiso nuevo y deja Sales User con write/create/delete en 1. Aquí se
    # corrige a solo-lectura (mínimo privilegio) sin tener que editar a mano.
    for p in PERMS:
        fila = por_rol.get(p["role"])
        if not fila:
            continue
        for campo in ("read", "write", "create", "delete"):
            deseado = frappe.utils.cint(p.get(campo, 0))
            if frappe.utils.cint(fila.get(campo)) != deseado:
                if not apply:
                    print(f"{p['role']}.{campo}: se corregiría a {deseado}")
                else:
                    fila.set(campo, deseado)

    if apply:
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        frappe.clear_cache(doctype=DOCTYPE)
        print("permisos finales:",
              [(x.role, x.read, x.write, x.create, x.delete) for x in
               frappe.get_doc("DocType", DOCTYPE).permissions])
