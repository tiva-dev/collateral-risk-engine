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
        # Only a branch filter (branches OR branches-ignore) keeps tags out; GitHub
        # rejects a workflow that sets both on the same event.
        has_b, has_bi = bool(push.get("branches")), bool(push.get("branches-ignore"))
        assert has_b or has_bi, f"{path.name}: push trigger without a branch filter also fires on tags"
        assert not (has_b and has_bi), f"{path.name}: branches and branches-ignore together make the workflow invalid"
        assert "tags" not in push and "tags-ignore" not in push, f"{path.name}: push trigger must not name tags"
        checked += 1
    assert checked >= 8


def test_deploy_and_terraform_refuse_tag_refs() -> None:
    for name in ("gcp-deploy.yml", "gcp-terraform.yml"):
        body = (WORKFLOWS / name).read_text()
        assert 'case "${{ github.ref }}" in refs/tags/*)' in body, name
