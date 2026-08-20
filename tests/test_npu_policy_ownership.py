from sonder_runtime.platform import npu_policy
import system_profile


def test_system_profile_reexports_npu_policy_by_identity():
    assert system_profile._npu_vendor_from_name is npu_policy.vendor_from_name
    assert system_profile._npu_vendor_from_pnp_id is npu_policy.vendor_from_pnp_id
    assert system_profile._linux_accel_is_npu is npu_policy.linux_accel_is_npu
    assert system_profile._NPU_NAME_RE is npu_policy.NPU_NAME_RE


def test_npu_policy_rejects_ambiguous_names_and_unsupported_drivers():
    assert npu_policy.NPU_NAME_RE.search("Microsoft Input Configuration Device") is None
    assert npu_policy.NPU_NAME_RE.search("Intel AI Boost NPU")
    assert npu_policy.linux_accel_is_npu("qualcomm", "qaic") is False
    assert npu_policy.linux_accel_is_npu("intel", "ivpu") is True
