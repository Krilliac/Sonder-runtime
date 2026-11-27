"""FED-05 tests: cross-host split policy (layer split only, local output head, speculation, dedicated link + RTT)."""
from __future__ import annotations

import ipaddress
import shutil
import subprocess
from pathlib import Path

import pytest

from sonder_runtime.adapters.inference.rpc_cluster import (
    DEFAULT_RPC_PORT,
    RpcClusterConfigError,
    RpcPeerEndpoint,
    ShardedLlamaServerConfig,
)
from sonder_runtime.adapters.inference.rpc_split_policy import (
    DEFAULT_MAX_RTT_MS,
    DEFAULT_RTT_SAMPLES,
    ENV_RPC_LINK_CIDR,
    ENV_RPC_MAX_RTT_MS,
    CrossHostSplitPolicy,
    CrossHostSplitReport,
    RpcPeerRttMeasurement,
    check_cross_host_split_policy,
    measure_rpc_peer_rtt,
    parse_max_rtt_ms,
    require_cross_host_split_policy,
)
from sonder_runtime.domain.routing.rpc_split_policy import (
    ALLOWED_SPLIT_MODES,
    ALLOWED_SPLIT_SPEC_TYPES,
    DEFAULT_SPLIT_MODE,
    DEFAULT_SPLIT_SPEC_TYPE,
    MIN_DEDICATED_LINK_PREFIXLEN,
    SplitLoadLogVerdict,
    is_rpc_device_name,
    validate_dedicated_link_cidr,
    validate_split_device_order,
    validate_split_mode,
    validate_split_spec_type,
    validate_split_tensor_weights,
    verify_split_load_log,
)
from sonder_runtime.platform.rpc_sharding_config import RpcShardingConfigError

LINK_CIDR = "10.77.0.0/30"
WORKER_ON_LINK = "10.77.0.2"
WORKER_OFF_LINK = "192.168.50.20"

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "rpc"
POLICY_PS1 = SCRIPTS / "rpc-policy.ps1"
SHARDED_PS1 = SCRIPTS / "start-rpc-sharded.ps1"
_SHELLS = [name for name in ("powershell", "pwsh") if shutil.which(name)]


# -- 1. Pure domain policy: layer split only ---------------------------------


def test_split_mode_allows_layer_only_and_refuses_tensor_row_none() -> None:
    assert DEFAULT_SPLIT_MODE == "layer"
    assert ALLOWED_SPLIT_MODES == frozenset({"layer"})
    assert validate_split_mode("layer") == "layer"
    assert validate_split_mode("  LAYER ") == "layer"

    for bad in ("tensor", "row", "none", "", "auto", "expert"):
        with pytest.raises(ValueError, match="layer"):
            validate_split_mode(bad)


# -- 2. Pure domain policy: logits / output head on local node ---------------


def test_device_order_requires_local_device_last_for_output_head() -> None:
    assert is_rpc_device_name("RPC0") is True
    assert is_rpc_device_name("RPC[10.77.0.2:50052]") is True
    assert is_rpc_device_name("CUDA0") is False
    assert is_rpc_device_name("Vulkan0") is False

    assert validate_split_device_order(("RPC0", "CUDA0")) == ("RPC0", "CUDA0")
    assert validate_split_device_order(("RPC0", "RPC1", "CUDA0")) == ("RPC0", "RPC1", "CUDA0")

    # RPC device last puts output.weight / logits over Ethernet -> refused!
    with pytest.raises(ValueError, match="output head|logits|local"):
        validate_split_device_order(("CUDA0", "RPC0"))
    # RPC-only (no local device) -> refused!
    with pytest.raises(ValueError, match="local"):
        validate_split_device_order(("RPC0",))
    # Local device placed before an RPC device -> refused!
    with pytest.raises(ValueError, match="precede|before|last"):
        validate_split_device_order(("CUDA0", "RPC0", "CUDA1"))


def test_tensor_split_requires_positive_trailing_local_weight() -> None:
    assert validate_split_tensor_weights("1,1", peer_count=1) == "1,1"
    assert validate_split_tensor_weights("7,1", peer_count=1) == "7,1"
    assert validate_split_tensor_weights("", peer_count=1) == ""

    # Trailing 0 leaves the last layer (output head / logits) on the remote RPC peer!
    with pytest.raises(ValueError, match="local|output|positive"):
        validate_split_tensor_weights("1,0", peer_count=1)
    with pytest.raises(ValueError, match="local|output|positive"):
        validate_split_tensor_weights("2,1,0", peer_count=2)
    # Fewer weights than peers + 1 local device when explicitly set -> refused.
    with pytest.raises(ValueError, match="weight"):
        validate_split_tensor_weights("1", peer_count=1)


