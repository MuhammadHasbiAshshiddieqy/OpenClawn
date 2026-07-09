"""Multi-user + RBAC per tenant (TODO.md § Prioritas 5, revisi eksplisit CLAUDE.md §7).

Role AKSES (`admin`/`member`/`viewer`) berbeda dari `role` fungsional (pm/qa/dev/
data/security = persona agent) yang dipakai di seluruh proyek lain — sengaja
dinamai `access_role` di kolom DB dan parameter di sini untuk menghindari
ambiguitas nama.

Identitas (`subject`) dua sumber:
- Shared-secret login (security/auth.py) → SELALU subject tetap `SHARED_SECRET_SUBJECT`,
  role admin (satu-satunya user shared-secret, dibuat otomatis saat pertama login).
- OIDC login (security/oidc.py) → subject = klaim `sub` provider, unik per akun.

Bootstrap: user OIDC PERTAMA yang login untuk satu tenant otomatis jadi admin
(tak ada admin lain untuk mengangkatnya). User berikutnya default `member`.
"""

from dataclasses import dataclass

from infra.database import DatabaseManager

SHARED_SECRET_SUBJECT = "shared-secret"

ACCESS_ROLES = ("admin", "member", "viewer")
# Urutan hierarki (index LEBIH TINGGI = akses lebih luas): viewer < member < admin.
# Dipakai role_at_least untuk cek "minimal role X", bukan sekadar exact match.
# Sengaja terpisah dari urutan ACCESS_ROLES (yang ditulis admin-dulu agar wajar
# dibaca manusia/UI) — jangan disatukan lagi, itu penyebab bug index terbalik.
_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}


@dataclass(frozen=True)
class User:
    id: int
    tenant_id: str
    subject: str
    email: str | None
    name: str | None
    access_role: str


def role_at_least(access_role: str, minimum: str) -> bool:
    """True bila `access_role` setara atau lebih tinggi dari `minimum` dalam hierarki
    viewer < member < admin. Role tak dikenal (data korup) → fail-safe False (paling ketat)."""
    if access_role not in _ROLE_RANK or minimum not in _ROLE_RANK:
        return False
    return _ROLE_RANK[access_role] >= _ROLE_RANK[minimum]


class UserStore:
    """CRUD user + bootstrap admin pertama, di-scope per tenant."""

    def __init__(self, db: DatabaseManager, tenant_id: str = "default"):
        self.db = db
        self.tenant_id = tenant_id

    async def get_by_subject(self, subject: str) -> User | None:
        row = await self.db.fetchone(
            "SELECT id, tenant_id, subject, email, name, access_role FROM users "
            "WHERE tenant_id=? AND subject=?",
            (self.tenant_id, subject),
        )
        return User(**row) if row else None

    async def get_by_id(self, user_id: int) -> User | None:
        row = await self.db.fetchone(
            "SELECT id, tenant_id, subject, email, name, access_role FROM users "
            "WHERE id=? AND tenant_id=?",
            (user_id, self.tenant_id),
        )
        return User(**row) if row else None

    async def upsert_on_login(
        self, subject: str, email: str | None = None, name: str | None = None
    ) -> User:
        """Dipanggil tiap login sukses (shared-secret ATAU OIDC). Idempoten:
        user baru → INSERT (bootstrap admin bila tenant ini belum punya user
        sama sekali, else default 'member'); user existing → UPDATE
        email/name/last_login_at, `access_role` TAK PERNAH ditimpa di sini
        (perubahan role hanya lewat `set_access_role`, admin action eksplisit)."""
        existing = await self.get_by_subject(subject)
        if existing:
            await self.db.execute(
                "UPDATE users SET email=?, name=?, last_login_at=CURRENT_TIMESTAMP WHERE id=?",
                (email, name, existing.id),
            )
            return await self.get_by_id(existing.id)

        is_first_user = (
            await self.db.fetchone(
                "SELECT COUNT(*) AS n FROM users WHERE tenant_id=?", (self.tenant_id,)
            )
        )["n"] == 0
        bootstrap_role = "admin" if is_first_user else "member"
        cursor = await self.db.execute(
            """INSERT INTO users (tenant_id, subject, email, name, access_role, last_login_at)
               VALUES (?,?,?,?,?, CURRENT_TIMESTAMP)""",
            (self.tenant_id, subject, email, name, bootstrap_role),
        )
        return await self.get_by_id(cursor.lastrowid)

    async def set_access_role(self, user_id: int, access_role: str) -> bool:
        """Ubah role akses user — admin action. Role tak dikenal → ditolak (False),
        tak crash. Return True bila berhasil (user ada & role valid)."""
        if access_role not in ACCESS_ROLES:
            return False
        cursor = await self.db.execute(
            "UPDATE users SET access_role=? WHERE id=? AND tenant_id=?",
            (access_role, user_id, self.tenant_id),
        )
        return cursor.rowcount > 0

    async def list_users(self) -> list[User]:
        rows = await self.db.fetchall(
            "SELECT id, tenant_id, subject, email, name, access_role FROM users "
            "WHERE tenant_id=? ORDER BY id",
            (self.tenant_id,),
        )
        return [User(**r) for r in rows]
