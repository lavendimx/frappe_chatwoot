"""Cada `Contact` de Sofía -> contacto de Google (People API).

Frappe core ya trae el doctype `Google Contacts` y el cableado
`Contact.after_insert/on_update -> insert/update_contacts_to_google_contacts`
(`apps/frappe/frappe/hooks.py`), pero ese camino **solo dispara si el Contact
trae `sync_with_google_contacts=1`** y corre de forma **síncrona**: si la cuenta
de Google no esta autorizada o la red falla, el guardado del Contact se rompe.

Por eso este modulo va por un worker propio:

- `encolar` es el doc_event (barato, fail-open): nunca toca la red.
- `sincronizar` corre en la cola `short` como Administrator, llama directo a la
  People API y **jamas propaga** un error. El flag `sync_with_google_contacts`
  del doc REAL no se pone nunca, asi el hook del core queda dormido.
- Se llama la People API directo (via `get_google_contacts_object`) y no
  `insert/update_contacts_to_google_contacts` para poder incluir
  `organizations` (company_name/designation), que el core no mapea, y para no
  tener que fingir `sync_with_google_contacts=1` ni child tables transitorias.

Fail-open por sitio: si `site_config.json` no declara `google_contacts_account`,
`encolar` sale sin hacer nada y ningun sitio existente cambia de comportamiento.
"""

import frappe

from frappe.integrations.doctype.google_contacts.google_contacts import (
	get_google_contacts_object,
)
from googleapiclient.errors import HttpError

# Campos escalares cuyo cambio amerita re-sincronizar. Los mismos que mapea el
# payload; las child tables `email_ids`/`phone_nos` se comparan aparte.
CAMPOS_RELEVANTES = (
	"first_name",
	"middle_name",
	"last_name",
	"email_id",
	"mobile_no",
	"phone",
	"company_name",
	"designation",
)

PERSON_FIELDS = "names,emailAddresses,organizations,phoneNumbers"


def _s(valor):
	return (valor or "").strip()


def _cuenta_configurada():
	return bool(_s(frappe.conf.get("google_contacts_account")))


def _resolver_cuenta():
	"""`google_contacts_account` es un name (`GC-...`) o un email. El doctype
	`Google Contacts` autonombra `GC-{email_id}` (ver google_contacts.json)."""
	valor = _s(frappe.conf.get("google_contacts_account"))
	if not valor:
		return None
	if valor.startswith("GC-"):
		return valor
	return "GC-" + valor


def _firma_hijas(doc):
	emails = sorted(
		e for e in (_s(r.get("email_id")) for r in (doc.get("email_ids") or [])) if e
	)
	phones = sorted(
		p for p in (_s(r.get("phone")) for r in (doc.get("phone_nos") or [])) if p
	)
	return (tuple(emails), tuple(phones))


def _cambio_relevante(doc):
	"""True si algun campo mapeado cambio. Ante cualquier duda responde True
	(fail-open hacia sincronizar; el worker es inocuo y no-op sin cuenta)."""
	try:
		anterior = doc.get_doc_before_save()
	except Exception:
		anterior = None

	if anterior is not None:
		for campo in CAMPOS_RELEVANTES:
			if _s(doc.get(campo)) != _s(anterior.get(campo)):
				return True
		try:
			return _firma_hijas(doc) != _firma_hijas(anterior)
		except Exception:
			return True

	# Sin `_doc_before_save` (raro fuera de on_update): comparar contra la DB.
	try:
		actual = frappe.db.get_value("Contact", doc.name, list(CAMPOS_RELEVANTES), as_dict=True)
	except Exception:
		return True
	if not actual:
		return True
	for campo in CAMPOS_RELEVANTES:
		if _s(doc.get(campo)) != _s(actual.get(campo)):
			return True
	# Hijas: la comparacion ligera no esta disponible -> conservador.
	return True


def encolar(doc, method=None):
	"""Doc_event `Contact.after_insert` / `Contact.on_update`. Barato y
	fail-open: no toca la red ni el doc; solo encola el worker."""
	if (
		getattr(frappe.flags, "in_import", None)
		or getattr(frappe.flags, "in_migrate", None)
		or getattr(frappe.flags, "in_patch", None)
	):
		return
	if not _cuenta_configurada():
		return
	if method == "on_update" and not _cambio_relevante(doc):
		return

	frappe.enqueue(
		"frappe_chatwoot.utils.google_contactos.sincronizar",
		queue="short",
		timeout=120,
		enqueue_after_commit=True,
		contact=doc.name,
	)