def test_verify_split_load_log_checks_real_llama_cpp_v1_lines() -> None:
    good_log = "\n".join([
        "llama_prepare_model_devices: using device RPC0 (10.77.0.2:50052) (AMD Radeon RX 9060 XT) - 15400 MiB free",
        "llama_prepare_model_devices: using device CUDA0 (NVIDIA GeForce RTX 5070 Ti) - 14800 MiB free",
        "load_tensors: layer   0 assigned to device RPC0, is_swa = 0",
        "load_tensors: layer  23 assigned to device RPC0, is_swa = 0",
        "load_tensors: layer  24 assigned to device CUDA0, is_swa = 0",
        "load_tensors: layer  48 assigned to device CUDA0, is_swa = 0",
    ])
    verdict = verify_split_load_log(good_log)
    assert isinstance(verdict, SplitLoadLogVerdict)
    assert verdict.ok is True
    assert verdict.device_order == ("RPC0", "CUDA0")
    assert verdict.output_device == "CUDA0"
    assert verdict.output_is_local is True
    assert verdict.errors == ()

    # Bad log: output layer (highest layer index) assigned to RPC0!
    bad_output_log = "\n".join([
        "llama_prepare_model_devices: using device CUDA0 (NVIDIA GeForce RTX 5070 Ti) - 14800 MiB free",
        "llama_prepare_model_devices: using device RPC0 (10.77.0.2:50052) - 15400 MiB free",
        "load_tensors: layer   0 assigned to device CUDA0, is_swa = 0",
        "load_tensors: layer  48 assigned to device RPC0, is_swa = 0",
    ])
    bad_verdict = verify_split_load_log(bad_output_log)
    assert bad_verdict.ok is False
    assert bad_verdict.output_device == "RPC0"
    assert bad_verdict.output_is_local is False
    assert any("RPC0" in err for err in bad_verdict.errors)

    # Empty / uninformative log fails closed (never claims ok without evidence).
    empty_verdict = verify_split_load_log("server started\n")
    assert empty_verdict.ok is False
    assert empty_verdict.errors


# -- 3. Pure domain policy: speculative decoding on --------------------------


def test_speculative_decoding_required_on_cross_host_split() -> None:
    assert DEFAULT_SPLIT_SPEC_TYPE == "ngram-mod"
    assert "ngram-mod" in ALLOWED_SPLIT_SPEC_TYPES
    assert "none" not in ALLOWED_SPLIT_SPEC_TYPES
    assert validate_split_spec_type("ngram-mod") == "ngram-mod"
    assert validate_split_spec_type("draft-mtp") == "draft-mtp"

    for bad in ("none", "", "off", "unknown-spec"):
        with pytest.raises(ValueError, match="spec"):
            validate_split_spec_type(bad)


# -- 4. Pure domain policy: dedicated link CIDR ------------------------------


def test_dedicated_link_cidr_requires_private_narrow_subnet() -> None:
    assert MIN_DEDICATED_LINK_PREFIXLEN == 24
    net = validate_dedicated_link_cidr(LINK_CIDR)
    assert net == ipaddress.IPv4Network("10.77.0.0/30")
    assert validate_dedicated_link_cidr("169.254.77.0/30") == ipaddress.IPv4Network("169.254.77.0/30")
    assert validate_dedicated_link_cidr("192.168.99.0/24") == ipaddress.IPv4Network("192.168.99.0/24")

    # Empty, public, IPv6, or overly broad shared subnet (/8, /16) are refused.
    for bad in ("", "203.0.113.0/30", "10.0.0.0/8", "192.168.0.0/16", "fd00::/64", "10.77.0.1/30"):
        with pytest.raises(ValueError):
            validate_dedicated_link_cidr(bad)


# -- 5. ShardedLlamaServerConfig enforces FED-05 flags & blocks bypass -------


