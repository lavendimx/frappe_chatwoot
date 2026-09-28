# -*- coding: utf-8 -*-
"""«Iniciar sesión como» (login-as de agencia) para Sofía CRM.

Qué hace
--------
Permite al equipo de agencia (allowlist ``AGENCIA_EMAILS`` o rol System Manager)
ver el CRM **exactamente como lo ve** un usuario concreto del sitio — permisos,
módulos por plan, datos filtrados por dueño, todo — y volver a su propia cuenta.

Cómo
----
Reutiliza el mecanismo nativo de Frappe (``LoginManager.login_as`` +
``Session.set_impersonated``), el mismo que usa el Desk para "Login As". Dos
diferencias deliberadas con el del core (``frappe.core.doctype.user.user.impersonate``):

1. El core exige ``Administrator``; aquí se abre al equipo de agencia, que en
   estos sitios no es Administrator sino System Manager (y la allowlist cubre a
   quienes no son System Manager en todos los sitios).
2. El core manda correo + Notification Log al usuario suplantado. Aquí **no**:
   los suplantados son usuarios de clientes; un correo "alguien se hizo pasar por
   ti" por cada revisión de soporte confundiría más de lo que informa. Queda
   rastro completo en ``Activity Log`` (operation="Impersonate").

Seguridad
---------
- Solo agencia: allowlist o System Manager **sin** ``api_key`` (las cuentas de
  servicio del agente/tableros también son System Manager y no deben entrar).
- Objetivos válidos: usuario del sitio, habilitado, con rol de CRM y sin
  ``api_key``. Nunca ``Administrator``.
- No se escala: quien no tenga System Manager no puede suplantar a un System
  Manager.
- ``volver`` no exige ser agencia (en ese momento el usuario de sesión ES el
  suplantado); solo exige que la sesión traiga ``impersonated_by``, que lo puso
  este mismo módulo al entrar.
- Entrar y volver dejan ``Activity Log``.
"""

from __future__ import annotations

import frappe
from frappe import _

AGENCIA_EMAILS = (
	"alejandro.moreno@lavendi.mx",
	"valente.flores@lavendi.mx",
	"contacto@lavendi.mx",
)

ROLES_CRM = ("System Manager", "Sales Manager", "Sales User")

# Orden de prioridad para etiquetar el rol "principal" de cada usuario.
_PRIORIDAD_ROL = ("System Manager", "Sales Manager", "Sales User")


def _original() -> str:
	"""Usuario que abrió la sesión, aunque en este momento esté suplantando."""
	return frappe.session.data.get("impersonated_by") or frappe.session.user


def _es_agencia(user: str) -> bool:
	if not user or user == "Guest":
		return False
	if user == "Administrator":
		return True
	if user in AGENCIA_EMAILS:
		return True
	if "System Manager" in frappe.get_roles(user):
		return not frappe.db.get_value("User", user, "api_key")
	return False


def _rol_principal(user: str) -> str:
	roles = frappe.get_roles(user)
	for rol in _PRIORIDAD_ROL:
		if rol in roles:
			return rol
	return ""


def _candidatos() -> list[dict]:
	"""Usuarios del sitio con acceso al CRM a los que se puede saltar.

	Excluye Administrator, cuentas de servicio (``api_key``), el usuario actual y
	el original de la sesión. No limita por "amigo": la agencia ya ve todo por rol;
	esto solo cambia la *perspectiva*, no el alcance de datos.
	"""
	nombres = frappe.get_all(
		"Has Role",
		filters={"parenttype": "User", "role": ["in", list(ROLES_CRM)]},
		pluck="parent",
		distinct=True,
	)

	original = _original()
	excluir = {"Administrator", "Guest", frappe.session.user, original}
	roles_original = set(frappe.get_roles(original)) if original != "Guest" else set()
	puede_system_manager = original == "Administrator" or "System Manager" in roles_original

	salida = []
	for nombre in sorted(set(nombres)):
		if nombre in excluir:
			continue
		datos = frappe.db.get_value(
			"User",
			nombre,
			["name", "full_name", "email", "user_image", "enabled", "api_key"],
			as_dict=True,
		)
		if not datos or not datos.enabled or datos.api_key:
			continue
		rol = _rol_principal(nombre)
		if not rol:
			continue
		# No escalar: sin System Manager no se puede suplantar a un System Manager.
		if rol == "System Manager" and not puede_system_manager:
			continue
		salida.append(
			{
				"name": datos.name,
				"full_name": datos.full_name or datos.name,
				"email": datos.email or datos.name,
				"user_image": datos.user_image or "",
				"rol": rol,
			}
		)

	salida.sort(key=lambda u: u["full_name"].lower())
	return salida


