# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Pruebas de `fusionar_contactos_multiple` y su heurística.

Todos los contactos de prueba llevan el prefijo `ZZFusionTest` y se borran en
`tearDown` — nunca se reusan datos reales (lección del incidente de
etiquetas, 2026-10-12: nunca validar un flujo destructivo contra un contacto
real). La mayoría de los casos usa contactos SIN teléfono a propósito: sin
teléfono, `_contacto_chatwoot(None)` corta antes de llamar a Chatwoot (ver
`chatwoot_contactos._contacto_chatwoot`), así que esos tests ejercitan la
mecánica de Frappe (pre-copia, reasignación, `rename_doc`) sin necesidad de
mockear nada. Los que sí necesitan el lado Chatwoot mockean
`_contacto_chatwoot` y `merge_contacts` explícitamente.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_chatwoot.frappe_chatwoot.api import panel

PREFIJO = "ZZFusionTest"
MODULO = "frappe_chatwoot.frappe_chatwoot.api.panel"


class _BaseFusionTest(FrappeTestCase):
    def setUp(self):
        self._creados = {"Contact": [], "CRM Deal": [], "Chatwoot Fusion": []}

    def tearDown(self):
        frappe.db.rollback()
        # `fusionar_contactos_multiple`/`_fusionar_en_chatwoot` comitean a
        # propósito (write-ahead log, ver panel.py) — ese rollback NO alcanza
        # a esas filas. Se limpian explícito por cualquier Chatwoot Fusion
        # que mencione a alguno de los Contact creados en el test, no solo
        # los que cada test marcó a mano en self._creados.
        contactos = set(self._creados["Contact"])
        if contactos:
            for campo in ("base_contact", "mergee_contact", "mergee_contact_nombre"):
                for name in frappe.get_all(
                    "Chatwoot Fusion", filters={campo: ["in", list(contactos)]}, pluck="name",
                ):
                    self._creados["Chatwoot Fusion"].append(name)

        for doctype in ("CRM Deal", "Contact", "Chatwoot Fusion"):
            for name in dict.fromkeys(self._creados[doctype]):
                if frappe.db.exists(doctype, name):
                    frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
        frappe.db.commit()

    def _contacto(self, sufijo, *, telefono=None, email=None, crm_organization=None,
                  creation=None, **extra):
        doc = frappe.get_doc({
            "doctype": "Contact",
            "first_name": PREFIJO,
            "last_name": sufijo,
            **extra,
        })
        if telefono:
            doc.append("phone_nos", {"phone": telefono, "is_primary_phone": 1,
                                     "is_primary_mobile_no": 1})
        if email:
            doc.append("email_ids", {"email_id": email, "is_primary": 1})
        if crm_organization:
            doc.crm_organization = crm_organization
        doc.insert(ignore_permissions=True)
        if creation:
            frappe.db.set_value("Contact", doc.name, "creation", creation, update_modified=False)
        self._creados["Contact"].append(doc.name)
        return doc.name

    def _deal(self, contact):
        doc = frappe.get_doc({
            "doctype": "CRM Deal",
            "contact": contact,
        }).insert(ignore_permissions=True)
        self._creados["CRM Deal"].append(doc.name)
        return doc.name


class TestElegirContactoBase(_BaseFusionTest):
    def test_prefiere_mas_datos(self):
        pobre = self._contacto("Pobre")
        rico = self._contacto("Rico", email="rico@example.com", crm_organization=None)
        frappe.db.set_value("Contact", rico, "company_name", "Acme")
        eleccion = panel._elegir_contacto_base([pobre, rico])
        self.assertEqual(eleccion["base"], rico)

    def test_prefiere_mas_deals_sobre_mas_campos(self):
        con_campos = self._contacto("ConCampos", email="x@example.com")
        frappe.db.set_value("Contact", con_campos, "company_name", "Acme")
        con_deals = self._contacto("ConDeals")
        self._deal(con_deals)
        self._deal(con_deals)
        eleccion = panel._elegir_contacto_base([con_campos, con_deals])
        self.assertEqual(eleccion["base"], con_deals)

    def test_empate_gana_el_mas_antiguo(self):
        viejo = self._contacto("Viejo", creation="2020-01-01 00:00:00")
        nuevo = self._contacto("Nuevo", creation="2026-01-01 00:00:00")
        eleccion = panel._elegir_contacto_base([nuevo, viejo])
        self.assertEqual(eleccion["base"], viejo)

    def test_es_determinista_sin_importar_el_orden(self):
        a = self._contacto("A", creation="2020-01-01 00:00:00")
        b = self._contacto("B", creation="2021-01-01 00:00:00")
        c = self._contacto("C", creation="2022-01-01 00:00:00")
        e1 = panel._elegir_contacto_base([a, b, c])
        e2 = panel._elegir_contacto_base([c, a, b])
        self.assertEqual(e1["base"], e2["base"])


