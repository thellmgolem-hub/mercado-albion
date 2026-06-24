# -*- coding: utf-8 -*-
"""Autenticacao local pseudonima, sem e-mail e sem senhas em texto.

Seguranca:
- scrypt com salt por senha;
- tokens aleatorios; banco guarda apenas hashes;
- sessao com validade absoluta e por inatividade;
- CSRF por token rotativo;
- lockout progressivo inclusive para usernames inexistentes;
- vínculo por IP: a conta é usável de no máximo MAX_IPS_PER_ACCOUNT IPs;
- auditoria sem e-mail ou identidade real.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import string
import time
import unicodedata
from contextlib import contextmanager


ROLES = {
    "admin": "Administrador global",
    "guild_leader": "Chefe de guilda",
    "treasurer": "Tesoureiro",
    "economic_officer": "Oficial economico",
    "member": "Membro",
    "viewer": "Somente leitura",
    "external": "Colaborador externo",
}

PROFILES = {
    "gatherer": "Coletor",
    "fisher": "Pescador",
    "refiner": "Refinador",
    "crafter": "Crafter",
    "farmer": "Ilhas e agricultura",
    "breeder": "Criador de animais",
    "cook_alchemist": "Cozinha e alquimia",
    "trader": "Trader e flipper",
    "transporter": "Transportador",
    "scout": "Scout",
    "pve": "PvE",
    "pvp": "PvP e ZvZ",
}

OPERATOR_ROLES = {"admin", "guild_leader", "economic_officer", "treasurer"}
ADMIN_ROLES = {"admin"}

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
PASSWORD_MIN = 12
PASSWORD_MAX = 128
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SCRYPT_MAXMEM = 64 * 1024 * 1024
SESSION_DAYS = 7
SESSION_IDLE_HOURS = 12
MAX_IPS_PER_ACCOUNT = 2   # conta usável de no máx. 2 IPs distintos (anti-share)
MAX_LOGIN_FAILURES = 5

COMMON_PASSWORDS = {
    "password", "password123", "123456789", "1234567890", "qwerty123",
    "admin123", "albiononline", "mercadoalbion", "letmein123", "senha123",
}


class AuthError(Exception):
    def __init__(self, message="Credenciais invalidas", code="auth_error",
                 status=401):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def normalize_username(value: str) -> str:
    value = unicodedata.normalize("NFKC", (value or "").strip()).casefold()
    if not USERNAME_RE.fullmatch(value):
        raise AuthError(
            "Usuario deve ter 3-32 caracteres: letras, numeros, ponto, "
            "hifen ou sublinhado.", "invalid_username", 400)
    return value


def validate_password(password: str, username: str | None = None):
    if not isinstance(password, str) or len(password) < PASSWORD_MIN:
        raise AuthError(f"A senha deve ter ao menos {PASSWORD_MIN} caracteres.",
                        "weak_password", 400)
    if len(password) > PASSWORD_MAX:
        raise AuthError(f"A senha deve ter no maximo {PASSWORD_MAX} caracteres.",
                        "weak_password", 400)
    low = password.casefold()
    if low in COMMON_PASSWORDS or (username and username.casefold() in low):
        raise AuthError("A senha nao pode ser comum nem conter o usuario.",
                        "weak_password", 400)
    classes = sum((any(c.islower() for c in password),
                   any(c.isupper() for c in password),
                   any(c.isdigit() for c in password),
                   any(not c.isalnum() for c in password)))
    # frase-senha (16+) dispensa as 3 classes SÓ se tiver diversidade real —
    # senão 'aaaaaaaaaaaaaaaa' passava (revisão de segurança).
    if classes < 3 and not (len(password) >= 16 and len(set(password)) >= 8):
        raise AuthError(
            "Use 3 tipos de caractere ou uma frase-senha com 16+ caracteres variados.",
            "weak_password", 400)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _token_hash(token: str) -> str:
    # Cookies sao entrada nao confiavel; UTF-8 evita 500 em valor malformado.
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
        p=SCRYPT_P, dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)


def hash_password(password: str, username: str | None = None):
    validate_password(password, username)
    salt = secrets.token_bytes(16)
    digest = _password_hash(password, salt)
    return _b64(digest), _b64(salt), f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}"


def verify_password(password: str, digest_b64: str, salt_b64: str) -> bool:
    if not isinstance(password, str) or len(password) > PASSWORD_MAX:
        return False
    try:
        got = _password_hash(password, _unb64(salt_b64))
        return hmac.compare_digest(got, _unb64(digest_b64))
    except (ValueError, TypeError):
        return False


def temporary_password() -> str:
    alphabet = string.ascii_letters + string.digits + "-_!@"
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(20))
        try:
            validate_password(value)
            return value
        except AuthError:
            pass


def _clean_text(value, maximum=80):
    value = unicodedata.normalize("NFKC", str(value or "").strip())
    return value[:maximum] or None


_AUTH_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS auth_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  username_norm TEXT NOT NULL UNIQUE,
  display_name TEXT, albion_nick TEXT, discord_nick TEXT,
  role TEXT NOT NULL,
  password_hash TEXT NOT NULL, password_salt TEXT NOT NULL,
  password_algo TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  must_change_password INTEGER NOT NULL DEFAULT 1,
  session_version INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  last_login_at REAL, created_by INTEGER,
  org_id INTEGER NOT NULL DEFAULT 1,
  is_super INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_profiles (
  account_id INTEGER NOT NULL, profile TEXT NOT NULL,
  PRIMARY KEY (account_id, profile)
);
CREATE TABLE IF NOT EXISTS auth_devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL, token_hash TEXT NOT NULL,
  label TEXT, approved_at REAL NOT NULL, last_seen_at REAL,
  revoked_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_device_active_token
  ON auth_devices (account_id, token_hash);
CREATE INDEX IF NOT EXISTS idx_auth_device_account
  ON auth_devices (account_id, revoked_at);
CREATE TABLE IF NOT EXISTS auth_sessions (
  token_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL,
  csrf_hash TEXT NOT NULL, session_version INTEGER NOT NULL,
  created_at REAL NOT NULL, expires_at REAL NOT NULL,
  idle_expires_at REAL NOT NULL, last_seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_session_account
  ON auth_sessions (account_id);
CREATE TABLE IF NOT EXISTS auth_login_throttle (
  username_norm TEXT PRIMARY KEY, failures INTEGER NOT NULL,
  locked_until REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_account_id INTEGER, target_account_id INTEGER,
  action TEXT NOT NULL, details_json TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_audit_time
  ON auth_audit (created_at DESC);
"""

