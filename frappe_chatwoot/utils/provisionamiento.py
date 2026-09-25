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

3. **Marca de la plataforma**: un sitio nuevo nace diciendo "Frappe" y sin logo
   (el SPA del CRM referencia `/files/sofia-logo.png`, que sin el archivo sale
   roto). Aqui se copian los PNG que viajan con el app y se llena `Website
   Settings` — solo si el sitio sigue en el default, para no pisar marca propia.

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

Todo es idempotente y cada paso va en su propio try/except con su propio commit:
si uno falla no debe revertir lo que ya hizo el otro (paso real — un rename que
choca hacia que se perdieran los borrados de la misma corrida).
"""

import base64
import json
import os
import shutil

import frappe

USUARIO_SERVICIO = "agente-ia@lavendi.mx"
VAPID_SUBJECT_DEFAULT = "mailto:contacto@lavendi.mx"

# Marca de la plataforma (mismos valores que crm.lavendi.mx). `splash_image` es la
# pantalla post-login; `favicon` la pestana; `app_logo` el logo del login/menu.
BRANDING = {
    "app_name": "Sofía GPT by lavendi.mx",
    "app_logo": "/files/sofia-logo.png",
    "splash_image": "/files/sofia-logo.png",
    "favicon": "/files/sofia-favicon.png",
}
ARCHIVOS_MARCA = ("sofia-logo.png", "sofia-favicon.png")

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
        "label": "Embudo de ventas", "type": "kanban", "route_name": "Deals",
        "is_default": 1, "pinned": 1, "filters": _ABIERTAS, "order_by": "modified desc",
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


def ajustar_sitio():
    """Punto de entrada de `after_migrate`. Nunca lanza."""
    for paso in (
        sembrar_catalogos_una_vez,
        sembrar_print_formats_una_vez,
        aplicar_fixtures_erpnext,
        deduplicar_web_form_fields,
        _branding_plataforma,
        _idioma_plataforma,
        _app_por_defecto,
        _quick_filters_deal,
        _vistas_por_defecto,
        _usuario_servicio_agente,
        _vapid_push,
    ):
        try:
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
    crm.lavendi.mx, 24 campos -> 48). Los campos viajan dentro de `web_form.json`.
    Esto limpia los duplicados que ya se hayan creado; es idempotente."""
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


def _branding_plataforma():
    """Marca de la plataforma en un sitio nuevo. Ver el docstring del modulo.

    Los PNG viajan dentro del app (`public/images/`) y se copian a `public/files/`
    del sitio, que es de donde el SPA del CRM los pide. No hace falta crear el
    registro `File`: Frappe sirve `/files/*` del disco (verificado 2026-09-16).
    """
    origen = os.path.join(frappe.get_app_path("frappe_chatwoot"), "public", "images")
    destino = frappe.get_site_path("public", "files")
    os.makedirs(destino, exist_ok=True)
    for nombre in ARCHIVOS_MARCA:
        src = os.path.join(origen, nombre)
        if not os.path.exists(src):
            continue
        dst = os.path.join(destino, nombre)
        if not os.path.exists(dst):
            shutil.copyfile(src, dst)

    ws = frappe.get_single("Website Settings")
    # Solo si nadie lo personalizo: un cliente con marca propia no se pisa.
    if (ws.app_name or "").strip() in ("", "Frappe"):
        for campo, valor in BRANDING.items():
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
        fs.brand_name = BRANDING["app_name"]
        fs.brand_logo = BRANDING["app_logo"]
        fs.favicon = BRANDING["favicon"]
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
