"""Ajustes que un sitio nuevo necesita DESPUES de que corren los fixtures.

Los fixtures (`hooks.py`) hacen upsert: agregan y actualizan, pero NUNCA borran.
Eso deja dos huecos que este modulo cierra en `after_migrate`:

1. **Embudo**: frappe/crm instala 22 etapas en ingles y el fixture solo
   agrega/actualiza las 16 nuestras, asi que quedarian las 6 nativas que en
   crm.lavendi.mx se borraron (cierre 17). Misma lista que `crm/limpiar_embudo.py`.

2. **Configuracion que depende de erpnext**: frappe/utils/fixtures.py envuelve el
   ARCHIVO COMPLETO en un try/except y lo salta entero si UN doctype falta. Los
   campos de `Customer`, los property setters de `Customer` y los permisos de
   `Customer`/`Sales Invoice`/`Payment Entry` no pueden vivir en `fixtures/` o un
   sitio sin erpnext se quedaria sin NINGUN campo. Viven en `fixtures/erpnext/` y
   se importan aqui solo si erpnext esta instalado.

3. **Marca de la plataforma**: un sitio nuevo nace diciendo "Frappe" y sin logo.
   Aqui se llena `Website Settings` y `FCRM Settings` — solo si el sitio sigue en
   el default, para no pisar marca propia. Desde el 2026-09-26 el valor NO es una
   constante cableada: lo resuelve `branding_de_sitio()` contra `site_config.json`
   (`brand_app_name`, `brand_logo`, `brand_splash_image`, `brand_favicon`). La
   decision del paso 15 (2026-09-26, Alejandro) es que los sitios de cliente
   CONSERVAN la marca de plataforma "Sofía GPT by lavendi.mx": sin claves
   declaradas, un sitio NO-agencia nace con los literales de la plataforma, y solo
   un sitio que declare su propia marca la reemplaza campo por campo. Antes se
   escribia la marca de lavendi.mx en CADA sitio y en CADA migrate: los 6 sitios
   de cliente la tenian puesta (medido en su `tabSingles`).

4. **Usuario de servicio del agente**: `agente-ia@lavendi.mx` es con quien el
   proceso Node se autentica contra CADA sitio. Se crea si falta; su API key NO
   se genera aqui (una key nueva romperia al agente que ya la tiene en su .env) —
   eso lo hace a mano `asegurar_usuario_servicio()`.

5. **VAPID del push propio**: `Sofia Push Settings` es un Single — nace vacio en
   cada sitio nuevo (confirmado en sixgardens.lavendi.mx, 2026-09-21). Sin llaves,
   `get_vapid_public_key()` devuelve "" y el botón de activar push del cliente
   nunca aparece suscribible. Las llaves son POR SITIO a propósito (no se copia
   el par de crm.lavendi.mx): si dos sitios compartieran la misma llave privada,
   una suscripción de un dispositivo podria, en teoria, validarse contra el
   backend equivocado. Se generan una sola vez, aqui, si el campo esta vacio.

6. **App por defecto del sitio**: un sitio nuevo no tiene `default_app`, asi que
   el login de un usuario de ventas cae en `/apps` (el conmutador) en vez del
   CRM. Aqui se fija en `crm` si esta vacio.

7. **Print Formats de Cobranza**: la "Nota de cobro" (Sales Invoice) se creo
   directo en la BD de crm.lavendi.mx (2026-09-23) y no viaja por fixture. Se
   siembra UNA VEZ por sitio (mismo patron que los catalogos): como fixture
   re-imponeria el branding de lavendi.mx en CADA migrate sobre cualquier
   edicion que el cliente haga a su propia nota.

8. **Web Forms de captacion**: el formulario "solicita-una-cotización-ahora" es
   de lavendi.mx (su texto de exito nombra a la agencia y su success_url manda a
   lavendi.mx/gracias). Como fixture viajaba y se re-imponia en cada migrate de
   cada sitio, asi que el dominio del cliente servia un formulario con marca
   ajena. Se siembra UNA VEZ por sitio desde `fixtures/web_forms/`, saltando los
   que el sitio ya tenga, y con esos literales resueltos por sitio.

9. **Plan del sitio** (`sofia_plan`): la llave que define el producto del sitio
   (categoria C1 del registro de capacidades) no la escribia nadie — se ponia a
   mano y 9 de 11 sitios no la tenian. `_plan_del_sitio()` la declara al alta con
   default RESTRICTIVO `gratuito` (D-5/O2) y deja un Error Log audible para que el
   operador declare el plan real. No escribe `lead_owner_default` ni `brand_*`:
   esos exigen un dato humano y un default inventado crearia una identidad falsa.

Todo es idempotente y cada paso va en su propio try/except con su propio commit:
si uno falla no debe revertir lo que ya hizo el otro (paso real — un rename que
choca hacia que se perdieran los borrados de la misma corrida).
"""

import base64
import json
import os
import re
import shutil

import frappe

USUARIO_SERVICIO = "agente-ia@lavendi.mx"
VAPID_SUBJECT_DEFAULT = "mailto:contacto@lavendi.mx"

