# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Backfill del vínculo `Contact` <-> `Customer` al mecanismo nativo de Frappe,
y limpieza de los `ghl_contact_id` sintéticos (`manual-<hash>`).

POR QUÉ EXISTE ESTE PATCH
-------------------------
Hasta el 2026-10-18 el único puente entre el CRM (`Contact`) y la facturación
(`Customer` de ERPNext) era comparar dos campos de texto iguales:
`Contact.ghl_contact_id == Customer.ghl_contact_id`. Un campo heredado de la
migración de GoHighLevel, que es un sistema **retirado desde el 2026-09-11**.

Ese puente se rompía en silencio. El caso que lo destapó: fusionar dos
contactos en la herramienta de Contactos consolida `tabContact` pero nunca miró
`tabCustomer`, así que el Customer del contacto absorbido sobrevivía activo y
huérfano mientras el `on_update` del contacto base creaba uno nuevo. Resultado
real: **"Eleazer Moreno" aparecía dos veces en el selector de Cobranza** con el
contacto ya fusionado — dos clientes facturables para una sola persona.

La migración (ver `utils/cliente_erp.py`) mueve el vínculo a lo que ERPNext ya
trae de fábrica: la child table `Dynamic Link` del `Contact`
(`link_doctype="Customer"`), más `Customer.customer_primary_contact` del lado
inverso. Lo entienden el Desk, los reportes nativos y `rename_doc(merge=True)`.

El código nuevo ya resuelve solo, pero de a poco
------------------------------------------------
`cliente_erp.customer_de_contacto()` hace *lazy backfill*: si no encuentra
`Dynamic Link` pero sí un match por el `ghl_contact_id` viejo, crea el link en
ese momento. O sea: el vínculo se iría migrando contacto por contacto conforme
alguien los abriera. Este patch simplemente **hace de golpe lo que de todos
modos iba a pasar solo**, con dos ventajas que sí importan:

  1. Deja la base en un estado verificable HOY (el criterio de done exige
     "todo Customer vivo tiene su Dynamic Link"), en vez de depender de que
     alguien visite cada ficha.
  2. Permite borrar después el puente de compatibilidad
     (`cliente_erp._backfill_desde_campo_legacy`) sin dejar clientes ciegos.

POR QUÉ SE LIMPIAN LOS `manual-<hash>` Y NO EL RESTO
----------------------------------------------------
Medido en producción el 2026-10-18 sobre `_bd4cbeb6e263ac60`:

  - 4,221 `Contact` con un `ghl_contact_id` **auténtico** de GHL. Eso es
    procedencia histórica legítima: dice de dónde vino ese contacto. NO SE
    TOCA. El campo se conserva con ese único propósito (decisión #1 del
    contrato: no renombrar, no borrar — solo dejar de usarlo como plumbing).
  - 41 `Contact` y 46 `Customer` con un valor `manual-<hash>`: UUIDs que el
    propio código inventaba para poder colgarle el vínculo a un contacto dado
    de alta a mano, ya sin GHL de por medio. No son ids de nada; su único
    efecto es contaminar el campo que sí tiene valor histórico y hacer creer
    que esos registros vinieron de GHL. Esos sí se vacían.

ORDEN OBLIGATORIO: PRIMERO VINCULAR, DESPUÉS LIMPIAR
-----------------------------------------------------
Los `manual-<hash>` son, hoy, la única forma de resolver a qué `Contact`
pertenecen esos ~46 Customers. Si la limpieza corriera antes del backfill, esos
clientes quedarían permanentemente huérfanos y habría que reconstruir el
vínculo a mano contra el backup. De ahí que el paso 4 vaya estrictamente
después del paso 2, en la misma transacción.