def construir_payload(doc):
	"""Mapea columnas + child tables del Contact al cuerpo de la People API.

	Puro: solo lee del `doc` (no toca frappe.db ni la red), para poder probarlo
	sin Google. Escalar primero (`email_id`/`mobile_no`/`phone`), luego hijas
	(`email_ids`/`phone_nos`), deduplicado."""
	names = {
		"givenName": _s(doc.get("first_name")),
		"middleName": _s(doc.get("middle_name")),
		"familyName": _s(doc.get("last_name")),
	}
	body = {"names": [names]}

	emails = []
	vistos = set()
	escalar = _s(doc.get("email_id"))
	if escalar:
		emails.append({"value": escalar})
		vistos.add(escalar.lower())
	for hija in doc.get("email_ids") or []:
		valor = _s(hija.get("email_id"))
		if valor and valor.lower() not in vistos:
			vistos.add(valor.lower())
			emails.append({"value": valor})
	if emails:
		body["emailAddresses"] = emails

	phones = []
	vistos = set()
	for campo in ("mobile_no", "phone"):
		valor = _s(doc.get(campo))
		if valor and valor not in vistos:
			vistos.add(valor)
			phones.append({"value": valor})
	for hija in doc.get("phone_nos") or []:
		valor = _s(hija.get("phone"))
		if valor and valor not in vistos:
			vistos.add(valor)
			phones.append({"value": valor})
	if phones:
		body["phoneNumbers"] = phones

	organizacion = {}
	if _s(doc.get("company_name")):
		organizacion["name"] = _s(doc.get("company_name"))
	if _s(doc.get("designation")):
		organizacion["title"] = _s(doc.get("designation"))
	if organizacion:
		body["organizations"] = [organizacion]

	return body


def _cuenta_operable(registro):
	if not registro.get("enable") or not registro.get("push_to_google_contacts"):
		return False
	try:
		return bool(registro.get_password("refresh_token", raise_exception=False))
	except Exception:
		return bool(registro.get("refresh_token"))


def _actualizar(servicio, doc, payload):
	resource = doc.get("google_contacts_id")
	try:
		actual = (
			servicio.people()
			.get(resourceName=resource, personFields=PERSON_FIELDS)
			.execute()
		)
		etag = actual.get("etag")
	except HttpError:
		# Si ya no existe en Google, igualmente intentamos el update; si vuelve
		# a fallar, el except de `sincronizar` lo registra.
		etag = None

	cuerpo = dict(payload)
	if etag:
		cuerpo["etag"] = etag
	servicio.people().updateContact(
		resourceName=resource,
		body=cuerpo,
		updatePersonFields=PERSON_FIELDS,
	).execute()


def sincronizar(contact):
	"""Worker. Carga el Contact, resuelve la cuenta por site_config y empuja a
	Google. Nunca propaga: todo error va a `frappe.log_error`."""
	try:
		doc = frappe.get_doc("Contact", contact)
	except Exception as err:
		frappe.log_error(f"google_contacts: no se pudo cargar Contact {contact}: {err}"[:1000],
		                 "Google Contactos")
		return {"ok": False, "motivo": "contacto inexistente"}

	cuenta = _resolver_cuenta()
	if not cuenta:
		return {"ok": False, "motivo": "sin cuenta configurada"}

	try:
		registro = frappe.get_doc("Google Contacts", cuenta)
	except Exception as err:
		frappe.log_error(f"google_contacts: no existe la cuenta {cuenta}: {err}"[:1000],
		                 "Google Contactos")
		return {"ok": False, "motivo": "cuenta inexistente"}

	if not _cuenta_operable(registro):
		frappe.log_error(
			f"google_contacts no-op: {cuenta} sin enable/push_to_google_contacts/refresh_token",
			"Google Contactos",
		)
		return {"ok": False, "motivo": "cuenta no habilitada"}

	payload = construir_payload(doc)

	try:
		servicio, _account = get_google_contacts_object(cuenta)
		if _s(doc.get("google_contacts_id")):
			_actualizar(servicio, doc, payload)
		else:
			creado = servicio.people().createContact(body=payload).execute()
			resource = (creado or {}).get("resourceName")
			if resource:
				# update_modified=False: escribir el id no debe verse como una
				# edicion del Contact (evita re-encolar en bucle).
				frappe.db.set_value(
					"Contact", doc.name, "google_contacts_id", resource, update_modified=False
				)
		return {"ok": True, "contacto": doc.name}
	except HttpError as err:
		resp = getattr(err, "resp", None)
		status = getattr(resp, "status", None)
		frappe.log_error(f"google_contacts {doc.name}: HttpError {status} {err}"[:1000],
		                 "Google Contactos")
		return {"ok": False, "motivo": "http_error"}
	except Exception as err:
		frappe.log_error(f"google_contacts {doc.name}: {err}"[:1000], "Google Contactos")
		return {"ok": False, "motivo": "error"}
