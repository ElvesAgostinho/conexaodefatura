"""Consultas de diagnóstico (catálogo do sistema) por motor de base de dados.

São apenas leituras de metadados. Não contêm nada específico do HOST: as
consultas de faturas só serão escritas depois de mapeada a estrutura real.
"""

# Bases de dados visíveis no servidor.
LIST_DATABASES = {
    "mssql": "SELECT name FROM sys.databases WHERE HAS_DBACCESS(name) = 1 ORDER BY name",
    "postgresql": "SELECT datname AS name FROM pg_database WHERE NOT datistemplate ORDER BY datname",
    "mysql": "SELECT schema_name AS name FROM information_schema.schemata ORDER BY schema_name",
    "mariadb": "SELECT schema_name AS name FROM information_schema.schemata ORDER BY schema_name",
}

# Número de linhas por tabela (estimativa do catálogo; não faz COUNT(*) nas tabelas do HOST).
ROW_ESTIMATES = {
    "mssql": """
        SELECT s.name AS schema_name, t.name AS table_name, SUM(p.rows) AS row_count
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
        GROUP BY s.name, t.name
    """,
    "postgresql": """
        SELECT n.nspname AS schema_name, c.relname AS table_name,
               CAST(c.reltuples AS BIGINT) AS row_count
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p')
    """,
    "mysql": """
        SELECT table_schema AS schema_name, table_name AS table_name, table_rows AS row_count
        FROM information_schema.tables
        WHERE table_type = 'BASE TABLE'
    """,
    "oracle": "SELECT owner AS schema_name, table_name AS table_name, num_rows AS row_count FROM all_tables",
}
ROW_ESTIMATES["mariadb"] = ROW_ESTIMATES["mysql"]

# Permissões do utilizador ligado (para confirmar que só pode ler).
PERMISSIONS = {
    "mssql": {
        "roles": """
            SELECT IS_SRVROLEMEMBER('sysadmin') AS sysadmin,
                   IS_MEMBER('db_owner') AS db_owner,
                   IS_MEMBER('db_datawriter') AS db_datawriter,
                   IS_MEMBER('db_ddladmin') AS db_ddladmin
        """,
        "database": "SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE')",
    },
    "postgresql": {
        "roles": "SELECT rolsuper AS superuser FROM pg_roles WHERE rolname = CURRENT_USER",
        "database": """
            SELECT DISTINCT privilege_type AS permission_name
            FROM information_schema.table_privileges
            WHERE grantee = CURRENT_USER
        """,
    },
    "mysql": {
        "database": """
            SELECT privilege_type AS permission_name FROM information_schema.user_privileges
            UNION
            SELECT privilege_type FROM information_schema.schema_privileges
            WHERE table_schema = DATABASE()
        """,
    },
}
PERMISSIONS["mariadb"] = PERMISSIONS["mysql"]

WRITE_PERMISSIONS = frozenset({
    "INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "CONTROL", "CREATE TABLE",
    "CREATE VIEW", "CREATE PROCEDURE", "ALTER ANY SCHEMA", "DROP", "CREATE",
    "TAKE OWNERSHIP", "IMPERSONATE", "ALL PRIVILEGES", "SUPER",
})

# Schemas de sistema que não interessam ao diagnóstico.
SYSTEM_SCHEMAS = {
    "mssql": {
        "sys", "INFORMATION_SCHEMA", "guest", "db_owner", "db_accessadmin",
        "db_securityadmin", "db_ddladmin", "db_backupoperator", "db_datareader",
        "db_datawriter", "db_denydatareader", "db_denydatawriter",
    },
    "postgresql": {"pg_catalog", "information_schema", "pg_toast"},
    "mysql": {"information_schema", "mysql", "performance_schema", "sys"},
    "mariadb": {"information_schema", "mysql", "performance_schema", "sys"},
}

# Motores em que "schema" = base de dados/utilizador: por omissão só se inspeciona o atual.
DEFAULT_SCHEMA_ONLY = {"mysql", "mariadb", "oracle", "sqlite"}