# Marca de la plataforma. `splash_image` es la pantalla post-login; `favicon` la
# pestana; `app_logo` el logo del login/menu.
#
# Hasta el 2026-09-26 esto era un dict constante (`BRANDING`) que
# `_branding_plataforma` escribia TAL CUAL en cualquier sitio, asi que los 6
# sitios de cliente nacian —y se re-imponian en cada migrate— diciendo "Sofía GPT
# by lavendi.mx" con el logo y el favicon de la agencia. Medido ese dia en el
# `tabSingles` de los 6 (sixgardens, estrublock, medicare, ena, eeplv, resanic):
# los 4 campos de `Website Settings` y los 3 de `FCRM Settings`, identicos a los
# de la agencia. Es la fuga de marca mas visible que teniamos: el cliente entra a
# SU CRM y lee el nombre de otra empresa en la pestana, el login y el menu.
#
# Ahora se resuelve por sitio (`branding_de_sitio`): estos literales son el
# DEFAULT de cualquier sitio —incluido uno de cliente— y el sitio los override
# campo por campo declarando su clave en `site_config.json`. Decision del paso 15
# (2026-09-26): los sitios de cliente CONSERVAN la marca de plataforma.
BRANDING_AGENCIA = {
    "app_name": "Sofía GPT by lavendi.mx",
    "app_logo": "/files/sofia-logo.png",
    "splash_image": "/files/sofia-logo.png",
    "favicon": "/files/sofia-favicon.png",
}
ARCHIVOS_MARCA = ("sofia-logo.png", "sofia-favicon.png")

# Campo de marca -> clave de `site_config.json` que lo declara por sitio.
CLAVES_MARCA_SITIO = {
    "app_name": "brand_app_name",
    "app_logo": "brand_logo",
    "splash_image": "brand_splash_image",
    "favicon": "brand_favicon",
}

ETAPAS_A_BORRAR = [
    "Futuras",
    "Qualification",
    "Demo/Making",
    "Proposal/Quotation",
    "Negotiation",
    "Ready to Close",
]
VIEJO, NUEVO = "4to Seguiiento", "4to Seguimiento"

WEB_FORMS_CON_DUPLICADOS = ["base-de-conocimiento", "solicita-una-cotización-ahora"]

# Items del menu de usuario (`FCRM Settings.dropdown_items`) que no deben verse en
# ningun sitio de Sofía, ni de cliente ni de la agencia. "Apps" (`app_selector`) es
# el conmutador de apps de Frappe: en un sitio que ES un solo producto (el CRM)
# solo saca al usuario del flujo. "About" expone el modal de Frappe CRM (version,
# creditos) que no le dice nada al cliente y ademas nombra a un tercero.
# Se marcan `hidden=1` en vez de borrarlos: son `is_standard`, `sync_table()` del
# propio crm los re-crearia en cada after_migrate, y su `validate()` prohibe
# borrarlos. Ocultar sobrevive a `sync_table` porque esa funcion solo AGREGA los
# que faltan y no reescribe los existentes. Este paso corre DESPUES de crm en
# after_migrate (orden de `get_installed_apps`: frappe, crm, frappe_chatwoot), asi
# que en un sitio nuevo los items ya existen cuando llegamos aqui.
# Reportado por Alejandro el 2026-09-26.
ITEMS_DROPDOWN_OCULTOS = ("app_selector", "about")

# --- UX de la plataforma que NO viaja por fixture -------------------------
# Los quick filters viven en `CRM Global Settings` (registro con `name` aleatorio,
# unicidad por `dt` + `type`): como fixture duplicaria, asi que va aqui. El CRM
# instala por defecto los 4 de abajo; crm.lavendi.mx los dejo en solo `status`
# (cierre 18) porque el buscador de texto ya cubre organizacion/correo.
QUICK_FILTERS_DEAL = ["status"]
QUICK_FILTERS_DEAL_DEFAULT = ["organization", "status", "probability", "email"]

# Vistas guardadas publicas que recibe un sitio de cliente SIN vistas propias.
# La "Embudo de ventas" de lavendi.mx filtra `ghl_status = open` (dato migrado de
# GHL); un cliente sin GHL tendria el kanban vacio, asi que aqui filtra por
# `status not in [Won, Lost]`, que funciona en cualquier sitio.
_VISTA_COLUMNAS = [
    {"label": "Organización", "type": "Link", "key": "organization", "options": "CRM Organization", "width": "12rem"},
    {"label": "Etapa", "type": "Link", "key": "status", "options": "CRM Deal Status", "width": "11rem"},
    {"label": "Valor", "type": "Currency", "key": "deal_value", "align": "right", "width": "9rem"},
    {"label": "Responsable", "type": "Link", "key": "deal_owner", "options": "User", "width": "11rem"},
    {"label": "Teléfono", "type": "Data", "key": "mobile_no", "width": "11rem"},
    {"label": "Modificado", "type": "Datetime", "key": "modified", "width": "9rem"},
]
_VISTA_FILAS = ["name", "organization", "status", "deal_value", "currency", "deal_owner", "mobile_no", "modified", "_assign"]
_ABIERTAS = {"status": ["not in", ["Won", "Lost"]]}

VISTAS_CLIENTE = [
    {
        # La vista por defecto de Oportunidades es una LISTA (no el kanban),
        # ordenada por valor desc (la decisión registrada el 2026-09-27, igual que
        # crm.lavendi.mx). `route_name: "Deals"` es lo que hace que el router del
        # SPA la elija al entrar a `/crm/deals`: solo se aplica a la vista con
        # `is_default=1`. El kanban queda como vista alterna, ya no por defecto.
        "label": "Lista", "type": "list", "route_name": "Deals",
        "is_default": 1, "pinned": 0, "filters": _ABIERTAS, "order_by": "deal_value desc",
    },
    {
        "label": "Embudo de ventas", "type": "kanban", "route_name": "Deals",
        "is_default": 0, "pinned": 1, "filters": _ABIERTAS, "order_by": "modified desc",
        "column_field": "status", "title_field": "organization",
        "kanban_fields": ["deal_value", "deal_owner", "mobile_no", "modified"],
    },
    {
        "label": "Oportunidades abiertas", "type": "list", "route_name": None,
        "is_default": 0, "pinned": 1, "filters": _ABIERTAS, "order_by": "modified desc",
    },
    {
        "label": "Ganadas", "type": "list", "route_name": None,
        "is_default": 0, "pinned": 0, "filters": {"status": "Won"}, "order_by": "closed_date desc",
    },
]


