# Blitze CDN

Blitze CDN is a small CDN edge management project made of two parts:

- `api/` - a FastAPI control plane for creating and updating CDN site definitions.
- `ansible/` - Ansible automation for provisioning edge servers and rendering Nginx CDN configs.

The API and Ansible project work together through shared YAML files. The API stores
site definitions in `api/data/cdn_sites.yml`, then writes the same data in an
Ansible-ready format to `ansible/generated/nginx_cdn_sites.yml`. The Ansible
playbook loads that generated file and applies the Nginx configuration to the
edge servers.

## Repository Structure

```text
api/
  app/                         FastAPI application
  data/cdn_sites.yml           API-managed CDN site store
  README.md                    API usage details
ansible/
  playbooks/defaults.yml       Main edge provisioning playbook
  inventory/main.yml           Ansible inventory
  roles/                       Base, sysctl, firewall, nginx, fail2ban roles
  generated/nginx_cdn_sites.yml API-generated Nginx CDN vars
  README.md                    Ansible usage details
```

## How It Works

1. Add or update CDN sites through the API.
2. The API validates the site payload and writes YAML data.
3. Ansible reads `ansible/generated/nginx_cdn_sites.yml` before running roles.
4. The Nginx role renders one Nginx site config per CDN site.
5. The API can optionally trigger the Ansible playbook through `POST /deploy`.

## Setup

Install the Ansible dependencies:

```bash
cd ansible
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/ansible-galaxy collection install -r requirements.yml
```

Install the API dependencies:

```bash
cd ../api
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

## Run the API

```bash
cd api
.venv/bin/uvicorn app.main:app --reload
```

The API will be available at `http://127.0.0.1:8000`.

Useful endpoints:

- `GET /health` - health check.
- `GET /cdn-sites` - list CDN sites.
- `POST /cdn-sites` - create a CDN site and generate Ansible vars.
- `PATCH /cdn-sites/{name}` - update a CDN site.
- `DELETE /cdn-sites/{name}` - delete a CDN site.
- `POST /deploy?wait=true` - run Ansible and return the result.
- `POST /deploy` - queue an Ansible run in the background.

## Create a CDN Site

```bash
curl -X POST http://127.0.0.1:8000/cdn-sites \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "client-a",
    "server_names": ["cdn.client-a.com"],
    "origin_host": "192.0.2.10",
    "ssl_enabled": true,
    "ssl_provider": "letsencrypt",
    "letsencrypt_email": "admin@example.com",
    "http2_enabled": true,
    "cache_enabled": true,
    "cache_valid_success": "30m"
  }'
```

After this request, the API updates:

- `api/data/cdn_sites.yml`
- `ansible/generated/nginx_cdn_sites.yml`

## Run Ansible Directly

Validate the playbook:

```bash
cd ansible
.venv/bin/ansible-playbook --syntax-check playbooks/defaults.yml
HOME="$PWD" .venv/bin/ansible-lint playbooks/defaults.yml
```

Apply the playbook:

```bash
cd ansible
.venv/bin/ansible-playbook playbooks/defaults.yml
```

## Deploy Through the API

```bash
curl -X POST 'http://127.0.0.1:8000/deploy?wait=true'
```

The API uses `ansible/.venv/bin/ansible-playbook` when it exists. If that binary
does not exist, it falls back to `ansible-playbook` from `PATH`.

## Production Notes

- Configure `ansible/inventory/main.yml` before running against real edge servers.
- Keep SSH keys and private SSL certificates out of Git.
- Use `ssl_provider: custom` for multi-node CDN clusters unless certificate
  issuance is coordinated outside each edge node.
- The `firewall` role is currently a placeholder, so add firewall rules before
  exposing production servers.
- See `api/README.md` and `ansible/README.md` for more detailed options.
