"""Catalog of supported data-source types.

On the lakehouse types (Databricks, Snowflake): DIS does **not** compete with a
customer's lakehouse, it sits on top of one. An enterprise that has already built a
Databricks or Snowflake platform is not going to rip it out — and it does not need
to. Registering their warehouse as a DataSource makes DIS the intelligence layer that
finally gets their lakehouse to *answer a question*, rather than another thing to
migrate to. Everything downstream — the certified metrics, the SPC engine, the risk
models, the COA writer — is dialect-portable and simply works against it.
"""


class SourceType:
    MSSQL = "mssql"
    ORACLE = "oracle"
    POSTGRES = "postgres"
    MYSQL = "mysql"
    SQLITE = "sqlite"
    ODBC = "odbc"
    DATABRICKS = "databricks"
    SNOWFLAKE = "snowflake"
    EXCEL = "excel"
    CSV = "csv"

    CHOICES = [
        (MSSQL, "Microsoft SQL Server"),
        (ORACLE, "Oracle Database"),
        (POSTGRES, "PostgreSQL"),
        (MYSQL, "MySQL / MariaDB"),
        (SQLITE, "SQLite"),
        (ODBC, "Generic ODBC / DSN"),
        (DATABRICKS, "Databricks SQL Warehouse"),
        (SNOWFLAKE, "Snowflake"),
        (EXCEL, "Excel Workbook"),
        (CSV, "CSV / Text File"),
    ]

    DATABASE_TYPES = {MSSQL, ORACLE, POSTGRES, MYSQL, SQLITE, ODBC,
                      DATABRICKS, SNOWFLAKE}
    FILE_TYPES = {EXCEL, CSV}
    # Cloud warehouses reached over HTTPS. DIS reads them; it never writes to them.
    LAKEHOUSE_TYPES = {DATABRICKS, SNOWFLAKE}


DEFAULT_PORTS = {
    SourceType.MSSQL: 1433,
    SourceType.ORACLE: 1521,
    SourceType.POSTGRES: 5432,
    SourceType.MYSQL: 3306,
    SourceType.DATABRICKS: 443,
    SourceType.SNOWFLAKE: 443,
}

# Describes each source type so the frontend can render a dynamic
# connection form (mirrors the C# "Enterprise Visual Query Builder").
SOURCE_TYPE_CATALOG = [
    {
        "value": SourceType.MSSQL, "label": "Microsoft SQL Server",
        "category": "database", "default_port": 1433,
        "fields": ["host", "port", "database_name", "schema_name", "username", "password"],
        "supports_windows_auth": True,
    },
    {
        "value": SourceType.ORACLE, "label": "Oracle Database",
        "category": "database", "default_port": 1521,
        "fields": ["host", "port", "database_name", "schema_name", "username", "password"],
        "supports_windows_auth": False,
    },
    {
        "value": SourceType.POSTGRES, "label": "PostgreSQL",
        "category": "database", "default_port": 5432,
        "fields": ["host", "port", "database_name", "schema_name", "username", "password"],
        "supports_windows_auth": False,
    },
    {
        "value": SourceType.MYSQL, "label": "MySQL / MariaDB",
        "category": "database", "default_port": 3306,
        "fields": ["host", "port", "database_name", "username", "password"],
        "supports_windows_auth": False,
    },
    {
        "value": SourceType.SQLITE, "label": "SQLite",
        "category": "database", "default_port": None,
        "fields": ["path"], "supports_windows_auth": False,
    },
    {
        "value": SourceType.EXCEL, "label": "Excel Workbook",
        "category": "file", "default_port": None,
        "fields": ["file"], "supports_windows_auth": False,
    },
    {
        "value": SourceType.CSV, "label": "CSV / Text File",
        "category": "file", "default_port": None,
        "fields": ["file"], "supports_windows_auth": False,
    },
    {
        "value": SourceType.ODBC, "label": "Generic ODBC / DSN",
        "category": "database", "default_port": None,
        "fields": ["dsn", "username", "password"],
        "supports_windows_auth": True,
    },
    {
        "value": SourceType.DATABRICKS, "label": "Databricks SQL Warehouse",
        "category": "lakehouse", "default_port": 443,
        # host = the workspace hostname (adb-xxxx.azuredatabricks.net / dbc-x.cloud.databricks.com)
        # http_path = the SQL warehouse's HTTP path (/sql/1.0/warehouses/<id>)
        # password = a personal access token; the username is always the literal "token"
        # database_name = Unity Catalog catalog; schema_name = schema
        "fields": ["host", "http_path", "password", "database_name", "schema_name"],
        "supports_windows_auth": False,
        "help": ("Point DIS at your existing lakehouse — it reads, it never writes. "
                 "Use a read-only service principal or a token scoped to SELECT."),
    },
    {
        "value": SourceType.SNOWFLAKE, "label": "Snowflake",
        "category": "lakehouse", "default_port": 443,
        # host = the account identifier (orgname-account_name)
        "fields": ["host", "username", "password", "database_name", "schema_name",
                   "warehouse", "role"],
        "supports_windows_auth": False,
        "help": ("Point DIS at your existing warehouse — it reads, it never writes. "
                 "Use a role granted SELECT only."),
    },
]