def ajustar_sitio(plan=None):
    """Punto de entrada de `after_migrate`. Nunca lanza.

    `plan` es opcional (D-5, 2026-09-27): el alta puede declarar el plan del
    sitio explícitamente —`ajustar_sitio(plan="lite")`—; si no lo pasa y el
    sitio no declara `sofia_plan`, `_plan_del_sitio` aplica el default
    restrictivo `gratuito` y lo deja en el Error Log para que el operador lo
    corrija. Nunca se hereda permiso por omisión.
    """
    for paso in (
        sembrar_catalogos_una_vez,
        sembrar_print_formats_una_vez,
        sembrar_web_forms_una_vez,
        aplicar_fixtures_erpnext,
        deduplicar_web_form_fields,
        _branding_plataforma,
        _idioma_plataforma,
        _app_por_defecto,
        _quick_filters_deal,
        _vistas_por_defecto,
        _dropdown_items_plataforma,
        _usuario_servicio_agente,
        _vapid_push,
        _plan_del_sitio,
    ):
        try:
            # El unico paso parametrizable: recibe el plan explicito del alta.
            if paso is _plan_del_sitio:
                paso(plan)
            else:
                paso()
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"provisionamiento: {paso.__name__}")


BANDERA_CATALOGOS = "fc_catalogos_sembrados"


def sembrar_catalogos_una_vez():
    """Siembra el embudo curado (CRM Deal Status / Lead Source / Lost Reason) y
    limpia las etapas nativas de frappe/crm — UNA SOLA VEZ por sitio.

    Por que no son fixtures (cambio del 2026-09-22): un fixture hace upsert en
    CADA `bench migrate`, asi que estos catalogos —que son datos de negocio de
    lavendi.mx, no producto— se re-imponian sobre el embudo que el cliente ya
    habia curado. Medido en estrublock.lavendi.mx: 34 `CRM Deal Status` = sus 18
    propias (migradas de su GHL) + nuestras 16 encima, como columnas ajenas en su
    kanban. Depurarlas a mano no servia de nada: volvian al siguiente migrate.

    Con la bandera, un sitio nuevo sigue naciendo con el embudo curado (mejor que
    las 22 etapas en ingles de frappe/crm) y uno existente deja de ser pisado.
    El borrado de etapas nativas y la correccion del typo se mudaron aqui adentro
    por la misma razon: son parte del nacimiento del sitio, no mantenimiento
    perpetuo — re-borrar en cada migrate una etapa que el cliente recreo a
    proposito es el mismo defecto.

    Para re-sembrar a mano un sitio concreto:
        frappe.db.set_default("fc_catalogos_sembrados", "")  # y correr ajustar_sitio()
    """
    if frappe.db.get_default(BANDERA_CATALOGOS):
        return

    ruta = os.path.join(frappe.get_app_path("frappe_chatwoot"), "fixtures", "catalogos")
    if os.path.isdir(ruta):
        from frappe.core.doctype.data_import.data_import import import_doc

        import_doc(ruta)

    _borrar_etapas_nativas()
    _corregir_typo()
    frappe.db.set_default(BANDERA_CATALOGOS, "1")


BANDERA_PRINT_FORMATS = "fc_print_formats_sembrados"


def sembrar_print_formats_una_vez():
    """Siembra los Print Formats de Cobranza (hoy: "Nota de cobro" de Sales
    Invoice, `fixtures/print_formats/`) — UNA SOLA VEZ por sitio.

    Mismo patron y misma razon que `sembrar_catalogos_una_vez`: el formato nacio
    directo en la BD de crm.lavendi.mx (2026-09-23) y como fixture re-imponeria
    el branding de lavendi.mx en CADA `bench migrate` sobre la nota que el
    cliente ya haya personalizado (es su cara ante su propio cliente final).

    Dos guardas: exige erpnext (Sales Invoice es de Accounts — en un sitio sin
    erpnext no hay nada que sembrar), y si el sitio ya trae un Print Format con
    el mismo nombre NO se pisa — se marca la bandera igual, porque `import_doc`
    importa con `force=True` (sobrescribe por nombre).

    Para re-sembrar a mano un sitio concreto:
        frappe.db.set_default("fc_print_formats_sembrados", "")  # y correr ajustar_sitio()
    """
    if "erpnext" not in frappe.get_installed_apps():
        return
    if frappe.db.get_default(BANDERA_PRINT_FORMATS):
        return

    ruta = os.path.join(frappe.get_app_path("frappe_chatwoot"), "fixtures", "print_formats")
    if os.path.isdir(ruta):
        nombres = []
        for f in os.listdir(ruta):
            if not f.endswith(".json"):
                continue
            with open(os.path.join(ruta, f)) as fh:
                for r in json.load(fh):
                    if r.get("name"):
                        nombres.append(r["name"])
        if any(frappe.db.exists("Print Format", n) for n in nombres):
            frappe.db.set_default(BANDERA_PRINT_FORMATS, "1")
            return

        from frappe.core.doctype.data_import.data_import import import_doc

        import_doc(ruta)

    frappe.db.set_default(BANDERA_PRINT_FORMATS, "1")


