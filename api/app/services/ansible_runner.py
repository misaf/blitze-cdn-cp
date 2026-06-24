import subprocess

from app.schemas import DeploymentResult
from app.settings import ANSIBLE_DIR, ANSIBLE_PLAYBOOK_BIN, PLAYBOOK_FILE


def run_ansible_playbook() -> DeploymentResult:
    ansible_playbook = ANSIBLE_PLAYBOOK_BIN if ANSIBLE_PLAYBOOK_BIN.exists() else "ansible-playbook"
    result = subprocess.run(
        [str(ansible_playbook), str(PLAYBOOK_FILE)],
        cwd=ANSIBLE_DIR,
        check=False,
        capture_output=True,
        text=True,
    )
    return DeploymentResult(
        started=True,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
