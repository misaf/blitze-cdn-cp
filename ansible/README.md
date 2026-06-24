# Blitze CDN Ansible

Ansible automation for provisioning CDN edge servers. The main playbook installs base packages, applies system tuning, configures Nginx as a CDN reverse proxy/cache, and enables Fail2Ban.

## Structure

- `playbooks/defaults.yml` - main playbook.
- `inventory/main.yml` - host inventory.
- `inventory/host_vars/` - per-host configuration.
- `roles/base` - base packages and timezone.
- `roles/sysctl` - kernel/network tuning.
- `roles/firewall` - firewall role placeholder.
- `roles/nginx` - CDN reverse proxy/cache role.
- `roles/fail2ban` - Fail2Ban installation and SSH jail.
- `generated/nginx_cdn_sites.yml` - optional CDN site vars written by the API.

## Setup

Install Python requirements into the local virtualenv:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/ansible-galaxy collection install -r requirements.yml
```

Run validation:

```bash
.venv/bin/ansible-playbook --syntax-check playbooks/defaults.yml
HOME="$PWD" .venv/bin/ansible-lint playbooks/defaults.yml
```

Run the playbook:

```bash
.venv/bin/ansible-playbook playbooks/defaults.yml
```

If the FastAPI control plane has created CDN sites, the playbook automatically
loads `generated/nginx_cdn_sites.yml`.

## CDN Sites

Define CDN domains in host vars or group vars with `nginx_cdn_sites`. Each item creates a separate Nginx config file:

```yaml
nginx_cdn_sites:
  - name: example-cdn
    server_names:
      - example.com
      - www.example.com
    origin_host: 192.168.1.10
    http2_enabled: true
    cache_enabled: true
```

This renders:

```text
/etc/nginx/sites-available/example-cdn.conf
/etc/nginx/sites-enabled/example-cdn.conf
```

The origin can be an IP. By default, Nginx proxies to the origin IP while preserving the client-requested host:

```nginx
proxy_pass https://192.168.1.10;
proxy_set_header Host $host;
```

If an origin expects a different host header or TLS SNI name, set:

```yaml
origin_request_host: origin.example.com
origin_sni: origin.example.com
```

## Cache Settings

Global cache defaults live in `roles/nginx/defaults/main.yml`:

```yaml
nginx_cache_path: /var/cache/nginx/cdn
nginx_cache_max_size: 10g
nginx_cache_valid_success: 10m
nginx_cache_valid_not_found: 1m
```

Per-site overrides are supported:

```yaml
nginx_cdn_sites:
  - name: client-a
    server_names:
      - cdn.client-a.com
    origin_host: 192.168.1.10
    cache_enabled: true
    cache_valid_success: 30m
    cache_valid_not_found: 30s
```

Set `cache_enabled: false` on a site to bypass the shared Nginx cache for that
domain.

## SSL

SSL is optional per site:

```yaml
ssl_enabled: true
```

The role supports two SSL providers:

- `custom` - deploy an existing/private certificate to every edge node. This is the default and recommended for clusters.
- `letsencrypt` - use Certbot on the server. This is useful for single-node setups or carefully coordinated ACME workflows.

### Custom Certificates

For CDN clusters such as `192.168.1.11-192.168.1.15`, issue or receive the certificate once, then deploy the same cert/key to all edge nodes:

```yaml
nginx_cdn_sites:
  - name: example-cdn
    server_names:
      - example.com
    origin_host: 192.168.1.10
    ssl_enabled: true
    ssl_provider: custom
    ssl_certificate_src: files/certs/example.com/fullchain.pem
    ssl_certificate_key_src: files/certs/example.com/privkey.pem
```

The role copies them to:

```text
/etc/nginx/ssl/example-cdn/fullchain.pem
/etc/nginx/ssl/example-cdn/privkey.pem
```

You can also reference certificate files that already exist on the target servers:

```yaml
ssl_certificate: /etc/nginx/ssl/example.com/fullchain.pem
ssl_certificate_key: /etc/nginx/ssl/example.com/privkey.pem
```

### Let’s Encrypt

Let’s Encrypt certificate creation is opt-in:

```yaml
nginx_letsencrypt_email: admin@example.com
nginx_letsencrypt_manage_certificates: true

nginx_cdn_sites:
  - name: example-cdn
    server_names:
      - example.com
    origin_host: 192.168.1.10
    ssl_enabled: true
    ssl_provider: letsencrypt
    letsencrypt_manage: true
    letsencrypt_email: admin@example.com
```

For multi-node clusters, avoid running HTTP-01 issuance independently on every edge. Use DNS-01 or a central certificate job, then deploy certificates with `ssl_provider: custom`.

## API Integration

The sibling FastAPI project can write CDN site definitions to
`generated/nginx_cdn_sites.yml`. That file has the same shape as the normal
`nginx_cdn_sites` variable and overrides host/group vars when present.

Example generated content:

```yaml
nginx_cdn_sites:
  - name: client-a
    server_names:
      - cdn.client-a.com
    origin_host: 192.0.2.10
    ssl_enabled: true
    ssl_provider: letsencrypt
    letsencrypt_manage: true
    letsencrypt_email: admin@example.com
    http2_enabled: true
    cache_enabled: true
```

## Notes

- `ansible.cfg` writes local logs to `.ansible/ansible.log`.
- Keep SSH private keys and client private certificates out of Git.
- `roles/firewall` is currently a placeholder; enable firewall rules before exposing production edges.