BANDERA_WEB_FORMS = "fc_web_forms_sembrados"

# El sitio de la agencia. Es el UNICO donde los literales de marca (del formulario
# de cotizacion y de la plataforma) son los correctos; en cualquier otro sitio son
# marca ajena. Se conserva el nombre en singular porque ya se usaba; la
# comparacion real va por `_es_sitio_agencia`, que contempla los DOS hostnames.
SITIO_AGENCIA = "crm.lavendi.mx"

# `sites/sofiav2.lavendi.mx` es un SYMLINK a `sites/crm.lavendi.mx`: el mismo
# sitio responde a dos hostnames, y `frappe.local.site` trae el que resolvio la
# peticion. Comparar solo contra "crm.lavendi.mx" haria que una peticion por el
# dominio publico se tratara como sitio de cliente y se le borrara su propia
# marca. Verificado en el bench el 2026-09-26.
SITIOS_AGENCIA = frozenset({SITIO_AGENCIA, "sofiav2.lavendi.mx"})

# El formulario de captacion. Es el unico de los Web Form que viajan con marca
# adentro: `base-de-conocimiento` es una herramienta interna sin literales.
WEB_FORM_COTIZACION = "solicita-una-cotización-ahora"

# Campo del Web Form -> clave de `site_config.json` que lo declara por sitio.
CLAVES_MARCA_WEB_FORM = {
    "success_url": "web_form_success_url",
    "success_message": "web_form_success_message",
    "allowed_embedding_domains": "web_form_embedding_domains",
}

MENSAJE_EXITO_NEUTRO = "Recibimos tu solicitud. Te contactamos pronto."


def sembrar_web_forms_una_vez():
    """Siembra los Web Form del app (`fixtures/web_forms/`) — UNA SOLA VEZ por
    sitio, saltando los que el sitio ya tenga y con la marca resuelta por sitio.

    Por que no son fixtures (cambio del 2026-09-26): mismo defecto que los
    catalogos. `{"dt": "Web Form", "filters": [["module", "=", "Custom"]]}` hacia
    upsert en CADA `bench migrate`, asi que el formulario de captacion de
    lavendi.mx se re-imponia en el dominio de cada cliente. Medido el 2026-09-26
    en medicare/ena/estrublock: `/solicitar-cotizacion` servia 200 con "Solicita
    una cotización ahora" y 10 menciones de lavendi.mx. Despublicarlo no servia de
    nada: volvia al siguiente migrate.

    Dos guardas, a diferencia de los catalogos:

    1. **Por documento, no por archivo.** `import_doc` importa con `force=True` y
       pisaria el formulario que el cliente ya haya personalizado. Aqui se inserta
       solo el que NO existe, asi que un sitio al que ya le llego el formulario
       (todos los actuales) no se toca: limpiarlo es decision suya, con
       `auditar_web_forms_ajenos.py`.

    2. **Los literales de marca se resuelven por sitio** (ver
       `_parametrizar_web_form`), no se copian tal cual del fixture.

    Para re-sembrar a mano un sitio concreto:
        frappe.db.set_default("fc_web_forms_sembrados", "")  # y correr ajustar_sitio()
    """
    if frappe.db.get_default(BANDERA_WEB_FORMS):
        return

    ruta = os.path.join(frappe.get_app_path("frappe_chatwoot"), "fixtures", "web_forms")
    if os.path.isdir(ruta):
        for archivo in sorted(os.listdir(ruta)):
            if not archivo.endswith(".json"):
                continue
            with open(os.path.join(ruta, archivo)) as fh:
                registros = json.load(fh)
            for registro in registros:
                nombre = registro.get("name")
                if not nombre or frappe.db.exists("Web Form", nombre):
                    continue
                doc = frappe.get_doc(_parametrizar_web_form(dict(registro)))
                doc.flags.ignore_permissions = True
                doc.insert()

    frappe.db.set_default(BANDERA_WEB_FORMS, "1")


def _parametrizar_web_form(registro):
    """Resuelve los literales de marca del formulario de captacion para ESTE sitio.

    Tres campos lo delatan cuando viaja: `success_url` (manda a lavendi.mx/gracias,
    o sea al sitio de otra empresa), `success_message` ("Un asesor de lavendi.mx te
    contacta hoy mismo") y `allowed_embedding_domains` (lavendi.mx, asi que el
    cliente no puede embeberlo en el suyo y nosotros si en el nuestro).

    Mismo criterio de diseno que `lib/correo.js` de `agente-ia`: se deriva de la
    configuracion del propio sitio y, si no hay valor, se deja NEUTRO — nunca se
    cae a la marca de la agencia. El valor del fixture sobrevive unicamente en
    `crm.lavendi.mx`, donde esa marca si es la del sitio.

    Cada sitio declara lo suyo en `site_config.json`:
        "web_form_success_url": "https://cliente.mx/gracias",
        "web_form_success_message": "Recibimos tu solicitud...",
        "web_form_embedding_domains": ["cliente.mx", "www.cliente.mx"]
    """
    if registro.get("name") != WEB_FORM_COTIZACION:
        return registro
    if _es_sitio_agencia():
        return registro

    for campo, clave in CLAVES_MARCA_WEB_FORM.items():
        valor = frappe.conf.get(clave)
        if valor is None:
            valor = _neutro_web_form(campo)
        if isinstance(valor, (list, tuple)):
            valor = "\n".join(valor)
        registro[campo] = valor
    return registro


