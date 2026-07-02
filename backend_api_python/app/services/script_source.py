"""Script source library service.

Script sources are reusable code assets. Runtime/live strategies reference a
source by id and keep market, account, notification, and risk settings in
``qd_strategies_trading``.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _json_dump(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False)


class ScriptSourceService:
    """CRUD and delivery helpers for script strategy source code."""

    def ensure_schema(self) -> None:
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS qd_script_sources (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
                        name VARCHAR(255) NOT NULL,
                        description TEXT DEFAULT '',
                        code TEXT NOT NULL DEFAULT '',
                        template_key VARCHAR(80) DEFAULT '',
                        param_schema JSONB DEFAULT '{}'::jsonb,
                        source_marketplace_indicator_id INTEGER,
                        source_script_source_id INTEGER,
                        visibility VARCHAR(32) DEFAULT 'private',
                        status VARCHAR(32) DEFAULT 'draft',
                        metadata JSONB DEFAULT '{}'::jsonb,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_script_sources_user_id ON qd_script_sources(user_id)")
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_script_sources_marketplace ON qd_script_sources(source_marketplace_indicator_id)"
                )
                self._ensure_version_schema(cur)
                db.commit()
                cur.close()
        except Exception as exc:
            logger.warning("script source schema ensure failed: %s", exc)

    def _ensure_version_schema(self, cur) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS qd_script_source_versions (
                id SERIAL PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES qd_script_sources(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
                version_no INTEGER NOT NULL,
                name VARCHAR(255) NOT NULL DEFAULT '',
                description TEXT DEFAULT '',
                code TEXT NOT NULL DEFAULT '',
                template_key VARCHAR(80) DEFAULT '',
                param_schema JSONB DEFAULT '{}'::jsonb,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_script_source_versions_source
            ON qd_script_source_versions (source_id, version_no DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_script_source_versions_user
            ON qd_script_source_versions (user_id)
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_script_source_versions_no
            ON qd_script_source_versions (source_id, version_no)
            """
        )

    def _insert_version(
        self,
        cur,
        source_id: int,
        user_id: int,
        name: str,
        description: str,
        code: str,
        template_key: str,
        param_schema: Any,
        metadata: Any,
    ) -> int:
        self._ensure_version_schema(cur)
        cur.execute(
            """
            SELECT COALESCE(MAX(version_no), 0) + 1 AS next_version
            FROM qd_script_source_versions
            WHERE source_id = ? AND user_id = ?
            """,
            (int(source_id), int(user_id)),
        )
        row = cur.fetchone() or {}
        version_no = int(row.get("next_version") or 1)
        cur.execute(
            """
            INSERT INTO qd_script_source_versions
              (source_id, user_id, version_no, name, description, code,
               template_key, param_schema, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?::jsonb, NOW())
            """,
            (
                int(source_id),
                int(user_id),
                version_no,
                name or "",
                description or "",
                code or "",
                template_key or "",
                _json_dump(param_schema),
                _json_dump(metadata),
            ),
        )
        return version_no

    def _row(self, row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        item = dict(row)
        item["param_schema"] = _json_dict(item.get("param_schema"))
        item["metadata"] = _json_dict(item.get("metadata"))
        return item

    def _version_row(self, row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        item = dict(row)
        if "param_schema" in item:
            item["param_schema"] = _json_dict(item.get("param_schema"))
        if "metadata" in item:
            item["metadata"] = _json_dict(item.get("metadata"))
        return item

    def list_sources(self, user_id: int) -> List[Dict[str, Any]]:
        self.ensure_schema()
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT id, user_id, name, description, code, template_key, param_schema,
                       source_marketplace_indicator_id, source_script_source_id,
                       visibility, status, metadata, created_at, updated_at
                FROM qd_script_sources
                WHERE user_id = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (int(user_id),),
            )
            rows = cur.fetchall()
            cur.close()
        return [self._row(row) for row in rows if row]

    def get_source(self, source_id: int, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        self.ensure_schema()
        with get_db_connection() as db:
            cur = db.cursor()
            if user_id is None:
                cur.execute(
                    """
                    SELECT id, user_id, name, description, code, template_key, param_schema,
                           source_marketplace_indicator_id, source_script_source_id,
                           visibility, status, metadata, created_at, updated_at
                    FROM qd_script_sources
                    WHERE id = ?
                    """,
                    (int(source_id),),
                )
            else:
                cur.execute(
                    """
                    SELECT id, user_id, name, description, code, template_key, param_schema,
                           source_marketplace_indicator_id, source_script_source_id,
                           visibility, status, metadata, created_at, updated_at
                    FROM qd_script_sources
                    WHERE id = ? AND user_id = ?
                    """,
                    (int(source_id), int(user_id)),
                )
            row = cur.fetchone()
            cur.close()
        return self._row(row)

    def create_source(self, payload: Dict[str, Any]) -> int:
        self.ensure_schema()
        user_id = int(payload.get("user_id") or 1)
        name = str(payload.get("name") or payload.get("strategy_name") or "Untitled Script").strip() or "Untitled Script"
        code = str(payload.get("code") or payload.get("strategy_code") or "")
        description = str(payload.get("description") or "")
        template_key = str(payload.get("template_key") or payload.get("templateKey") or "")
        param_schema = payload.get("param_schema") or payload.get("paramSchema") or {}
        metadata = payload.get("metadata") or {}
        source_marketplace_indicator_id = payload.get("source_marketplace_indicator_id") or payload.get("sourceMarketplaceIndicatorId")
        source_script_source_id = payload.get("source_script_source_id") or payload.get("sourceScriptSourceId")

        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                INSERT INTO qd_script_sources
                  (user_id, name, description, code, template_key, param_schema,
                   source_marketplace_indicator_id, source_script_source_id,
                   visibility, status, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?::jsonb, ?, ?, ?, ?, ?::jsonb, NOW(), NOW())
                """,
                (
                    user_id,
                    name,
                    description,
                    code,
                    template_key,
                    _json_dump(param_schema),
                    int(source_marketplace_indicator_id) if source_marketplace_indicator_id else None,
                    int(source_script_source_id) if source_script_source_id else None,
                    str(payload.get("visibility") or "private"),
                    str(payload.get("status") or "draft"),
                    _json_dump(metadata),
                ),
            )
            new_id = int(cur.lastrowid or 0)
            self._insert_version(
                cur,
                new_id,
                user_id,
                name,
                description,
                code,
                template_key,
                param_schema,
                metadata,
            )
            db.commit()
            cur.close()
        return new_id

    def update_source(self, source_id: int, user_id: int, payload: Dict[str, Any]) -> bool:
        self.ensure_schema()
        existing = self.get_source(source_id, user_id=user_id)
        if not existing:
            return False
        name = str(payload.get("name") or payload.get("strategy_name") or existing.get("name") or "Untitled Script").strip()
        code = str(payload.get("code") if payload.get("code") is not None else payload.get("strategy_code", existing.get("code") or ""))
        description = str(payload.get("description") if payload.get("description") is not None else existing.get("description") or "")
        template_key = str(payload.get("template_key") or payload.get("templateKey") or existing.get("template_key") or "")
        param_schema = payload.get("param_schema") if "param_schema" in payload else payload.get("paramSchema", existing.get("param_schema") or {})
        metadata = payload.get("metadata") if "metadata" in payload else existing.get("metadata") or {}

        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                UPDATE qd_script_sources
                SET name = ?, description = ?, code = ?, template_key = ?,
                    param_schema = ?::jsonb, metadata = ?::jsonb, updated_at = NOW()
                WHERE id = ? AND user_id = ?
                """,
                (
                    name,
                    description,
                    code,
                    template_key,
                    _json_dump(param_schema),
                    _json_dump(metadata),
                    int(source_id),
                    int(user_id),
                ),
            )
            ok = cur.rowcount > 0
            if ok:
                self._insert_version(
                    cur,
                    source_id,
                    user_id,
                    name,
                    description,
                    code,
                    template_key,
                    param_schema,
                    metadata,
                )
            db.commit()
            cur.close()
        return ok

    def list_versions(self, source_id: int, user_id: int) -> tuple[bool, List[Dict[str, Any]]]:
        self.ensure_schema()
        with get_db_connection() as db:
            cur = db.cursor()
            self._ensure_version_schema(cur)
            cur.execute("SELECT id FROM qd_script_sources WHERE id = ? AND user_id = ?", (int(source_id), int(user_id)))
            if not cur.fetchone():
                cur.close()
                return False, []
            cur.execute(
                """
                SELECT id, source_id, user_id, version_no, name, description, template_key, created_at
                FROM qd_script_source_versions
                WHERE source_id = ? AND user_id = ?
                ORDER BY version_no DESC
                LIMIT 100
                """,
                (int(source_id), int(user_id)),
            )
            rows = cur.fetchall() or []
            db.commit()
            cur.close()
        return True, [self._version_row(row) for row in rows if row]

    def get_version(self, version_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        self.ensure_schema()
        with get_db_connection() as db:
            cur = db.cursor()
            self._ensure_version_schema(cur)
            cur.execute(
                """
                SELECT id, source_id, user_id, version_no, name, description, code,
                       template_key, param_schema, metadata, created_at
                FROM qd_script_source_versions
                WHERE id = ? AND user_id = ?
                """,
                (int(version_id), int(user_id)),
            )
            row = cur.fetchone()
            db.commit()
            cur.close()
        return self._version_row(row)

    def restore_version(self, version_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        self.ensure_schema()
        with get_db_connection() as db:
            cur = db.cursor()
            self._ensure_version_schema(cur)
            cur.execute(
                """
                SELECT v.source_id, v.name, v.description, v.code, v.template_key,
                       v.param_schema, v.metadata
                FROM qd_script_source_versions v
                JOIN qd_script_sources s ON s.id = v.source_id
                WHERE v.id = ? AND v.user_id = ? AND s.user_id = ?
                """,
                (int(version_id), int(user_id), int(user_id)),
            )
            row = self._version_row(cur.fetchone())
            if not row:
                cur.close()
                return None

            source_id = int(row.get("source_id") or 0)
            name = row.get("name") or "Untitled Script"
            description = row.get("description") or ""
            code = row.get("code") or ""
            template_key = row.get("template_key") or ""
            param_schema = row.get("param_schema") or {}
            metadata = row.get("metadata") or {}
            cur.execute(
                """
                UPDATE qd_script_sources
                SET name = ?, description = ?, code = ?, template_key = ?,
                    param_schema = ?::jsonb, metadata = ?::jsonb, updated_at = NOW()
                WHERE id = ? AND user_id = ?
                """,
                (
                    name,
                    description,
                    code,
                    template_key,
                    _json_dump(param_schema),
                    _json_dump(metadata),
                    source_id,
                    int(user_id),
                ),
            )
            if cur.rowcount <= 0:
                cur.close()
                return None
            version_no = self._insert_version(
                cur,
                source_id,
                user_id,
                name,
                description,
                code,
                template_key,
                param_schema,
                metadata,
            )
            db.commit()
            cur.close()

        restored = self.get_source(source_id, user_id=user_id) or {}
        restored["version_no"] = version_no
        return restored

    def delete_source(self, source_id: int, user_id: int) -> bool:
        self.ensure_schema()
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute("DELETE FROM qd_script_sources WHERE id = ? AND user_id = ?", (int(source_id), int(user_id)))
            ok = cur.rowcount > 0
            db.commit()
            cur.close()
        return ok

    def create_from_marketplace_asset(self, buyer_id: int, asset: Dict[str, Any]) -> int:
        now = int(time.time())
        return self.create_source(
            {
                "user_id": buyer_id,
                "name": asset.get("name") or "Purchased Script",
                "description": asset.get("description") or "",
                "code": asset.get("code") or "",
                "source_marketplace_indicator_id": asset.get("id"),
                "visibility": "private",
                "status": "draft",
                "metadata": {
                    "from_marketplace": True,
                    "purchased_at": now,
                    "asset_type": "script_template",
                },
            }
        )


_service: Optional[ScriptSourceService] = None


def get_script_source_service() -> ScriptSourceService:
    global _service
    if _service is None:
        _service = ScriptSourceService()
    return _service
