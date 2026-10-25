"""What the control plane ships but does not import as code: the platform's
Ansible tree, the image build inputs and the Alembic migrations. Each is read
by path, through :func:`blitzecdn.core.runtime.resources.package_directory`."""