GARANTÍAS DE ESTE PATCH
-----------------------
  - **Idempotente**: la segunda corrida no crea links (ya existen) ni limpia
    filas (ya están vacías). Lo demuestran los contadores `vinculados` y
    `sinteticos_*` en cero, con `total_con_vinculo` igual entre corridas.
  - **Fail-open por registro**: un Customer con datos rotos se registra en el
    log y la corrida sigue con los otros 172. Un patch de migración que aborta
    a la mitad deja la base en un estado peor que no haberlo corrido.
  - **No-op limpio sin ERPNext**: 9 de los 12 sitios son `sofia_plan: lite` y
    no tienen `tabCustomer`. Ahí el patch sale en la primera línea.
  - **La limpieza nunca destruye una pista viva**: si un Customer quedó sin
    vincular por ambigüedad o por un error aislado, su `manual-<hash>` se
    conserva — es lo único que permite resolverlo a mano después. Solo se
    vacían los sintéticos de registros ya vinculados o irrecuperables.
  - **No deshabilita ni borra nada.** Los Customers que no resuelven a ningún
    Contact vivo (~9 en producción) solo se cuentan y se enlistan en el log.
    Decidir cuál se consolida y cuál se deshabilita exige mirar si tiene
    facturas emitidas — es criterio humano (paso P6 del contrato), no de un
    patch que corre solo en cada `bench migrate`.

CÓMO SE REVIERTE
----------------
Backup `.sql` de `tabContact`, `tabCustomer`, `tabDynamic Link`,
`tabSales Invoice` y `tabAuto Repeat` tomado en P0 del contrato. Los
`Dynamic Link` creados aquí se pueden barrer con:

    DELETE FROM `tabDynamic Link`
     WHERE parenttype='Contact' AND link_doctype='Customer';

Correr a mano (el sitio de pruebas, nunca producción sin el orquestador):

    docker exec frappe-<id> bench --site erp-prueba.local execute \\
      frappe_chatwoot.patches.vinculo_contact_customer_nativo.execute
