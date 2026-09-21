"""Guard: /app (Frappe Desk) redirige a /crm salvo System Manager.

Los 5 usuarios de la plataforma nacen como System User -- cualquiera puede
teclear /app y ver el Desk completo (ERPNext, reportes, todos los doctypes),
y el usuario de un cliente tambien seria System User. Medido en produccion
(sesion 2026-09-17, plan producto-calendario-y-erpnext.md): nadie usa el
Desk mas alla de 3 doctypes que el SPA ya cubre -- no hay backlog de
operaciones que replicar, asi que la via elegida es nunca dejar salir del
SPA en vez de tematizar el Desk (CSS sobre upstream, se revierte con
cualquier bench update -- mismo patron ya sufrido con middlewares.py).

Administrator SIEMPRE exceptuado: es la llave de rescate si este guard
tiene un bug que deje a todos sin poder entrar al Desk, incluido quien
necesita arreglarlo.

SITIOS_ACTIVOS acota el guard: todos los sitios de este bench comparten el
mismo proceso `bench serve`, asi que el codigo se carga para todos a la vez
en cada reinicio -- esta lista es el unico control fino por sitio que queda.
Validado primero en erp-prueba.local (spike 2026-09-21); ampliado el mismo
dia a los sitios reales tras confirmar 302 para Sales Manager/User y 200
para System Manager, sin tocar /api ni el propio /crm. `sofiav2.lavendi.mx`
es symlink a `crm.lavendi.mx` pero corre con su propio X-Frappe-Site-Name
por vhost -- hay que listar ambos nombres de host, no basta uno.
`estrublock.lavendi.mx` (pista B, adaptador GHL) queda fuera a proposito:
es otro producto, sin el mismo supuesto de "todos son System User".
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


class _RedirigirAlCRM(HTTPException):
    code = 302

    def get_response(self, environ=None, scope=None):
        return WerkzeugResponse(status=302, headers={"Location": "/crm"})


def antes_de_la_peticion():
    if frappe.local.site not in SITIOS_ACTIVOS:
        return

    request = getattr(frappe.local, "request", None)
    if not request:
        return

    path = request.path or ""
    if not (path == "/app" or path.startswith("/app/")):
        return

    user = frappe.session.user
    if user in ("Guest", "Administrator"):
        return

    if "System Manager" in frappe.get_roles(user):
        return

    raise _RedirigirAlCRM()
