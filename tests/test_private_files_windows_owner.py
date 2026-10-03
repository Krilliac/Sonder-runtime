"""Windows owner identity from controlled, in-memory valid descriptors.

No test reads or changes a host file, token owner, ACL, or trust setting.
"""
from __future__ import annotations

import os
import threading

import pytest

from sonder_runtime.platform import private_files

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows owner descriptor APIs")


@pytest.fixture
def synthetic_owner(monkeypatch):
    original_api = private_files._windows_api
    original_user_sid = private_files._windows_user_sid
    observed_thread = threading.get_ident()
    api = original_api()
    assert api is not None
    ctypes, wintypes, advapi32, kernel32 = api
    descriptor = ctypes.c_void_p()
    try:
        assert advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "O:SY", private_files._SDDL_REVISION_1, ctypes.byref(descriptor), None,
        )
        length = advapi32.GetSecurityDescriptorLength
        length.argtypes = [ctypes.c_void_p]
        length.restype = wintypes.DWORD
        descriptor_size = length(descriptor)
        assert descriptor_size > 0

        class DescriptorApi:
            mode = "valid"
            user_sid = "S-1-5-18"
            invalid_sid = ctypes.create_string_buffer(8)

            def __getattr__(self, name):
                return getattr(advapi32, name)

            def GetFileSecurityW(self, _path, _information, buffer, _size, needed):
                if self.mode == "file_error":
                    ctypes.set_last_error(5)
                    needed._obj.value = 0
                    return False
                needed._obj.value = descriptor_size
                if buffer is None:
                    return False
                capacity = _size.value if hasattr(_size, "value") else int(_size)
                if capacity < descriptor_size or ctypes.sizeof(buffer) < descriptor_size:
                    ctypes.set_last_error(122)
                    return False
                ctypes.memmove(buffer, descriptor, descriptor_size)
                return True

            def GetSecurityDescriptorOwner(self, buffer, owner, defaulted):
                if self.mode == "owner_error":
                    ctypes.set_last_error(5)
                    return False
                if self.mode in {"null_owner", "invalid_owner"}:
                    owner._obj.value = None if self.mode == "null_owner" else ctypes.addressof(self.invalid_sid)
                    defaulted._obj.value = False
                    return True
                return advapi32.GetSecurityDescriptorOwner(buffer, owner, defaulted)

            def ConvertSidToStringSidW(self, owner, text):
                if self.mode == "conversion_error":
                    ctypes.set_last_error(5)
                    return False
                return advapi32.ConvertSidToStringSidW(owner, text)

        controlled = DescriptorApi()

        def observed_api():
            if threading.get_ident() != observed_thread:
                return original_api()
            if controlled.mode == "api_unavailable":
                return None
            return ctypes, wintypes, controlled, kernel32

        def observed_user_sid():
            if threading.get_ident() != observed_thread:
                return original_user_sid()
            return controlled.user_sid

        with monkeypatch.context() as scoped:
            scoped.setattr(private_files, "_windows_api", observed_api)
            scoped.setattr(private_files, "_windows_user_sid", observed_user_sid)
            yield controlled
    finally:
        if descriptor.value:
            kernel32.LocalFree(descriptor)


def test_owner_alias_is_compared_as_the_same_numeric_identity(synthetic_owner):
    # Win32 serializes this controlled owner as an alias, while the token
    # identity helper supplies numeric SID text. Both identify the same SID.
    serialized_alias_matches = private_files._windows_sddl(
        "unused", private_files._OWNER_SECURITY_INFORMATION,
    ) == "O:SY"
    assert serialized_alias_matches
    owner_matches_user = private_files._windows_owned_by_me("unused")
    assert owner_matches_user


def test_another_valid_numeric_owner_is_still_refused(synthetic_owner):
    synthetic_owner.user_sid = "S-1-5-32-544"
    assert private_files._windows_owned_by_me("unused") is False


@pytest.mark.parametrize("mode", [
    "file_error", "owner_error", "null_owner", "invalid_owner", "conversion_error",
])
def test_unavailable_or_invalid_owner_is_refused(synthetic_owner, mode):
    synthetic_owner.mode = mode
    assert private_files._windows_owned_by_me("unused") is False


def test_unavailable_windows_owner_api_is_refused(synthetic_owner):
    synthetic_owner.mode = "api_unavailable"
    assert private_files._windows_owned_by_me("unused") is False