def _neutro_web_form(campo):
    """Que poner cuando el sitio no declaro el valor. Nunca la marca de la agencia."""
    if campo == "success_url":
        # Sin destino propio, el visitante se queda en el formulario leyendo
        # `success_message`. Es la salida honesta: mandarlo a lavendi.mx/gracias
        # seria sacarlo del sitio del cliente hacia el de otra empresa.
        return ""
    if campo == "success_message":
        return MENSAJE_EXITO_NEUTRO
    # Embebido: el unico dominio deducible sin inventar es el del propio sitio
    # (los sitios de este bench se llaman como su host). El dominio de marketing
    # del cliente —donde de verdad va el iframe— lo declara el en site_config.
    # Vacio no rompe nada: `csp_embebido` cae a `frame-ancestors 'self'`.
    propio = (frappe.local.site or "").strip()
    return f"{propio}\nwww.{propio}" if "." in propio else ""


def _borrar_etapas_nativas():
    for name in ETAPAS_A_BORRAR:
        if not frappe.db.exists("CRM Deal Status", name):
            continue
        if frappe.db.count("CRM Deal", {"status": name}):
            frappe.log_error(
                f"Etapa '{name}' tiene oportunidades; no se borro.", "provisionamiento"
            )
            continue
        frappe.delete_doc("CRM Deal Status", name, force=True, ignore_permissions=True)


def _corregir_typo():
    """En un sitio migrado de GHL pueden coexistir el typo y el nombre bueno.
    Si el bueno ya existe, el typo solo se borra si nadie lo usa."""
    if not frappe.db.exists("CRM Deal Status", VIEJO):
        return
    if frappe.db.exists("CRM Deal Status", NUEVO):
        if frappe.db.count("CRM Deal", {"status": VIEJO}):
            frappe.log_error(
                f"'{VIEJO}' tiene oportunidades y '{NUEVO}' tambien existe; no se toco.",
                "provisionamiento",
            )
            return
        frappe.delete_doc("CRM Deal Status", VIEJO, force=True, ignore_permissions=True)
        return
    frappe.rename_doc("CRM Deal Status", VIEJO, NUEVO, force=True)


def aplicar_fixtures_erpnext():
    """Importa `fixtures/erpnext/` si erpnext esta instalado. `import_doc` de
    Frappe acepta un directorio y recorre los .json de adentro."""
    if "erpnext" not in frappe.get_installed_apps():
        return
    ruta = os.path.join(frappe.get_app_path("frappe_chatwoot"), "fixtures", "erpnext")
    if not os.path.isdir(ruta):
        return
    from frappe.core.doctype.data_import.data_import import import_doc

    import_doc(ruta)


def deduplicar_web_form_fields():
    """Las child tables NO se exportan como fixture: al importarlas Frappe las
    vuelve a insertar y duplica los campos del formulario (paso el 2026-09-16 en
    crm.lavendi.mx, 24 campos -> 48). Los campos viajan dentro de
    `fixtures/web_forms/web_form.json`.
    Esto limpia los duplicados que ya se hayan creado; es idempotente.

    Se queda aunque desde el 2026-09-26 los Web Form ya no sean fixture: los
    duplicados que el barrido repetido dejo en los sitios vivos siguen ahi."""
    filas = frappe.db.sql(
        """SELECT parent, fieldname, MIN(name) keep, COUNT(*) n
           FROM `tabWeb Form Field`
           WHERE parent IN %(parents)s
           GROUP BY parent, fieldname HAVING n > 1""",
        {"parents": tuple(WEB_FORMS_CON_DUPLICADOS)},
        as_dict=True,
    )
    for fila in filas:
        sobrantes = frappe.db.sql(
            """SELECT name FROM `tabWeb Form Field`
               WHERE parent=%s AND fieldname=%s AND name<>%s""",
            (fila.parent, fila.fieldname, fila.keep),
            pluck=True,
        )
        for name in sobrantes:
            frappe.db.delete("Web Form Field", {"name": name})


def _es_sitio_agencia(sitio=None):
    """Si ESTE sitio es el de lavendi.mx (por cualquiera de sus dos hostnames)."""
    return (sitio or frappe.local.site or "") in SITIOS_AGENCIA


def _nombre_desde_host(sitio=None):
    """Nombre de plataforma deducido del propio host: `medicare.lavendi.mx` ->
    "Medicare".

    Es lo unico que se puede poner sin inventar cuando el sitio no declaro
    `brand_app_name`, y es honesto: nombra al cliente, no a la agencia. Un
    acronimo sale con mayuscula inicial nada mas (`eeplv` -> "Eeplv"); si al
    cliente le importa, declara la clave y manda ese valor.
    """
    etiqueta = (sitio or frappe.local.site or "").split(".")[0]
    palabras = [p for p in re.split(r"[-_]+", etiqueta) if p]
    return " ".join(p.capitalize() for p in palabras)


