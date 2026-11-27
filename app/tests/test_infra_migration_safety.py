"""Regression tests for the Terraform state-migration safety guarantees (27 Nov 2026)."""

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "infra/scripts/expect_denied.sh"


def _probe(stderr: str, rc: int) -> subprocess.CompletedProcess[str]:
    cmd = f"echo {stderr!r} >&2; exit {rc}"
    return subprocess.run(["bash", str(PROBE), "probe", "sh", "-c", cmd], capture_output=True, text=True, check=False)


def test_probe_passes_only_on_permission_denial() -> None:
    assert _probe("ERROR: HTTPError 403: x does not have storage.objects.list access", 1).returncode == 0
    assert _probe("ERROR: The caller does not have permission. PERMISSION_DENIED", 1).returncode == 0
    # gcloud's wording for project IAM reads (observed in the 27 Nov identity verification run)
    assert _probe("ERROR: (gcloud.projects.get-iam-policy) [sa@x.iam.gserviceaccount.com] does not have permission "
                  "to access projects instance [p:getIamPolicy] (or it may not exist): The caller does not have "
                  "permission.", 1).returncode == 0
    assert _probe("", 0).returncode == 1                                  # success: isolation broken
    assert _probe("ERROR: Could not resolve host", 1).returncode == 1     # network: inconclusive
    assert _probe("ERROR: HTTPError 404: not found", 1).returncode == 1   # wrong name: inconclusive


def test_isolation_steps_use_classified_probes_only() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/gcp-identity-verify.yml").read_text())
    for job in wf["jobs"].values():
        step = next(s for s in job["steps"] if s.get("name", "").startswith("Isolation"))
        body = step["run"]
        assert "expect_denied.sh" in body and "set +e" not in body and ">/dev/null 2>&1 &&" not in body
        assert "tiva-cri-ops-cda9-tfstate/" in body           # retired backend probed too


def test_terraform_apply_refuses_while_frozen() -> None:
    body = (ROOT / ".github/workflows/gcp-terraform.yml").read_text()
    assert "infra/MIGRATION_FREEZE" in body and (ROOT / "infra/MIGRATION_FREEZE").exists()
    assert 'env="${{ inputs.environment }}"' in body


def test_committed_backends_never_target_the_retired_bucket() -> None:
    for env in ("dev", "staging", "production"):
        tf = (ROOT / f"infra/terraform/envs/{env}/main.tf").read_text()
        assert f'bucket = "tiva-cri-ops-cda9-tfstate-{env}"' in tf


def test_stage1_freezes_old_backend_and_demands_exact_copy() -> None:
    s = (ROOT / "infra/bootstrap/harden-stage1.sh").read_text()
    assert "--temporary-hold" in s and "roles/storage.objectViewer" in s
    assert "is not an exact copy" in s and 'frozen" = 1' in s
    assert "public_access_prevention" in s and "versioning_enabled" in s
