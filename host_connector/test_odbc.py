"""Escolha automática do driver ODBC do SQL Server."""

from unittest import mock

from django.test import SimpleTestCase

from host_connector.config import (MISSING_DRIVER_MESSAGE, HostConfigError, HostConnectionConfig,
                                   best_odbc_driver)

PATCH = "host_connector.config.installed_odbc_drivers"


def mssql(**kw):
    return HostConnectionConfig.from_values(engine="mssql", host="srv", name="db", user="u", password="p", **kw)


class OdbcDriverTests(SimpleTestCase):
    def test_prefers_18_then_17(self):
        with mock.patch(PATCH, return_value=["SQL Server", "ODBC Driver 17 for SQL Server",
                                             "ODBC Driver 18 for SQL Server"]):
            self.assertEqual(best_odbc_driver(), "ODBC Driver 18 for SQL Server")
        with mock.patch(PATCH, return_value=["SQL Server", "ODBC Driver 17 for SQL Server"]):
            self.assertEqual(best_odbc_driver(), "ODBC Driver 17 for SQL Server")
            self.assertEqual(mssql().sqlalchemy_url().query["driver"], "ODBC Driver 17 for SQL Server")

    def test_only_legacy_driver_gives_clear_message(self):
        for drivers in (["SQL Server"], ["SQL Server", "SQL Server Native Client 11.0"], ["MySQL ODBC 8.0"]):
            with self.subTest(drivers=drivers), mock.patch(PATCH, return_value=drivers):
                with self.assertRaisesRegex(HostConfigError, "ODBC Driver 18"):
                    mssql().sqlalchemy_url()
                self.assertEqual(mssql().describe()["location"], MISSING_DRIVER_MESSAGE)

    def test_unknown_driver_list_assumes_18(self):
        with mock.patch(PATCH, return_value=[]):
            self.assertEqual(mssql().sqlalchemy_url().query["driver"], "ODBC Driver 18 for SQL Server")

    def test_explicit_driver_is_respected(self):
        with mock.patch(PATCH, return_value=["SQL Server"]):
            url = mssql(odbc_driver="ODBC Driver 17 for SQL Server").sqlalchemy_url()
        self.assertEqual(url.query["driver"], "ODBC Driver 17 for SQL Server")

    def test_env_connection_without_driver_is_automatic(self):
        env = {"HOST_DB_ENGINE": "mssql", "HOST_DB_HOST": "srv", "HOST_DB_NAME": "db"}
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch(PATCH, return_value=["ODBC Driver 17 for SQL Server"]):
            config = HostConnectionConfig.from_env()
            self.assertEqual(config.odbc_driver, "")
            self.assertEqual(config.sqlalchemy_url().query["driver"], "ODBC Driver 17 for SQL Server")

    def test_other_engines_do_not_need_odbc(self):
        with mock.patch(PATCH, return_value=["SQL Server"]):
            url = HostConnectionConfig.from_values(engine="postgresql", host="h", name="d", user="u").sqlalchemy_url()
        self.assertNotIn("driver", url.query)

    def test_test_connection_reports_missing_driver(self):
        from host_connector.connection import HostDatabase

        with mock.patch(PATCH, return_value=["SQL Server"]):
            result = HostDatabase(mssql()).test_connection()
        self.assertFalse(result.ok)
        self.assertIn("ODBC Driver 18", result.message)