"""

import frappe

from frappe_chatwoot.utils import cliente_erp

# Prefijo de los ids sintéticos que el alta manual inventaba antes de que el
# vínculo fuera nativo. Se usa tal cual en el LIKE de la limpieza.
PREFIJO_SINTETICO = "manual-"

# Cuántos huérfanos se enlistan por nombre en el log. Son ~9 en producción; el
# tope existe para que un sitio con datos sucios no escupa 500 líneas.
TOPE_HUERFANOS_EN_LOG = 25


def _log(mensaje: str) -> None:
    """Doble salida a propósito: `print` para cuando se corre a mano con
    `bench execute` (que es como se valida), y `frappe.logger` para cuando
    corre dentro de `bench migrate` y nadie está mirando la consola."""
    print(mensaje)
    frappe.logger("cliente_erp").info(mensaje)


def _contact_de_ghl_id(ghl_id: str) -> tuple[str | None, bool]:
    """Resuelve `Contact` por el campo viejo. Devuelve `(contact, ambiguo)`.

    Si dos contactos comparten el mismo `ghl_contact_id` no se adivina: ligar
    el Customer equivocado significa facturarle a otra persona. Se devuelve
    `ambiguo=True` y el caller lo deja sin vincular, para que lo resuelva el
    paso de limpieza humana. Medido el 2026-10-18: cero duplicados en
    producción, así que esta rama no se activa ahí — está por si acaso.
    """
    candidatos = frappe.get_all(
        "Contact",
        filters={"ghl_contact_id": ghl_id},
        pluck="name",
        limit=2,
    )
    if not candidatos:
        return None, False
    if len(candidatos) > 1:
        return None, True
    return candidatos[0], False


def _link_existente(customer: str) -> str | None:
    """`Contact` que ya tiene un `Dynamic Link` hacia este Customer, si lo hay."""
    return frappe.db.get_value(
        "Dynamic Link",
        {"parenttype": "Contact", "link_doctype": "Customer", "link_name": customer},
        "parent",
    )


def execute(limpiar_sinteticos: int = 0):
    """El default es **0**: `bench migrate` corre SOLO el backfill y deja los
    `manual-%` intactos. El blanqueo se pide explícito:

        bench --site <sitio> execute \\
          frappe_chatwoot.patches.vinculo_contact_customer_nativo.execute \\
          --kwargs "{'limpiar_sinteticos':1}"

    Por qué el default es el seguro y no el completo:

    `bench serve` corre con auto-reload, así que el proceso web toma el código
    nuevo solo; `bench worker` y `bench schedule` NO. Mientras los workers
    tengan el código viejo en memoria, `autofollowup._es_cliente` sigue
    resolviendo por `Contact.ghl_contact_id` — y con el campo ya vaciado
    devolvería `False` para los 41 contactos sintéticos, es decir, los trataría
    como NO clientes y les mandaría seguimiento comercial por WhatsApp. Mandarle
    un follow-up de ventas a un cliente que paga es exactamente lo que ese
    criterio existe para evitar.

    Medido el 2026-10-18 en producción: los 41 contactos con id sintético son
    TODOS clientes con `Customer` activo y 37 tienen conversación de Chatwoot
    abierta. El riesgo no es teórico.

    El default queda en 0 de forma permanente — no se "revierte a 1 después del
    despliegue" — porque un `bench migrate` futuro no tiene forma de saber si
    los workers ya reiniciaron. Y no hace falta: los ids sintéticos solo existen
    en esta base (el código nuevo nunca los escribe), así que el blanqueo es una
    operación de una vez, no algo que un patch genérico deba hacer por default.
    """
    limpiar_sinteticos = int(limpiar_sinteticos)
    if not cliente_erp._erpnext_instalado():
        # Sitio `lite`: no existe el doctype `Customer`. No es un error.
        _log("[vinculo_contact_customer_nativo] Sin ERPNext en este sitio — no-op.")
        return

    vinculados = 0          # Dynamic Link creados en ESTA corrida
    ya_vinculados = 0        # candidatos que ya tenían su link
    huerfanos = 0            # ghl_contact_id que no resuelve a ningún Contact
    ambiguos = 0             # ghl_contact_id compartido por 2+ Contacts
    errores = 0              # fallos aislados (fail-open)
    multiples_por_contacto = 0  # un Contact que termina con 2+ Customers
    nombres_huerfanos: list[str] = []

    # Ids que la limpieza final NO debe borrar aunque sean `manual-`: son los
    # de registros que quedaron SIN vincular pero que sí son resolubles
    # (ambiguos, o que reventaron por un error aislado). Para ellos el
    # `ghl_contact_id` sigue siendo la única pista de a qué Contact pertenecen
    # — vaciarlo convertiría un problema arreglable a mano en uno irreversible.
    # Vacío en producción (0 ambiguos, 0 errores medidos), pero sin esto el
    # patch destruiría información en cualquier sitio que sí los tenga.
    ids_preservados: set[str] = set()

    # Se lee con SQL directo y no con `frappe.get_all` porque `ghl_contact_id`
    # es un custom field: en un sitio donde el fixture no se haya aplicado, el
    # get_all truena por campo inexistente mientras que la columna sí existe.
    try:
        candidatos = frappe.db.sql(
            """
            SELECT name, ghl_contact_id
              FROM `tabCustomer`
             WHERE IFNULL(ghl_contact_id, '') != ''
            """,
            as_dict=True,
        )
    except Exception as exc:
        _log(f"[vinculo_contact_customer_nativo] ABORTA: no se pudo leer tabCustomer: {exc}")
        return

    for fila in candidatos:
        customer = fila.get("name")
        ghl_id = (fila.get("ghl_contact_id") or "").strip()
        if not ghl_id:
            continue
        try:
            previo = _link_existente(customer)
            if previo:
                ya_vinculados += 1
                # Idempotente y barato: aprovecha para rellenar
                # `customer_primary_contact` si quedó vacío (los 173 Customers
                # de producción lo tenían vacío), sin tocar el link.
                cliente_erp.vincular(previo, customer)
                continue

            contact, ambiguo = _contact_de_ghl_id(ghl_id)
            if ambiguo:
                ambiguos += 1
                ids_preservados.add(ghl_id)
                _log(
                    f"[vinculo_contact_customer_nativo] AMBIGUO: Customer {customer!r} "
                    f"con ghl_contact_id {ghl_id!r} compartido por 2+ Contacts — sin vincular."
                )
                continue
            if not contact:
                huerfanos += 1
                if len(nombres_huerfanos) < TOPE_HUERFANOS_EN_LOG:
                    nombres_huerfanos.append(customer)
                continue

            # Si el Contact ya tiene otro Customer colgado, se vincula igual
            # (el dato refleja la realidad previa) pero se deja constancia: eso
            # es exactamente un duplicado de Cobranza, y su consolidación es
            # decisión humana con las facturas a la vista (P6 del contrato).
            otros = frappe.get_all(
                "Dynamic Link",
                filters={
                    "parenttype": "Contact",
                    "parent": contact,
                    "link_doctype": "Customer",
                },
                pluck="link_name",
            )
            if otros:
                multiples_por_contacto += 1
                _log(
                    f"[vinculo_contact_customer_nativo] DUPLICADO: Contact {contact!r} "
                    f"ya tenía {otros} y además {customer!r} — se vincula, revisar en P6."
                )

            cliente_erp.vincular(contact, customer)
            vinculados += 1
        except Exception as exc:
            # Fail-open: un registro problemático no puede tumbar la migración
            # de los otros 172.
            errores += 1
            if ghl_id:
                ids_preservados.add(ghl_id)
            _log(
                f"[vinculo_contact_customer_nativo] ERROR en Customer {customer!r}: {exc}"
            )
            frappe.log_error(
                f"vinculo_contact_customer_nativo / Customer {customer}: {exc}",
                "Cliente ERP",
            )

    if nombres_huerfanos:
        _log(
            "[vinculo_contact_customer_nativo] HUERFANOS (sin Contact resoluble, "
            f"NO se tocan): {nombres_huerfanos}"
        )

    # ------------------------------------------------------------------
    # Limpieza de los ids sintéticos — SOLO después del backfill de arriba
    # ------------------------------------------------------------------
    # UPDATE directo en vez de cargar documentos: son ~87 filas entre las dos
    # tablas, cargar los docs dispararía `on_update` (que crea Customers) y
    # movería `modified`, ensuciando la auditoría de 4,000+ contactos por un
    # cambio puramente técnico. El SQL crudo no toca `modified` ni `_user_tags`.
    sinteticos_contact = 0
    sinteticos_customer = 0

    # `NOT IN (...)` construido con placeholders, nunca interpolando valores.
    preservados = sorted(ids_preservados)
    if preservados:
        clausula_excluir = " AND ghl_contact_id NOT IN (%s)" % ", ".join(
            ["%s"] * len(preservados)
        )
    else:
        clausula_excluir = ""
    args_base = [PREFIJO_SINTETICO + "%"] + preservados

    tablas = (("Contact", "tabContact"), ("Customer", "tabCustomer"))
    if not limpiar_sinteticos:
        _log(
            "[vinculo_contact_customer_nativo] limpiar_sinteticos=0 — se omite el "
            "blanqueo de los `manual-%`; correr de nuevo tras reiniciar los workers."
        )
        tablas = ()

    for doctype, tabla in tablas:
        try:
            pendientes = frappe.db.sql(
                f"SELECT COUNT(*) FROM `{tabla}` "
                f"WHERE ghl_contact_id LIKE %s{clausula_excluir}",
                tuple(args_base),
            )[0][0]
            if pendientes:
                frappe.db.sql(
                    f"UPDATE `{tabla}` SET ghl_contact_id = '' "
                    f"WHERE ghl_contact_id LIKE %s{clausula_excluir}",
                    tuple(args_base),
                )
            if doctype == "Contact":
                sinteticos_contact = pendientes
            else:
                sinteticos_customer = pendientes
        except Exception as exc:
            _log(
                f"[vinculo_contact_customer_nativo] ERROR limpiando sintéticos "
                f"en {doctype}: {exc}"
            )
            frappe.log_error(
                f"vinculo_contact_customer_nativo / limpieza {doctype}: {exc}",
                "Cliente ERP",
            )

    frappe.db.commit()

    # Métrica estable entre corridas: a diferencia de `candidatos` (que baja en
    # la 2a corrida porque los `manual-` ya se vaciaron), este total no cambia.
    # Es el número con el que se prueba la idempotencia.
    total_con_vinculo = frappe.db.sql(
        """
        SELECT COUNT(DISTINCT link_name)
          FROM `tabDynamic Link`
         WHERE parenttype = 'Contact' AND link_doctype = 'Customer'
        """
    )[0][0]

    _log(
        "[vinculo_contact_customer_nativo] RESUMEN "
        f"candidatos={len(candidatos)} "
        f"vinculados={vinculados} "
        f"ya_vinculados={ya_vinculados} "
        f"huerfanos={huerfanos} "
        f"ambiguos={ambiguos} "
        f"duplicados_por_contacto={multiples_por_contacto} "
        f"errores={errores} "
        f"ids_preservados={len(preservados)} "
        f"sinteticos_contact={sinteticos_contact} "
        f"sinteticos_customer={sinteticos_customer} "
        f"total_customers_con_vinculo={total_con_vinculo}"
    )
