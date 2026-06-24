# Blitze CDN API

FastAPI control plane for managing CDN site definitions used by the Ansible project.

The API stores CDN sites in `api/data/cdn_sites.yml` and writes Ansible-ready
vars to `ansible/generated/nginx_cdn_sites.yml`.

## Run locally

```bash
cd api
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
```

## Endpoints

- `GET /health` - health check.
- `GET /cdn-sites` - list configured CDN sites.
- `POST /cdn-sites` - create a CDN site and write Ansible vars.
- `PATCH /cdn-sites/{name}` - update a CDN site.
- `DELETE /cdn-sites/{name}` - delete a CDN site.
- `POST /deploy?wait=true` - run the Ansible playbook and return output.
- `POST /deploy` - queue an Ansible run as a FastAPI background task.

## Create a CDN site

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

Supported fields include:

- `name`
- `server_names`
- `origin_host`
- `origin_scheme`
- `origin_request_host`
- `origin_sni`
- `enabled`
- `ssl_enabled`
- `ssl_provider`
- `letsencrypt_manage`
- `letsencrypt_email`
- `http2_enabled`
- `cache_enabled`
- `cache_valid_success`
- `cache_valid_not_found`

## Generated Files

The API writes:

- `api/data/cdn_sites.yml`
- `ansible/generated/nginx_cdn_sites.yml`

## Deploy

```bash
curl -X POST 'http://127.0.0.1:8000/deploy?wait=true'
```

The deploy endpoint uses `ansible/.venv/bin/ansible-playbook` when it exists;
otherwise it falls back to `ansible-playbook` from `PATH`.
