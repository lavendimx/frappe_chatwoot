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

Todo es idempotente y cada paso va en su propio try/except con su propio commit:
si uno falla no debe revertir lo que ya hizo el otro (paso real — un rename que
choca hacia que se perdieran los borrados de la misma corrida).
"""

import os
import shutil

import frappe

USUARIO_SERVICIO = "agente-ia@lavendi.mx"

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


def ajustar_sitio():
    """Punto de entrada de `after_migrate`. Nunca lanza."""
    for paso in (
        _borrar_etapas_nativas,
        _corregir_typo,
        aplicar_fixtures_erpnext,
        deduplicar_web_form_fields,
        _branding_plataforma,
        _usuario_servicio_agente,
    ):
        try:
            paso()
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"provisionamiento: {paso.__name__}")


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
