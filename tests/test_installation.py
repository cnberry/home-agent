from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_preserves_existing_secrets_and_durable_state() -> None:
    script = (ROOT / "scripts/bootstrap.sh").read_text(encoding="utf-8")
    assert 'if [[ ! -e "$config_file" ]]' in script
    assert 'elif [[ ! -e "$token_file" ]]' in script
    assert 'if [[ ! -e "$data_root/durable/$state_file" ]]' in script
    assert "usermod --home" in script
    assert '-G "$state_group" "$agent_user"' in script


def test_bootstrap_installs_hash_locked_release() -> None:
    script = (ROOT / "scripts/bootstrap.sh").read_text(encoding="utf-8")
    assert "--require-hashes -r" in script
    assert "--no-build-isolation --no-deps" in script
    assert 'chown -R root:root "$release_dir"' in script
    assert "doctor --skip-telegram" in script


def test_bootstrap_installs_full_access_ssh_wrapper() -> None:
    bootstrap = (ROOT / "scripts/bootstrap.sh").read_text(encoding="utf-8")
    wrapper = (ROOT / "scripts/home-agent-codex.sh").read_text(encoding="utf-8")
    assert 'scripts/home-agent-codex.sh"' in bootstrap
    assert "/usr/local/bin/home-agent-codex" in bootstrap
    assert "bundled_codex_path" in wrapper
    assert 'sudo -n -u "$agent_user"' in wrapper
    assert "--dangerously-bypass-approvals-and-sandbox" in wrapper
    assert 'CODEX_HOME="$data_root/codex-home"' in wrapper


def test_bootstrap_installs_queue_bridge_wrapper() -> None:
    bootstrap = (ROOT / "scripts/bootstrap.sh").read_text(encoding="utf-8")
    wrapper = (ROOT / "scripts/home-agent-codex-bridge.sh").read_text(encoding="utf-8")
    assert 'scripts/home-agent-codex-bridge.sh"' in bootstrap
    assert "/usr/local/bin/home-agent-codex-bridge" in bootstrap
    assert 'sudo -n -u "$agent_user"' in wrapper
    assert '"$agentctl" --config "$config" bridge' in wrapper


def test_bootstrap_installs_unrestricted_passwordless_sudo() -> None:
    bootstrap = (ROOT / "scripts/bootstrap.sh").read_text(encoding="utf-8")
    sudoers = (ROOT / "deploy/sudoers/home-agent").read_text(encoding="utf-8")
    service = (ROOT / "deploy/systemd/home-agent.service").read_text(encoding="utf-8")
    uninstall = (ROOT / "scripts/uninstall.sh").read_text(encoding="utf-8")

    assert "python3-venv sudo" in bootstrap
    assert "python python-pip sudo" in bootstrap
    assert 'distro_family="arch"' in bootstrap
    assert "pacman -S --needed --noconfirm" in bootstrap
    assert "command -v nologin" in bootstrap
    assert 'install -o root -g root -m 0440' in bootstrap
    assert 'visudo -cf "$sudoers_file"' in bootstrap
    assert sudoers == "home-agent ALL=(ALL:ALL) NOPASSWD: ALL\n"
    assert "NoNewPrivileges=true" not in service
    assert "RestrictSUIDSGID=true" not in service
    assert "/etc/sudoers.d/home-agent" in uninstall


def test_every_systemd_unit_is_in_the_ci_verify_command() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "systemd-analyze verify deploy/systemd/*.service deploy/systemd/*.timer" in workflow
    units = list((ROOT / "deploy/systemd").iterdir())
    assert len(units) == 5


def test_state_backup_schedule_matches_recovery_contract() -> None:
    timer = (ROOT / "deploy/systemd/home-agent-state-backup.timer").read_text(
        encoding="utf-8"
    )
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=1d" in timer
    assert "daily curated-state backup" in readme


def test_recovery_keeps_private_deployment_details_external() -> None:
    recovery = (ROOT / "docs/RECOVERY.md").read_text(encoding="utf-8")
    assert "Arch/Omarchy" in recovery
    assert "/etc/home-agent/backup-repository" in recovery
    assert "private deployment repository" in recovery
    assert "machine-specific acceptance checks" in recovery


def test_default_model_is_fast_and_cost_sensitive() -> None:
    config = (ROOT / "deploy/config.example.toml").read_text(encoding="utf-8")
    assert 'model = "gpt-5.6-luna"' in config
    assert 'reasoning_effort = "low"' in config


def test_legacy_installation_has_state_preserving_migration() -> None:
    migration = (ROOT / "scripts/migrate-from-strawberry-agent.sh").read_text(
        encoding="utf-8"
    )
    assert "usermod --login" in migration
    assert "groupmod --new-name" in migration
    assert 'move_and_link /var/lib/strawberry-agent /var/lib/home-agent' in migration
    assert "gpt-5.6-luna" in migration
    assert "reasoning_effort low" in migration
    assert "git@github.com:cnberry/home-agent.git" not in migration
    assert '"$source_root/scripts/bootstrap.sh" --non-interactive --skip-auth' in migration
    assert "rm -rf" not in migration


def test_backup_repository_must_come_from_private_configuration() -> None:
    configure = (ROOT / "scripts/configure-backup.sh").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts/bootstrap.sh").read_text(encoding="utf-8")
    assert "HOME_AGENT_BACKUP_REPOSITORY" in configure
    assert "/etc/home-agent/backup-repository" in configure
    assert "Backup repository is not configured" in configure
    assert "git@github.com:cnberry" not in configure
    assert 'backup_configured=false' in bootstrap