def test_sharded_llama_server_config_enforces_layer_split_spec_and_local_output() -> None:
    peers = (RpcPeerEndpoint(WORKER_ON_LINK),)
    cfg = ShardedLlamaServerConfig(
        model_path="model.gguf",
        rpc_peers=peers,
        tensor_split="1,1",
        devices=("RPC0", "CUDA0"),
    )
    args = cfg.build_args()
    assert args[args.index("--split-mode") + 1] == "layer"
    assert args[args.index("--spec-type") + 1] == "ngram-mod"
    assert args[args.index("--device") + 1] == "RPC0,CUDA0"

    with pytest.raises(RpcClusterConfigError):
        ShardedLlamaServerConfig(model_path="model.gguf", rpc_peers=peers, split_mode="tensor")
    with pytest.raises(RpcClusterConfigError):
        ShardedLlamaServerConfig(model_path="model.gguf", rpc_peers=peers, split_mode="row")
    with pytest.raises(RpcClusterConfigError):
        ShardedLlamaServerConfig(model_path="model.gguf", rpc_peers=peers, spec_type="none")
    with pytest.raises(RpcClusterConfigError):
        ShardedLlamaServerConfig(model_path="model.gguf", rpc_peers=peers, tensor_split="1,0")
    with pytest.raises(RpcClusterConfigError):
        ShardedLlamaServerConfig(model_path="model.gguf", rpc_peers=peers, devices=("CUDA0", "RPC0"))

    # extra_args cannot sneak forbidden split-mode, spec-type, or device overrides.
    for extra in (
        ("--split-mode", "tensor"),
        ("-sm", "row"),
        ("--spec-type", "none"),
        ("--device", "CUDA0,RPC0"),
        ("-dev", "RPC0"),
    ):
        with pytest.raises(RpcClusterConfigError, match="extra_args"):
            ShardedLlamaServerConfig(model_path="model.gguf", rpc_peers=peers, extra_args=extra)


# -- 6. Adapter: RTT measurement & cross-host split gate (FED-02 compat) -----


def _fake_clock_connector(latencies_ms: list[float]):
    """Build deterministic (connect_fn, clock) pair advancing by latencies_ms per call."""
    now = [100.0]
    idx = [0]

    def clock() -> float:
        return now[0]

    def connect(addr: tuple[str, int], timeout: float) -> None:
        dt_ms = latencies_ms[min(idx[0], len(latencies_ms) - 1)]
        idx[0] += 1
        now[0] += dt_ms / 1000.0

    return connect, clock


def test_measure_rpc_peer_rtt_and_require_split_policy() -> None:
    assert DEFAULT_RTT_SAMPLES >= 3
    assert DEFAULT_MAX_RTT_MS > 0.0
    assert parse_max_rtt_ms(None) == DEFAULT_MAX_RTT_MS
    assert parse_max_rtt_ms("1.5") == pytest.approx(1.5)
    with pytest.raises(RpcShardingConfigError):
        parse_max_rtt_ms("0")
    with pytest.raises(RpcShardingConfigError):
        parse_max_rtt_ms("nan")

    connect_fast, clock_fast = _fake_clock_connector([0.14, 0.16, 0.18, 0.20, 0.22])
    rtt = measure_rpc_peer_rtt(
        WORKER_ON_LINK,
        port=DEFAULT_RPC_PORT,
        samples=5,
        connect_fn=connect_fast,
        clock=clock_fast,
    )
    assert isinstance(rtt, RpcPeerRttMeasurement)
    assert rtt.reachable is True
    assert rtt.min_ms == pytest.approx(0.14, rel=1e-3)
    assert rtt.median_ms == pytest.approx(0.18, rel=1e-3)
    assert rtt.max_ms == pytest.approx(0.22, rel=1e-3)

    cfg = ShardedLlamaServerConfig(
        model_path="model.gguf",
        rpc_peers=(RpcPeerEndpoint(WORKER_ON_LINK),),
        tensor_split="1,1",
        devices=("RPC0", "CUDA0"),
    )
    connect_fast2, clock_fast2 = _fake_clock_connector([0.15, 0.18, 0.20, 0.21, 0.25])
    report = require_cross_host_split_policy(
        cfg,
        env={ENV_RPC_LINK_CIDR: LINK_CIDR, ENV_RPC_MAX_RTT_MS: "2.0"},
        connect_fn=connect_fast2,
        clock=clock_fast2,
    )
    assert isinstance(report, CrossHostSplitReport)
    assert report.ok is True
    assert report.link_network == ipaddress.IPv4Network(LINK_CIDR)
    assert report.split_mode == "layer"
    assert report.spec_type == "ngram-mod"
    assert len(report.peer_rtts) == 1
    assert report.peer_rtts[0].median_ms == pytest.approx(0.20, rel=1e-3)

    # High RTT (e.g. Wi-Fi / routed hop at 4.5 ms > 2.0 ms) is refused fail-closed!
    connect_slow, clock_slow = _fake_clock_connector([4.2, 4.5, 4.8, 5.0, 5.1])
    with pytest.raises(RpcShardingConfigError, match="RTT"):
        require_cross_host_split_policy(
            cfg,
            env={ENV_RPC_LINK_CIDR: LINK_CIDR, ENV_RPC_MAX_RTT_MS: "2.0"},
            connect_fn=connect_slow,
            clock=clock_slow,
        )

    # Missing dedicated link CIDR is refused fail-closed!
    connect_fast3, clock_fast3 = _fake_clock_connector([0.15, 0.18, 0.20])
    with pytest.raises(RpcShardingConfigError, match=ENV_RPC_LINK_CIDR):
        require_cross_host_split_policy(
            cfg,
            env={},
            policy=CrossHostSplitPolicy(rtt_samples=3),
            connect_fn=connect_fast3,
            clock=clock_fast3,
        )

    # Off-link peer is refused fail-closed!
    off_link_cfg = ShardedLlamaServerConfig(
        model_path="model.gguf",
        rpc_peers=(RpcPeerEndpoint(WORKER_OFF_LINK),),
        tensor_split="1,1",
    )
    bad_report = check_cross_host_split_policy(
        off_link_cfg,
        env={ENV_RPC_LINK_CIDR: LINK_CIDR},
        connect_fn=connect_fast3,
        clock=clock_fast3,
    )
    assert bad_report.ok is False
    assert any("private link" in err or "not on" in err for err in bad_report.errors)


