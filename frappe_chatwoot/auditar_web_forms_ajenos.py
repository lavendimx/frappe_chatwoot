# Copyright (c) 2026, lavendi.mx
"""Web Forms de la agencia que se colaron a un sitio de cliente.

PARA QUÉ
    Hasta el 2026-09-26 `hooks.py` mandaba `{"dt": "Web Form", "filters":
    [["module", "=", "Custom"]]}` como fixture, así que el formulario de captación
    de lavendi.mx ("solicita-una-cotización-ahora") se re-imponía en CADA
    `bench migrate` de CADA sitio. Medido ese día: medicare, ena y estrublock
    servían `/solicitar-cotizacion` con el título y los textos de la agencia.

    Sacarlo de los fixtures detiene la reimposición, pero NO limpia lo que ya
    aterrizó. Eso es lo que reporta este script, sitio por sitio.

    No decide por el cliente: un formulario puede estar recibiendo leads reales
    (los cuenta) y despublicarlo sin avisar sería tirar captación viva. Por eso
    el default es solo mirar.

CÓMO
    Dry-run por defecto: lista los Web Form de módulo `Custom` del sitio, marca
    cuáles traen literales de lavendi.mx, cuántas `Solicitud Web` han entrado por
    cada uno y qué haría con ellos. Para escribir hay que pasar `aplicar=True`
    explícitamente, y aun así la acción por defecto es NEUTRALIZAR (despublicar y
    limpiar los literales), no borrar — borrar se pide aparte con `borrar=True`.

Uso (solo lectura, seguro):
    bench --site medicare.lavendi.mx execute \
        frappe_chatwoot.auditar_web_forms_ajenos.ejecutar

Uso (escribe — requiere gate humano, NO correr sin autorización):
    bench --site medicare.lavendi.mx execute \
        frappe_chatwoot.auditar_web_forms_ajenos.ejecutar \
        --kwargs "{'aplicar': True}"
"""

import frappe

# Los Web Form que este app siembra a propósito (fixtures/web_forms/). Cualquier
# otro de módulo Custom en un sitio de cliente es suyo: ni se reporta como ajeno
# ni se toca.
PROPIOS = ["solicita-una-cotización-ahora", "base-de-conocimiento"]

# El formulario que de verdad lleva marca adentro. `base-de-conocimiento` es una
# herramienta interna (login requerido, sin literales de la agencia): viaja igual,
# pero no contamina la cara pública de nadie.
CON_MARCA = "solicita-una-cotización-ahora"

SITIO_AGENCIA = "crm.lavendi.mx"

# Qué campos se revisan y con qué se quedan al neutralizar.
CAMPOS_MARCA = {
    "success_url": "",
    "success_message": "Recibimos tu solicitud. Te contactamos pronto.",
    "allowed_embedding_domains": "",
}

MARCADORES = ("lavendi.mx", "lavendi")


def _literales(doc):
    """Campos del formulario que mencionan a la agencia."""
    hallazgos = {}
    for campo in CAMPOS_MARCA:
        valor = (doc.get(campo) or "").strip()
        if valor and any(m in valor.lower() for m in MARCADORES):
            hallazgos[campo] = valor
    return hallazgos


def ejecutar(aplicar=False, borrar=False):
    """Reporta (y opcionalmente neutraliza) los Web Form ajenos de ESTE sitio."""
    sitio = frappe.local.site
    reporte = {"sitio": sitio, "modo": "aplicar" if aplicar else "dry-run", "formularios": []}

    if sitio == SITIO_AGENCIA:
        # Aquí los literales de lavendi.mx son los correctos: es su propio sitio.
        reporte["nota"] = "sitio de la agencia: nada que limpiar, no se toca"
        return reporte

    nombres = frappe.get_all(
        "Web Form", filters={"module": "Custom"}, pluck="name", order_by="name"
    )

    for nombre in nombres:
        doc = frappe.get_doc("Web Form", nombre)
        propio_del_app = nombre in PROPIOS
        literales = _literales(doc) if propio_del_app else {}

        # Un formulario que ya recibió solicitudes está captando de verdad: el
        # script lo dice en voz alta para que nadie lo apague a ciegas.
        try:
            solicitudes = frappe.db.count(doc.doc_type)
        except Exception:
            solicitudes = None

        fila = {
            "name": nombre,
            "route": doc.route,
            "doc_type": doc.doc_type,
            "published": doc.published,
            "del_app": propio_del_app,
            "literales_de_la_agencia": literales,
            f"registros_en_{doc.doc_type}": solicitudes,
        }

        if not propio_del_app:
            fila["accion"] = "ninguna (formulario del cliente)"
        elif nombre != CON_MARCA and not literales:
            fila["accion"] = "ninguna (herramienta interna, sin marca)"
        elif borrar:
            fila["accion"] = "BORRAR el Web Form completo"
        elif literales or doc.published:
            fila["accion"] = (
                "neutralizar: published=0 y "
                + ", ".join(f"{c}={v!r}" for c, v in CAMPOS_MARCA.items())
            )
        else:
            fila["accion"] = "ninguna (ya neutralizado)"

        if aplicar and fila["accion"].startswith(("neutralizar", "BORRAR")):
            _aplicar(doc, borrar)
            fila["aplicado"] = True

        reporte["formularios"].append(fila)

    if aplicar:
        frappe.db.commit()

    # Sin `print`: `bench execute` ya imprime el valor de retorno como JSON.
    return reporte


def _aplicar(doc, borrar):
    if borrar:
        frappe.delete_doc("Web Form", doc.name, force=True, ignore_permissions=True)
        return
    doc.published = 0
    for campo, neutro in CAMPOS_MARCA.items():
        doc.set(campo, neutro)
    doc.flags.ignore_permissions = True
    doc.save()
