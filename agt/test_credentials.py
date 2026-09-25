"""Credenciais da AGT no painel: chaves RSA (PEM/PFX) cifradas, senha da API, assinatura JWS."""

import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase

from agt.jws import b64url_decode, sign_rs256, verify_rs256
from agt.keys import KeyFileError, load_private_key
from agt.models import AgtConfiguration
from audit.models import AuditEvent
from companies.models import Company, Membership

KEY_2048 = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key=KEY_2048, password=None):
    enc = serialization.BestAvailableEncryption(password.encode()) if password else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)


def pfx(key=KEY_2048, password="senha-pfx"):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    import datetime

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Teste")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    return pkcs12.serialize_key_and_certificates(b"t", key, cert, None,
                                                 serialization.BestAvailableEncryption(password.encode()))


class KeyLoadingTests(SimpleTestCase):
    def test_pem_plain_and_protected(self):
        loaded = load_private_key(pem())
        self.assertEqual(loaded.bits, 2048)
        self.assertIn("RSA 2048 bits", loaded.info)
        protected = load_private_key(pem(password="abc123"), "abc123")
        self.assertEqual(protected.fingerprint, loaded.fingerprint)  # a mesma chave
        self.assertIn("BEGIN PRIVATE KEY", protected.pem)  # guardada sem senha (depois cifrada)

    def test_pfx(self):
        self.assertEqual(load_private_key(pfx(), "senha-pfx").bits, 2048)

    def test_rejections(self):
        cases = {
            "senha em falta": (pem(password="abc123"), ""),
            "senha errada": (pem(password="abc123"), "errada"),
            "pfx senha errada": (pfx(), "errada"),
            "lixo": (b"isto nao e uma chave", ""),
            "vazio": (b"", ""),
            "grande demais": (b"-----BEGIN" + b"A" * 70000, ""),
            "rsa 1024": (pem(rsa.generate_private_key(public_exponent=65537, key_size=1024)), ""),
            "não rsa": (pem(ec.generate_private_key(ec.SECP256R1())), ""),
        }
        for label, (data, password) in cases.items():
            with self.subTest(label), self.assertRaises(KeyFileError):
                load_private_key(data, password)


class JwsTests(SimpleTestCase):
    def test_sign_and_verify_like_agt_example(self):
        payload = {"taxRegistrationNumber": "5000000000", "requestID": "202500000000118"}
        token = sign_rs256(payload, KEY_2048)
        header = json.loads(b64url_decode(token.split(".")[0]))
        self.assertEqual(header, {"typ": "JOSE", "alg": "RS256"})  # igual ao cabeçalho dos exemplos oficiais
        self.assertNotIn("=", token)  # Base64URL sem preenchimento
        self.assertEqual(verify_rs256(token, KEY_2048.public_key()), payload)

    def test_tampering_detected(self):
        token = sign_rs256({"documentNo": "FT A/1"}, KEY_2048)
        header, _payload, signature = token.split(".")
        forged = ".".join([header, token.split(".")[1][:-2] + "AA", signature])
        with self.assertRaises(ValueError):
            verify_rs256(forged, KEY_2048.public_key())
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(ValueError):
            verify_rs256(token, other.public_key())


class CredentialsPanelTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Hotel", nif="5000000000")
        self.admin = get_user_model().objects.create_user("admin", password="x")
        Membership.objects.create(user=self.admin, company=self.company, role="ADMIN")
        self.client.force_login(self.admin)

    def post(self, **extra):
        data = {"environment": "TEST", "base_url": "", "api_username": "produtor-x", "software_name": "HOST",
                "software_version": "10.2", "software_certificate_number": "C_999", "signature_version": "1",
                "software_signature": "", "timeout_seconds": 30, "max_attempts": 5, "retry_delay_seconds": 300}
        data.update(extra)
        return self.client.post("/agt/", data)

    def test_upload_key_and_password_encrypted_never_shown(self):
        response = self.post(api_password="Senha-API-99",
                             issuer_key_file=SimpleUploadedFile("contribuinte.pem", pem(password="abc123")),
                             issuer_key_password="abc123")
        self.assertRedirects(response, "/agt/", fetch_redirect_response=False)
        config = AgtConfiguration.objects.get(company=self.company)
        self.assertEqual(config.api_password(), "Senha-API-99")
        self.assertNotIn("Senha-API-99", config.api_password_encrypted)
        self.assertNotIn("PRIVATE KEY", config.issuer_key_encrypted)
        self.assertIn("RSA 2048 bits", config.issuer_key_info)
        key = config.private_key("issuer")
        self.assertEqual(verify_rs256(sign_rs256({"a": 1}, key), KEY_2048.public_key()), {"a": 1})
        page = self.client.get("/agt/").content.decode()
        self.assertNotIn("Senha-API-99", page)
        self.assertNotIn("abc123", page)
        self.assertNotIn("BEGIN PRIVATE KEY", page)
        self.assertIn("guardada cifrada", page)
        event = AuditEvent.objects.filter(action="AGT_CONFIG_UPDATED").latest("id")
        dumped = json.dumps(event.details)
        self.assertNotIn("Senha-API-99", dumped)
        self.assertIn("issuer_key_file", event.details["protected_fields_changed"])

    def test_keep_password_when_empty_and_clear_key(self):
        self.post(api_password="Senha-API-99", issuer_key_file=SimpleUploadedFile("k.pem", pem()))
        self.post()  # sem senha nem ficheiro: mantém
        config = AgtConfiguration.objects.get(company=self.company)
        self.assertEqual(config.api_password(), "Senha-API-99")
        self.assertTrue(config.issuer_key_encrypted)
        self.post(clear_issuer_key="on")
        config.refresh_from_db()
        self.assertEqual((config.issuer_key_encrypted, config.issuer_key_info), ("", ""))

    def test_invalid_key_shows_error_and_saves_nothing(self):
        response = self.post(api_password="Senha-API-99",
                             issuer_key_file=SimpleUploadedFile("k.pem", pem(password="abc123")),
                             issuer_key_password="errada")
        self.assertContains(response, "senha errada")
        config = AgtConfiguration.objects.filter(company=self.company).first()
        self.assertTrue(config is None or not config.issuer_key_encrypted)

    def test_pfx_producer_key_and_signature_format(self):
        self.post(producer_key_file=SimpleUploadedFile("produtor.pfx", pfx()), producer_key_password="senha-pfx")
        self.assertIn("RSA 2048", AgtConfiguration.objects.get(company=self.company).producer_key_info)
        response = self.post(software_signature="nao-e-jws")
        self.assertContains(response, "Não parece uma assinatura JWS")

    def test_status_complete_when_all_present(self):
        self.post(api_password="Senha-API-99", issuer_key_file=SimpleUploadedFile("k.pem", pem()),
                  software_signature="aaa.bbb.ccc")
        config = AgtConfiguration.objects.get(company=self.company)
        self.assertEqual(config.missing_for_real_sending(), [])
        self.assertTrue(all(row["ok"] for row in config.credentials_status()))
