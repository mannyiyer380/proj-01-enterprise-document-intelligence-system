"""Per-session file storage, access checks, atomic JSON and vector writes.

Local filesystem is intentionally single-instance. Use object storage + DB for multi-replica production.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from fastapi import HTTPException

from app.ai_module.schemas import DocumentWorkflow, InstructionDefaults
from app.security import require_session_id


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.general_chat_lock = threading.RLock()
        self.instruction_defaults_lock = threading.Lock()

    def general_history_path(self, owner: str) -> Path:
        owner_key = sha256(owner.encode("utf-8")).hexdigest()
        return self.root.parent / "general_chat" / f"{owner_key}.json"

    def _instruction_defaults_path(self, owner: str) -> Path:
        owner_key = sha256(owner.encode("utf-8")).hexdigest()
        return self.root.parent / "instruction_defaults" / f"{owner_key}.json"

    def instruction_defaults(self, owner: str) -> InstructionDefaults:
        with self.instruction_defaults_lock:
            values = self.read_json(self._instruction_defaults_path(owner), fallback={})
        return InstructionDefaults.model_validate(values)

    def save_instruction_default(self, owner: str, workflow: DocumentWorkflow, instructions: str) -> None:
        path = self._instruction_defaults_path(owner)
        with self.instruction_defaults_lock:
            values = self.read_json(path, fallback={})
            values[workflow] = instructions
            self.write_json(path, values)

    def path(self, sid: str) -> Path:
        require_session_id(sid)
        return self.root / sid

    def create(self, owner: str) -> dict:
        with self.lock:
            sid = uuid.uuid4().hex
            folder = self.path(sid)
            folder.mkdir(mode=0o700)
            manifest = {"session_id": sid, "owner": owner, "created_at": utc_now(), "documents": []}
            self.write_json(folder / "manifest.json", manifest)
            self.write_json(folder / "history.json", [])
            return self.public_manifest(manifest)

    def owned(self, sid: str, owner: str) -> dict:
        directory = self.path(sid)
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise HTTPException(status_code=404, detail="Session not found")
        m = self.read_json(manifest_path)
        if m.get("owner") != owner:
            # Avoid leaking whether another user's session exists.
            raise HTTPException(status_code=404, detail="Session not found")
        return m

    @staticmethod
    def public_manifest(manifest: dict) -> dict:
        return {k: manifest[k] for k in ("session_id", "created_at", "documents")}

    @staticmethod
    def read_json(path: Path, fallback=None):
        if not path.exists():
            return fallback if fallback is not None else []
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _tmp(path: Path) -> tuple[int, str]:
        path.parent.mkdir(parents=True, exist_ok=True)
        return tempfile.mkstemp(prefix=".pending-", suffix=path.suffix, dir=str(path.parent))

    @classmethod
    def write_json(cls, path: Path, obj) -> None:
        fd, tmp = cls._tmp(path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


    def delete(self, sid: str, owner: str) -> None:
        import shutil
        with self.lock:
            self.owned(sid, owner)
            shutil.rmtree(self.path(sid))
