#!/usr/bin/env bash
# Superseded on 27 Nov 2026: retiring the shared identity is now a separate,
# gated stage. Use harden-stage1.sh, then harden-stage2.sh after verification.
echo "harden.sh is superseded: run infra/bootstrap/harden-stage1.sh, then harden-stage2.sh" >&2
exit 1
