from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = API_DIR.parent
ANSIBLE_DIR = PROJECT_DIR / "ansible"
DATA_DIR = API_DIR / "data"
SITES_FILE = DATA_DIR / "cdn_sites.yml"
ANSIBLE_VARS_FILE = ANSIBLE_DIR / "generated" / "nginx_cdn_sites.yml"
PLAYBOOK_FILE = ANSIBLE_DIR / "playbooks" / "defaults.yml"
ANSIBLE_PLAYBOOK_BIN = ANSIBLE_DIR / ".venv" / "bin" / "ansible-playbook"
