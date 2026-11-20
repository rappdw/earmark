"""Static checks of the container image, compose file, systemd units and Caddy example.

Nothing here builds or runs a container: the sandbox has no Docker. CI builds the image.
"""

import ipaddress
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from earmark.config import Settings, env_var
from tests.doc_helpers import ROOT, read_doc

DOCKERFILE = (ROOT / "Dockerfile").read_text()
PINNED_IMAGE = re.compile(r"^[^\s@]+:[^\s@]+@sha256:[0-9a-f]{64}$")
ALLOWED_CONTEXT = {"pyproject.toml", "uv.lock", ".python-version", "README.md", "src/"}
UNITS = {"api": "earmark-api.service", "worker": "earmark-worker.service"}


def _logical_lines(text: str) -> list[str]:
    """Dockerfile instructions with comments dropped and continuations joined."""
    joined: list[str] = []
    pending = ""
    for raw in text.splitlines():
        if raw.strip().startswith("#"):
            continue
        if raw.rstrip().endswith("\\"):
            pending += raw.rstrip()[:-1] + " "
            continue
        joined.append((pending + raw).strip())
        pending = ""
    return [line for line in joined if line]


def _instructions(name: str) -> list[str]:
    return [line for line in _logical_lines(DOCKERFILE) if line.split()[0].upper() == name]


def _froms() -> list[tuple[str, str | None]]:
    result: list[tuple[str, str | None]] = []
    for line in _instructions("FROM"):
        words = line.split()
        stage = words[words.index("AS") + 1] if "AS" in words else None
        image = next(w for w in words[1:] if not w.startswith("--"))
        result.append((image, stage))
    return result


def _external_froms() -> list[str]:
    stages = {s for _, s in _froms() if s}
    return [image for image, _ in _froms() if image not in stages]


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


# --- Dockerfile ---------------------------------------------------------------------------


def test_dockerfile_images_pinned_by_digest() -> None:
    images = _external_froms()
    assert len(images) == 3
    for image in images:
        assert PINNED_IMAGE.match(image), image


def test_dockerfile_python_matches_project() -> None:
    python = [i for i, _ in _froms() if i.startswith("python:")]
    assert len(python) == 2
    assert python[0] == python[1]
    version = (ROOT / ".python-version").read_text().strip()
    assert python[0].startswith(f"python:{version}.")
    assert [s for _, s in _froms()] == ["uv", "build", "runtime"]


def test_dockerfile_uv_matches_build_backend() -> None:
    [uv_image] = [i for i in _external_froms() if "astral-sh/uv" in i]
    tag = uv_image.split(":", 1)[1].split("@", 1)[0]
    minor = ".".join(tag.split(".")[:2])
    requires = _pyproject()["build-system"]["requires"]
    [bound] = [r for r in requires if r.startswith("uv_build")]
    assert f">={minor}." in bound or f">={minor}," in bound
    assert f"<{minor.split('.')[0]}.{int(minor.split('.')[1]) + 1}.0" in bound
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert f">={minor}." in ci
    assert f"<{minor.split('.')[0]}.{int(minor.split('.')[1]) + 1}" in ci


def test_dockerfile_uv_sync_locked_no_dev() -> None:
    syncs = [line for line in _instructions("RUN") if "uv sync" in line]
    assert len(syncs) == 2
    for sync in syncs:
        assert "--locked" in sync
        assert "--no-dev" in sync
    assert "--no-editable" in syncs[-1]


def test_dockerfile_default_extras_exist() -> None:
    [arg] = [line for line in _instructions("ARG") if line.startswith("ARG EXTRAS=")]
    default = arg.split("=", 1)[1].strip('"')
    extras = _pyproject()["project"]["optional-dependencies"]
    assert default.split()
    assert set(default.split()) <= set(extras)


def test_dockerfile_runtime_user_not_root() -> None:
    assert _froms()[-1][1] == "runtime"
    users = _instructions("USER")
    assert users
    last = users[-1].split()[1]
    assert last.startswith("earmark")
    assert last not in {"root", "0"}
    assert 'test "$UID" -ne 0' in DOCKERFILE
    assert 'test "$GID" -ne 0' in DOCKERFILE


def test_dockerfile_entrypoint() -> None:
    assert _instructions("ENTRYPOINT") == ['ENTRYPOINT ["earmark"]']
    assert _instructions("CMD") == ['CMD ["serve"]']


def test_dockerfile_no_volume_or_healthcheck() -> None:
    assert _instructions("VOLUME") == []
    assert _instructions("HEALTHCHECK") == []


def test_dockerfile_installs_ca_certificates() -> None:
    assert "ca-certificates" in DOCKERFILE


