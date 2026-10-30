"""Secure company/personal LLM API-key profiles backed by macOS Keychain."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import ctypes
from pathlib import Path
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_LABELS = {"company": "公司 Key", "personal": "个人 Key"}
DEFAULT_SERVICE_NAME = "data-closed-loop-platform.llm-api-key.v1"
PERSONAL_MODEL = "deepseek-v4-pro"
PERSONAL_API_URL = "https://api.deepseek.com/beta/chat/completions"


class CredentialProfileError(RuntimeError):
    """A safe, user-facing credential operation error."""


class MacOSKeychain:
    """Minimal Security.framework wrapper that never places secrets in argv."""

    item_not_found = -25300

    def __init__(self) -> None:
        try:
            self.security = ctypes.CDLL(
                "/System/Library/Frameworks/Security.framework/Security"
            )
            self.core_foundation = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
        except OSError as exc:
            raise CredentialProfileError("系统钥匙串不可用") from exc

        self.security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self.security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self.core_foundation.CFRelease.argtypes = [ctypes.c_void_p]

    def find(self, service: str, account: str, *, reveal: bool = False) -> tuple[bool, str]:
        service_bytes = service.encode("utf-8")
        account_bytes = account.encode("utf-8")
        secret_length = ctypes.c_uint32()
        secret_data = ctypes.c_void_p()
        item_ref = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            ctypes.byref(secret_length),
            ctypes.byref(secret_data),
            ctypes.byref(item_ref),
        )
        if status == self.item_not_found:
            return False, ""
        if status != 0:
            raise CredentialProfileError("系统钥匙串读取失败")
        try:
            value = (
                ctypes.string_at(secret_data, secret_length.value).decode("utf-8")
                if reveal
                else ""
            )
            return True, value
        finally:
            if secret_data:
                self.security.SecKeychainItemFreeContent(None, secret_data)
            if item_ref:
                self.core_foundation.CFRelease(item_ref)

    def save(self, service: str, account: str, secret: str) -> None:
        service_bytes = service.encode("utf-8")
        account_bytes = account.encode("utf-8")
        secret_bytes = secret.encode("utf-8")
        secret_length = ctypes.c_uint32()
        secret_data = ctypes.c_void_p()
        item_ref = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            ctypes.byref(secret_length),
            ctypes.byref(secret_data),
            ctypes.byref(item_ref),
        )
        if status == 0:
            try:
                update_status = self.security.SecKeychainItemModifyAttributesAndData(
                    item_ref, None, len(secret_bytes), secret_bytes
                )
            finally:
                if secret_data:
                    self.security.SecKeychainItemFreeContent(None, secret_data)
                if item_ref:
                    self.core_foundation.CFRelease(item_ref)
            if update_status != 0:
                raise CredentialProfileError("保存到系统钥匙串失败")
            return
        if status != self.item_not_found:
            raise CredentialProfileError("系统钥匙串读取失败")
        add_status = self.security.SecKeychainAddGenericPassword(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            len(secret_bytes),
            secret_bytes,
            None,
        )
        if add_status != 0:
            raise CredentialProfileError("保存到系统钥匙串失败")


class CredentialProfileStore:
    def __init__(
        self,
        *,
        state_file: str | Path | None = None,
        service_name: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        security_executable: str = "/usr/bin/security",
    ) -> None:
        self.state_file = Path(
            state_file
            or os.environ.get("DCLP_CREDENTIAL_STATE_FILE")
            or REPO_ROOT / ".runtime" / "credentials" / "active.json"
        )
        self.service_name = service_name or os.environ.get(
            "DCLP_KEYCHAIN_SERVICE", DEFAULT_SERVICE_NAME
        )
        self.runner = runner
        self.native_keychain = None if runner else MacOSKeychain()
        self.security_executable = security_executable

    @staticmethod
    def _profile_id(profile_id: str) -> str:
        normalized = str(profile_id or "").strip().lower()
        if normalized not in PROFILE_LABELS:
            raise CredentialProfileError("凭据名称无效")
        return normalized

    def _security(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
        try:
            return self.runner(
                [self.security_executable, *args],
                check=False,
                capture_output=True,
                text=True,
                input=input_text,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CredentialProfileError("系统钥匙串不可用") from exc

    def is_configured(self, profile_id: str) -> bool:
        profile_id = self._profile_id(profile_id)
        if self.native_keychain:
            configured, _ = self.native_keychain.find(self.service_name, profile_id)
            return configured
        result = self._security(
            "find-generic-password", "-s", self.service_name, "-a", profile_id
        )
        return result.returncode == 0

    def save(self, profile_id: str, secret: str) -> dict:
        profile_id = self._profile_id(profile_id)
        value = str(secret or "").strip()
        if not value:
            raise CredentialProfileError("Key 不能为空")
        if len(value) > 65536:
            raise CredentialProfileError("Key 长度超过限制")
        if self.native_keychain:
            self.native_keychain.save(self.service_name, profile_id, value)
        else:
            result = self._security(
                "add-generic-password",
                "-U",
                "-s",
                self.service_name,
                "-a",
                profile_id,
                "-w",
                input_text=value + "\n",
            )
            if result.returncode != 0:
                raise CredentialProfileError("保存到系统钥匙串失败")
        state = self._read_state()
        if not state.get("active_profile"):
            self._write_state({"active_profile": profile_id})
        return self.profile(profile_id)

    def activate(self, profile_id: str) -> dict:
        profile_id = self._profile_id(profile_id)
        if not self.is_configured(profile_id):
            raise CredentialProfileError("该凭据尚未配置")
        self._write_state({"active_profile": profile_id})
        return self.profile(profile_id)

    def resolve_active_secret(self) -> str:
        profile_id = self.active_profile()
        if not profile_id:
            raise CredentialProfileError("尚未选择可用凭据")
        if self.native_keychain:
            configured, secret = self.native_keychain.find(
                self.service_name, profile_id, reveal=True
            )
            if not configured:
                raise CredentialProfileError("当前凭据不可用")
        else:
            result = self._security(
                "find-generic-password",
                "-s",
                self.service_name,
                "-a",
                profile_id,
                "-w",
            )
            if result.returncode != 0:
                raise CredentialProfileError("当前凭据不可用")
            secret = str(result.stdout or "").rstrip("\r\n")
        if not secret:
            raise CredentialProfileError("当前凭据不可用")
        return secret

    def resolve_active_runtime(self, base_env: dict[str, str] | None = None) -> dict[str, str]:
        """Return one atomic, process-only runtime snapshot for the active profile."""
        profile_id = self.active_profile()
        if not profile_id:
            raise CredentialProfileError("尚未选择可用凭据")
        secret = self.resolve_active_secret()
        runtime = dict(base_env or os.environ)
        overrides = {
            "DCLP_RUNTIME_PROFILE": profile_id,
            "DCLP_ASSURANCE_LEVEL": "company" if profile_id == "company" else "personal",
            "LLM_API_KEY": secret,
            "OPENAI_API_KEY": secret,
        }
        if profile_id == "personal":
            overrides.update({
                "N1_AGENT_PROVIDER": "openai_compatible",
                "N1_AGENT_MODEL": PERSONAL_MODEL,
                "N1_AGENT_API_URL": PERSONAL_API_URL,
                "SKILL_AGENT_PROVIDER": "openai_compatible",
                "SKILL_AGENT_MODEL": PERSONAL_MODEL,
                "SKILL_AGENT_API_URL": PERSONAL_API_URL,
                "N2A_AGENT_PROVIDER": "openai_compatible",
                "N2A_AGENT_MODEL": PERSONAL_MODEL,
                "N2A_AGENT_API_URL": PERSONAL_API_URL,
                "N3_AGENT_MODEL": PERSONAL_MODEL,
                "N4_AGENT_MODEL": PERSONAL_MODEL,
                "N5_AGENT_MODEL": PERSONAL_MODEL,
            })
        runtime.update(overrides)
        return runtime

    def active_profile(self) -> str | None:
        value = self._read_state().get("active_profile")
        return value if value in PROFILE_LABELS else None

    def profile(self, profile_id: str) -> dict:
        profile_id = self._profile_id(profile_id)
        return {
            "id": profile_id,
            "name": PROFILE_LABELS[profile_id],
            "configured": self.is_configured(profile_id),
            "active": self.active_profile() == profile_id,
        }

    def status(self) -> dict:
        return {
            "active_profile": self.active_profile(),
            "profiles": [self.profile(profile_id) for profile_id in PROFILE_LABELS],
        }

    def _read_state(self) -> dict:
        if not self.state_file.exists():
            return {"active_profile": None}
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CredentialProfileError("凭据状态文件不可用") from exc
        if not isinstance(payload, dict):
            raise CredentialProfileError("凭据状态文件不可用")
        return {"active_profile": payload.get("active_profile")}

    def _write_state(self, payload: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_file.parent, 0o700)
        fd, temporary_name = tempfile.mkstemp(
            prefix="active.", suffix=".json", dir=self.state_file.parent
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.state_file)
            os.chmod(self.state_file, 0o600)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)


def active_llm_api_key() -> str | None:
    """Resolve the selected Keychain profile without exposing it to callers' logs."""
    try:
        return CredentialProfileStore().resolve_active_secret()
    except CredentialProfileError:
        return None


def active_runtime_environment(base_env: dict[str, str] | None = None) -> dict[str, str] | None:
    """Resolve the selected profile as a child environment, never as an API response."""
    try:
        return CredentialProfileStore().resolve_active_runtime(base_env)
    except CredentialProfileError:
        return None