# -- 7. PowerShell parity for FED-05 split policy ----------------------------


@pytest.mark.skipif(not _SHELLS, reason="PowerShell is not available")
@pytest.mark.parametrize("shell", _SHELLS)
def test_powershell_rpc_split_policy_helpers(shell: str, tmp_path: Path) -> None:
    script = tmp_path / "fed05_probe.ps1"
    script.write_text(
        f". '{POLICY_PS1}'\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$net = Assert-RpcDedicatedLink -LinkCidr '10.77.0.0/30' -PeerHosts @('10.77.0.2')\n"
        "$p = Assert-RpcSplitPolicy -SplitMode 'layer' -SpecType 'ngram-mod' -TensorSplit '1,1' -PeerCount 1\n"
        "Write-Output \"OK:$($net.Cidr):$($p.splitMode):$($p.specType)\"\n",
        encoding="utf-8-sig",
    )
    res = subprocess.run([shell, "-NoProfile", "-File", str(script)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert "OK:10.77.0.0/30:layer:ngram-mod" in res.stdout

    # Refuses -sm tensor, -SpecType none, trailing 0 weight, and broad /8 CIDR.
    for bad_line in (
        "Assert-RpcSplitPolicy -SplitMode 'tensor' -SpecType 'ngram-mod' -TensorSplit '1,1' -PeerCount 1",
        "Assert-RpcSplitPolicy -SplitMode 'row' -SpecType 'ngram-mod' -TensorSplit '1,1' -PeerCount 1",
        "Assert-RpcSplitPolicy -SplitMode 'layer' -SpecType 'none' -TensorSplit '1,1' -PeerCount 1",
        "Assert-RpcSplitPolicy -SplitMode 'layer' -SpecType 'ngram-mod' -TensorSplit '1,0' -PeerCount 1",
        "Assert-RpcDedicatedLink -LinkCidr '10.0.0.0/8' -PeerHosts @('10.77.0.2')",
        "Assert-RpcDedicatedLink -LinkCidr '10.77.0.0/30' -PeerHosts @('192.168.50.20')",
    ):
        bad_script = tmp_path / "bad.ps1"
        bad_script.write_text(
            f". '{POLICY_PS1}'\n$ErrorActionPreference = 'Stop'\n{bad_line}\n",
            encoding="utf-8-sig",
        )
        bad_res = subprocess.run(
            [shell, "-NoProfile", "-File", str(bad_script)],
            capture_output=True, text=True, timeout=60,
        )
        assert bad_res.returncode != 0, f"expected refusal for: {bad_line}"


def test_start_rpc_sharded_script_wires_fed05_split_policy() -> None:
    text = SHARDED_PS1.read_text(encoding="utf-8")
    assert "'--split-mode'" in text
    assert "'--spec-type'" in text
    assert "Assert-RpcSplitPolicy" in text
    assert "Measure-RpcPeerRtt" in text
