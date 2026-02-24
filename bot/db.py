# db.py
import asyncio
import json
import logging
import hashlib
import os
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

import aiosqlite
import sqlite3

logger = logging.getLogger(__name__)



class _IdentityCipher:
    """Cipher stub used when encryption is disabled (mostly for tests).

    Provides Fernet-like interface: encrypt()/decrypt() accept/return bytes.
    """
    def encrypt(self, data: bytes) -> bytes:  # noqa: D401
        return data

    def decrypt(self, token: bytes) -> bytes:  # noqa: D401
        return token


def _is_truthy_env(name: str) -> bool:
    return (os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y", "on"})



# Текущая версия схемы базы данных (увеличена до 6)
SCHEMA_VERSION = 8

class Database:
    def __init__(self, db_path: str = None, encryption_key: Optional[bytes] = None):
        if db_path is None:
            db_path = os.getenv("DATABASE_PATH", "/app/bot/data/jira_bot.db")
        
        self.db_path = os.path.abspath(db_path)
        logger.info(f"📂 Database path: {self.db_path}")
        
        data_dir = os.path.dirname(self.db_path)
        os.makedirs(data_dir, exist_ok=True)
        
        backup_dir = os.path.join(data_dir, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        
        try:
            os.chmod(data_dir, 0o750)
            os.chmod(backup_dir, 0o750)
        except Exception as e:
            logger.warning(f"⚠️ Could not set permissions: {e}")
        
        
        # -------------------------------------------------------------------
        # Encryption / Fernet
        #
        # IMPORTANT:
        # - In production we use cryptography.fernet.Fernet to encrypt tokens.
        # - In tests (especially on Windows / Python 3.13+ / 3.14+) you may
        #   encounter issues with old 'cryptography' wheels (PyO3 mismatch).
        #   To keep tests runnable, encryption can be disabled via env:
        #       BOT_DISABLE_ENCRYPTION=1
        #   In that mode we use _IdentityCipher (no-op).
        # -------------------------------------------------------------------
        disable_encryption = _is_truthy_env("BOT_DISABLE_ENCRYPTION")
        if disable_encryption:
            self.cipher = _IdentityCipher()
            logger.warning("🔒 Encryption disabled via BOT_DISABLE_ENCRYPTION=1 (test mode)")
        else:
            try:
                from cryptography.fernet import Fernet  # type: ignore
            except Exception as e:
                # Fail fast with a clear message in production runs.
                raise ImportError(
                    "cryptography is required for encryption but failed to import. "
                    "Install/upgrade 'cryptography' or set BOT_DISABLE_ENCRYPTION=1 for tests."
                ) from e

            if encryption_key:
                self.cipher = Fernet(encryption_key)
                logger.info("🔑 Using provided encryption key")
            else:
                key_path = os.path.join(data_dir, "encryption.key")
                if os.path.exists(key_path):
                    with open(key_path, "rb") as f:
                        key = f.read()
                    logger.info(f"🔑 Loaded encryption key from file: {key_path}")
                else:
                    key = Fernet.generate_key()
                    with open(key_path, "wb") as f:
                        f.write(key)
                    try:
                        os.chmod(key_path, 0o600)
                    except Exception:
                        pass
                    logger.info(f"🔑 Generated new encryption key and saved to: {key_path}")
                self.cipher = Fernet(key)
        
        self._lock = asyncio.Lock()
        self._conn: Optional[aiosqlite.Connection] = None

        # Поведение при проблемах ФС/volume (часто в Docker):
        # WAL иногда падает на некоторых storage / при правах на каталог.
        # Если нужно принудительно отключить WAL — выставь SQLITE_JOURNAL_MODE=DELETE.
        self._journal_mode = (os.getenv("SQLITE_JOURNAL_MODE", "WAL") or "WAL").strip().upper()
        self._max_retry = int(os.getenv("SQLITE_MAX_RETRY", "5"))
        self._base_retry_delay = float(os.getenv("SQLITE_RETRY_DELAY", "0.25"))
        # Сколько ждать блокировку (database is locked). Если базу открывали внешним клиентом
        # (DBeaver/SQLiteStudio) и он держит транзакцию — бот будет терпеливо ждать.
        self._busy_timeout_ms = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "15000"))
    
    async def _get_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            # Быстрый sanity-check на права записи в каталог базы
            try:
                test_path = os.path.join(os.path.dirname(self.db_path), ".db_write_test")
                with open(test_path, "w", encoding="utf-8") as f:
                    f.write("ok")
                os.remove(test_path)
            except Exception as e:
                logger.error(
                    "❌ Database directory is not writable. "
                    "This will cause sqlite3.OperationalError: disk I/O error. "
                    f"dir={os.path.dirname(self.db_path)} error={e}"
                )

            self._conn = await aiosqlite.connect(
                self.db_path,
                timeout=30.0,
                check_same_thread=False
            )
            self._conn.row_factory = aiosqlite.Row
            # Настраиваем PRAGMA. WAL по умолчанию, но можно переопределить env.
            await self._conn.execute(f"PRAGMA journal_mode={self._journal_mode};")
            await self._conn.execute("PRAGMA synchronous=NORMAL;")
            await self._conn.execute("PRAGMA foreign_keys=ON;")
            await self._conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms};")
            # Чуть более стабильное поведение на некоторых volume/FS
            await self._conn.execute("PRAGMA temp_store=MEMORY;")
            await self._conn.execute("PRAGMA wal_autocheckpoint=1000;")
            await self._initialize_schema()
        return self._conn

    async def _reset_conn(self):
        """Закрыть и сбросить соединение (используем для recovery после disk I/O)."""
        try:
            if self._conn is not None:
                await self._conn.close()
        except Exception:
            pass
        self._conn = None

    def _is_transient_sqlite_error(self, e: Exception) -> bool:
        msg = str(e).lower()
        # "disk I/O error" часто возникает при проблемах с volume/permissions/FS.
        # "database is locked" и "busy" — тоже временные.
        return any(
            s in msg
            for s in (
                "disk i/o error",
                "database is locked",
                "database is busy",
                "readonly",
                "unable to open database file",
                "ioerr",
            )
        )

    async def _execute_with_retry(self, sql: str, params: tuple = (), *, commit: bool = False):
        """Выполнить запрос с ретраями и recovery на проблемах ФС/lock.

        Важно: это не лечит переполненный диск/битый volume, но позволяет пережить
        кратковременные ошибки и пересоздать соединение, если SQLite/WAL упал.
        """
        last_err: Optional[Exception] = None
        for attempt in range(1, self._max_retry + 1):
            try:
                conn = await self._get_conn()
                cur = await conn.execute(sql, params)
                if commit:
                    await conn.commit()
                return cur
            except (sqlite3.OperationalError, aiosqlite.OperationalError) as e:
                last_err = e
                if not self._is_transient_sqlite_error(e):
                    raise

                logger.warning(
                    f"⚠️ SQLite transient error on attempt {attempt}/{self._max_retry}: {e} | sql={sql[:80]}"
                )

                # Recovery: сбрасываем соединение на disk I/O и пытаемся снова.
                try:
                    if "disk i/o" in str(e).lower() and self._journal_mode == "WAL":
                        logger.warning("⚠️ Switching SQLITE journal_mode from WAL to DELETE due to disk I/O")
                        self._journal_mode = "DELETE"
                except Exception:
                    pass

                await self._reset_conn()
                await asyncio.sleep(self._base_retry_delay * attempt)

        raise last_err  # type: ignore
    
    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None
    
    async def _initialize_schema(self):
        conn = await self._get_conn()
        async with self._lock:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
            )
            table_exists = await cursor.fetchone()
            
            if not table_exists:
                logger.info("🆕 New database, creating tables...")
                await self._create_all_tables(conn)
                await self._set_schema_version(SCHEMA_VERSION, conn)
                logger.info(f"✅ Schema initialized to version {SCHEMA_VERSION}")
            else:
                current_version = await self._get_schema_version(conn)
                logger.info(f"📊 Current database schema version: {current_version}")
                
                if current_version < SCHEMA_VERSION:
                    logger.info(f"🔄 Upgrading schema from v{current_version} to v{SCHEMA_VERSION}...")
                    await self._migrate(current_version, conn)
                    await self._set_schema_version(SCHEMA_VERSION, conn)
                    logger.info(f"✅ Schema upgraded to version {SCHEMA_VERSION}")
                else:
                    logger.info(f"✅ Schema is up to date (version {current_version})")
            
            await self._ensure_columns(conn)
    
    async def _ensure_columns(self, conn: aiosqlite.Connection):
        required_columns = {
            'sessions': ['session_id', 'user_id', 'step', 'data', 'created_at', 'expires_at'],
            'notifications': ['id', 'user_id', 'filter_name', 'issue_key', 'notification_hash', 'change_type', 'details', 'message_id', 'is_reminder', 'user_reaction', 'reaction_time', 'created_at'],
            'muted_tasks': ['id', 'user_id', 'issue_key', 'filter_name', 'muted_until', 'created_at'],
            'user_mentions': ['id', 'user_id', 'issue_key', 'comment_id', 'mentioned_by', 'mention_text', 'mention_type', 'mention_hash', 'notification_sent', 'created_at'],
            'user_filters': ['id', 'user_id', 'filter_name', 'jql', 'check_interval', 'retention_days', 'is_active', 'created_at', 'updated_at'],
            'users': ['user_id', 'username', 'first_name', 'last_name', 'chat_id', 'jira_user', 'jira_token', 'last_mention_check', 'work_start_time', 'last_cleanup_date', 'last_auth_error_time', 'created_at', 'updated_at', 'is_active', 'fun_mode', 'personality', 'zodiac', 'xp', 'level', 'streak', 'last_ack_date', 'whisper_enabled', 'last_whisper_date', 'notifications_muted'],
            'issue_history': ['id', 'user_id', 'filter_name', 'issue_key', 'issue_data', 'issue_hash', 'created_at']
        }

        for table, columns in required_columns.items():
            cursor = await conn.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in await cursor.fetchall()}
            
            for col in columns:
                if col not in existing:
                    if col in ('id', 'user_id', 'message_id', 'notification_sent', 'is_reminder', 'is_active', 'check_interval', 'retention_days', 'xp', 'level', 'streak', 'whisper_enabled', 'notifications_muted'):
                        col_type = 'INTEGER'
                    elif col in ('created_at', 'updated_at', 'expires_at', 'muted_until', 'reaction_time', 'jira_token', 'data', 'details', 'mention_hash', 'comment_id', 'mentioned_by', 'mention_text', 'mention_type', 'last_mention_check', 'work_start_time', 'last_cleanup_date', 'last_auth_error_time'):
                        col_type = 'TEXT'
                    else:
                        col_type = 'TEXT'

                    logger.info(f"   Adding missing column {col} to {table}")
                    try:
                        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                    except Exception as e:
                        logger.error(f"Error adding column {col} to {table}: {e}")

            if table == 'issue_history':
                extra_columns = existing - set(columns)
                for col in extra_columns:
                    if col == 'id':
                        continue
                    # NOTE: Не удаляем лишние колонки автоматически.
                    # SQLite поддерживает DROP COLUMN только в новых версиях, а в Docker/CI это часто ломается.
                    # Если нужно — делай миграцию с пересозданием таблицы.
        await conn.commit()
    
    async def _get_schema_version(self, conn: aiosqlite.Connection) -> int:
        try:
            cursor = await conn.execute("SELECT version FROM schema_version LIMIT 1")
            row = await cursor.fetchone()
            return row["version"] if row else 0
        except aiosqlite.OperationalError:
            return 0
    
    async def _set_schema_version(self, version: int, conn: aiosqlite.Connection):
        await conn.execute("DELETE FROM schema_version")
        await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        await conn.commit()
    
    async def _create_all_tables(self, conn: aiosqlite.Connection):
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
        """)
        
        await conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            chat_id INTEGER,
            jira_user TEXT,
            jira_token TEXT,
            last_mention_check TEXT,
            work_start_time TEXT,
            last_cleanup_date TEXT,
            last_auth_error_time TEXT,

            -- Fun / UX
            fun_mode TEXT DEFAULT 'off',              -- off | light | full
            personality TEXT DEFAULT 'neutral',       -- neutral | developer | tester | support
            zodiac TEXT,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            streak INTEGER DEFAULT 0,
            last_ack_date TEXT,
            whisper_enabled INTEGER DEFAULT 1,
            last_whisper_date TEXT,

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            notifications_muted INTEGER DEFAULT 0
        )
''')
        
        await conn.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            step TEXT NOT NULL,
            data TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        ''')
        
        await conn.execute('''
        CREATE TABLE IF NOT EXISTS user_filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filter_name TEXT NOT NULL,
            jql TEXT NOT NULL,
            check_interval INTEGER DEFAULT 5,
            retention_days INTEGER DEFAULT 30,
            is_active INTEGER DEFAULT 1,
            notifications_muted INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, filter_name),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        ''')
        
        await conn.execute('''
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filter_name TEXT NOT NULL,
            issue_key TEXT NOT NULL,
            notification_hash TEXT NOT NULL,
            change_type TEXT NOT NULL,
            details TEXT,
            message_id INTEGER,
            is_reminder INTEGER DEFAULT 0,
            user_reaction TEXT,
            reaction_time TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        ''')
        
        await conn.execute('''
        CREATE TABLE IF NOT EXISTS muted_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            issue_key TEXT NOT NULL,
            filter_name TEXT NOT NULL,
            muted_until TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        ''')
        
        await conn.execute('''
        CREATE TABLE IF NOT EXISTS issue_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filter_name TEXT NOT NULL,
            issue_key TEXT NOT NULL,
            issue_data TEXT NOT NULL,
            issue_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        ''')
        
        await conn.execute('''
        CREATE TABLE IF NOT EXISTS user_mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            issue_key TEXT NOT NULL,
            comment_id TEXT,
            mentioned_by TEXT,
            mention_text TEXT,
            mention_type TEXT,
            mention_hash TEXT NOT NULL DEFAULT '',
            notification_sent INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        ''')
        
        await self._create_indexes(conn)
        await conn.commit()
    
    async def _create_indexes(self, conn: aiosqlite.Connection):
        indexes = [
            ('idx_users_user_id', 'users', 'user_id'),
            ('idx_users_chat_id', 'users', 'chat_id'),
            ('idx_sessions_user_id', 'sessions', 'user_id'),
            ('idx_user_filters_user_id', 'user_filters', 'user_id'),
            ('idx_user_filters_active', 'user_filters', 'user_id, is_active'),
            ('idx_notifications_user', 'notifications', 'user_id'),
            ('idx_notifications_issue', 'notifications', 'issue_key'),
            ('idx_notifications_hash', 'notifications', 'user_id, notification_hash'),
            ('idx_notifications_reaction', 'notifications', 'user_id, issue_key, user_reaction'),
            ('idx_muted_tasks_user', 'muted_tasks', 'user_id, issue_key, filter_name'),
            ('idx_issue_history_user', 'issue_history', 'user_id, filter_name, issue_key'),
            ('idx_issue_history_hash', 'issue_history', 'user_id, filter_name, issue_key, issue_hash'),
            ('idx_user_mentions_user', 'user_mentions', 'user_id'),
            ('idx_user_mentions_notification', 'user_mentions', 'user_id, notification_sent'),
            ('idx_user_mentions_issue', 'user_mentions', 'issue_key'),
        ]
        
        for idx_name, table, columns in indexes:
            try:
                await conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({columns})")
            except Exception as e:
                logger.error(f"Error creating index {idx_name}: {e}")
        
        try:
            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_user_mentions_unique 
                ON user_mentions(user_id, issue_key, mention_hash)
            """)
        except Exception as e:
            logger.error(f"Error creating unique index idx_user_mentions_unique: {e}")
        
        try:
            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_user_mentions_comment 
                ON user_mentions(user_id, issue_key, comment_id) 
                WHERE comment_id IS NOT NULL
            """)
        except Exception as e:
            logger.error(f"Error creating unique index idx_user_mentions_comment: {e}")
    
    async def _migrate(self, from_version: int, conn: aiosqlite.Connection):
        if from_version < 2:
            logger.info("Running migration from v1 to v2: adding missing columns")
            tables_to_update = {
                'notifications': [
                    ('message_id', 'INTEGER'),
                    ('is_reminder', 'INTEGER DEFAULT 0'),
                    ('user_reaction', 'TEXT'),
                    ('reaction_time', 'TEXT'),
                ],
                'muted_tasks': [
                    ('muted_until', 'TEXT'),
                ],
                'user_mentions': [
                    ('mention_hash', 'TEXT NOT NULL DEFAULT \'\''),
                ],
                'user_filters': [
                    ('retention_days', 'INTEGER DEFAULT 30'),
                ]
            }
            
            for table, columns in tables_to_update.items():
                cursor = await conn.execute(f"PRAGMA table_info({table})")
                existing = {row[1] for row in await cursor.fetchall()}
                
                for col_name, col_type in columns:
                    if col_name not in existing:
                        logger.info(f"   Adding column {col_name} to {table}")
                        try:
                            await conn.execute(f'ALTER TABLE {table} ADD COLUMN {col_name} {col_type}')
                        except aiosqlite.OperationalError as e:
                            if "duplicate column name" not in str(e):
                                raise
            
            cursor = await conn.execute("PRAGMA table_info(sessions)")
            existing_sessions = {row[1] for row in await cursor.fetchall()}
            if 'expires_at' not in existing_sessions:
                logger.info("   Adding column expires_at to sessions")
                await conn.execute("ALTER TABLE sessions ADD COLUMN expires_at TEXT")
            if 'data' not in existing_sessions:
                logger.info("   Adding column data to sessions")
                await conn.execute("ALTER TABLE sessions ADD COLUMN data TEXT")
            if 'created_at' not in existing_sessions:
                logger.info("   Adding column created_at to sessions")
                await conn.execute("ALTER TABLE sessions ADD COLUMN created_at TEXT")

            await conn.commit()
            logger.info("Migration to v2 completed")
        
        if from_version < 3:
            logger.info("Running migration from v2 to v3: adding unique index on user_mentions")
            try:
                await conn.execute("""
                    DELETE FROM user_mentions
                    WHERE id NOT IN (
                        SELECT MIN(id)
                        FROM user_mentions
                        GROUP BY user_id, issue_key, mention_hash
                    )
                """)
                await conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_user_mentions_unique 
                    ON user_mentions(user_id, issue_key, mention_hash)
                """)
                await conn.commit()
                logger.info("✅ Unique index created on user_mentions")
            except Exception as e:
                logger.error(f"Failed to create unique index: {e}")

        if from_version < 4:
            logger.info("Running migration from v3 to v4: adding last_mention_check to users")
            try:
                cursor = await conn.execute("PRAGMA table_info(users)")
                existing_users = {row[1] for row in await cursor.fetchall()}
                if 'last_mention_check' not in existing_users:
                    await conn.execute("ALTER TABLE users ADD COLUMN last_mention_check TEXT")
                    logger.info("✅ Column last_mention_check added to users")
                await conn.commit()
            except Exception as e:
                logger.error(f"Failed to add last_mention_check column: {e}")

        if from_version < 5:
            logger.info("Running migration from v4 to v5: adding work_start_time and last_cleanup_date to users")
            try:
                cursor = await conn.execute("PRAGMA table_info(users)")
                existing_users = {row[1] for row in await cursor.fetchall()}
                if 'work_start_time' not in existing_users:
                    await conn.execute("ALTER TABLE users ADD COLUMN work_start_time TEXT")
                    logger.info("✅ Column work_start_time added to users")
                if 'last_cleanup_date' not in existing_users:
                    await conn.execute("ALTER TABLE users ADD COLUMN last_cleanup_date TEXT")
                    logger.info("✅ Column last_cleanup_date added to users")
                await conn.commit()
            except Exception as e:
                logger.error(f"Failed to add work_start_time/last_cleanup_date columns: {e}")

        if from_version < 6:
            logger.info("Running migration from v5 to v6: adding last_auth_error_time to users")
            try:
                cursor = await conn.execute("PRAGMA table_info(users)")
                existing_users = {row[1] for row in await cursor.fetchall()}
                if 'last_auth_error_time' not in existing_users:
                    await conn.execute("ALTER TABLE users ADD COLUMN last_auth_error_time TEXT")
                    logger.info("✅ Column last_auth_error_time added to users")
                await conn.commit()
            except Exception as e:
                logger.error(f"Failed to add last_auth_error_time column: {e}")
    
        if from_version < 8:
            logger.info("Running migration to v8: adding notifications_muted to users")
            try:
                cursor = await conn.execute("PRAGMA table_info(users)")
                existing_users = {row[1] for row in await cursor.fetchall()}
                if "notifications_muted" not in existing_users:
                    await conn.execute("ALTER TABLE users ADD COLUMN notifications_muted INTEGER DEFAULT 0")
                    logger.info("✅ Column notifications_muted added to users")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_notifications_muted ON users(notifications_muted)")
                await conn.commit()
            except Exception as e:
                logger.error(f"Failed to add notifications_muted column: {e}")


    # ------------------- Шифрование -------------------
    def _encrypt(self, data: str) -> str:
        if not data:
            return ""
        encrypted = self.cipher.encrypt(data.encode())
        return encrypted.decode()
    
    def _decrypt(self, data: str) -> str:
        if not data:
            return ""
        try:
            decrypted = self.cipher.decrypt(data.encode())
            return decrypted.decode()
        except Exception as e:
            logger.error(f"Error decrypting data: {e}")
            return ""
    
    # ------------------- Методы пользователей -------------------
    async def get_or_create_user(self, user_id: int, username: str = None,
                                first_name: str = None, last_name: str = None,
                                chat_id: int = None) -> Dict[str, Any]:
        conn = await self._get_conn()
        cursor = await conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()
        
        now = datetime.now().isoformat()
        
        if user:
            update_fields = []
            update_values = []
            
            if username and username != user["username"]:
                update_fields.append("username = ?")
                update_values.append(username)
            
            if first_name and first_name != user["first_name"]:
                update_fields.append("first_name = ?")
                update_values.append(first_name)
            
            if last_name and last_name != user["last_name"]:
                update_fields.append("last_name = ?")
                update_values.append(last_name)
            
            if chat_id and chat_id != user["chat_id"]:
                update_fields.append("chat_id = ?")
                update_values.append(chat_id)
            
            if update_fields:
                update_fields.append("updated_at = ?")
                update_values.append(now)
                update_values.append(user_id)
                
                await conn.execute(
                    f"UPDATE users SET {', '.join(update_fields)} WHERE user_id = ?",
                    update_values
                )
                await conn.commit()
            
            cursor = await conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            user = await cursor.fetchone()
            
            result = dict(user)
            if result.get("jira_token"):
                result["jira_token"] = self._decrypt(result["jira_token"])
            return result
        else:
            await conn.execute(
                """INSERT INTO users 
                   (user_id, username, first_name, last_name, chat_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, username, first_name, last_name, chat_id, now, now)
            )
            await conn.commit()
            logger.info(f"👤 Created new user: {user_id} ({username})")
            return {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
                "chat_id": chat_id,
                "jira_user": None,
                "jira_token": None,
                "last_mention_check": None,
                "work_start_time": None,
                "last_cleanup_date": None,
                "last_auth_error_time": None,
                "created_at": now,
                "updated_at": now,
                "is_active": 1
            }
    
    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        conn = await self._get_conn()
        cursor = await conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()
        if user:
            result = dict(user)
            if result.get("jira_token"):
                result["jira_token"] = self._decrypt(result["jira_token"])
            return result
        return None
    
    async def update_user_jira_credentials(self, user_id: int, jira_user: str, jira_token: str) -> bool:
        now = datetime.now().isoformat()
        encrypted_token = self._encrypt(jira_token) if jira_token else None
        conn = await self._get_conn()
        await conn.execute(
            "UPDATE users SET jira_user = ?, jira_token = ?, updated_at = ?, is_active = 1 WHERE user_id = ?",
            (jira_user, encrypted_token, now, user_id)
        )
        await conn.commit()
        logger.info(f"🔑 Updated Jira credentials for user {user_id}")
        return True
    
    async def get_all_active_users(self) -> List[Dict[str, Any]]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM users WHERE is_active = 1 AND jira_user IS NOT NULL AND jira_token IS NOT NULL AND chat_id IS NOT NULL"
        )
        rows = await cursor.fetchall()
        users = []
        for row in rows:
            user = dict(row)
            if user.get("jira_token"):
                user["jira_token"] = self._decrypt(user["jira_token"])
            users.append(user)
        logger.info(f"👥 Found {len(users)} active users")
        return users
    
    async def deactivate_user(self, user_id: int) -> bool:
        conn = await self._get_conn()
        await conn.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
        await conn.commit()
        logger.info(f"👤 Deactivated user {user_id}")
        return True

    # ========== НОВЫЕ МЕТОДЫ для last_auth_error_time ==========
    async def get_last_auth_error_time(self, user_id: int) -> Optional[str]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT last_auth_error_time FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        return row["last_auth_error_time"] if row else None

    async def update_last_auth_error_time(self, user_id: int, time_str: str):
        conn = await self._get_conn()
        await conn.execute(
            "UPDATE users SET last_auth_error_time = ?, updated_at = ? WHERE user_id = ?",
            (time_str, datetime.now().isoformat(), user_id)
        )
        await conn.commit()
        logger.debug(f"🔐 Updated last_auth_error_time for user {user_id} to {time_str}")

    # ========== Ранее добавленные методы ==========
    async def get_last_mention_check_time(self, user_id: int) -> Optional[str]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT last_mention_check FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        return row["last_mention_check"] if row else None

    async def update_last_mention_check(self, user_id: int, check_time: str = None):
        if not check_time:
            check_time = datetime.now().isoformat()
        conn = await self._get_conn()
        await conn.execute(
            "UPDATE users SET last_mention_check = ?, updated_at = ? WHERE user_id = ?",
            (check_time, datetime.now().isoformat(), user_id)
        )
        await conn.commit()
        logger.debug(f"🕒 Updated last_mention_check for user {user_id} to {check_time}")

    async def get_work_start_time(self, user_id: int) -> Optional[str]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT work_start_time FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        return row["work_start_time"] if row else None

    async def set_work_start_time(self, user_id: int, time_str: str) -> bool:
        conn = await self._get_conn()
        await conn.execute(
            "UPDATE users SET work_start_time = ?, updated_at = ? WHERE user_id = ?",
            (time_str, datetime.now().isoformat(), user_id)
        )
        await conn.commit()
        logger.info(f"⏰ Set work_start_time={time_str} for user {user_id}")
        return True

    async def get_last_cleanup_date(self, user_id: int) -> Optional[str]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT last_cleanup_date FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        return row["last_cleanup_date"] if row else None

    async def update_last_cleanup_date(self, user_id: int, date_str: str = None):
        if not date_str:
            date_str = datetime.now().date().isoformat()
        conn = await self._get_conn()
        await conn.execute(
            "UPDATE users SET last_cleanup_date = ?, updated_at = ? WHERE user_id = ?",
            (date_str, datetime.now().isoformat(), user_id)
        )
        await conn.commit()
        logger.debug(f"🗓 Updated last_cleanup_date for user {user_id} to {date_str}")

    async def cleanup_user_old_data(self, user_id: int) -> int:
        """Удаляет старые записи для всех фильтров пользователя на основе их retention_days."""
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT filter_name, IFNULL(retention_days, 30) as days FROM user_filters WHERE user_id = ?",
            (user_id,)
        )
        filters = await cursor.fetchall()
        
        today = datetime.now().date()
        deleted_total = 0
        
        for f in filters:
            filter_name = f["filter_name"]
            days = f["days"]
            cutoff_date = (today - timedelta(days=days)).isoformat()
            
            cursor = await conn.execute(
                "DELETE FROM issue_history WHERE user_id = ? AND filter_name = ? AND created_at < ?",
                (user_id, filter_name, cutoff_date)
            )
            deleted_total += cursor.rowcount
            
            cursor = await conn.execute(
                "DELETE FROM notifications WHERE user_id = ? AND filter_name = ? AND created_at < ?",
                (user_id, filter_name, cutoff_date)
            )
            deleted_total += cursor.rowcount
        
        await conn.commit()
        logger.info(f"🧹 Cleaned up {deleted_total} old records for user {user_id}")
        return deleted_total

    # ------------------- Методы сессий -------------------
    async def create_session(self, user_id: int, step: str, data: Dict = None,
                            expires_in: int = 3600) -> str:
        import uuid
        session_id = str(uuid.uuid4())
        now = datetime.now()
        expires_at = (now + timedelta(seconds=expires_in)).isoformat()
        data_json = json.dumps(data) if data else "{}"
        
        conn = await self._get_conn()
        await conn.execute(
            """INSERT INTO sessions 
               (session_id, user_id, step, data, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, user_id, step, data_json, now.isoformat(), expires_at)
        )
        await conn.commit()
        return session_id
    
    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        conn = await self._get_conn()
        cursor = await conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        row = await cursor.fetchone()
        if row:
            session = dict(row)
            expires_at = datetime.fromisoformat(session["expires_at"])
            if expires_at < datetime.now():
                await self.delete_session(session_id)
                return None
            if session["data"]:
                session["data"] = json.loads(session["data"])
            else:
                session["data"] = {}
            return session
        return None
    
    async def update_session(self, session_id: str, step: str = None, data: Dict = None) -> bool:
        updates = []
        values = []
        if step:
            updates.append("step = ?")
            values.append(step)
        if data is not None:
            updates.append("data = ?")
            values.append(json.dumps(data))
        if not updates:
            return False
        values.append(session_id)
        
        conn = await self._get_conn()
        await conn.execute(
            f"UPDATE sessions SET {', '.join(updates)} WHERE session_id = ?",
            values
        )
        await conn.commit()
        return True
    
    async def delete_session(self, session_id: str) -> bool:
        conn = await self._get_conn()
        cursor = await conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        await conn.commit()
        return cursor.rowcount > 0
    
    async def cleanup_expired_sessions(self) -> int:
        now = datetime.now().isoformat()
        cursor = await self._execute_with_retry(
            "DELETE FROM sessions WHERE expires_at < ?",
            (now,),
            commit=True,
        )
        deleted = cursor.rowcount
        if deleted:
            logger.info(f"🗑 Cleaned up {deleted} expired sessions")
        return deleted
    
    # ------------------- Методы фильтров -------------------
    async def add_user_filter(self, user_id: int, filter_name: str, jql: str,
                              check_interval: int = 5) -> bool:
        now = datetime.now().isoformat()
        conn = await self._get_conn()
        try:
            await conn.execute(
                """INSERT INTO user_filters 
                   (user_id, filter_name, jql, check_interval, retention_days, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, filter_name, jql, check_interval, 30, now, now)
            )
            await conn.commit()
            logger.info(f"🔍 Added filter '{filter_name}' for user {user_id}")
            return True
        except aiosqlite.IntegrityError:
            logger.warning(f"Filter '{filter_name}' already exists for user {user_id}")
            return False
    
    async def get_user_filters(self, user_id: int, active_only: bool = True) -> List[Dict[str, Any]]:
        conn = await self._get_conn()
        if active_only:
            cursor = await conn.execute(
                "SELECT * FROM user_filters WHERE user_id = ? AND is_active = 1 ORDER BY created_at DESC",
                (user_id,)
            )
        else:
            cursor = await conn.execute(
                "SELECT * FROM user_filters WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,)
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    
    async def get_user_filters_with_id(self, user_id: int, active_only: bool = True) -> List[Tuple[int, str, str]]:
        """Вернуть фильтры в виде (id, filter_name, jql). Полезно для callback_data (лимит 64 байта)."""
        conn = await self._get_conn()
        if active_only:
            cursor = await conn.execute(
                "SELECT id, filter_name, jql FROM user_filters WHERE user_id = ? AND is_active = 1 ORDER BY created_at DESC",
                (user_id,)
            )
        else:
            cursor = await conn.execute(
                "SELECT id, filter_name, jql FROM user_filters WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,)
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]

    async def get_filter_id_by_name(self, user_id: int, filter_name: str) -> Optional[int]:
        """Найти id фильтра по имени (для формирования короткого callback_data)."""
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT id FROM user_filters WHERE user_id = ? AND filter_name = ? LIMIT 1",
            (user_id, filter_name)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None
        return int(row[0])

    async def get_filter_name_by_id(self, user_id: int, filter_id: int) -> Optional[str]:
        """Найти имя фильтра по id."""
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT filter_name FROM user_filters WHERE user_id = ? AND id = ? LIMIT 1",
            (user_id, filter_id)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None
        return str(row[0])

    async def delete_user_filter(self, user_id: int, filter_name: str) -> bool:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "DELETE FROM user_filters WHERE user_id = ? AND filter_name = ?",
            (user_id, filter_name)
        )
        await conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info(f"🗑 Deleted filter '{filter_name}' for user {user_id}")
        return deleted
    
    async def toggle_filter_active(self, user_id: int, filter_name: str, is_active: bool) -> bool:
        now = datetime.now().isoformat()
        conn = await self._get_conn()
        cursor = await conn.execute(
            "UPDATE user_filters SET is_active = ?, updated_at = ? WHERE user_id = ? AND filter_name = ?",
            (1 if is_active else 0, now, user_id, filter_name)
        )
        await conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            status = "enabled" if is_active else "disabled"
            logger.info(f"🔧 Filter '{filter_name}' {status} for user {user_id}")
        return updated
    
    async def update_filter_interval(self, user_id: int, filter_name: str, interval: int) -> bool:
        now = datetime.now().isoformat()
        conn = await self._get_conn()
        cursor = await conn.execute(
            "UPDATE user_filters SET check_interval = ?, updated_at = ? WHERE user_id = ? AND filter_name = ?",
            (interval, now, user_id, filter_name)
        )
        await conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info(f"⏱️ Filter '{filter_name}' interval set to {interval} minutes for user {user_id}")
        return updated
    
    async def update_filter_retention(self, user_id: int, filter_name: str, days: int) -> bool:
        now = datetime.now().isoformat()
        conn = await self._get_conn()
        cursor = await conn.execute(
            "UPDATE user_filters SET retention_days = ?, updated_at = ? WHERE user_id = ? AND filter_name = ?",
            (days, now, user_id, filter_name)
        )
        await conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info(f"♻️ Filter '{filter_name}' retention set to {days} days for user {user_id}")
        return updated
    
    async def update_filter_jql(self, user_id: int, filter_name: str, new_jql: str) -> bool:
        now = datetime.now().isoformat()
        conn = await self._get_conn()
        cursor = await conn.execute(
            "UPDATE user_filters SET jql = ?, updated_at = ? WHERE user_id = ? AND filter_name = ?",
            (new_jql, now, user_id, filter_name)
        )
        await conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info(f"✏️ JQL of filter '{filter_name}' updated for user {user_id}")
        return updated
    
    async def get_filter(self, user_id: int, filter_name: str) -> Optional[Dict[str, Any]]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM user_filters WHERE user_id = ? AND filter_name = ?",
            (user_id, filter_name)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    
    # ------------------- Методы истории задач -------------------

    async def get_filter_by_id(self, user_id: int, filter_id: int) -> Optional[Dict[str, Any]]:
        """Получить фильтр по ID (безопасно для callback'ов)."""
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM user_filters WHERE user_id = ? AND id = ?",
            (user_id, filter_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        columns = [description[0] for description in cursor.description]
        return dict(zip(columns, row))

    async def is_notifications_muted(self, user_id: int) -> bool:
        """True если пользователь отключил все уведомления (persisted в БД)."""
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT notifications_muted FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        return bool(row[0])

    async def set_notifications_muted(self, user_id: int, muted: bool) -> None:
        """Включает/выключает уведомления для пользователя (persisted)."""
        conn = await self._get_conn()
        await conn.execute(
            "UPDATE users SET notifications_muted = ?, updated_at = ? WHERE user_id = ?",
            (1 if muted else 0, datetime.utcnow().isoformat(), user_id),
        )
        await conn.commit()

    async def save_issue_state(self, user_id: int, filter_name: str, issue_key: str,
                               issue_data: Dict, issue_hash: str) -> bool:
        now = datetime.now().isoformat()
        issue_data_json = json.dumps(issue_data)
        conn = await self._get_conn()
        try:
            await conn.execute(
                """INSERT INTO issue_history 
                   (user_id, filter_name, issue_key, issue_data, issue_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, filter_name, issue_key, issue_data_json, issue_hash, now)
            )
            await conn.commit()
            return True
        except Exception as e:
            logger.error(f"❌ Error saving issue state: {e}")
            return False
    
    async def get_last_issue_state(self, user_id: int, filter_name: str, issue_key: str) -> Optional[Dict]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            """SELECT issue_data, issue_hash FROM issue_history 
               WHERE user_id = ? AND filter_name = ? AND issue_key = ?
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, filter_name, issue_key)
        )
        row = await cursor.fetchone()
        if row:
            if row["issue_data"] is None:
                logger.warning(f"⚠️ issue_data is None for {user_id}:{filter_name}:{issue_key}, returning None")
                return None
            issue_data = json.loads(row["issue_data"])
            issue_hash = row["issue_hash"]
            return {"issue_data": issue_data, "issue_hash": issue_hash}
        return None
    
    async def cleanup_old_issue_history(self, default_days: int = 30) -> int:
        conn = await self._get_conn()
        total_deleted = 0
        async with self._lock:
            await conn.execute("BEGIN")
            try:
                cursor = await conn.execute("SELECT user_id, filter_name, retention_days FROM user_filters")
                filters = await cursor.fetchall()
                
                for f in filters:
                    uid = f["user_id"]
                    fname = f["filter_name"]
                    days = f["retention_days"] if f["retention_days"] is not None else default_days
                    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
                    
                    cursor = await conn.execute(
                        "DELETE FROM issue_history WHERE user_id = ? AND filter_name = ? AND created_at < ?",
                        (uid, fname, cutoff)
                    )
                    total_deleted += cursor.rowcount
                    
                    cursor = await conn.execute(
                        "DELETE FROM notifications WHERE user_id = ? AND filter_name = ? AND created_at < ?",
                        (uid, fname, cutoff)
                    )
                    total_deleted += cursor.rowcount
                
                global_cutoff = (datetime.now() - timedelta(days=default_days)).isoformat()
                cursor = await conn.execute("""
                    DELETE FROM issue_history 
                    WHERE filter_name NOT IN (SELECT filter_name FROM user_filters WHERE user_id = issue_history.user_id)
                    AND created_at < ?
                """, (global_cutoff,))
                total_deleted += cursor.rowcount
                
                await conn.commit()
                if total_deleted:
                    logger.info(f"🗑 Cleaned up {total_deleted} old records based on per-filter retention")
            except Exception as e:
                await conn.rollback()
                logger.error(f"❌ Error in cleanup_old_issue_history: {e}")
                raise
        return total_deleted
    
    # ------------------- Методы обнаружения изменений -------------------

    # ---------------------------------------------------------------------
    # Backward compatibility helpers
    # ---------------------------------------------------------------------
    async def is_user_muted(self, user_id: int) -> bool:
        """Alias for :meth:`is_notifications_muted` (kept for older code/tests)."""
        return await self.is_notifications_muted(user_id)

    async def set_user_muted(self, user_id: int, muted: bool) -> None:
        """Alias for :meth:`set_notifications_muted` (kept for older code/tests)."""
        await self.set_notifications_muted(user_id, muted)

    def _calculate_issue_hash(self, issue_data: Dict[str, Any]) -> str:
        fields = issue_data.get("fields", {})
        issue_simplified = {
            "key": issue_data.get("key"),
            "summary": fields.get("summary"),
            "status": fields.get("status", {}).get("name") if fields.get("status") else None,
            "priority": fields.get("priority", {}).get("name") if fields.get("priority") else None,
            "assignee": fields.get("assignee", {}).get("displayName") if fields.get("assignee") else None,
            "updated": fields.get("updated"),
            "comment_count": len(fields.get("comment", {}).get("comments", [])),
            "resolution": fields.get("resolution", {}).get("name") if fields.get("resolution") else None
        }
        issue_str = json.dumps(issue_simplified, sort_keys=True)
        return hashlib.md5(issue_str.encode()).hexdigest()
    
    def _calculate_notification_hash(self, issue: Dict[str, Any], change_type: str) -> str:
        notification_data = {
            "issue_key": issue.get("key"),
            "change_type": change_type,
            "timestamp": datetime.now().strftime("%Y%m%d%H%M")
        }
        data_str = json.dumps(notification_data, sort_keys=True)
        return hashlib.md5(data_str.encode()).hexdigest()
    
    async def check_and_update_issue(self, user_id: int, filter_name: str, issue: Dict[str, Any]) -> Dict[str, Any]:
        issue_key = issue.get("key")
        if not issue_key:
            return {"has_changes": False}
        
        current_hash = self._calculate_issue_hash(issue)
        last_state = await self.get_last_issue_state(user_id, filter_name, issue_key)
        
        if not last_state:
            await self.save_issue_state(user_id, filter_name, issue_key, issue, current_hash)
            return {
                "has_changes": True,
                "change_type": "new",
                "issue_key": issue_key,
                "is_new": True
            }
        else:
            if last_state["issue_hash"] != current_hash:
                await self.save_issue_state(user_id, filter_name, issue_key, issue, current_hash)
                return {
                    "has_changes": True,
                    "change_type": "updated",
                    "issue_key": issue_key,
                    "is_new": False,
                    "previous_hash": last_state["issue_hash"],
                    "current_hash": current_hash
                }
            else:
                return {"has_changes": False}
    
    async def was_notification_sent(self, user_id: int, filter_name: str, issue_key: str, notification_hash: str) -> bool:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND filter_name = ? AND issue_key = ? AND notification_hash = ?",
            (user_id, filter_name, issue_key, notification_hash)
        )
        count = (await cursor.fetchone())[0]
        return count > 0
    
    # ------------------- Методы уведомлений -------------------
    async def record_notification(self, user_id: int, filter_name: str, issue_key: str,
                                  notification_hash: str, change_type: str,
                                  details: Dict[str, Any] = None) -> int:
        """Записать уведомление и вернуть его ID."""
        if not details:
            details = {}
        details_json = json.dumps(details)
        message_id = details.get('message_id')
        
        conn = await self._get_conn()
        try:
            cursor = await conn.execute(
                """INSERT INTO notifications 
                   (user_id, filter_name, issue_key, notification_hash, change_type, details, message_id, is_reminder, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (user_id, filter_name, issue_key, notification_hash, change_type,
                 details_json, message_id, details.get('is_reminder', 0), datetime.now().isoformat())
            )
            row = await cursor.fetchone()
            await conn.commit()
            notif_id = row["id"] if row else None
            return notif_id
        except Exception as e:
            logger.error(f"❌ Error recording notification: {e}")
            return None
    
    async def get_notification_by_id(self, notif_id: int) -> Optional[Dict]:
        conn = await self._get_conn()
        cursor = await conn.execute("SELECT * FROM notifications WHERE id = ?", (notif_id,))
        row = await cursor.fetchone()
        if row:
            notif = dict(row)
            if notif.get("details"):
                notif["details"] = json.loads(notif["details"])
            return notif
        return None

    async def get_user_notifications(self, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)
        )
        rows = await cursor.fetchall()
        notifications = []
        for row in rows:
            notif = dict(row)
            if notif.get("details"):
                notif["details"] = json.loads(notif["details"])
            notifications.append(notif)
        return notifications
    
    async def get_notifications_with_blockers(self, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? AND details LIKE '%blockers%' ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)
        )
        rows = await cursor.fetchall()
        notifications = []
        for row in rows:
            notif = dict(row)
            if notif.get("details"):
                notif["details"] = json.loads(notif["details"])
            notifications.append(notif)
        return notifications
    
    async def get_notifications_by_issue(self, user_id: int, issue_key: str) -> List[Dict[str, Any]]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? AND issue_key = ? ORDER BY created_at DESC",
            (user_id, issue_key)
        )
        rows = await cursor.fetchall()
        notifications = []
        for row in rows:
            notif = dict(row)
            if notif.get("details"):
                notif["details"] = json.loads(notif["details"])
            notifications.append(notif)
        return notifications
    
    # ------------------- Новые методы для дашборда и группировки -------------------
    async def get_dashboard_stats(self, user_id: int) -> Dict[str, int]:
        """Возвращает статистику для главного экрана: неподтверждённые уведомления, непрочитанные упоминания."""
        conn = await self._get_conn()
        # Неподтверждённые уведомления (не просроченные)
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND (user_reaction IS NULL OR user_reaction = '') AND is_reminder = 0",
            (user_id,)
        )
        pending = (await cursor.fetchone())[0] or 0

        cursor = await conn.execute(
            "SELECT COUNT(*) FROM user_mentions WHERE user_id = ? AND notification_sent = 0",
            (user_id,)
        )
        unread_mentions = (await cursor.fetchone())[0] or 0

        return {
            "pending_notifications": pending,
            "unread_mentions": unread_mentions,
        }

    async def get_pending_notifications_grouped(self, user_id: int) -> List[Dict]:
        """Возвращает список неподтверждённых уведомлений, сгруппированных по задачам.
           Каждая запись содержит issue_key, filter_name, count, последнее уведомление, notification_hash для первого?"""
        conn = await self._get_conn()
        cursor = await conn.execute(
            """SELECT issue_key, filter_name, COUNT(*) as cnt, MAX(created_at) as last_time,
                      (SELECT notification_hash FROM notifications n2 
                       WHERE n2.user_id = n.user_id AND n2.issue_key = n.issue_key AND n2.filter_name = n.filter_name 
                       AND (n2.user_reaction IS NULL OR n2.user_reaction = '') AND n2.is_reminder = 0 
                       ORDER BY created_at DESC LIMIT 1) as last_hash,
                      (SELECT message_id FROM notifications n2 
                       WHERE n2.user_id = n.user_id AND n2.issue_key = n.issue_key AND n2.filter_name = n.filter_name 
                       AND (n2.user_reaction IS NULL OR n2.user_reaction = '') AND n2.is_reminder = 0 
                       ORDER BY created_at DESC LIMIT 1) as last_msg_id,
                      (SELECT id FROM notifications n2 
                       WHERE n2.user_id = n.user_id AND n2.issue_key = n.issue_key AND n2.filter_name = n.filter_name 
                       AND (n2.user_reaction IS NULL OR n2.user_reaction = '') AND n2.is_reminder = 0 
                       ORDER BY created_at DESC LIMIT 1) as last_notif_id
               FROM notifications n
               WHERE user_id = ? AND (user_reaction IS NULL OR user_reaction = '') AND is_reminder = 0
               GROUP BY issue_key, filter_name
               ORDER BY last_time DESC""",
            (user_id,)
        )
        rows = await cursor.fetchall()
        result = []
        for r in rows:
            result.append({
                "issue_key": r["issue_key"],
                "filter_name": r["filter_name"],
                "count": r["cnt"],
                "last_time": r["last_time"],
                "last_hash": r["last_hash"],
                "last_msg_id": r["last_msg_id"],
                "last_notif_id": r["last_notif_id"],
            })
        return result

    # ------------------- Методы для упоминаний -------------------
    async def get_mention_by_hash(self, user_id: int, mention_hash: str) -> Optional[Dict]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM user_mentions WHERE user_id = ? AND mention_hash = ?",
            (user_id, mention_hash)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    
    async def save_mention(self, user_id: int, issue_key: str, comment_id: str = None,
                           mentioned_by: str = None, mention_text: str = None,
                           mention_type: str = None, mention_hash: str = None) -> bool:
        now = datetime.now().isoformat()
        if not mention_hash:
            mention_hash = ""
        conn = await self._get_conn()
        try:
            await conn.execute(
                """INSERT OR IGNORE INTO user_mentions 
                   (user_id, issue_key, comment_id, mentioned_by, mention_text, mention_type, mention_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, issue_key, comment_id, mentioned_by, mention_text, mention_type, mention_hash, now)
            )
            await conn.commit()
            saved = conn.total_changes > 0
            if saved:
                logger.info(f"💬 Saved mention for user {user_id} in {issue_key}")
            return saved
        except Exception as e:
            logger.error(f"Error saving mention: {e}")
            return False
    
    async def get_unnotified_mentions(self, user_id: int) -> List[Dict[str, Any]]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM user_mentions WHERE user_id = ? AND notification_sent = 0 ORDER BY created_at DESC",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    
    async def mark_mentions_as_notified(self, user_id: int, issue_key: str = None) -> bool:
        conn = await self._get_conn()
        if issue_key:
            cursor = await conn.execute(
                "UPDATE user_mentions SET notification_sent = 1 WHERE user_id = ? AND issue_key = ?",
                (user_id, issue_key)
            )
        else:
            cursor = await conn.execute(
                "UPDATE user_mentions SET notification_sent = 1 WHERE user_id = ?",
                (user_id,)
            )
        await conn.commit()
        updated = cursor.rowcount
        if updated:
            logger.info(f"✅ Marked {updated} mentions as notified for user {user_id}" + (f" in {issue_key}" if issue_key else ""))
        return True
    
    async def get_total_mentions(self, user_id: int) -> int:
        conn = await self._get_conn()
        cursor = await conn.execute("SELECT COUNT(*) FROM user_mentions WHERE user_id = ?", (user_id,))
        count = (await cursor.fetchone())[0]
        return count or 0
    
    # ------------------- Методы для системы напоминаний -------------------
    async def should_send_reminder(self, user_id: int, filter_name: str, issue_key: str) -> Tuple[bool, Optional[int]]:
        try:
            if await self.is_task_muted(user_id, issue_key, filter_name):
                return False, None
            
            from .utils import should_send_reminder_check
            from .config import REMINDER_INTERVAL_MINUTES
            
            conn = await self._get_conn()
            cursor = await conn.execute(
                """SELECT id, message_id, created_at FROM notifications 
                   WHERE user_id = ? AND filter_name = ? AND issue_key = ? 
                   AND (user_reaction IS NULL OR user_reaction = '') 
                   AND is_reminder = 0
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id, filter_name, issue_key)
            )
            row = await cursor.fetchone()
            if not row:
                return False, None
            
            notif_id, message_id, created_at = row
            now = datetime.now()
            notification_time = datetime.fromisoformat(created_at)
            time_diff = now - notification_time
            reminder_interval = timedelta(minutes=REMINDER_INTERVAL_MINUTES)
            
            if time_diff > reminder_interval:
                if should_send_reminder_check():
                    return True, message_id
                else:
                    return False, None
            return False, None
        except Exception as e:
            logger.error(f"❌ Error in should_send_reminder: {e}")
            return False, None
    
    async def get_pending_notifications(self, user_id: int) -> List[Dict[str, Any]]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            """SELECT n.issue_key, n.filter_name, n.created_at as last_notification,
                      COUNT(*) as notification_count, MAX(n.created_at) as latest_notification
               FROM notifications n
               WHERE n.user_id = ? AND (n.user_reaction IS NULL OR user_reaction = '') AND n.is_reminder = 0
               GROUP BY n.issue_key, n.filter_name
               ORDER BY latest_notification DESC""",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [{
            'issue_key': row[0],
            'filter_name': row[1],
            'last_notification': row[2],
            'notification_count': row[3],
            'latest_notification': row[4]
        } for row in rows]
    
    async def get_last_notification_info(self, user_id: int, filter_name: str, issue_key: str) -> Optional[Dict]:
        try:
            conn = await self._get_conn()
            cursor = await conn.execute(
                """SELECT notification_hash, change_type, details FROM notifications 
                   WHERE user_id = ? AND filter_name = ? AND issue_key = ? AND is_reminder = 0
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id, filter_name, issue_key)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            
            notification_hash, change_type, details_json = row
            details = json.loads(details_json) if details_json else {}
            
            return {
                "issue_key": issue_key,
                "notification_hash": notification_hash,
                "change_type": change_type,
                "issue": {
                    "key": issue_key,
                    "fields": {
                        "summary": details.get("summary", "Без названия"),
                        "status": {"name": details.get("status", "Неизвестно")},
                        "priority": {"name": details.get("priority", "Не указан")},
                        "assignee": {"displayName": details.get("assignee", "Не назначен")},
                        "issuelinks": []
                    }
                },
                "is_new": change_type == "new",
                "blockers": details.get("blockers", []),
                "blockers_count": details.get("blockers_count", 0),
                "active_blockers": details.get("active_blockers", 0)
            }
        except Exception:
            return None
    
    async def record_user_reaction(self, user_id: int, issue_key: str, notification_hash: str) -> bool:
        conn = await self._get_conn()
        now = datetime.now().isoformat()
        await conn.execute(
            "UPDATE notifications SET user_reaction = 'acknowledged', reaction_time = ? WHERE user_id = ? AND issue_key = ? AND notification_hash = ?",
            (now, user_id, issue_key, notification_hash)
        )
        await conn.commit()
        
        await conn.execute(
            "UPDATE notifications SET user_reaction = 'acknowledged' WHERE user_id = ? AND issue_key = ? AND (user_reaction IS NULL OR user_reaction = '')",
            (user_id, issue_key)
        )
        await conn.commit()
        return True
    
    async def has_user_reacted(self, user_id: int, issue_key: str, notification_hash: str) -> bool:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND issue_key = ? AND notification_hash = ? AND user_reaction IS NOT NULL AND user_reaction != ''",
            (user_id, issue_key, notification_hash)
        )
        count = (await cursor.fetchone())[0]
        return count > 0
    
    async def is_task_muted(self, user_id: int, issue_key: str, filter_name: str = None) -> bool:
        try:
            conn = await self._get_conn()
            now = datetime.now().isoformat()
            if filter_name:
                cursor = await conn.execute(
                    "SELECT muted_until FROM muted_tasks WHERE user_id = ? AND issue_key = ? AND filter_name = ? AND muted_until > ?",
                    (user_id, issue_key, filter_name, now)
                )
            else:
                cursor = await conn.execute(
                    "SELECT muted_until FROM muted_tasks WHERE user_id = ? AND issue_key = ? AND muted_until > ? LIMIT 1",
                    (user_id, issue_key, now)
                )
            return await cursor.fetchone() is not None
        except Exception:
            return False
    
    async def mute_task_notifications(self, user_id: int, issue_key: str, filter_name: str, hours: int = 24) -> bool:
        conn = await self._get_conn()
        await conn.execute(
            "DELETE FROM muted_tasks WHERE user_id = ? AND issue_key = ? AND filter_name = ?",
            (user_id, issue_key, filter_name)
        )
        muted_until = datetime.now() + timedelta(hours=hours)
        await conn.execute(
            "INSERT INTO muted_tasks (user_id, issue_key, filter_name, muted_until, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, issue_key, filter_name, muted_until.isoformat(), datetime.now().isoformat())
        )
        await conn.commit()
        return True
    
    async def cleanup_expired_muted_tasks(self) -> int:
        now = datetime.now().isoformat()
        cursor = await self._execute_with_retry(
            "DELETE FROM muted_tasks WHERE muted_until < ?",
            (now,),
            commit=True,
        )
        deleted = cursor.rowcount
        return deleted
    
    async def get_changes_from_notification(self, notification_id: int) -> Optional[Dict]:
        conn = await self._get_conn()
        cursor = await conn.execute("SELECT details FROM notifications WHERE id = ?", (notification_id,))
        row = await cursor.fetchone()
        if row and row["details"]:
            try:
                return json.loads(row["details"]).get("changes", {})
            except:
                return None
        return None
    
    # ------------------- Статистика -------------------
    async def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        stats = {
            'total_filters': 0,
            'active_filters': 0,
            'total_tracked': 0,
            'total_notifications': 0,
            'pending_notifications': 0,
            'blockers_count': 0,
            'active_blockers': 0,
            'mentions_count': 0
        }
        conn = await self._get_conn()
        
        cursor = await conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) FROM user_filters WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        if row:
            stats['total_filters'] = row[0] or 0
            stats['active_filters'] = row[1] or 0
        
        cursor = await conn.execute(
            "SELECT COUNT(DISTINCT issue_key) FROM notifications WHERE user_id = ?",
            (user_id,)
        )
        stats['total_tracked'] = (await cursor.fetchone())[0] or 0
        
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ?",
            (user_id,)
        )
        stats['total_notifications'] = (await cursor.fetchone())[0] or 0
        
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND (user_reaction IS NULL OR user_reaction = '') AND is_reminder = 0",
            (user_id,)
        )
        stats['pending_notifications'] = (await cursor.fetchone())[0] or 0
        
        cursor = await conn.execute(
            "SELECT details FROM notifications WHERE user_id = ? AND details LIKE '%blockers%'",
            (user_id,)
        )
        rows = await cursor.fetchall()
        for row in rows:
            if row[0]:
                try:
                    details = json.loads(row[0])
                    stats['blockers_count'] += details.get('blockers_count', 0)
                    stats['active_blockers'] += details.get('active_blockers', 0)
                except:
                    pass
        
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM user_mentions WHERE user_id = ?",
            (user_id,)
        )
        stats['mentions_count'] = (await cursor.fetchone())[0] or 0
        
        return stats
    
    async def get_system_stats(self) -> Dict[str, Any]:
        stats = {
            'total_users': 0,
            'active_users': 0,
            'total_filters': 0,
            'active_filters': 0,
            'total_notifications': 0,
            'blockers_count': 0,
            'active_blockers': 0,
            'total_mentions': 0
        }
        conn = await self._get_conn()
        
        cursor = await conn.execute("SELECT COUNT(*) FROM users")
        stats['total_users'] = (await cursor.fetchone())[0] or 0
        
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_active = 1 AND jira_user IS NOT NULL AND jira_token IS NOT NULL"
        )
        stats['active_users'] = (await cursor.fetchone())[0] or 0
        
        cursor = await conn.execute("SELECT COUNT(*) FROM user_filters")
        stats['total_filters'] = (await cursor.fetchone())[0] or 0
        
        cursor = await conn.execute("SELECT COUNT(*) FROM user_filters WHERE is_active = 1")
        stats['active_filters'] = (await cursor.fetchone())[0] or 0
        
        cursor = await conn.execute("SELECT COUNT(*) FROM notifications")
        stats['total_notifications'] = (await cursor.fetchone())[0] or 0
        
        cursor = await conn.execute("SELECT details FROM notifications WHERE details LIKE '%blockers%'")
        rows = await cursor.fetchall()
        for row in rows:
            if row[0]:
                try:
                    details = json.loads(row[0])
                    stats['blockers_count'] += details.get('blockers_count', 0)
                    stats['active_blockers'] += details.get('active_blockers', 0)
                except:
                    pass
        
        cursor = await conn.execute("SELECT COUNT(*) FROM user_mentions")
        stats['total_mentions'] = (await cursor.fetchone())[0] or 0
        
        return stats
    
    # ------------------- Очистка данных -------------------
    async def cleanup_null_issue_data(self) -> int:
        # В некоторых окружениях (Docker + volume) на старте иногда ловится disk I/O.
        # Делаем retry + сброс соединения.
        async with self._lock:
            cursor = await self._execute_with_retry(
                "DELETE FROM issue_history WHERE issue_data IS NULL",
                (),
                commit=True,
            )
            deleted = cursor.rowcount
        if deleted:
            logger.info(f"🗑 Cleaned up {deleted} records with NULL issue_data")
        return deleted

    async def cleanup_old_data(self, days: int = 30) -> int:
        total_deleted = 0
        # Cleanup — это maintenance, он не должен валить весь бот при временных I/O/lock.
        try:
            total_deleted += await self.cleanup_null_issue_data()
            total_deleted += await self.cleanup_expired_sessions()
            total_deleted += await self.cleanup_old_issue_history(default_days=days)
            total_deleted += await self.cleanup_expired_muted_tasks()
        except Exception as e:
            logger.warning(f"⚠️ Cleanup failed (continuing): {e}")
            return total_deleted
        
        mention_cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        try:
            cursor = await self._execute_with_retry(
                "DELETE FROM user_mentions WHERE created_at < ?",
                (mention_cutoff,),
                commit=True,
            )
            total_deleted += cursor.rowcount
        except Exception as e:
            logger.warning(f"⚠️ Cleanup user_mentions failed (continuing): {e}")
        
        logger.info(f"✅ Total cleaned up {total_deleted} old records")
        return total_deleted
    

    # ------------------- Fun / Personality / Gamification -------------------
    async def get_fun_settings(self, user_id: int) -> Dict[str, Any]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT fun_mode, personality, zodiac, xp, level, streak, last_ack_date, whisper_enabled, last_whisper_date "
            "FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return {
                "fun_mode": "off",
                "personality": "neutral",
                "zodiac": None,
                "xp": 0,
                "level": 1,
                "streak": 0,
                "last_ack_date": None,
                "whisper_enabled": 1,
                "last_whisper_date": None,
            }
        return dict(row)

    async def set_fun_mode(self, user_id: int, fun_mode: str) -> None:
        fun_mode = (fun_mode or "off").strip().lower()
        if fun_mode not in ("off", "light", "full"):
            fun_mode = "off"
        conn = await self._get_conn()
        now = datetime.utcnow().isoformat()
        await conn.execute(
            "UPDATE users SET fun_mode = ?, updated_at = ? WHERE user_id = ?",
            (fun_mode, now, user_id)
        )
        await conn.commit()

    async def set_personality(self, user_id: int, personality: str) -> None:
        personality = (personality or "neutral").strip().lower()
        if personality not in ("neutral", "developer", "tester", "support"):
            personality = "neutral"
        conn = await self._get_conn()
        now = datetime.utcnow().isoformat()
        await conn.execute(
            "UPDATE users SET personality = ?, updated_at = ? WHERE user_id = ?",
            (personality, now, user_id)
        )
        await conn.commit()

    async def set_zodiac(self, user_id: int, zodiac: str) -> None:
        zodiac = (zodiac or "").strip().lower()
        conn = await self._get_conn()
        now = datetime.utcnow().isoformat()
        await conn.execute(
            "UPDATE users SET zodiac = ?, updated_at = ? WHERE user_id = ?",
            (zodiac, now, user_id)
        )
        await conn.commit()

    async def set_whisper_enabled(self, user_id: int, enabled: bool) -> None:
        conn = await self._get_conn()
        now = datetime.utcnow().isoformat()
        await conn.execute(
            "UPDATE users SET whisper_enabled = ?, updated_at = ? WHERE user_id = ?",
            (1 if enabled else 0, now, user_id)
        )
        await conn.commit()

    def _level_from_xp(self, xp: int) -> int:
        # Простая прогрессия: lvl 1 = 0..99, lvl 2 = 100..249, lvl 3 = 250..449 ...
        if xp is None:
            xp = 0
        lvl = 1
        threshold = 0
        step = 100
        while xp >= threshold + step:
            threshold += step
            step += 50  # чуть сложнее с каждым уровнем
            lvl += 1
            if lvl > 50:
                break
        return lvl

    async def add_xp(self, user_id: int, amount: int) -> Dict[str, Any]:
        amount = int(amount or 0)
        if amount <= 0:
            s = await self.get_fun_settings(user_id)
            return {"xp": s.get("xp", 0), "level": s.get("level", 1)}
        conn = await self._get_conn()
        cursor = await conn.execute("SELECT xp FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        current_xp = int(row["xp"] or 0) if row else 0
        new_xp = current_xp + amount
        new_level = self._level_from_xp(new_xp)
        now = datetime.utcnow().isoformat()
        await conn.execute(
            "UPDATE users SET xp = ?, level = ?, updated_at = ? WHERE user_id = ?",
            (new_xp, new_level, now, user_id)
        )
        await conn.commit()
        return {"xp": new_xp, "level": new_level}

    async def apply_ack_gamification(self, user_id: int) -> Dict[str, Any]:
        """Начисляет XP и поддерживает ежедневный стрик по факту подтверждения уведомления."""
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT xp, level, streak, last_ack_date FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        xp = int(row["xp"] or 0) if row else 0
        streak = int(row["streak"] or 0) if row else 0
        last_ack_date = (row["last_ack_date"] if row else None)

        today = datetime.utcnow().date()
        gain = 10

        if last_ack_date:
            try:
                last_date = datetime.fromisoformat(last_ack_date).date()
            except Exception:
                last_date = None
        else:
            last_date = None

        if last_date == today:
            # уже подтверждал сегодня: небольшой бонус
            gain = 3
        elif last_date == (today - timedelta(days=1)):
            streak += 1
            gain = 12 + min(8, streak)  # чем больше стрик, тем приятнее
        else:
            streak = 1
            gain = 10

        xp += gain
        level = self._level_from_xp(xp)
        now = datetime.utcnow().isoformat()

        await conn.execute(
            "UPDATE users SET xp = ?, level = ?, streak = ?, last_ack_date = ?, updated_at = ? WHERE user_id = ?",
            (xp, level, streak, today.isoformat(), now, user_id)
        )
        await conn.commit()

        return {"xp_gain": gain, "xp": xp, "level": level, "streak": streak}


    # ------------------- Backup -------------------
    async def backup_database(self, backup_path: str = None) -> str:
        import shutil
        if backup_path is None:
            backup_dir = os.path.join(os.path.dirname(self.db_path), "backups")
            os.makedirs(backup_dir, exist_ok=True)
            backup_path = os.path.join(backup_dir, f"jira_bot_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
        
        await self.close()
        shutil.copy2(self.db_path, backup_path)
        key_path = os.path.join(os.path.dirname(self.db_path), "encryption.key")
        if os.path.exists(key_path):
            shutil.copy2(key_path, backup_path.replace(".db", ".key"))
        await self._get_conn()
        return backup_path

from .config import ENCRYPTION_KEY, DATABASE_PATH

db = Database(db_path=DATABASE_PATH, encryption_key=ENCRYPTION_KEY)