def test_dockerfile_environment() -> None:
    env = " ".join(_instructions("ENV"))
    for expected in (
        "EARMARK_HOST=0.0.0.0",
        "EARMARK_DATA_DIR=/data",
        "EARMARK_VAULT_DIR=/vault",
        "HF_HOME=/data/huggingface",
    ):
        assert expected in env


# --- .dockerignore ------------------------------------------------------------------------


def _dockerignore_rules() -> list[str]:
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    return [x.strip() for x in lines if x.strip() and not x.startswith("#")]


def test_dockerignore_is_allow_list() -> None:
    rules = _dockerignore_rules()
    assert rules[0] == "*"
    assert {r[1:] for r in rules if r.startswith("!")} == ALLOWED_CONTEXT


def test_dockerfile_copies_only_allowed_paths() -> None:
    copies = [c for c in _instructions("COPY") if "--from" not in c]
    assert copies
    for line in copies:
        *sources, destination = line.split()[1:]
        assert destination
        for source in sources:
            normalised = source.removeprefix("./")
            allowed = ALLOWED_CONTEXT | {s.rstrip("/") for s in ALLOWED_CONTEXT}
            assert normalised in allowed, source


# --- compose.yaml -------------------------------------------------------------------------

COMPOSE_TEXT = (ROOT / "compose.yaml").read_text()
COMPOSE: dict[str, Any] = yaml.safe_load(COMPOSE_TEXT)
SERVICES: dict[str, dict[str, Any]] = COMPOSE["services"]


def test_compose_services() -> None:
    assert set(SERVICES) == {"api", "worker", "asr"}
    assert SERVICES["api"]["command"] == ["serve"]
    assert SERVICES["worker"]["command"] == ["worker"]
    assert SERVICES["api"]["image"] == SERVICES["worker"]["image"]
    assert SERVICES["api"]["build"] == SERVICES["worker"]["build"]


def test_compose_api_publishes_loopback_by_default() -> None:
    assert SERVICES["api"]["ports"] == ["${API_BIND:-127.0.0.1:8787}:8787"]


def test_compose_worker_and_asr_publish_nothing() -> None:
    assert "ports" not in SERVICES["worker"]
    assert "ports" not in SERVICES["asr"]
    assert "0.0.0.0:" not in COMPOSE_TEXT


def test_compose_shared_data_and_vault() -> None:
    for name in ("api", "worker"):
        volumes = SERVICES[name]["volumes"]
        assert "data:/data" in volumes
        assert any(v.startswith("${VAULT_PATH:?") and v.endswith("}:/vault") for v in volumes), (
            volumes
        )
    assert "data" in COMPOSE["volumes"]


def test_compose_forces_container_paths() -> None:
    for name in ("api", "worker"):
        environment = SERVICES[name]["environment"]
        assert environment["EARMARK_DATA_DIR"] == "/data"
        assert environment["EARMARK_VAULT_DIR"] == "/vault"
        assert environment["EARMARK_HOST"] == "0.0.0.0"  # noqa: S104 - the in-container bind
        assert environment["EARMARK_PORT"] == "8787"
        assert environment["EARMARK_ASR_MODEL_DIR"] == ""
        assert environment["EARMARK_EMBED_MODEL_DIR"] == ""
        assert SERVICES[name]["env_file"] == ".env"


def test_compose_worker_stop_grace() -> None:
    grace = SERVICES["worker"]["stop_grace_period"]
    match = re.fullmatch(r"(\d+)(s|m)", grace)
    assert match
    seconds = int(match[1]) * (60 if match[2] == "m" else 1)
    assert seconds >= 60


def test_compose_hardening() -> None:
    for name in ("api", "worker"):
        service = SERVICES[name]
        assert service["init"] is True
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["cap_drop"] == ["ALL"]
    for name, service in SERVICES.items():
        assert not service.get("privileged"), name
        assert "network_mode" not in service, name
        assert "pid" not in service, name
        assert service.get("ipc") != "host", name


def test_compose_gpu_profile() -> None:
    asr = SERVICES["asr"]
    assert asr["profiles"] == ["gpu"]
    assert PINNED_IMAGE.match(asr["image"])
    [device] = asr["deploy"]["resources"]["reservations"]["devices"]
    assert device["driver"] == "nvidia"
    assert "gpu" in device["capabilities"]
    environment = asr["environment"]
    assert environment["VLLM_API_KEY"] == "${EARMARK_ASR_API_KEY:-}"
    assert int(environment["VLLM_MAX_AUDIO_CLIP_FILESIZE_MB"]) > 25
    assert int(environment["VLLM_MAX_AUDIO_DECODE_DURATION_S"]) > 600
    assert int(environment["VLLM_MAX_AUDIO_DECODE_BYTES"]) > 268435456
    assert [n for n, s in SERVICES.items() if "profiles" in s] == ["asr"]


