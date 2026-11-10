"""Rebuild the local pose-preserving image from the official SO101 driver.

Run with Python 3 from the repository root. Requires Docker and system patch.
Does not change the configured driver, start a service, or access hardware.
"""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASE = "cyberwaveos/so101-driver@sha256:90e54614d43e74e9648a020ec59198632453da1454c92019a3108a45acd01450"


def main():
    with tempfile.TemporaryDirectory(prefix="so101-image-") as tmp:
        context = Path(tmp)
        container = subprocess.check_output(
            ["docker", "create", "--entrypoint", "/bin/true", BASE], text=True).strip()
        try:
            for path in ("main.py", "so101/follower.py", "scripts/cw_remoteoperate.py", "utils/cw_remoteoperate_helpers.py"):
                dest = context / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["docker", "cp", f"{container}:/app/{path}", str(dest)], check=True)
        finally:
            subprocess.run(["docker", "rm", container], check=True)
        subprocess.run(["patch", "--batch", "-p1", "-i", str(ROOT / "build/cyberwave-preserve-pose.patch")],
                       cwd=context, check=True)
        subprocess.run(["patch", "--batch", "-p1", "-i", str(ROOT / "build/cyberwave-local-trajectory.patch")],
                       cwd=context, check=True)
        verify_workflow_inputs(context)
        subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(ROOT / "build/cyberwave-workflow-v1.patch")],
                       cwd=context, check=True)
        shutil.copyfile(ROOT / "build/cyberwave_hold.py", context / "so101/session_hold.py")
        shutil.copyfile(ROOT / "build/cyberwave_trajectory.py", context / "utils/cw_local_trajectory.py")
        shutil.copyfile(ROOT / "build/cyberwave_workflow_driver.py", context / "utils/cw_workflow.py")
        shutil.copyfile(ROOT / "build/coordinated_driver.py", context / "utils/cw_coordinated_driver.py")
        shutil.copyfile(ROOT / "build/workflow_protocol.py", context / "utils/cw_workflow_protocol.py")
        (context / "Dockerfile").write_text(
            f"FROM {BASE}\nCOPY main.py /app/main.py\nCOPY so101/ /app/so101/\nCOPY scripts/ /app/scripts/\nCOPY utils/ /app/utils/\n"
            "ENV CYBERWAVE_PRESERVE_POSE=1\n"
            "RUN python3 -m compileall -q /app/so101 /app/scripts /app/utils\n")
        subprocess.run(["docker", "build", "-t", "so101-local:workflow-v1", str(context)], check=True)


def verify_workflow_inputs(context):
    manifest = json.loads((ROOT / 'build/cyberwave-workflow-v1.json').read_text())
    if manifest['base_image'] != BASE:
        raise RuntimeError('Workflow manifest base image differs from builder.')
    for path, expected in manifest['sha256'].items():
        if hashlib.sha256((context / path).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Unreviewed driver source differs from workflow-v1 manifest: '+path)


if __name__ == "__main__":
    main()
