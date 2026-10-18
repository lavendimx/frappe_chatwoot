# Copyright (c) 2026, lavendi.mx
# License: MIT
"""
Limpieza puntual de `Customer` duplicados y de prueba (2026-10-18).

Por qué no es un patch
    Un patch corre solo en cada `bench migrate` y debe valer para cualquier
    sitio. Esto es lo contrario: una limpieza de los registros concretos que
    quedaron sucios en la base de lavendi.mx por el bug del vínculo
    (`utils/cliente_erp.py` explica la causa). Se corre a mano, una vez, con el
    resultado a la vista.

Qué hace y qué NO hace
    - **Nunca borra.** Un `Customer` duplicado se marca `disabled = 1`. Es
      reversible con un UPDATE y deja la trazabilidad de que esa persona estuvo
      dos veces en la base. Borrar es irreversible y además arrastraría
      `Dynamic Link` y referencias.
    - **Nunca toca una factura emitida.** Si el Customer que iba a deshabilitar
      tiene `Sales Invoice` o `Payment Entry`, el script se NIEGA y lo reporta
      en vez de improvisar una reasignación contable. Medido el 2026-10-18: los
      9 candidatos tienen 0 facturas y 0 pagos, así que esta rama no se dispara
      — está para que no se dispare en silencio si alguien lo vuelve a correr
      con otros datos.
    - Es idempotente: correrlo dos veces no cambia nada la segunda.

Criterio del sobreviviente (decisión #5 del contrato)
    Gana el que tiene facturas; en empate, el más antiguo por `creation`.
    Reasignar facturas tiene más riesgo contable que reasignar un registro
    vacío, así que el que ya carga historia se queda quieto.
"""

import re
import unicodedata

import frappe

# Registros de prueba que quedaron vivos en producción. Se listan explícitos,
# no por patrón: un `LIKE 'TEST%'` podría barrer un cliente real que se llame
# así. Si alguno tuviera facturas, el guard de abajo lo protege igual.
CUSTOMERS_DE_PRUEBA = [
    "TEST-OBJETIVO Prueba Objetivo",
    "TmpFusionRecenciaA",
    "TmpFusionRecenciaB",
    "ZZZ Fusion Test A",
    "ZZZ Fusion Test B",
]


_SUFIJO_AUTOGENERADO = re.compile(r"(?:\s*\(\d+\)|\s+-\s*\d+)\s*$")


def _normalizar(nombre: str) -> str:
    # Mismo criterio que `facturacion._nombre_normalizado`: el sufijo que
    # agrega ERPNext al chocar un nombre ("Irene Martinez (2)") es ruido.
    nombre = _SUFIJO_AUTOGENERADO.sub("", (nombre or "").strip()).lower()
    nombre = "".join(
        ch for ch in unicodedata.normalize("NFD", nombre)
        if unicodedata.category(ch) != "Mn"
    )
    return " ".join(sorted(nombre.split()))


def _agrupar_por_identidad(customers: list[dict]) -> list[list[dict]]:
    """Componentes conexas por `Contact` compartido O nombre normalizado
    compartido. Misma lógica que `facturacion._agrupar_por_identidad` — están
    duplicadas a propósito: este script es desechable y no debe obligar al
    módulo de facturación a exportar un helper solo para él.

    Agrupar por UNA sola clave (contacto si existe, si no el nombre) no sirve:
    `Hector Morelos` no tenía `Dynamic Link` y `Hector Morelos - 1` sí, así que
    con prioridad por contacto caían en grupos distintos pese a llamarse igual.
    """
    from frappe_chatwoot.utils import cliente_erp

    padre: dict[str, str] = {}

    def raiz(x: str) -> str:
        while padre.setdefault(x, x) != x:
            padre[x] = padre[padre[x]]
            x = padre[x]
        return x

    for c in customers:
        c["contact"] = cliente_erp.contacto_de_customer(c["name"])
        claves = []
        if c["contact"]:
            claves.append(f"contact:{c['contact']}")
        norm = _normalizar(c["customer_name"])
        if norm:
            claves.append(f"nombre:{norm}")
        c["_claves"] = claves or [f"self:{c['name']}"]
        for k in c["_claves"][1:]:
            ra, rb = raiz(c["_claves"][0]), raiz(k)
            if ra != rb:
                padre[ra] = rb

    grupos: dict[str, list[dict]] = {}
    for c in customers:
        grupos.setdefault(raiz(c["_claves"][0]), []).append(c)
    return list(grupos.values())


