"""Deteção de servidores e ligação assistida (sem depender do que está instalado)."""

import ipaddress
import socket
import threading
from unittest import mock

from django.test import SimpleTestCase

from host_connector import discovery
from host_connector.config import HostConnectionConfig
from host_connector.discovery import (DetectedServer, DiscoveryError, DiscoveryResult, classify_service,
                                      discover_local, discover_network, networks_for, parse_browser_response,
                                      validate_network)
from host_connector.provisioning import (ProvisioningError, admin_config, generate_password, provisioning_plan)

SC_OUTPUT = """
SERVICE_NAME: MSSQLSERVER
DISPLAY_NAME: SQL Server (MSSQLSERVER)
        TYPE               : 10  WIN32_OWN_PROCESS
        STATE              : 4  RUNNING
SERVICE_NAME: MSSQL$SQLEXPRESS
DISPLAY_NAME: SQL Server (SQLEXPRESS)
        STATE              : 1  STOPPED
SERVICE_NAME: postgresql-x64-16
DISPLAY_NAME: postgresql-x64-16 - PostgreSQL Server 16
        STATE              : 4  RUNNING
SERVICE_NAME: MySQL80
DISPLAY_NAME: MySQL80
        STATE              : 4  RUNNING
SERVICE_NAME: Spooler
DISPLAY_NAME: Print Spooler
        STATE              : 4  RUNNING
"""


def browser_packet(text: str) -> bytes:
    body = text.encode("latin-1")
    return bytes([0x05]) + len(body).to_bytes(2, "little") + body


class ClassifyTests(SimpleTestCase):
    def test_service_names(self):
        self.assertEqual(classify_service("MSSQLSERVER"), ("mssql", ""))
        self.assertEqual(classify_service("MSSQL$HOTEL"), ("mssql", "HOTEL"))
        self.assertEqual(classify_service("postgresql-x64-16"), ("postgresql", ""))
        self.assertEqual(classify_service("MySQL80"), ("mysql", ""))
        self.assertEqual(classify_service("MariaDB"), ("mariadb", ""))
        self.assertEqual(classify_service("OracleServiceORCL"), ("oracle", "ORCL"))
        self.assertEqual(classify_service("FirebirdServerDefaultInstance"), ("firebird", ""))
        self.assertIsNone(classify_service("Spooler"))
        self.assertIsNone(classify_service("SQLBrowser"))  # não é um servidor de dados

    def test_server_name_and_merge_key(self):
        default = DetectedServer("mssql", "localhost", instance="MSSQLSERVER")
        named = DetectedServer("mssql", "127.0.0.1", instance="SQLEXPRESS")
        self.assertEqual(default.server_name, "localhost")
        self.assertEqual(named.server_name, "127.0.0.1\\SQLEXPRESS")
        self.assertEqual(DetectedServer("mssql", "localhost", port=1433).key, default.key)
        self.assertEqual(DetectedServer("mariadb", "localhost", port=3306).key,
                         DetectedServer("mysql", "127.0.0.1", port=3306).key)


