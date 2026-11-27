"""Release-tag pushes must not start deployments, infrastructure operations or validation runs.

On 27 Nov 2026 pushing the annotated tag cri-1.0.0-rc1 started GCP Deploy, Identity
Verification and Candidate Validation on the tagged commit, because path-filtered
`push` triggers without a branch filter also fire for tags.
"""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"


def test_push_triggers_are_branch_only() -> None:
    checked = 0
    for path in sorted(WORKFLOWS.glob("*.yml")):
        wf = yaml.safe_load(path.read_text())
        on = wf.get(True, wf.get("on"))
        if not isinstance(on, dict) or "push" not in on:
            continue
        push = on["push"] or {}
        assert push.get("branches"), f"{path.name}: push trigger without a branch filter also fires on tags"
        assert "tags" not in push, f"{path.name}: push trigger must not run on tags"
        checked += 1
    assert checked >= 8


def test_deploy_and_terraform_refuse_tag_refs() -> None:
    for name in ("gcp-deploy.yml", "gcp-terraform.yml"):
        body = (WORKFLOWS / name).read_text()
        assert 'case "${{ github.ref }}" in refs/tags/*)' in body, name
