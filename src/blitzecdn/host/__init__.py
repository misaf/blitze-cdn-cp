"""Helpers shipped in the application image for host lifecycle operations.

The installer uses ``install_inputs`` and ``image_releases`` before convergence;
``compose_files`` stages and publishes configuration for the host CLI wrapper.
Container lifecycle belongs to Docker Compose on the host.
"""