def _carga_contable(customer: str) -> dict:
    return {
        "facturas": frappe.db.count("Sales Invoice", {"customer": customer}),
        "pagos": frappe.db.count(
            "Payment Entry", {"party_type": "Customer", "party": customer}
        ),
    }


def _deshabilitar(customer: str, motivo: str, reporte: list) -> None:
    carga = _carga_contable(customer)
    if carga["facturas"] or carga["pagos"]:
        reporte.append(
            f"  OMITIDO {customer}: tiene {carga['facturas']} factura(s) y "
            f"{carga['pagos']} pago(s) — requiere decisión contable, no se toca"
        )
        return
    if frappe.db.get_value("Customer", customer, "disabled"):
        reporte.append(f"  ya estaba deshabilitado: {customer}")
        return
    frappe.db.set_value("Customer", customer, "disabled", 1, update_modified=False)
    reporte.append(f"  DESHABILITADO {customer} ({motivo})")


def run(dry_run: int = 1):
    """`dry_run=1` (default) solo reporta. Correr con `dry_run=0` para aplicar.

    `bench --site <sitio> execute frappe_chatwoot.limpieza_customers_duplicados.run --kwargs "{'dry_run':0}"`
    """
    dry_run = int(dry_run)
    reporte = []
    reporte.append(f"=== Limpieza de Customers · dry_run={dry_run} ===")

    # --- 0) Vincular por nombre exacto lo que el patch no pudo ---------------
    # El patch de backfill resuelve por `ghl_contact_id`, así que los Customers
    # que NUNCA tuvieron ese campo (los más viejos de la migración del
    # 2026-09-10) se quedaron sin vínculo. Aquí se rescatan por coincidencia
    # EXACTA de `customer_name` con `Contact.full_name`, y SOLO cuando el match
    # es 1:1 — con dos candidatos se prefiere no vincular a vincular mal: un
    # vínculo equivocado le cuelga las facturas de alguien a otra persona en el
    # panel que el equipo mira mientras habla con el cliente.
    from frappe_chatwoot.utils import cliente_erp

    vinculados_ahora: set[str] = set()
    reporte.append("\nVínculo por nombre exacto (los que el patch no alcanzó):")
    sin_vinculo = frappe.db.sql(
        """
        SELECT c.name, c.customer_name
        FROM `tabCustomer` c
        WHERE c.disabled = 0
          AND NOT EXISTS (
              SELECT 1 FROM `tabDynamic Link` dl
              WHERE dl.parenttype = 'Contact' AND dl.link_doctype = 'Customer'
                AND dl.link_name = c.name
          )
        """,
        as_dict=True,
    )
    for c in sin_vinculo:
        candidatos = frappe.db.sql(
            "SELECT name FROM `tabContact` WHERE LOWER(full_name) = LOWER(%s)",
            c["customer_name"], as_dict=True,
        )
        if len(candidatos) != 1:
            reporte.append(
                f"  sin match único ({len(candidatos)} candidatos): {c['name']}"
            )
            continue
        contacto = candidatos[0]["name"]
        # Se anota SIEMPRE, también en dry-run: el paso 1b tiene que razonar
        # sobre el estado que habría DESPUÉS de este paso, no sobre el actual.
        # Sin esto, un dry-run reportaba que deshabilitaría a "Albercas JCV" y
        # "Six Gardens" — clientes reales que este mismo paso acababa de
        # rescatar. Un dry-run que miente es peor que no tenerlo.
        vinculados_ahora.add(c["name"])
        if dry_run:
            reporte.append(f"  [dry-run] vincularía {c['name']} -> Contact {contacto}")
        else:
            cliente_erp.vincular(contacto, c["name"])
            reporte.append(f"  VINCULADO {c['name']} -> Contact {contacto}")

    activos = frappe.get_all(
        "Customer", filters={"disabled": 0}, fields=["name", "customer_name", "creation"],
        order_by="creation asc",
    )

    # --- 1) Duplicados por identidad -------------------------------------
    duplicados = [g for g in _agrupar_por_identidad(activos) if len(g) > 1]
    reporte.append(f"\nGrupos con más de un Customer activo: {len(duplicados)}")
    for miembros in duplicados:
        enriquecidos = [{**m, **_carga_contable(m["name"])} for m in miembros]
        enriquecidos.sort(key=lambda m: (-m["facturas"], -m["pagos"], m["creation"]))
        sobreviviente = enriquecidos[0]
        reporte.append(
            f"\n{' + '.join(m['name'] for m in enriquecidos)}"
            f"\n  ->  sobrevive {sobreviviente['name']} "
            f"({sobreviviente['facturas']} fact., {sobreviviente['pagos']} pagos)"
        )
        for m in enriquecidos[1:]:
            if dry_run:
                reporte.append(
                    f"  [dry-run] deshabilitaría {m['name']} "
                    f"({m['facturas']} fact., {m['pagos']} pagos)"
                )
            else:
                _deshabilitar(m["name"], f"duplicado de {sobreviviente['name']}", reporte)

    # --- 1b) Huérfanos sin carga contable ----------------------------------
    # Un `Customer` activo que (a) no resuelve a ningún `Contact` ni por
    # Dynamic Link ni por nombre exacto, y (b) no tiene ninguna factura ni
    # ningún pago, es un registro muerto: en este sistema todo cliente real
    # nace de un contacto del CRM. Es el caso de "Moreno Eleazer", el Customer
    # que sobrevivió a la fusión del 2026-10-17 y que seguía apareciendo en el
    # selector de Cobranza como una segunda persona.
    #
    # El agrupado por nombre NO lo atrapa: "Moreno Eleazer" y "Eleazer Moreno
    # SG" normalizan distinto por el sufijo "SG". Por eso hace falta esta regla
    # aparte y no basta con el dedup.
    #
    # Sigue siendo `disabled`, no `DELETE`: si alguno resultara ser un cliente
    # real sin contacto, se revierte con un UPDATE.
    reporte.append("\nHuérfanos sin facturas ni pagos (registros muertos):")
    for c in sin_vinculo:
        if c["name"] in vinculados_ahora or frappe.db.exists(
            "Dynamic Link",
            {"parenttype": "Contact", "link_doctype": "Customer", "link_name": c["name"]},
        ):
            continue  # el paso 0 ya lo vinculó (o lo vinculará en corrida real)
        carga = _carga_contable(c["name"])
        if carga["facturas"] or carga["pagos"]:
            reporte.append(
                f"  CONSERVADO {c['name']}: sin contacto pero con "
                f"{carga['facturas']} factura(s) — requiere revisión humana"
            )
            continue
        if dry_run:
            reporte.append(f"  [dry-run] deshabilitaría {c['name']} (huérfano, 0 fact., 0 pagos)")
        else:
            _deshabilitar(c["name"], "huérfano sin carga contable", reporte)

    # --- 2) Registros de prueba -------------------------------------------
    reporte.append("\nRegistros de prueba:")
    for nombre in CUSTOMERS_DE_PRUEBA:
        if not frappe.db.exists("Customer", nombre):
            reporte.append(f"  no existe (nada que hacer): {nombre}")
            continue
        if dry_run:
            carga = _carga_contable(nombre)
            estado = "ya deshabilitado" if frappe.db.get_value("Customer", nombre, "disabled") else "activo"
            reporte.append(
                f"  [dry-run] {nombre} — {estado}, "
                f"{carga['facturas']} fact., {carga['pagos']} pagos"
            )
        else:
            _deshabilitar(nombre, "registro de prueba", reporte)

    if not dry_run:
        frappe.db.commit()
        reporte.append("\nCambios aplicados y confirmados (commit).")
    else:
        reporte.append("\nNada aplicado — correr con dry_run=0 para ejecutar.")

    print("\n".join(reporte))
    return "\n".join(reporte)