# Mesmo esquema em Postgres: SERIAL no lugar de AUTOINCREMENT, REAL->DOUBLE
# PRECISION. Executado statement-a-statement (PG não tem executescript).
_AUTH_SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS auth_accounts (
  id SERIAL PRIMARY KEY,
  username TEXT NOT NULL,
  username_norm TEXT NOT NULL UNIQUE,
  display_name TEXT, albion_nick TEXT, discord_nick TEXT,
  role TEXT NOT NULL,
  password_hash TEXT NOT NULL, password_salt TEXT NOT NULL,
  password_algo TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  must_change_password INTEGER NOT NULL DEFAULT 1,
  session_version INTEGER NOT NULL DEFAULT 1,
  created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
  last_login_at DOUBLE PRECISION, created_by INTEGER,
  org_id INTEGER NOT NULL DEFAULT 1,
  is_super INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_profiles (
  account_id INTEGER NOT NULL, profile TEXT NOT NULL,
  PRIMARY KEY (account_id, profile)
);
CREATE TABLE IF NOT EXISTS auth_devices (
  id SERIAL PRIMARY KEY,
  account_id INTEGER NOT NULL, token_hash TEXT NOT NULL,
  label TEXT, approved_at DOUBLE PRECISION NOT NULL, last_seen_at DOUBLE PRECISION,
  revoked_at DOUBLE PRECISION
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_device_active_token
  ON auth_devices (account_id, token_hash);
CREATE INDEX IF NOT EXISTS idx_auth_device_account
  ON auth_devices (account_id, revoked_at);
CREATE TABLE IF NOT EXISTS auth_sessions (
  token_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL,
  csrf_hash TEXT NOT NULL, session_version INTEGER NOT NULL,
  created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
  idle_expires_at DOUBLE PRECISION NOT NULL, last_seen_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_session_account
  ON auth_sessions (account_id);
CREATE TABLE IF NOT EXISTS auth_login_throttle (
  username_norm TEXT PRIMARY KEY, failures INTEGER NOT NULL,
  locked_until DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_audit (
  id SERIAL PRIMARY KEY,
  actor_account_id INTEGER, target_account_id INTEGER,
  action TEXT NOT NULL, details_json TEXT, created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_audit_time
  ON auth_audit (created_at DESC);
"""


class AuthManager:
    def __init__(self, con, lock):
        self.con = con
        self.lock = lock
        self._init_schema()

    @contextmanager
    def _tx(self):
        with self.lock:
            try:
                yield self.con
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise

    def _init_schema(self):
        with self._tx() as con:
            if getattr(con, "backend", "sqlite") == "sqlite":
                con.executescript(_AUTH_SCHEMA_SQLITE)
            else:
                for stmt in _AUTH_SCHEMA_PG.split(";"):
                    if stmt.strip():
                        con.execute(stmt)
        self._migrate_org_columns()
        self._revoke_legacy_devices()

    def _migrate_org_columns(self):
        """Idempotente: base viva ganha org_id/is_super em auth_accounts e o
        admin-dono é promovido a super (a nuvem já tinha admin SEM is_super antes
        do multi-inquilino, então o DEFAULT 0 deixaria ninguém super)."""
        from . import store  # import tardio: evita ciclo de importação
        with self.lock:
            store.add_column(self.con, "auth_accounts",
                             "org_id INTEGER NOT NULL DEFAULT 1")
            store.add_column(self.con, "auth_accounts",
                             "is_super INTEGER NOT NULL DEFAULT 0")
        with self._tx() as con:
            if not con.execute("SELECT 1 FROM auth_accounts WHERE is_super=1 "
                               "LIMIT 1").fetchone():
                con.execute(
                    "UPDATE auth_accounts SET is_super=1 WHERE id=("
                    "SELECT id FROM auth_accounts WHERE role='admin' "
                    "AND active=1 ORDER BY id LIMIT 1)")

    def _revoke_legacy_devices(self):
        """Migração idempotente: registros LEGADOS de dispositivo (label não é um
        IP) seriam contados no limite de IPs e nunca casariam num login — revoga.
        Numa base nova (nuvem) não há nada a fazer."""
        try:
            with self._tx() as con:
                rows = con.execute(
                    "SELECT id,label FROM auth_devices "
                    "WHERE revoked_at IS NULL").fetchall()
                now = time.time()
                for rid, label in rows:
                    try:
                        ipaddress.ip_address((label or "").strip())
                    except ValueError:
                        con.execute("UPDATE auth_devices SET revoked_at=? "
                                    "WHERE id=?", [now, rid])
        except Exception:
            pass   # nunca derruba o boot por causa da migração

    @staticmethod
    def _insert_id(con, sql, params):
        """INSERT devolvendo o id gerado: lastrowid (SQLite) / RETURNING (PG)."""
        if getattr(con, "backend", "sqlite") == "sqlite":
            return con.execute(sql, params).lastrowid
        return con.execute(sql + " RETURNING id", params).fetchone()[0]

    def _audit(self, con, action, actor_id=None, target_id=None, details=None):
        safe = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)
        con.execute(
            "INSERT INTO auth_audit "
            "(actor_account_id,target_account_id,action,details_json,created_at) "
            "VALUES (?,?,?,?,?)",
            [actor_id, target_id, action, safe[:2000], time.time()])

    @contextmanager
    def _read(self):
        """Leitura que ENCERRA a transação implícita ao sair. No Postgres
        (conexão gravável, autocommit=False) um SELECT abre transação que
        ficaria 'idle in transaction' até commit/rollback — aqui damos rollback
        ao final (sem efeito, é só-leitura) para não reter o backend do pooler.
        No SQLite, um SELECT puro não abre transação: rollback é no-op."""
        with self.lock:
            try:
                yield self.con
            finally:
                try:
                    self.con.rollback()
                except Exception:
                    pass

    def has_admin(self) -> bool:
        with self._read() as con:
            row = con.execute(
                "SELECT 1 FROM auth_accounts WHERE role='admin' AND active=1 "
                "LIMIT 1").fetchone()
        return bool(row)

    def account_count(self) -> int:
        with self._read() as con:
            return con.execute(
                "SELECT COUNT(*) FROM auth_accounts").fetchone()[0]

    def _profiles(self, con, account_id):
        return [r[0] for r in con.execute(
            "SELECT profile FROM auth_profiles WHERE account_id=? "
            "ORDER BY profile", [account_id])]

    def _account_dict(self, con, row):
        if row is None:
            return None
        keys = ["id", "username", "display_name", "albion_nick",
                "discord_nick", "role", "active", "must_change_password",
                "created_at", "updated_at", "last_login_at",
                "org_id", "is_super"]
        data = dict(zip(keys, row[:len(keys)]))
        data["active"] = bool(data["active"])
        data["must_change_password"] = bool(data["must_change_password"])
        data["is_super"] = bool(data["is_super"])
        data["org_id"] = int(data["org_id"])
        data["profiles"] = self._profiles(con, data["id"])
        rows = con.execute(
            "SELECT label, last_seen_at FROM auth_devices "
            "WHERE account_id=? AND revoked_at IS NULL ORDER BY last_seen_at DESC",
            [data["id"]]).fetchall()
        data["ips"] = [{"ip": r[0], "last_seen_at": r[1]} for r in rows]
        return data

    @staticmethod
    def _account_select():
        return ("SELECT id,username,display_name,albion_nick,discord_nick,role,"
                "active,must_change_password,created_at,updated_at,last_login_at,"
                "org_id,is_super "
                "FROM auth_accounts")

    def bootstrap_admin(self, username: str, display_name: str | None = None):
        norm = normalize_username(username)
        pwd = temporary_password()
        digest, salt, algo = hash_password(pwd, norm)
        now = time.time()
        with self._tx() as con:
            if con.execute("SELECT 1 FROM auth_accounts LIMIT 1").fetchone():
                raise AuthError("Bootstrap bloqueado: ja existem contas.",
                                "bootstrap_closed", 409)
            account_id = self._insert_id(con, """
                INSERT INTO auth_accounts
                  (username,username_norm,display_name,role,password_hash,
                   password_salt,password_algo,created_at,updated_at,
                   org_id,is_super)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, [norm, norm, _clean_text(display_name) or "Administrador",
                   "admin", digest, salt, algo, now, now, 1, 1])
            self._audit(con, "bootstrap_admin", account_id, account_id,
                        {"username": norm})
        return {"account_id": account_id, "username": norm,
                "temporary_password": pwd}

    def create_account(self, actor_id: int, username: str, role="member",
                       profiles=None, display_name=None, albion_nick=None,
                       discord_nick=None):
        norm = normalize_username(username)
        if role not in ROLES:
            raise AuthError("Papel invalido.", "invalid_role", 400)
        profile_set = sorted(set(profiles or []))
        invalid = [p for p in profile_set if p not in PROFILES]
        if invalid:
            raise AuthError("Perfis invalidos: " + ", ".join(invalid),
                            "invalid_profile", 400)
        pwd = temporary_password()
        digest, salt, algo = hash_password(pwd, norm)
        now = time.time()
        with self._tx() as con:
            arow = con.execute("SELECT org_id FROM auth_accounts WHERE id=?",
                               [actor_id]).fetchone()
            org_id = int(arow[0]) if arow else 1  # nova conta nasce na org do actor
            try:
                account_id = self._insert_id(con, """
                    INSERT INTO auth_accounts
                      (username,username_norm,display_name,albion_nick,
                       discord_nick,role,password_hash,password_salt,
                       password_algo,created_at,updated_at,created_by,org_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, [norm, norm, _clean_text(display_name),
                       _clean_text(albion_nick), _clean_text(discord_nick),
                       role, digest, salt, algo, now, now, actor_id, org_id])
            except Exception as exc:
                s = str(exc).upper()
                if "UNIQUE" in s or "DUPLICATE KEY" in s:
                    raise AuthError("Esse usuario ja existe.",
                                    "username_exists", 409) from exc
                raise
            con.executemany(
                "INSERT INTO auth_profiles (account_id,profile) VALUES (?,?)",
                [(account_id, p) for p in profile_set])
            self._audit(con, "account_created", actor_id, account_id,
                        {"username": norm, "role": role,
                         "profiles": profile_set})
        return {"account_id": account_id, "username": norm,
                "temporary_password": pwd}

    def list_accounts(self):
        with self._read() as con:
            rows = con.execute(
                self._account_select() + " ORDER BY active DESC, username_norm"
            ).fetchall()
            return [self._account_dict(con, r) for r in rows]

    def get_account(self, account_id):
        with self._read() as con:
            row = con.execute(
                self._account_select() + " WHERE id=?", [account_id]).fetchone()
            return self._account_dict(con, row)

    def update_account(self, actor_id, account_id, *, role=None, active=None,
                       profiles=None, display_name=None, albion_nick=None,
                       discord_nick=None):
        with self._tx() as con:
            current = con.execute(
                "SELECT role,active FROM auth_accounts WHERE id=?",
                [account_id]).fetchone()
            if not current:
                raise AuthError("Conta nao encontrada.", "not_found", 404)
            new_role = role if role is not None else current[0]
            new_active = int(bool(active)) if active is not None else current[1]
            if new_role not in ROLES:
                raise AuthError("Papel invalido.", "invalid_role", 400)
            if account_id == actor_id and (new_role != "admin" or not new_active):
                raise AuthError("O admin nao pode remover o proprio acesso.",
                                "self_lockout", 400)
            if current[0] == "admin" and current[1] and (
                    new_role != "admin" or not new_active):
                others = con.execute(
                    "SELECT COUNT(*) FROM auth_accounts WHERE role='admin' "
                    "AND active=1 AND id<>?", [account_id]).fetchone()[0]
                if not others:
                    raise AuthError(
                        "A plataforma precisa manter um administrador ativo.",
                        "last_admin", 400)
            fields, values = ["role=?", "active=?", "updated_at=?"], [
                new_role, new_active, time.time()]
            for col, value in (("display_name", display_name),
                               ("albion_nick", albion_nick),
                               ("discord_nick", discord_nick)):
                if value is not None:
                    fields.append(f"{col}=?")
                    values.append(_clean_text(value))
            if role is not None or active is not None:
                fields.append("session_version=session_version+1")
            values.append(account_id)
            con.execute("UPDATE auth_accounts SET " + ",".join(fields) +
                        " WHERE id=?", values)
            if profiles is not None:
                profile_set = sorted(set(profiles))
                invalid = [p for p in profile_set if p not in PROFILES]
                if invalid:
                    raise AuthError("Perfis invalidos: " + ", ".join(invalid),
                                    "invalid_profile", 400)
                con.execute("DELETE FROM auth_profiles WHERE account_id=?",
                            [account_id])
                con.executemany(
                    "INSERT INTO auth_profiles (account_id,profile) VALUES (?,?)",
                    [(account_id, p) for p in profile_set])
            if role is not None or active is not None:
                con.execute("DELETE FROM auth_sessions WHERE account_id=?",
                            [account_id])
            self._audit(con, "account_updated", actor_id, account_id,
                        {"role": new_role, "active": bool(new_active),
                         "profiles": profiles})
        return self.get_account(account_id)

    def reset_password(self, actor_id, account_id):
        pwd = temporary_password()
        with self._tx() as con:
            row = con.execute(
                "SELECT username_norm FROM auth_accounts WHERE id=?",
                [account_id]).fetchone()
            if not row:
                raise AuthError("Conta nao encontrada.", "not_found", 404)
            digest, salt, algo = hash_password(pwd, row[0])
            con.execute("""
                UPDATE auth_accounts SET password_hash=?,password_salt=?,
                  password_algo=?,must_change_password=1,
                  session_version=session_version+1,updated_at=? WHERE id=?
            """, [digest, salt, algo, time.time(), account_id])
            con.execute("DELETE FROM auth_sessions WHERE account_id=?",
                        [account_id])
            self._audit(con, "password_reset", actor_id, account_id)
        return {"temporary_password": pwd}

    def reset_device(self, actor_id, account_id):
        with self._tx() as con:
            if not con.execute("SELECT 1 FROM auth_accounts WHERE id=?",
                               [account_id]).fetchone():
                raise AuthError("Conta nao encontrada.", "not_found", 404)
            now = time.time()
            con.execute("UPDATE auth_devices SET revoked_at=? "
                        "WHERE account_id=? AND revoked_at IS NULL",
                        [now, account_id])
            con.execute("DELETE FROM auth_sessions WHERE account_id=?",
                        [account_id])
            con.execute("UPDATE auth_accounts SET "
                        "session_version=session_version+1,updated_at=? "
                        "WHERE id=?", [now, account_id])
            self._audit(con, "ip_reset", actor_id, account_id)

    def _throttle(self, con, norm, now):
        """Estado do throttle (failures, locked) — NÃO levanta: a senha CORRETA
        nunca pode ser recusada por lock (senão um atacante DoS-a a conta
        trancando-a por username). O lock só bloqueia tentativas INVÁLIDAS."""
        row = con.execute(
            "SELECT failures,locked_until FROM auth_login_throttle "
            "WHERE username_norm=?", [norm]).fetchone()
        return (row[0] if row else 0, bool(row and row[1] > now))

    def _failed_login(self, con, norm, failures, now):
        failures += 1
        lock_seconds = (min(3600, 60 * (2 ** (failures - MAX_LOGIN_FAILURES)))
                        if failures >= MAX_LOGIN_FAILURES else 0)
        con.execute("""
            INSERT INTO auth_login_throttle
              (username_norm,failures,locked_until,updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(username_norm) DO UPDATE SET failures=excluded.failures,
              locked_until=excluded.locked_until,updated_at=excluded.updated_at
        """, [norm, failures, now + lock_seconds, now])

    def login(self, username, password, ip=None):
        try:
            norm = normalize_username(username)
        except AuthError:
            # sentinela impossível pela regex (começa com \x00): username
            # malformado não pode colidir com a conta legítima 'invalid'.
            norm = "\x00malformed"
        now = time.time()
        with self._tx() as con:
            failures, locked = self._throttle(con, norm, now)
            row = con.execute("""
                SELECT id,username_norm,password_hash,password_salt,active,
                       session_version FROM auth_accounts WHERE username_norm=?
            """, [norm]).fetchone()
            # SEMPRE roda exatamente um scrypt no caminho de credencial inválida
            # (conta ativa, desativada OU inexistente) — sem oráculo de timing.
            if row is not None:
                pw_ok = verify_password(password or "", row[2], row[3])
            else:
                _password_hash((password or "")[:PASSWORD_MAX], b"albion-auth-fake")
                pw_ok = False
            valid = bool(row and row[4] and pw_ok)
            if not valid:
                self._failed_login(con, norm, failures, now)
                self._audit(con, "login_failed", None,
                            row[0] if row else None)
                # O AuthError faz o context manager dar rollback; persiste o
                # throttle antes de devolver a resposta negativa.
                con.commit()
                if locked:
                    raise AuthError("Muitas tentativas. Aguarde antes de "
                                    "tentar novamente.", "login_locked", 429)
                raise AuthError("Usuario ou senha invalidos.",
                                "invalid_credentials", 401)
            account_id, version = row[0], row[5]
            con.execute("DELETE FROM auth_login_throttle WHERE username_norm=?",
                        [norm])
            # Limite de IPs por conta (anti-compartilhamento). auth_devices é
            # reusada como registro de IPs: token_hash = hash do IP, label = IP.
            ip = (ip or "0.0.0.0")[:45]
            ip_hash = _token_hash(ip)
            registered = con.execute(
                "SELECT id,token_hash FROM auth_devices "
                "WHERE account_id=? AND revoked_at IS NULL", [account_id]
            ).fetchall()
            matched = next((d for d in registered if hmac.compare_digest(
                d[1], ip_hash)), None)
            if matched:
                con.execute("UPDATE auth_devices SET last_seen_at=? WHERE id=?",
                            [now, matched[0]])
            elif len(registered) < MAX_IPS_PER_ACCOUNT:
                self._insert_id(con, """
                    INSERT INTO auth_devices
                      (account_id,token_hash,label,approved_at,last_seen_at)
                    VALUES (?,?,?,?,?)
                """, [account_id, ip_hash, ip, now, now])
            else:
                self._audit(con, "ip_limit", account_id, account_id)
                con.commit()
                raise AuthError(
                    f"Limite de {MAX_IPS_PER_ACCOUNT} IPs atingido para esta "
                    "conta. Peça ao administrador para liberar os IPs.",
                    "ip_limit", 403)
            # Uma sessao ativa por conta; abas compartilham o cookie.
            con.execute("DELETE FROM auth_sessions WHERE account_id=?",
                        [account_id])
            session_token = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(24)
            con.execute("""
                INSERT INTO auth_sessions
                  (token_hash,account_id,csrf_hash,session_version,created_at,
                   expires_at,idle_expires_at,last_seen_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, [_token_hash(session_token), account_id,
                   _token_hash(csrf_token), version, now,
                   now + SESSION_DAYS * 86400,
                   now + SESSION_IDLE_HOURS * 3600, now])
            con.execute("UPDATE auth_accounts SET last_login_at=?,updated_at=? "
                        "WHERE id=?", [now, now, account_id])
            self._audit(con, "login_success", account_id, account_id,
                        {"ip": ip, "new_ip": matched is None})
        account = self.get_account(account_id)
        return {"session_token": session_token, "csrf_token": csrf_token,
                "account": account}

    def authenticate(self, session_token, ip=None):
        if not session_token:
            raise AuthError("Sessao ausente.", "session_missing", 401)
        now = time.time()
        token_hash = _token_hash(session_token)
        with self._tx() as con:
            row = con.execute("""
                SELECT s.account_id,s.csrf_hash,s.session_version,s.expires_at,
                       s.idle_expires_at,s.last_seen_at,a.active,a.session_version
                FROM auth_sessions s JOIN auth_accounts a ON a.id=s.account_id
                WHERE s.token_hash=?
            """, [token_hash]).fetchone()
            if (not row or not row[6] or row[2] != row[7]
                    or row[3] <= now or row[4] <= now):
                con.execute("DELETE FROM auth_sessions WHERE token_hash=?",
                            [token_hash])
                raise AuthError("Sessao expirada. Entre novamente.",
                                "session_expired", 401)
            # Vínculo por IP verificado A CADA requisição: a sessão deixa de ser
            # bearer puro — um albion_session roubado e usado de um IP fora dos
            # ≤2 registrados é rejeitado (e força novo login, que respeita o
            # limite de IPs).
            ips = con.execute(
                "SELECT token_hash FROM auth_devices WHERE account_id=? "
                "AND revoked_at IS NULL", [row[0]]).fetchall()
            if ips:
                supplied = _token_hash((ip or "0.0.0.0")[:45])   # default igual ao login()
                if not any(hmac.compare_digest(d[0], supplied) for d in ips):
                    raise AuthError("IP nao reconhecido para esta sessao. Entre "
                                    "novamente.", "ip_unrecognized", 401)
            if now - row[5] >= 300:
                con.execute("UPDATE auth_sessions SET last_seen_at=?,"
                            "idle_expires_at=? WHERE token_hash=?",
                            [now, now + SESSION_IDLE_HOURS * 3600, token_hash])
            account_row = con.execute(
                self._account_select() + " WHERE id=?", [row[0]]).fetchone()
            account = self._account_dict(con, account_row)
        return {"token_hash": token_hash, "account_id": row[0],
                "csrf_hash": row[1], "account": account}

    def rotate_csrf(self, session_token):
        token_hash = _token_hash(session_token)
        csrf_token = secrets.token_urlsafe(24)
        with self._tx() as con:
            if not con.execute("SELECT 1 FROM auth_sessions WHERE token_hash=?",
                               [token_hash]).fetchone():
                raise AuthError("Sessao expirada.", "session_expired", 401)
            con.execute("UPDATE auth_sessions SET csrf_hash=? WHERE token_hash=?",
                        [_token_hash(csrf_token), token_hash])
        return csrf_token

    @staticmethod
    def verify_csrf(auth_context, csrf_token):
        return bool(csrf_token and hmac.compare_digest(
            auth_context["csrf_hash"], _token_hash(csrf_token)))

    def logout(self, session_token, actor_id=None):
        with self._tx() as con:
            con.execute("DELETE FROM auth_sessions WHERE token_hash=?",
                        [_token_hash(session_token)])
            self._audit(con, "logout", actor_id, actor_id)

    def change_password(self, account_id, current_password, new_password):
        with self._tx() as con:
            row = con.execute("""
                SELECT username_norm,password_hash,password_salt
                FROM auth_accounts WHERE id=? AND active=1
            """, [account_id]).fetchone()
            if not row or not verify_password(current_password, row[1], row[2]):
                raise AuthError("Senha atual incorreta.",
                                "invalid_current_password", 403)
            # a nova senha não pode ser igual à atual (anulava a troca forçada:
            # dava pra "trocar" a senha temporária por ela mesma).
            if verify_password(new_password, row[1], row[2]):
                raise AuthError("A nova senha deve ser diferente da atual.",
                                "password_reuse", 400)
            digest, salt, algo = hash_password(new_password, row[0])
            con.execute("""
                UPDATE auth_accounts SET password_hash=?,password_salt=?,
                  password_algo=?,must_change_password=0,
                  session_version=session_version+1,updated_at=? WHERE id=?
            """, [digest, salt, algo, time.time(), account_id])
            con.execute("DELETE FROM auth_sessions WHERE account_id=?",
                        [account_id])
            self._audit(con, "password_changed", account_id, account_id)

    def audit_log(self, limit=200):
        limit = max(1, min(int(limit), 1000))
        with self._read() as con:
            rows = con.execute("""
                SELECT l.id,l.created_at,l.action,l.actor_account_id,
                       aa.username,l.target_account_id,ta.username,l.details_json
                FROM auth_audit l
                LEFT JOIN auth_accounts aa ON aa.id=l.actor_account_id
                LEFT JOIN auth_accounts ta ON ta.id=l.target_account_id
                ORDER BY l.id DESC LIMIT ?
            """, [limit]).fetchall()
        return [{"id": r[0], "created_at": r[1], "action": r[2],
                 "actor_id": r[3], "actor": r[4], "target_id": r[5],
                 "target": r[6], "details": json.loads(r[7] or "{}")}
                for r in rows]

    def cleanup(self):
        now = time.time()
        with self._tx() as con:
            con.execute("DELETE FROM auth_sessions WHERE expires_at<=? "
                        "OR idle_expires_at<=?", [now, now])
            con.execute("DELETE FROM auth_login_throttle "
                        "WHERE updated_at<?", [now - 7 * 86400])