def branding_de_sitio():
    """Los 4 campos de marca de la plataforma, resueltos para ESTE sitio.

    Decision del paso 15 (2026-09-26, Alejandro): los sitios de cliente CONSERVAN
    la marca de plataforma "Sofía GPT by lavendi.mx". Se abandono la
    neutralizacion: un sitio nuevo NO-agencia que no declare marca propia nace con
    los literales de `BRANDING_AGENCIA`, no con un nombre deducido del host ni con
    visuales vacios. Se conserva el override por sitio.

    Orden de resolucion:

    1. **Sitio de la agencia** -> los literales de siempre (`BRANDING_AGENCIA`).
       Es su marca; ahi no hay nada que corregir.
    2. **Cualquier otro sitio** -> arranca de `BRANDING_AGENCIA` y reemplaza,
       campo por campo, lo que el sitio declare en `site_config.json`:
           "brand_app_name": "Medicare One",
           "brand_logo": "/files/logo-cliente.png",
           "brand_splash_image": "/files/logo-cliente.png",
           "brand_favicon": "/files/favicon-cliente.png"
       Un campo declarado vacio (o no declarado) cae al default de plataforma.
    3. **Sin declarar nada** -> marca de plataforma. Es intencional: "Sofía GPT by
       lavendi.mx" es el producto que el cliente contrata, y hasta que exista marca
       blanca real (dominio propio del cliente) no se neutraliza.

    Cortesia: si el sitio declaro logo propio pero no splash, el splash usa SU
    logo — se deriva de lo que EL declaro, no del PNG de la plataforma, que es
    justo lo que hace la agencia (el mismo PNG en los dos campos).
    """
    if _es_sitio_agencia():
        return dict(BRANDING_AGENCIA)

    marca = dict(BRANDING_AGENCIA)
    declarado = {}
    for campo, clave in CLAVES_MARCA_SITIO.items():
        valor = frappe.conf.get(clave)
        declarado[campo] = valor.strip() if isinstance(valor, str) and valor.strip() else ""
        if declarado[campo]:
            marca[campo] = declarado[campo]

    # Cortesia: declaro logo propio pero no splash -> su splash es su logo. Solo
    # cuando el logo tambien es suyo; si el logo es el de plataforma, el splash se
    # queda en el de plataforma (no en un logo derivado).
    if declarado["app_logo"] and not declarado["splash_image"]:
        marca["splash_image"] = declarado["app_logo"]
    return marca


def _branding_plataforma():
    """Marca de la plataforma en un sitio nuevo. Ver el docstring del modulo.

    Los PNG viajan dentro del app (`public/images/`) y se copian a `public/files/`
    del sitio, que es de donde el SPA del CRM los pide. No hace falta crear el
    registro `File`: Frappe sirve `/files/*` del disco (verificado 2026-09-16).

    Lo que se escribe sale de `branding_de_sitio()` (default: marca de plataforma;
    override por sitio), no de una constante cableada. La guarda de abajo solo
    escribe cuando `Website Settings` sigue en el default de Frappe, asi que un
    sitio con marca propia ya puesta no se pisa. Lo que ya quedo escrito en los
    sitios vivos no se corrige aqui: se reescribe solo si un humano lo autoriza.
    """
    marca = branding_de_sitio()

    # Los PNG de la agencia se plantan si la marca resuelta de ESTE sitio los
    # referencia. Con el default de plataforma (paso 15) un sitio de cliente sin
    # marca propia SI los referencia, asi que se copian; un sitio con marca propia
    # que no los use no los recibe. Los que ya estan en disco no se borran aqui.
    referenciados = {
        valor.rsplit("/", 1)[-1]
        for valor in marca.values()
        if isinstance(valor, str) and valor.startswith("/files/")
    }
    origen = os.path.join(frappe.get_app_path("frappe_chatwoot"), "public", "images")
    destino = frappe.get_site_path("public", "files")
    os.makedirs(destino, exist_ok=True)
    for nombre in ARCHIVOS_MARCA:
        if nombre not in referenciados:
            continue
        src = os.path.join(origen, nombre)
        if not os.path.exists(src):
            continue
        dst = os.path.join(destino, nombre)
        if not os.path.exists(dst):
            shutil.copyfile(src, dst)

    ws = frappe.get_single("Website Settings")
    # Solo si nadie lo personalizo: un cliente con marca propia no se pisa.
    if (ws.app_name or "").strip() in ("", "Frappe"):
        for campo, valor in marca.items():
            ws.set(campo, valor)
        ws.save(ignore_permissions=True)

    # El SPA del CRM (BrandLogo.vue / stores/settings.js) NO lee Website
    # Settings -- lee FCRM Settings.{brand_name,brand_logo,favicon}, sembrado
    # ademas en el boot por `crm/www/crm.py:get_brand()`. Website Settings de
    # arriba es el que ve el login/Desk de Frappe; sin este bloque un sitio
    # nuevo nace con el isotipo de Frappe CRM en el SPA aunque Website Settings
    # ya diga "Sofía GPT" -- hallazgo real: crm.lavendi.mx lo tenia seteado a
    # mano desde el 18-sep, sixgardens (17-sep) nunca lo tuvo.
    fs = frappe.get_single("FCRM Settings")
    if not (fs.brand_name or "").strip():
        fs.brand_name = marca["app_name"]
        fs.brand_logo = marca["app_logo"]
        fs.favicon = marca["favicon"]
        fs.save(ignore_permissions=True)


def _idioma_plataforma():
    """Idioma por defecto del sitio: espanol.

    Sin esto un sitio nuevo nace con `System Settings.language` vacio y el SPA del
    CRM cae a ingles: los textos propios del front van en espanol (parche de la
    SPA), pero TODO lo que rotula el backend —etiquetas de campo, "Crear
    oportunidad", "Valor de la oportunidad", filtros— sale en ingles porque la
    `Translation` solo se aplica si el idioma resuelto es `es`. Verificado en
    sixgardens el 2026-09-17.

    Solo si esta vacio: un cliente que quiera ingles no se pisa. El idioma por
    USUARIO (`User.language`) sigue mandando por encima de este default.
    """
    if frappe.db.get_single_value("System Settings", "language"):
        return
    frappe.db.set_single_value("System Settings", "language", "es")


