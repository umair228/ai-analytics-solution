"""Catalog of supported data-source types."""


class SourceType:
    MSSQL = "mssql"
    ORACLE = "oracle"
    POSTGRES = "postgres"
    MYSQL = "mysql"
    SQLITE = "sqlite"
    ODBC = "odbc"
    EXCEL = "excel"
    CSV = "csv"

    CHOICES = [
        (MSSQL, "Microsoft SQL Server"),
        (ORACLE, "Oracle Database"),
        (POSTGRES, "PostgreSQL"),
        (MYSQL, "MySQL / MariaDB"),
        (SQLITE, "SQLite"),
        (ODBC, "Generic ODBC / DSN"),
        (EXCEL, "Excel Workbook"),
        (CSV, "CSV / Text File"),
    ]

    DATABASE_TYPES = {MSSQL, ORACLE, POSTGRES, MYSQL, SQLITE, ODBC}
    FILE_TYPES = {EXCEL, CSV}


DEFAULT_PORTS = {
    SourceType.MSSQL: 1433,
    SourceType.ORACLE: 1521,
    SourceType.POSTGRES: 5432,
    SourceType.MYSQL: 3306,
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
]