class BrowserTests(SimpleTestCase):
    def test_parse_two_instances(self):
        data = browser_packet("ServerName;SRV;InstanceName;MSSQLSERVER;IsClustered;No;Version;16.0.1000.6;tcp;1433;;"
                              "ServerName;SRV;InstanceName;HOTEL;IsClustered;No;Version;15.0.2000.5;np;\\\\SRV\\pipe;;")
        servers = parse_browser_response(data, "192.168.1.10")
        self.assertEqual([(s.instance, s.port, s.version) for s in servers],
                         [("MSSQLSERVER", 1433, "16.0.1000.6"), ("HOTEL", None, "15.0.2000.5")])
        self.assertFalse(servers[1].tcp_enabled)

    def test_garbage_is_ignored(self):
        for data in (b"", b"\x05", b"\x04abcdef", browser_packet("lixo;sem;instancia"), b"\x05\xff\xff\xff\xfe"):
            self.assertEqual(parse_browser_response(data, "1.2.3.4"), [])

    def test_real_udp_roundtrip_with_fake_browser(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        real_sendto = socket.socket.sendto  # o servidor falso responde sem passar pelo desvio

        def reply():
            data, address = server.recvfrom(10)
            if data == b"\x02":
                real_sendto(server, browser_packet("ServerName;X;InstanceName;TESTE;Version;16.0;tcp;14330;;"),
                            address)

        thread = threading.Thread(target=reply)
        thread.start()

        with mock.patch.object(socket.socket, "sendto",
                               lambda self, data, addr: real_sendto(self, data, ("127.0.0.1", port))):
            found = discovery.sqlserver_browser("127.0.0.1", timeout=2)
        thread.join(3)
        server.close()
        self.assertEqual([(s.instance, s.port) for s in found], [("TESTE", 14330)])


class LocalDiscoveryTests(SimpleTestCase):
    def test_services_registry_and_ports_are_merged(self):
        registry = [{"instance": "MSSQLSERVER", "internal": "MSSQL16.MSSQLSERVER", "port": 1433,
                     "tcp_enabled": False, "version": "16.0.1000.6"},
                    {"instance": "SQLEXPRESS", "internal": "MSSQL16.SQLEXPRESS", "port": 51000,
                     "tcp_enabled": True, "version": "16.0.1000.6"}]
        open_ports = {5432, 3306}
        with mock.patch.object(discovery.sys, "platform", "win32"), \
                mock.patch.object(discovery, "_run", return_value=SC_OUTPUT), \
                mock.patch.object(discovery, "sqlserver_registry_instances", return_value=registry), \
                mock.patch.object(discovery, "port_open", side_effect=lambda h, p, t=0: p in open_ports), \
                mock.patch.object(discovery, "sqlserver_browser", return_value=[]):
            result = discover_local()
        by_name = {(s.engine, s.instance): s for s in result.servers}
        self.assertEqual(len(result.servers), 4)  # 2 SQL Server + PostgreSQL + MySQL, sem duplicados
        default = by_name[("mssql", "")]
        self.assertEqual((default.running, default.port, default.tcp_enabled), (True, 1433, False))
        express = by_name[("mssql", "SQLEXPRESS")]
        self.assertEqual((express.running, express.port, express.server_name), (False, 51000, "localhost\\SQLEXPRESS"))
        self.assertIn("porta 5432 aberta", by_name[("postgresql", "")].evidence)
        self.assertNotIn(("print", ""), by_name)

    def test_port_only_detection_and_non_windows(self):
        with mock.patch.object(discovery.sys, "platform", "linux"), \
                mock.patch.object(discovery, "sqlserver_registry_instances", return_value=[]), \
                mock.patch.object(discovery, "port_open", side_effect=lambda h, p, t=0: p == 1433), \
                mock.patch.object(discovery, "sqlserver_browser", return_value=[]):
            result = discover_local()
        self.assertEqual([(s.engine, s.port) for s in result.servers], [("mssql", 1433)])

    def test_port_open_real_socket(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        self.assertTrue(discovery.port_open("127.0.0.1", port))
        listener.close()
        self.assertFalse(discovery.port_open("127.0.0.1", port, timeout=0.2))


class NetworkDiscoveryTests(SimpleTestCase):
    def test_only_private_small_networks(self):
        self.assertEqual(str(validate_network("192.168.1.77/24")), "192.168.1.0/24")
        for bad in ("8.8.8.0/24", "10.0.0.0/8", "192.168.0.0/16", "lixo", "172.16.0.0/20"):
            with self.subTest(bad=bad), self.assertRaises(DiscoveryError):
                validate_network(bad)

    def test_networks_for_addresses(self):
        nets = networks_for(["192.168.18.5", "10.2.0.2", "127.0.0.1", "169.254.1.1", "41.63.1.1", "lixo"])
        self.assertEqual([str(n) for n in nets], ["192.168.18.0/24", "10.2.0.0/24"])

    def test_scan_finds_open_ports_and_skips_own_address(self):
        net = ipaddress.IPv4Network("192.168.50.0/29")
        probed = []

        def fake_open(host, port, timeout=0):
            probed.append(host)
            return (host, port) in {("192.168.50.3", 1433), ("192.168.50.3", 3306), ("192.168.50.6", 5432)}

        with mock.patch.object(discovery, "port_open", side_effect=fake_open), \
                mock.patch.object(discovery, "sqlserver_browser", return_value=[]):
            result = discover_network([net], own_addresses=["192.168.50.1"])
        self.assertEqual(result.scanned_hosts, 5)
        self.assertNotIn("192.168.50.1", probed)
        self.assertEqual(sorted((s.host, s.engine) for s in result.servers),
                         [("192.168.50.3", "mssql"), ("192.168.50.3", "mysql"), ("192.168.50.6", "postgresql")])

    def test_scan_refuses_public_network(self):
        with self.assertRaises(DiscoveryError):
            discover_network([ipaddress.IPv4Network("41.63.0.0/24")])


class ProvisioningPlanTests(SimpleTestCase):
    def cfg(self, engine):
        return HostConnectionConfig.from_values(engine=engine, host="h", name="d", user="admin", password="pw")

    def test_password_generator(self):
        passwords = {generate_password() for _ in range(50)}
        self.assertEqual(len(passwords), 50)
        for p in passwords:
            self.assertRegex(p, r"^[A-Za-z0-9]{28}$")
            self.assertTrue(any(c.isupper() for c in p) and any(c.islower() for c in p) and any(c.isdigit() for c in p))

    def test_mssql_plan_only_read_role_and_hidden_password(self):
        plan = provisioning_plan(self.cfg("mssql"), "HOST DB", known_databases=["HOST DB"])
        sql = " ".join(plan.statements)
        self.assertIn("[HOST DB]", sql)
        self.assertIn("db_datareader", sql)
        for forbidden in ("db_datawriter", "db_owner", "sysadmin", "GRANT INSERT", "GRANT UPDATE", "GRANT DELETE"):
            self.assertNotIn(forbidden, sql)
        self.assertIn(plan.password, sql)
        self.assertNotIn(plan.password, plan.display())

    def test_postgres_and_mysql_plans(self):
        pg = provisioning_plan(self.cfg("postgresql"), "erp", known_databases=["erp"])
        self.assertIn("pg_read_all_data", " ".join(pg.statements))
        my = provisioning_plan(self.cfg("mysql"), "loja", known_databases=["loja"], login_host="%")
        sql = " ".join(my.statements)
        self.assertIn("GRANT SELECT ON `loja`.* TO 'gateway_leitura'@'%'", sql)
        self.assertNotIn("ALL PRIVILEGES", sql)

    def test_injection_attempts_rejected(self):
        cfg = self.cfg("mssql")
        with self.assertRaises(ProvisioningError):
            provisioning_plan(cfg, "x]; DROP DATABASE y; --", known_databases=["outra"])
        with self.assertRaises(ProvisioningError):
            provisioning_plan(cfg, "d", username="a'; DROP", known_databases=["d"])
        with self.assertRaises(ProvisioningError):
            provisioning_plan(cfg, "d", password="abc' OR 1=1", known_databases=["d"])
        with self.assertRaises(ProvisioningError):
            provisioning_plan(self.cfg("mysql"), "d", login_host="x' OR", known_databases=["d"])
        # Um nome real com ] é citado com escape, nunca fecha o identificador.
        plan = provisioning_plan(cfg, "Base]X", known_databases=["Base]X"])
        self.assertIn("[Base]]X]", " ".join(plan.statements))

    def test_unsupported_engine(self):
        with self.assertRaisesRegex(ProvisioningError, "guia"):
            provisioning_plan(HostConnectionConfig.from_values(engine="oracle", host="h", name="s", user="u"),
                              "s", known_databases=["s"])

    def test_admin_config_windows_auth(self):
        cfg = admin_config("mssql", "localhost")
        self.assertTrue(cfg.uses_windows_auth)
        self.assertEqual(cfg.name, "master")
        self.assertTrue(cfg.trust_server_certificate)
        self.assertEqual(admin_config("postgresql", "h", user="postgres", password="x").name, "postgres")
