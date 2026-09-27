"""Guard de entrada: la raiz del dominio y /app nunca sacan al usuario del SPA.

Dos responsabilidades, el mismo reflejo — no dejar al usuario fuera del CRM, que
es el unico producto de estos sitios:

1. **La raiz del dominio** (`/`). Hasta el 2026-09-26 abrir
   `https://<sitio>.lavendi.mx` con sesion iniciada caia en la pagina
   "My Account" (`/me`) de Frappe: es el default de `get_home_page()` cuando el
   sitio no declara `home_page` ni `Role.home_page`, y ninguno de los sitios de
   este bench lo declara. Reportado por Alejandro: el enlace de un cliente
   "lleva a la pagina de mi cuenta y no a la pantalla de conversaciones".
   Ahora `/` con sesion -> `/crm` (el SPA resuelve Conversaciones como Home) y
   sin sesion -> `/login?redirect-to=/crm`. Aplica a cualquier sitio con el CRM
   instalado, no a una lista fija: un cliente nuevo hereda el arreglo sin que
   nadie se acuerde de anadirlo.

2. **`/app` (Frappe Desk)** -> `/crm` salvo System Manager. Los 5 usuarios de
   la plataforma nacen como System User -- cualquiera puede teclear /app y ver
   el Desk completo (ERPNext, reportes, todos los doctypes). Medido en
   produccion (sesion 2026-09-17, plan producto-calendario-y-erpnext.md): nadie
   usa el Desk mas alla de 3 doctypes que el SPA ya cubre -- no hay backlog de
   operaciones que replicar, asi que la via elegida es nunca dejar salir del
   SPA en vez de tematizar el Desk (CSS sobre upstream, se revierte con
   cualquier bench update -- mismo patron ya sufrido con middlewares.py).

Administrator SIEMPRE exceptuado en el guard de /app: es la llave de rescate si
este guard tiene un bug que deje a todos sin poder entrar al Desk, incluido
quien necesita arreglarlo. En la raiz no se exceptua: Administrator tambien
debe aterrizar en el CRM (puede entrar al Desk escribiendo /app).

SITIOS_ACTIVOS acota SOLO el guard de /app: todos los sitios de este bench
comparten el mismo proceso `bench serve`, asi que el codigo se carga para todos
a la vez en cada reinicio -- esta lista es el unico control fino por sitio que
queda. Validado primero en erp-prueba.local (spike 2026-09-21); ampliado el
mismo dia a los sitios reales tras confirmar 302 para Sales Manager/User y 200
para System Manager, sin tocar /api ni el propio /crm. `sofiav2.lavendi.mx`
es symlink a `crm.lavendi.mx` pero corre con su propio X-Frappe-Site-Name
por vhost -- hay que listar ambos nombres de host, no basta uno.
`estrublock.lavendi.mx` (pista B, adaptador GHL) queda fuera de /app a
proposito: es otro producto, sin el mismo supuesto de "todos son System User".
El redirect de la raiz NO usa esta lista: aplica a todo sitio con el CRM
instalado, que es justo el pool de sitios donde la raiz debe ser el CRM.
"""

import frappe
from werkzeug.exceptions import HTTPException
from werkzeug.wrappers import Response as WerkzeugResponse

SITIOS_ACTIVOS = {
    "erp-prueba.local",
    "crm.lavendi.mx",
    "sofiav2.lavendi.mx",
    "sixgardens.lavendi.mx",
}


class _RedirigirA(HTTPException):
    code = 302

    def __init__(self, location):
        super().__init__()
        self.location = location

    def get_response(self, environ=None, scope=None):
        return WerkzeugResponse(status=302, headers={"Location": self.location})


def _tiene_crm():
    """Si ESTE sitio tiene el CRM instalado. Un sitio sin CRM no debe perder su
    raiz, asi que el redirect de `/` solo aplica donde el CRM es el producto."""
    try:
        return "crm" in frappe.get_installed_apps()
    except Exception:
        return False


def antes_de_la_peticion():
    request = getattr(frappe.local, "request", None)
    if not request:
        return

    path = request.path or ""

    # 1) Raiz del dominio -> bandeja con sesion, login sin ella. Nunca a /me.
    if path == "/" and _tiene_crm():
        if frappe.session.user == "Guest":
            raise _RedirigirA("/login?redirect-to=/crm")
        raise _RedirigirA("/crm")

    # 2) /app -> /crm salvo System Manager.
    if frappe.local.site not in SITIOS_ACTIVOS:
        return

    if not (path == "/app" or path.startswith("/app/")):
        return

    user = frappe.session.user
    if user in ("Guest", "Administrator"):
        return

    if "System Manager" in frappe.get_roles(user):
        return

    raise _RedirigirA("/crm")
