# Real-edge rehearsal: preparation checkpoints

The user authorized the previously deferred Ubuntu rehearsal after completing
the five controller operating/recovery steps. This record covers preparation
against application commit `fc8bc06`, investigated on 2026-10-25. All builds
and executable checks ran on the supplied remote server; none ran on the
workstation.

## Isolation and provenance

- The existing Ubuntu 24.04 host and its Nginx service are separate from the
  rehearsal. The test environment is an Ubuntu 26.04 KVM guest, using an official
  cloud image checked against its published SHA-256 manifest. Guest SSH is
  forwarded only on the host's loopback interface.
- Host Ubuntu packages use the Arvan mirror. Both Docker daemons use
  `https://docker.arvancloud.ir` as their registry mirror, with TLS verification
  enabled. The guest has Docker CE 29.9.2 and Compose 5.6.1.
- The edge image was built from the committed Dockerfile and its resolved module
  arguments. Nginx 1.31.4, HTTP/3 build support, configured module directives and
  `nginx -t` passed during the build. The image was loaded into the guest as
  `blitzecdn-edge:rehearsal`; this does not yet prove site activation.
- The pinned Ansible collections were copied from existing packages after the
  Galaxy endpoint returned HTTP 403. Collection versions and every file listed
  in their package manifests were verified before use.

## DNS and download investigation

The requested resolver `172.16.200.20` timed out from both the host and guest;
host tests covered UDP and TCP. It was removed after the failed trial.
The replacement `94.183.119.90` answered over both protocols and is configured
persistently through a systemd-resolved drop-in on both machines. A fresh guest
container lists it first in `/etc/resolv.conf`.

DNS resolution succeeds, but HTTPS behavior still differs between environments:

- The host and Python 3.13 Bookworm container established TCP connections to all
  four returned IPv4 addresses for `files.pythonhosted.org`, then timed out in
  TLS handshakes. TLS 1.2, TLS 1.3 and a per-socket TCP maximum segment size of
  1200 did not resolve those failures. These checks did not change network MTU.
- The guest downloaded the exact locked `ansible-core` 2.21.3 wheel through three
  of those addresses with certificate verification enabled. Each download was
  2,446,988 bytes and matched the SHA-256 recorded in `uv.lock`:
  `9e7dd367f7dc5d5e9fc5ae1baf8af9c4edc09e916a73a40108a3f32e3ad93f10`.
  The fourth address timed out during TLS in that comparison.
- These observations do not establish the cause of the TLS stalls. They show
  that changing DNS alone did not make every download path reliable.

## Build transport used for the rehearsal

The ordinary online controller build failed while fetching dependencies. A
rehearsal-only build recipe outside the Python package copies in verified
collections and a dependency cache prepared on the guest. Python dependencies
are still installed with `uv sync --frozen --no-dev --no-editable`; that stage
runs with `UV_OFFLINE=1`. The cache was prepared with Python 3.13 and **uv 0.12.3**,
matching the production Dockerfile. A cache previously prepared with uv 0.12.16
did not supply the build requirements successfully to that pinned image tool.

The packaged Dockerfile and application source remain unchanged. The guest's
installer selects the alternate recipe through the role's existing
`blitzecdn_controlplane_dockerfile` variable. This cache-copy recipe is a test
transport workaround, not a change to the repository's supported installer.

## Verified build checkpoint

- Compose built `blitzecdn-control-plane:local` successfully for the API and
  worker. The offline Python stage installed 53 packages; Ansible reported that
  all requested collections were already installed.
- A fresh container compared all **485 committed package files** with the
  installed wheel using SHA-256: **zero mismatches**. The loaded distribution
  reports version 5.0.0 and resides in the image's `site-packages` directory.
- The installed `blitzecdn --help` command exited successfully as UID 65534,
  the image's unprivileged account. This smoke check did not start services or
  initialize controller state.
- The existing host Nginx remained active with its original process ID and the
  same SHA-256 of its HTTP response. Documentation file links were checked on
  the remote server: 35 targets existed.

## Remaining acceptance work

Controller service startup, real Ansible site deployment, externally observed
Nginx release identity, rollback, worker interruption/reclaim, ACME
issuance/renewal and controller backup/rebuild remain unverified by this
checkpoint. Preparation does not close the gaps listed in
[RECOVERY_ACCEPTANCE.md](RECOVERY_ACCEPTANCE.md). Each subsequent step requires
the user's confirmation and its own commit.

## Direct host after the fresh OS installation

On 2026-10-25 the user reinstalled the disposable server with Ubuntu 26.04.1
and requested that the rehearsal run directly on it. The earlier VM observations
above remain historical evidence; no VM was created on the fresh installation.
The source snapshot copied to `/opt/blitzecdn` is commit `a6ba9cb`.

Completed and checked remotely:

- Both Ubuntu archive and security source entries use
  `http://mirror.arvancloud.ir/ubuntu`, retaining Ubuntu's archive signing key
  and the Resolute suites. `apt-get update --error-on=any` succeeded and fetched
  34.9 MB of indexes. Cloud-init is configured to preserve these sources.
- `94.183.119.90` is the active global systemd-resolved DNS server, with a
  persistent configuration and the `~.` routing domain. A direct UDP query
  returned a successful answer in 6 ms; system hostname resolution also passed.
  The interface still lists DHCP-provided resolvers. A fresh container lists
  the requested resolver first, followed by those additional addresses.
- Docker Engine 29.9.2 and Compose 5.6.1 are installed from Docker's signed APT
  repository. Docker reports `https://docker.arvancloud.ir/` as its active image
  mirror. A systemd service override preserves this setting when the application
  role writes its daemon configuration; TLS verification remains enabled.
- Pulling `docker.arvancloud.ir/library/python:3.13-slim-bookworm` succeeded,
  with digest
  `sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530`.
  Docker and systemd-resolved are active.
- All four pinned collection versions and **4,210 file hashes** matched their
  package manifests after transfer to the fresh host.

The ordinary standalone installer was attempted twice. The first attempt failed
fetching `pycparser` 3.0 from `files.pythonhosted.org`; a retry with serialized
downloads failed fetching `cryptography` 50.0.0. Both were connection timeouts
during bootstrap, before controller convergence. No application containers or
initialized controller database exist on this installation yet. The previously
verified VM image was saved on the old host before the reinstall, but its
continued presence must not be assumed after a fresh OS installation.

A Tsinghua PyPI mirror index returned HTTP 200 during investigation. No package
from it has been installed or accepted as verified at this checkpoint. The next
installation step can prepare an offline bundle and verify every runtime wheel
against `uv.lock` before use; the lockfile and source must remain unchanged.

Repository references: [Arvan Ubuntu mirror instructions](https://www.arvancloud.ir/en/dev/linux-repository),
[Arvan Docker image mirror instructions](https://www.arvancloud.ir/fa/dev/docker)
and [Tsinghua PyPI mirror documentation](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/).