class TestPrevisualizarFusion(_BaseFusionTest):
    def test_no_tiene_efectos_secundarios(self):
        a = self._contacto("PrevA")
        b = self._contacto("PrevB")
        contactos_antes = frappe.db.count("Contact")
        fusiones_antes = frappe.db.count("Chatwoot Fusion")
        with patch(f"{MODULO}.cw.merge_contacts") as mock_merge:
            r = panel.previsualizar_fusion_contactos(json.dumps([a, b]))
            mock_merge.assert_not_called()
        self.assertEqual(frappe.db.count("Contact"), contactos_antes)
        self.assertEqual(frappe.db.count("Chatwoot Fusion"), fusiones_antes)
        self.assertIn(r["base"], (a, b))
        self.assertEqual(len(r["candidatos"]), 2)


class TestPrecopiarDatos(_BaseFusionTest):
    def test_telefono_alterno_va_a_phone_nos_sin_tocar_el_escalar(self):
        base = self._contacto("Base")
        mergee = self._contacto("Mergee", telefono="4422196109")
        base_doc = frappe.get_doc("Contact", base)
        mobile_antes = base_doc.mobile_no
        panel._precopiar_datos(base_doc, mergee)
        base_doc.save(ignore_permissions=True)
        base_doc.reload()
        telefonos = {p.phone for p in base_doc.phone_nos}
        self.assertIn("4422196109", telefonos)
        # Regresión del hallazgo #1: la fila copiada es is_primary_mobile_no=0
        # a propósito (nunca pisa un primario del base), así que
        # Contact.validate() NO promueve el escalar — sigue vacío, igual que
        # antes de copiar. El dato vive en phone_nos, no en el escalar.
        self.assertFalse(base_doc.mobile_no)
        self.assertFalse(mobile_antes)

    def test_no_sobrescribe_dato_existente(self):
        base = self._contacto("Base2")
        frappe.db.set_value("Contact", base, "company_name", "Original")
        mergee = self._contacto("Mergee2")
        frappe.db.set_value("Contact", mergee, "company_name", "DelMergee")
        base_doc = frappe.get_doc("Contact", base)
        panel._precopiar_datos(base_doc, mergee)
        self.assertEqual(base_doc.company_name, "Original")

    def test_dedupe_por_telefono_corto(self):
        base = self._contacto("Base3", telefono="+524422196109")
        mergee = self._contacto("Mergee3", telefono="5214422196109")
        base_doc = frappe.get_doc("Contact", base)
        panel._precopiar_datos(base_doc, mergee)
        self.assertEqual(len(base_doc.phone_nos), 1)

    def test_no_copia_google_contacts_id(self):
        base = self._contacto("Base4")
        mergee = self._contacto("Mergee4")
        frappe.db.set_value("Contact", mergee, "google_contacts_id", "abc123")
        base_doc = frappe.get_doc("Contact", base)
        copiado = panel._precopiar_datos(base_doc, mergee)
        self.assertFalse(base_doc.get("google_contacts_id"))
        self.assertNotIn("google_contacts_id", copiado)