def _app_por_defecto():
    """App por defecto del sitio: el CRM.

    Sin esto el login de un usuario de ventas cae en `/apps` (el conmutador de
    apps), no en el CRM. Ni el `role_home_page` de hooks ni `Role.home_page`
    sirven para esto: `frappe/auth.py` antepone `get_default_path()`, que
    devuelve `/apps` cuando hay mas de una app instalada y ninguna ruta es
    `/app` — el resultado de `get_home_page()` nunca se consulta. El lever real
    es `System Settings.default_app`.

    Solo si esta vacio: un cliente que prefiera otra landing no se pisa. El
    `User.default_app` de un usuario sigue mandando por encima de este default.
    """
    if frappe.get_system_settings("default_app"):
        return
    frappe.db.set_single_value("System Settings", "default_app", "crm")


# Clave del plan del sitio (categoria C1). La lee `utils/plan.py`; su default
# cambio a fail-closed `gratuito` el 2026-09-27 (D-1), en paralelo a que este
# paso empieza a escribirla (D-5/Fase 3).
CLAVE_PLAN = "sofia_plan"
PLAN_DEFAULT = "gratuito"


def _escribir_site_config(clave, valor):
    """Escribe una llave en `site_config.json` del sitio actual.

    El archivo hasta ahora solo LEIA `frappe.conf`; para Escribir de verdad se
    usa la API estandar de Frappe (`frappe.installer.update_site_config`), la
    misma que usan los patches del bench — no se inventa una via nueva.
    """
    from frappe.installer import update_site_config

    update_site_config(clave, valor)
    # `update_site_config` reescribe el JSON pero NO refresca el `frappe.conf`
    # ya cargado en este proceso: sin esto, el mismo `after_migrate` que acaba
    # de declarar el plan seguiria leyendo el default hasta el siguiente request.
    frappe.conf[clave] = valor


def _plan_del_sitio(plan=None):
    """Declara `sofia_plan` en `site_config.json` al alta (D-5, categoria C1).

    Idempotente: si el sitio YA declara la llave, no la toca — un operador que
    corrigio el plan a mano no se pisa en cada `bench migrate`.

    `plan` es opcional. El alta puede pasarlo explicito
    (`ajustar_sitio(plan="lite")`). Si no se pasa y el sitio no tiene la llave,
    se aplica el default RESTRICTIVO `gratuito` (decision D-5/O2, Alejandro
    2026-09-27) y se deja un `Error Log` audible para que el operador lo
    corrija: el sitio nace bloqueado, nunca sobre-otorgado. Un valor explicito
    que no este en `NIVELES` se rechaza igual de ruidosamente y NO se escribe
    (el sitio se queda con el default fail-closed de `plan.py`).

    No se escribe `lead_owner_default` ni `brand_*` aqui: exigen un dato humano
    (el dueno de negocio / la marca del cliente) y un default inventado crearia
    una identidad falsa. Se declaran a mano en el alta (ver §5 del registro de
    capacidades y `RUNBOOK-ALTA-CLIENTE.md`).
    """
    from frappe_chatwoot.utils.plan import NIVELES

    if frappe.conf.get(CLAVE_PLAN):
        return

    if plan:
        if plan not in NIVELES:
            frappe.log_error(
                f"Sitio {frappe.local.site}: sofia_plan explicito invalido ({plan!r}); "
                f"validos: {', '.join(NIVELES)}. No se escribio; el sitio queda en el "
                f"default fail-closed '{PLAN_DEFAULT}'.",
                "provisionamiento: plan invalido",
            )
            return
        declarado = plan
    else:
        declarado = PLAN_DEFAULT
        frappe.log_error(
            f"Sitio {frappe.local.site} sin sofia_plan: se aplico el default "
            f"restrictivo '{PLAN_DEFAULT}'. Declara el plan real del cliente "
            f"(site_config.sofia_plan) para que el gate no lo deje bloqueado.",
            "provisionamiento: plan por default",
        )

    _escribir_site_config(CLAVE_PLAN, declarado)


def _dropdown_items_plataforma():
    """Oculta del menu de usuario los items que no son de producto. Ver
    `ITEMS_DROPDOWN_OCULTOS`. Idempotente: si ya estan ocultos no guarda."""
    crm_settings = frappe.get_single("FCRM Settings")
    ocultados = 0
    for item in crm_settings.dropdown_items:
        if item.name1 in ITEMS_DROPDOWN_OCULTOS and not item.hidden:
            item.hidden = 1
            ocultados += 1
    if ocultados:
        crm_settings.save(ignore_permissions=True)


def _quick_filters_deal():
    """Deja los quick filters de `CRM Deal` como en crm.lavendi.mx.

    Solo si el sitio todavia tiene los 4 por defecto del CRM: un cliente que ya
    los ajusto a su gusto no se pisa. No toca `in_standard_filter` de los
    DocFields — eso viaja por Property Setter (fixture) y ya coincide.
    """
    existente = frappe.db.exists("CRM Global Settings", {"dt": "CRM Deal", "type": "Quick Filters"})
    if existente:
        actual = frappe.parse_json(frappe.db.get_value("CRM Global Settings", existente, "json") or "[]")
        if actual != QUICK_FILTERS_DEAL_DEFAULT:
            return
        frappe.db.set_value("CRM Global Settings", existente, "json", json.dumps(QUICK_FILTERS_DEAL))
        return
    frappe.get_doc(
        {
            "doctype": "CRM Global Settings",
            "dt": "CRM Deal",
            "type": "Quick Filters",
            "json": json.dumps(QUICK_FILTERS_DEAL),
        }
    ).insert(ignore_permissions=True)