def test_compose_earmark_variables_are_settings() -> None:
    known = {env_var(f) for f in Settings.model_fields}
    used = set(re.findall(r"\$\{(EARMARK_[A-Z0-9_]+)", COMPOSE_TEXT))
    for service in SERVICES.values():
        environment = service.get("environment", {})
        used |= {k for k in environment if k.startswith("EARMARK_")}
    assert used
    assert used <= known, used - known


def _compose_only_variables() -> set[str]:
    names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", COMPOSE_TEXT))
    return {n for n in names if not n.startswith("EARMARK_")}


def test_compose_only_variables_documented() -> None:
    names = _compose_only_variables()
    assert names == {
        "VAULT_PATH",
        "API_BIND",
        "APP_UID",
        "APP_GID",
        "EXTRAS",
        "ASR_GPU_MEMORY_UTILIZATION",
    }
    deploy = read_doc("docs/deploy.md")
    example = (ROOT / ".env.example").read_text()
    for name in names:
        assert re.search(rf"^\| `{name}` \|", deploy, re.MULTILINE), name
        assert re.search(rf"^# {name}=", example, re.MULTILINE), name


# --- systemd units ------------------------------------------------------------------------


def _parse_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    sections: dict[str, dict[str, list[str]]] = {}
    current = ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, {})
            continue
        key, _, value = line.partition("=")
        sections[current].setdefault(key, []).append(value)
    return sections


@pytest.mark.parametrize(("role", "filename"), sorted(UNITS.items()))
def test_systemd_units(role: str, filename: str) -> None:
    unit = _parse_unit(ROOT / "deploy" / "systemd" / filename)
    service = unit["Service"]
    [exec_start] = service["ExecStart"]
    assert exec_start.startswith("/srv/earmark/.venv/bin/earmark ")
    assert exec_start.endswith(f" {'serve' if role == 'api' else 'worker'}")
    assert service["User"] == ["earmark"]
    assert service["KillSignal"] == ["SIGTERM"]
    assert service["Restart"] == ["on-failure"]
    assert service["NoNewPrivileges"] == ["yes"]
    assert service["ProtectSystem"] == ["strict"]
    assert service["WorkingDirectory"] == ["/srv/earmark"]
    write_paths = " ".join(service["ReadWritePaths"]).split()
    assert "/srv/earmark/data" in write_paths
    [timeout] = service["TimeoutStopSec"]
    if role == "worker":
        assert int(timeout) >= 60
        assert len(write_paths) >= 2  # the vault
    assert unit["Install"]["WantedBy"] == ["multi-user.target"]


# --- Caddy example ------------------------------------------------------------------------

CADDYFILE = (ROOT / "deploy" / "caddy" / "Caddyfile").read_text()


def _caddy_code() -> list[str]:
    return [
        x.strip() for x in CADDYFILE.splitlines() if x.strip() and not x.strip().startswith("#")
    ]


def test_caddyfile_example() -> None:
    code = _caddy_code()
    site_blocks = [x for x in CADDYFILE.splitlines() if x.endswith(" {") and not x.startswith("\t")]
    assert len(site_blocks) == 1
    address = site_blocks[0][:-2].strip()
    assert re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", address), address
    [bind] = [x.split()[1] for x in code if x.startswith("bind ")]
    parsed = ipaddress.ip_address(bind)
    assert parsed.version == 4
    assert parsed.is_private
    assert str(parsed) != "0.0.0.0"  # noqa: S104 - checking the example never binds everything
    assert "dns cloudflare {env.CF_API_TOKEN}" in code
    tls_start = code.index("tls {")
    assert code.index("dns cloudflare {env.CF_API_TOKEN}") > tls_start
    assert "reverse_proxy 127.0.0.1:8787" in code
    assert "auto_https disable_redirects" in code
    assert "http://" not in CADDYFILE.replace("https://", "")
    compose_bind = COMPOSE["services"]["api"]["ports"][0]
    assert "127.0.0.1:8787" in compose_bind


def test_proxy_token_stays_out_of_earmark() -> None:
    files = [
        ROOT / ".env.example",
        ROOT / "compose.yaml",
        ROOT / "Dockerfile",
        *sorted((ROOT / "deploy" / "systemd").glob("*.service")),
        *sorted((ROOT / "src" / "earmark").rglob("*.py")),
    ]
    for path in files:
        text = path.read_text()
        assert re.search(r"\bCF_[A-Z_]*TOKEN|CLOUDFLARE", text, re.IGNORECASE) is None, path
