"""Espeja `_assign` -> `deal_owner` / `lead_owner` (F1, pendiente real de Estrublock).

QUÉ PROBLEMA RESUELVE
---------------------
En Estrublock las 2 `Assignment Rule` de Round Robin (Larisa -> Belen -> Fernanda)
escriben `_assign` y crean un `ToDo`, pero **no** tocan `deal_owner`/`lead_owner`.
El equipo viene de GHL, donde se trabaja por "dueno del contacto"; si el panel
nuevo filtra/asigna por dueno, esas fichas quedan sin dueno. Este handler copia,
cuando corresponde, el usuario que acaba de asignar la regla al campo de dueno.

HOOK ELEGIDO: `on_change` -- Y POR QUE **NO** `on_update`
---------------------------------------------------------
Verificado en el codigo de Frappe 15.120.0 de este mismo bench:

1. La `Assignment Rule` corre en el hook **comodin** `"*"`:
   `apps/frappe/frappe/hooks.py` registra
   `frappe.automation.doctype.assignment_rule.assignment_rule.apply` en
   `doc_events["*"]["on_update"]` (y en `on_update_after_submit` / `on_cancel`).

2. Frappe ejecuta los handlers de un evento asi
   (`frappe/model/document.py`, `Document.hook` -> `composer`):
       `doc_events[doctype][method]`  **antes de**  `doc_events["*"][method]`
   O sea: cualquier `on_update` que colguemos aqui corre **antes** de que la
   regla asigne, y leeria un `_assign` **viejo / vacio**. Por eso `on_update` no
   sirve para este caso, aunque sea el hook "obvio".

3. `_assign` lo escribe un tercero todavia mas abajo: `Assignment Rule` inserta
   un `ToDo`; el `ToDo.on_update` -> `update_in_reference()`
   (`apps/frappe/frappe/desk/doctype/todo/todo.py`) hace
   `frappe.db.set_value(<ref>, "_assign", ..., update_modified=False)`.

4. El unico evento estandar que Frappe corre **despues** de `on_update` en la
   misma transaccion es `on_change`
   (`Document.run_post_save_methods`: `run_method("on_update")` ... luego
   `run_method("on_change")`). En 15.120.0 **no existe** `after_save` (no lo
   llama `_save` ni `run_post_save_methods`). Al correr `on_change`, el `ToDo`
   ya se inserto y el `"_assign"` ya esta en la BD -> se lee fresco.

Riesgo aceptado del hook: `on_change` tambien se dispara cuando el documento se
guarda sin cambios. El handler es barato (una lectura + comparacion de strings,
sin I/O de red) y sale en la primera linea si no hay nada que hacer.

PRECEDENCIA DEL DUENO (la regla que se eligio)
----------------------------------------------
Si el dueno ya es un **usuario valido del sitio**, NO se pisa -- aunque sea
distinto del que asigna la regla. Es el caso de una asignacion manual hecha en
el panel, y respetarla es lo pedido ("no debe pisar un owner ya puesto a mano").
El campo se siembra **solo** cuando esta vacio o cuando trae un valor que NO es
un usuario valido (p. ej. un correo de lavendi.mx que no existe en el sitio,
justo el patron que reventaba con 417 en sixgardens): ahi se corrige.

Costo del tradeoff: la rotacion posterior del Round Robin no re-escribe un dueno
ya sembrado (las reglas solo asignan cuando no hay asignacion previa, asi que la
rotacion tampoco es frecuente). Si algun dia se quiere que el dueno siga siempre
a `_assign`, se cambia a pisar cuando el dueno actual pertenezca al propio set de
asesoras de la regla -- se decidio no acoplarlo a la `Assignment Rule` hoy.

FORMATO DE `_assign`
--------------------
Puede venir como JSON (`'["a@x"]'`), como lista, como dict, o como CSV de
Frappe viejo (`"a@x, b@y"`). Se normaliza a lista de strings; vacio o
malformado -> no hace nada. Nunca lanza: un fallo se registra y el guardado del
documento sigue (mismo criterio que `estatus_deal.sincronizar`).
"""

import json

import frappe

# Campo de dueno propio de cada doctype (Link -> `User`, verificado contra los
# JSON del app crm: `crm_deal.deal_owner` y `crm_lead.lead_owner`).
CAMPOS_DUENO = {
    "CRM Deal": "deal_owner",
    "CRM Lead": "lead_owner",
}


def _saltar():
    """No corremos en cargas masivas / migraciones / instalacion.

    Espeja los flags que el propio `assignment_rule.apply()` consulta e incluye
    `in_migrate`/`in_import`: durante el ETL historico (F7) el dueno lo define la
    importacion, no este espejo.
    """
    return bool(
        frappe.flags.in_patch
        or frappe.flags.in_install
        or frappe.flags.in_setup_wizard
        or frappe.flags.in_migrate
        or frappe.flags.in_import
    )


def usuarios_asignados(valor):
    """Normaliza `_assign` a una lista de usuarios (strings). Nunca lanza."""
    if not valor:
        return []
    datos = valor
    if isinstance(datos, (bytes, bytearray)):
        datos = datos.decode("utf-8", "ignore")
    if isinstance(datos, str):
        texto = datos.strip()
        if not texto:
            return []
        try:
            datos = json.loads(texto)
        except (TypeError, ValueError):
            # Formato viejo de Frappe: CSV.
            return [p.strip() for p in texto.split(",") if p.strip()]

    if isinstance(datos, dict):
        datos = [datos]

    if not isinstance(datos, (list, tuple)):
        return []

    salida = []
    for u in datos:
        if isinstance(u, dict):
            u = u.get("user") or u.get("name") or ""
        u = str(u or "").strip()
        if u:
            salida.append(u)
    return salida


def usuario_asignado(valor):
    """Primer usuario de `_assign` que **existe** en el sitio (evita el 417 de
    un Link a un usuario inexistente). `None` si no hay ninguno valido."""
    for u in usuarios_asignados(valor):
        if frappe.db.exists("User", u):
            return u
    return None


def sincronizar_fila(doctype, nombre):
    """Siembra el dueno del documento desde `_assign`. Devuelve el usuario
    escrito, o `None` si no habia nada que hacer. Idempotente y sin efectos
    secundarios: usa `db.set_value(update_modified=False)`, que no vuelve a
    disparar hooks (no hay recursion)."""
    campo = CAMPOS_DUENO.get(doctype)
    if not campo or not nombre or _saltar():
        return None
    try:
        valores = frappe.db.get_value(doctype, nombre, ["_assign", campo], as_dict=True) or {}
        actual = (valores.get(campo) or "").strip()
        asignado = usuario_asignado(valores.get("_assign"))

        if not asignado or actual == asignado:
            return None
        if actual and frappe.db.exists("User", actual):
            # Dueno valido puesto a mano: no se pisa (ver docstring, precedencia).
            return None

        frappe.db.set_value(doctype, nombre, campo, asignado, update_modified=False)
        return asignado
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"dueno_desde_assign.{doctype}")
        return None


def sincronizar(doc, method=None):
    """Handler de `doc_events` para `CRM Deal` y `CRM Lead` (evento `on_change`)."""
    return sincronizar_fila(getattr(doc, "doctype", None), getattr(doc, "name", None))