@frappe.whitelist()
def estado() -> dict:
	"""Estado para el banner y el menú: quién suplanta a quién y a quién se puede saltar."""
	original = frappe.session.data.get("impersonated_by")
	actual = frappe.session.user
	puede = _es_agencia(_original())
	return {
		"puede": puede,
		"impersonado_por": original,
		"usuario_actual": actual,
		"nombre_actual": frappe.db.get_value("User", actual, "full_name") or actual,
		"nombre_original": (frappe.db.get_value("User", original, "full_name") if original else None)
		or original,
		"usuarios": _candidatos() if puede else [],
	}


def _registrar(actor: str, accion: str, detalles: str) -> None:
	frappe.get_doc(
		{
			"doctype": "Activity Log",
			"user": actor,
			"status": "Success",
			"subject": f"[login-as] {accion}: {detalles}",
			"operation": "Impersonate",
		}
	).insert(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
def entrar_como(usuario: str, motivo: str | None = None) -> dict:
	"""Cambia la sesión actual para que sea la de ``usuario`` (login-as)."""
	original = _original()
	if not _es_agencia(original):
		frappe.throw(_("No tienes permiso para «iniciar sesión como»."), frappe.PermissionError)

	usuario = (usuario or "").strip()
	if not usuario or usuario in ("Guest", "Administrator", original):
		frappe.throw(_("Usuario no válido."))
	if not frappe.db.exists("User", usuario):
		frappe.throw(_("El usuario no existe."))

	datos = frappe.db.get_value("User", usuario, ["enabled", "api_key"], as_dict=True)
	if not datos or not datos.enabled or datos.api_key:
		frappe.throw(_("Ese usuario está deshabilitado o es una cuenta de servicio."))

	rol = _rol_principal(usuario)
	if not rol:
		frappe.throw(_("Ese usuario no tiene acceso al CRM."))
	if rol == "System Manager" and original != "Administrator" and "System Manager" not in frappe.get_roles(original):
		frappe.throw(_("No puedes iniciar sesión como un Administrador del sistema."))

	motivo = (motivo or "").strip()[:140]
	_registrar(original, "entrar", f"{original} → {usuario}" + (f" · {motivo}" if motivo else ""))

	# El core hace exactamente esto (auth.py LoginManager.impersonate): cambiar la
	# sesión al objetivo y marcar en la sesión quién la abrió.
	frappe.local.login_manager.login_as(usuario)
	frappe.local.session_obj.set_impersonated(original)

	return {
		"ok": True,
		"usuario": usuario,
		"full_name": frappe.db.get_value("User", usuario, "full_name") or usuario,
	}


@frappe.whitelist(methods=["POST"])
def volver() -> dict:
	"""Restaura la sesión a la cuenta que abrió el login-as."""
	original = frappe.session.data.get("impersonated_by")
	if not original:
		frappe.throw(_("No estás en una sesión de «iniciar sesión como»."))

	actual = frappe.session.user
	if not frappe.db.exists("User", original) or not frappe.db.get_value("User", original, "enabled"):
		frappe.throw(
			_("Tu cuenta original ({0}) ya no está disponible. Cierra sesión y vuelve a entrar.").format(original)
		)

	_registrar(original, "volver", f"{actual} → {original}")

	frappe.local.login_manager.login_as(original)
	frappe.local.session_obj.set_impersonated(None)

	return {"ok": True, "usuario": original}
