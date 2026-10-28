# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Quién puede ver y usar la sección Agente IA del SPA (2026-10-28, pedido de Alejandro).

La sección incluye la configuración del agente y su pestaña "Base de conocimiento".
Solo entran:

- la agencia / el administrador del sitio (``Administrator`` y rol ``System Manager``), y
- quien tenga el rol ``Administrador Agente IA``, que se asigna a mano por cliente.

Por qué un rol nuevo y no ``Sales Manager``: en Estrublock los administradores reales de la
cuenta (Irene Martínez y Félix Orozco) son Sales Manager, pero el sitio tiene otros tres
Managers (Spencer, Natalia, Ruth) que NO deben ver la sección. Gatear con ``Sales Manager``
sobraría; gatear solo con ``System Manager`` se quedaría corto. El rol nuevo se asigna por
usuario y por default no lo tiene nadie, así que en el resto de los sitios el resultado es
exactamente el pedido: agencia + System Manager.

Este es el candado REAL (backend). El frontend solo oculta el ítem del menú y la ruta con la
bandera ``window.crm_puede_agente_ia`` que produce ``crm/www/crm.py`` con esta misma función.
"""

import frappe
from frappe import _

ROL = "Administrador Agente IA"


def puede_agente_ia(user=None) -> bool:
	"""True si el usuario puede entrar a la sección Agente IA de este sitio."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	roles = frappe.get_roles(user)
	return "System Manager" in roles or ROL in roles


def exigir_agente_ia() -> None:
	"""Llamar al inicio de cualquier endpoint de la sección Agente IA."""
	if not puede_agente_ia():
		frappe.throw(
			_("Solo los administradores pueden acceder a la configuración del Agente IA."),
			frappe.PermissionError,
		)
