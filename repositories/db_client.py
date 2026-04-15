from __future__ import annotations

from typing import Any, Iterable

import pymysql
from pymysql.cursors import DictCursor

from config import Settings


class DatabaseConnectionError(RuntimeError):
    pass


class MySQLReadOnlyClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _connect(self) -> pymysql.connections.Connection:
        if not self.settings.has_database_credentials:
            raise DatabaseConnectionError("MySQL credentials are incomplete.")
        try:
            connection = pymysql.connect(
                host=self.settings.mysql_host,
                port=self.settings.mysql_port,
                user=self.settings.mysql_user,
                password=self.settings.mysql_password,
                database=self.settings.mysql_database,
                charset=self.settings.mysql_charset,
                connect_timeout=self.settings.mysql_connect_timeout,
                cursorclass=DictCursor,
                autocommit=True,
            )
        except pymysql.MySQLError as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return connection

    def ping(self) -> bool:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1 AS ok")
                    cursor.fetchone()
            return True
        except DatabaseConnectionError:
            return False

    def query(self, sql: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SET SESSION TRANSACTION READ ONLY")
                    cursor.execute(sql, tuple(params or ()))
                    return list(cursor.fetchall())
        except pymysql.MySQLError as exc:
            raise DatabaseConnectionError(str(exc)) from exc