class TestFusionMultipleLote(_BaseFusionTest):
    def test_reasigna_deal_sin_disparar_sus_hooks(self):
        base = self._contacto("DealBase")
        mergee = self._contacto("DealMergee")
        deal = self._deal(mergee)
        modified_antes = frappe.db.get_value("CRM Deal", deal, "modified")

        r = panel.fusionar_contactos_multiple(json.dumps([base, mergee]), base)

        self.assertEqual(r["ok"], 1)
        self.assertEqual(frappe.db.get_value("CRM Deal", deal, "contact"), base)
        # Regresión del hallazgo: rename_doc(merge=True) reasigna por SQL
        # directo, sin pasar por .save() del Deal — ningún hook de CRM Deal
        # corre ni se bumpea `modified`.
        self.assertEqual(frappe.db.get_value("CRM Deal", deal, "modified"), modified_antes)

    def test_borra_los_mergees(self):
        base = self._contacto("BorraBase")
        m1 = self._contacto("BorraM1")
        m2 = self._contacto("BorraM2")
        panel.fusionar_contactos_multiple(json.dumps([base, m1, m2]), base)
        self.assertFalse(frappe.db.exists("Contact", m1))
        self.assertFalse(frappe.db.exists("Contact", m2))
        self.assertTrue(frappe.db.exists("Contact", base))

    def test_es_idempotente_en_un_segundo_llamado(self):
        base = self._contacto("IdemBase")
        mergee = self._contacto("IdemMergee")
        panel.fusionar_contactos_multiple(json.dumps([base, mergee]), base)
        r2 = panel.fusionar_contactos_multiple(json.dumps([base, mergee]), base)
        self.assertEqual(r2["resultados"][0]["ya_fusionado"], True)

    def test_un_fallo_no_aborta_el_lote(self):
        base = self._contacto("ParcialBase")
        m1 = self._contacto("ParcialM1")
        m2 = self._contacto("ParcialM2")
        m3 = self._contacto("ParcialM3")

        original = frappe.model.rename_doc.rename_doc

        def falla_solo_m2(*args, **kwargs):
            old = kwargs.get("old") or (args[1] if len(args) > 1 else None)
            if old == m2:
                raise RuntimeError("boom")
            return original(*args, **kwargs)

        with patch("frappe.model.rename_doc.rename_doc", side_effect=falla_solo_m2):
            r = panel.fusionar_contactos_multiple(json.dumps([base, m1, m2, m3]), base)

        por_mergee = {x["mergee"]: x for x in r["resultados"]}
        self.assertTrue(por_mergee[m1]["ok"])
        self.assertFalse(por_mergee[m2]["ok"])
        self.assertTrue(por_mergee[m3]["ok"])
        self.assertFalse(frappe.db.exists("Contact", m1))
        self.assertTrue(frappe.db.exists("Contact", m2))  # no se borró: falló antes
        self.assertFalse(frappe.db.exists("Contact", m3))

    def test_sigue_con_frappe_aunque_no_haya_homologo_en_chatwoot(self):
        base = self._contacto("SinCwBase")
        mergee = self._contacto("SinCwMergee")
        with patch(f"{MODULO}.cw.merge_contacts") as mock_merge:
            r = panel.fusionar_contactos_multiple(json.dumps([base, mergee]), base)
            mock_merge.assert_not_called()
        self.assertTrue(r["resultados"][0]["ok"])
        self.assertTrue(r["resultados"][0]["chatwoot_omitido"])
        self.assertFalse(frappe.db.exists("Contact", mergee))

    def test_fusiona_tambien_en_chatwoot_cuando_hay_homologo(self):
        base = self._contacto("CwBase", telefono="4422196101")
        mergee = self._contacto("CwMergee", telefono="4422196102")

        def fake_contacto_chatwoot(telefono):
            return {"id": 111 if telefono and telefono.endswith("01") else 222}

        with patch(f"{MODULO}._contacto_chatwoot", side_effect=fake_contacto_chatwoot), \
             patch(f"{MODULO}.cw.merge_contacts") as mock_merge:
            r = panel.fusionar_contactos_multiple(json.dumps([base, mergee]), base)

        mock_merge.assert_called_once_with(111, 222)
        self.assertTrue(r["resultados"][0]["ok"])
        self.assertFalse(r["resultados"][0]["chatwoot_omitido"])


class TestAuditoriaYGuardas(_BaseFusionTest):
    def test_conserva_el_nombre_del_mergee_tras_el_rename(self):
        base = self._contacto("AuditBase")
        mergee = self._contacto("AuditMergee")

        # Forzar resolución de Chatwoot aunque los contactos no tengan
        # teléfono, para ejercitar el registro de auditoría con
        # mergee_contact reescrito por el merge nativo.
        with patch(f"{MODULO}.cw.merge_contacts"), \
             patch(f"{MODULO}._resolver_contacto_chatwoot_suave",
                  side_effect=[(1, base), (2, mergee)]):
            panel.fusionar_contactos_multiple(json.dumps([base, mergee]), base)

        fila = frappe.db.get_value(
            "Chatwoot Fusion", {"base_chatwoot_id": 1, "mergee_chatwoot_id": 2},
            ["mergee_contact", "mergee_contact_nombre", "estado", "contact_frappe_merged"],
            as_dict=True,
        )
        self.assertEqual(fila.mergee_contact, base)  # reescrito por rename_doc
        self.assertEqual(fila.mergee_contact_nombre, mergee)  # snapshot intacto
        self.assertEqual(fila.estado, "Completa")
        self.assertEqual(fila.contact_frappe_merged, 1)
        self._creados["Chatwoot Fusion"].append(
            frappe.db.get_value("Chatwoot Fusion", {"base_chatwoot_id": 1, "mergee_chatwoot_id": 2})
        )

    def test_dedupe_crm_contacts_child(self):
        base = self._contacto("DedupeBase")
        mergee = self._contacto("DedupeMergee")
        deal = self._deal(base)
        deal_doc = frappe.get_doc("CRM Deal", deal)
        deal_doc.append("contacts", {"contact": mergee, "is_primary": 0})
        deal_doc.save(ignore_permissions=True)

        panel.fusionar_contactos_multiple(json.dumps([base, mergee]), base)

        filas = frappe.get_all("CRM Contacts", filters={"parent": deal, "contact": base})
        self.assertEqual(len(filas), 1)

    def test_rechaza_menos_de_dos(self):
        a = self._contacto("Solo")
        with self.assertRaises(frappe.ValidationError):
            panel._parse_contactos(json.dumps([a]))

    def test_rechaza_base_fuera_de_la_lista(self):
        a = self._contacto("FueraA")
        b = self._contacto("FueraB")
        otro = self._contacto("FueraOtro")
        with self.assertRaises(frappe.ValidationError):
            panel.fusionar_contactos_multiple(json.dumps([a, b]), otro)

    def test_rechaza_lote_mayor_a_diez(self):
        nombres = [self._contacto(f"Lote{i}") for i in range(11)]
        with self.assertRaises(frappe.ValidationError):
            panel._parse_contactos(json.dumps(nombres))