def _vistas_por_defecto():
    """Siembra las vistas guardadas publicas de `CRM Deal` en un sitio sin ninguna.

    La guarda es a proposito amplia (cualquier vista publica de `CRM Deal`): si
    el cliente ya armo las suyas, no se le mete nada. Las vistas no viajan por
    fixture porque su `name` es un consecutivo y `user` distingue las personales.
    """
    if frappe.db.exists("CRM View Settings", {"dt": "CRM Deal", "public": 1}):
        return
    for vista in VISTAS_CLIENTE:
        doc = frappe.get_doc(
            {
                "doctype": "CRM View Settings",
                "label": vista["label"],
                "dt": "CRM Deal",
                "type": vista["type"],
                "route_name": vista["route_name"],
                "is_default": vista["is_default"],
                "pinned": vista["pinned"],
                "public": 1,
                # '' y no None: la API de vistas filtra `user = ''` para las
                # publicas. Con NULL la vista queda invisible para todos (el
                # mismo tropiezo del 2026-09-06 con las vistas de la migracion).
                "user": "",
                "filters": json.dumps(vista["filters"]),
                "order_by": vista["order_by"],
                "column_field": vista.get("column_field"),
                "title_field": vista.get("title_field"),
                "columns": json.dumps(_VISTA_COLUMNAS),
                "rows": json.dumps(_VISTA_FILAS),
                "kanban_fields": json.dumps(vista.get("kanban_fields") or []),
            }
        )
        doc.insert(ignore_permissions=True)


def _usuario_servicio_agente():
    """Crea el usuario con el que el agente Node se autentica contra este sitio.

    NO genera la API key a proposito: `frappe.core.doctype.user.user.generate_keys`
    SIEMPRE regenera el `api_secret`, asi que correrlo en cada migrate rompería al
    agente que ya tiene la key en su `.env`. La key se genera a mano una sola vez
    con `asegurar_usuario_servicio()`.
    """
    if frappe.db.exists("User", USUARIO_SERVICIO):
        return
    doc = frappe.get_doc(
        {
            "doctype": "User",
            "email": USUARIO_SERVICIO,
            "first_name": "Agente IA",
            "user_type": "System User",
            "send_welcome_email": 0,
            "roles": [{"role": "System Manager"}],
        }
    )
    doc.insert(ignore_permissions=True)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _vapid_push():
    """Genera el par de llaves VAPID de este sitio si `Sofia Push Settings`
    sigue vacio. Ver el punto 5 del docstring del modulo.

    Formato: privada en "raw" (el escalar de 32 bytes, b64url sin padding) y
    publica en punto EC sin comprimir (0x04 + x + y, 65 bytes, b64url) — el
    mismo formato que `applicationServerKey` de la Push API del navegador.
    `Vapid01.from_string()` detecta "raw" por longitud (32 bytes tras decodificar)
    sin necesitar encabezados PEM; es la variante que costo diagnosticar el
    2026-09-20 en crm.lavendi.mx (la privada se habia guardado como PEM completo
    y pywebpush tronaba con `ValueError: Could not deserialize key data`). Se
    verifica el roundtrip contra `from_string()` antes de guardar, para no repetir
    ese incidente en un sitio nuevo.
    """
    settings = frappe.get_single("Sofia Push Settings")
    if settings.vapid_public_key:
        return
    from py_vapid import Vapid01 as Vapid

    vapid = Vapid()
    vapid.generate_keys()
    priv_numbers = vapid.private_key.private_numbers()
    pub_numbers = vapid.public_key.public_numbers()
    private_raw = priv_numbers.private_value.to_bytes(32, "big")
    public_raw = b"\x04" + pub_numbers.x.to_bytes(32, "big") + pub_numbers.y.to_bytes(32, "big")
    private_b64 = _b64url(private_raw)
    public_b64 = _b64url(public_raw)

    # Roundtrip: si esto no coincide, mejor no guardar nada (el push quedaria
    # configurado con una llave que webpush() no puede leer, y el sintoma seria
    # un fallo silencioso en cada envio, no un error visible aqui).
    comprobar = Vapid.from_string(private_b64)
    if comprobar.private_key.private_numbers().private_value != priv_numbers.private_value:
        frappe.log_error("VAPID: el roundtrip de la llave generada no coincide", "provisionamiento")
        return

    settings.vapid_public_key = public_b64
    settings.vapid_private_key = private_b64
    settings.vapid_subject = VAPID_SUBJECT_DEFAULT
    settings.save(ignore_permissions=True)


def asegurar_usuario_servicio():
    """Punto de entrada MANUAL (no `after_migrate`).

        bench --site <sitio> execute frappe_chatwoot.utils.provisionamiento.asegurar_usuario_servicio

    Crea el usuario de servicio si falta y genera su API key SOLO si no tiene.
    Devuelve la key para copiarla al `FRAPPE_SITES` del `.env` del agente. Nunca
    pisa una key existente: en el sitio compartido es un no-op.
    """
    _usuario_servicio_agente()
    frappe.db.commit()
    doc = frappe.get_doc("User", USUARIO_SERVICIO)
    generada = False
    if not doc.api_key:
        from frappe.core.doctype.user.user import generate_keys

        claves = generate_keys(USUARIO_SERVICIO)
        frappe.db.commit()
        doc.reload()
        generada = True
        return {"usuario": USUARIO_SERVICIO, "generada": generada, **claves}
    return {"usuario": USUARIO_SERVICIO, "generada": generada, "api_key": doc.api_key}
