"""Test puro de `utils/google_contactos.py`.

No toca Google ni `frappe.db`: stubea `frappe`, el modulo del core
(`get_google_contacts_object`) y `googleapiclient.errors.HttpError` antes de
cargar el archivo por ruta. Cubre:

- `construir_payload`: columnas + child tables -> names/emails/phones/orgs,
  con escalar primero y deduplicado.
- Guards de `encolar`: sin clave de sitio no encola; `in_import` no encola;
  `after_insert` con clave si encola; `on_update` sin cambio no encola y con
  cambio si.

Corre con: python3 utils/test_google_contactos.py
"""

import importlib.util
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "google_contactos.py"

_passes = 0
_fails = 0


def _check(cond, label):
    global _passes, _fails
    if cond:
        _passes += 1
        print(f"  ok  {label}")
    else:
        _fails += 1
        print(f"  FAIL {label}")


class _Conf(dict):
    def get(self, k, default=None):
        return dict.get(self, k, default)


class FakeRow:
    def __init__(self, **kw):
        self._d = kw

    def get(self, k, default=None):
        return self._d.get(k, default)


class FakeDoc:
    def __init__(self, name="C-1", _before=None, **fields):
        self.name = name
        self._fields = fields
        self._before = _before

    def get(self, k, default=None):
        return self._fields.get(k, default)

    def get_doc_before_save(self):
        return self._before


def _install_stubs():
    calls = {"enqueue": [], "log_error": []}

    frappe = types.ModuleType("frappe")
    frappe.flags = types.SimpleNamespace(in_import=False, in_migrate=False, in_patch=False)
    frappe.conf = _Conf()
    frappe.enqueue = lambda *a, **k: calls["enqueue"].append((a, k))
    frappe.log_error = lambda *a, **k: calls["log_error"].append((a, k))
    frappe.db = types.SimpleNamespace(
        get_value=lambda *a, **k: None, set_value=lambda *a, **k: None
    )
    frappe.get_doc = lambda *a, **k: None
    sys.modules["frappe"] = frappe

    integ = types.ModuleType("frappe.integrations")
    doctype = types.ModuleType("frappe.integrations.doctype")
    gcpkg = types.ModuleType("frappe.integrations.doctype.google_contacts")
    gcmod = types.ModuleType("frappe.integrations.doctype.google_contacts.google_contacts")
    gcmod.get_google_contacts_object = lambda name: (None, None)
    sys.modules["frappe.integrations"] = integ
    sys.modules["frappe.integrations.doctype"] = doctype
    sys.modules["frappe.integrations.doctype.google_contacts"] = gcpkg
    sys.modules["frappe.integrations.doctype.google_contacts.google_contacts"] = gcmod
    frappe.integrations = integ
    integ.doctype = doctype
    doctype.google_contacts = gcpkg
    gcpkg.google_contacts = gcmod

    gerr = types.ModuleType("googleapiclient.errors")

    class HttpError(Exception):
        resp = None

    gerr.HttpError = HttpError
    sys.modules["googleapiclient"] = types.ModuleType("googleapiclient")
    sys.modules["googleapiclient.errors"] = gerr

    return frappe, calls


def _load_module():
    spec = importlib.util.spec_from_file_location("_gc_under_test", str(TARGET))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_construir_payload(mod):
    print("construir_payload")
    doc = FakeDoc(
        first_name="Ana",
        middle_name="",
        last_name="Lopez",
        email_id="ana@x.com",
        mobile_no="+5215500000001",
        phone="",
        company_name="Acme",
        designation="Directora",
        email_ids=[FakeRow(email_id="alt@x.com"), FakeRow(email_id="ana@x.com")],
        phone_nos=[FakeRow(phone="+5211100000002"), FakeRow(phone="+5215500000001")],
    )
    p = mod.construir_payload(doc)

    _check(p["names"][0] == {"givenName": "Ana", "middleName": "", "familyName": "Lopez"},
           "names: given/middle/family")
    _check([e["value"] for e in p["emailAddresses"]] == ["ana@x.com", "alt@x.com"],
           "emails: escalar primero + hijas deduplicadas")
    _check([x["value"] for x in p["phoneNumbers"]] == ["+5215500000001", "+5211100000002"],
           "phones: mobile primero + hijas deduplicadas")
    _check(p["organizations"] == [{"name": "Acme", "title": "Directora"}],
           "organizations: company_name/designation")

    vacio = mod.construir_payload(FakeDoc(first_name="Solo"))
    _check("emailAddresses" not in vacio and "phoneNumbers" not in vacio
           and "organizations" not in vacio,
           "vacio: sin emails/phones/orgs si no hay datos")
    _check(vacio["names"][0]["givenName"] == "Solo", "vacio: names conservado")


def test_encolar_guards(mod, frappe, calls):
    print("encolar guards")

    # 1. sin clave de sitio -> no encola
    mod.encolar(FakeDoc("C-1"), "after_insert")
    _check(len(calls["enqueue"]) == 0, "sin google_contacts_account no encola")

    # 2. con clave + after_insert -> encola con el path correcto
    frappe.conf["google_contacts_account"] = "contactos@lavendi.mx"
    mod.encolar(FakeDoc("C-2"), "after_insert")
    _check(len(calls["enqueue"]) == 1, "con cuenta + after_insert encola")
    if calls["enqueue"]:
        ruta = calls["enqueue"][0][0][0]
        kwargs = calls["enqueue"][0][1]
        _check(ruta == "frappe_chatwoot.utils.google_contactos.sincronizar",
               "ruta de enqueue correcta")
        _check(kwargs.get("contact") == "C-2" and kwargs.get("enqueue_after_commit") is True,
               "kwargs: contact + enqueue_after_commit")

    # 3. in_import -> no encola
    frappe.flags.in_import = True
    mod.encolar(FakeDoc("C-3"), "after_insert")
    _check(len(calls["enqueue"]) == 1, "in_import no encola")
    frappe.flags.in_import = False

    # 4. on_update sin cambio relevante -> no encola
    sin_cambio = FakeDoc("C-4", first_name="Ana", _before=FakeDoc("C-4", first_name="Ana"))
    mod.encolar(sin_cambio, "on_update")
    _check(len(calls["enqueue"]) == 1, "on_update sin cambio no encola")

    # 5. on_update con cambio relevante -> si encola
    con_cambio = FakeDoc("C-5", first_name="Beatriz", _before=FakeDoc("C-5", first_name="Ana"))
    mod.encolar(con_cambio, "on_update")
    _check(len(calls["enqueue"]) == 2, "on_update con cambio si encola")

    # 6. on_update cambiando solo una hija (email_ids) -> si encola
    antes = FakeDoc("C-6", first_name="Ana", email_ids=[FakeRow(email_id="a@x.com")])
    ahora = FakeDoc("C-6", first_name="Ana",
                    email_ids=[FakeRow(email_id="a@x.com"), FakeRow(email_id="b@x.com")],
                    _before=antes)
    mod.encolar(ahora, "on_update")
    _check(len(calls["enqueue"]) == 3, "on_update con hija nueva si encola")


def main():
    frappe, calls = _install_stubs()
    mod = _load_module()
    test_construir_payload(mod)
    test_encolar_guards(mod, frappe, calls)
    print(f"\n{_passes}/{_passes + _fails} ok")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
