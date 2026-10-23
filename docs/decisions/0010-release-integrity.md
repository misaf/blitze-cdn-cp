# Release documents have immutable canonical content

Status: accepted; first implementation step of the unreleased application rebuild.

## Decision

An artifact stores canonical JSON text. Its decoded dictionary is a disposable
working copy, never the source of its digest. Compiled site variables use the
same representation. JSON values must be encodable without a fallback string
conversion, non-finite numbers, or non-string object keys.

Release persistence refuses mismatched canonical inputs and verifies release
identity and input identity when reading stored documents. Frozen model fields
alone do not provide these guarantees: a dictionary inside a frozen model is
still mutable.

This intentionally changes the internal stored release representation. There
are no deployed databases or compatibility obligations requiring an upcaster.

## Certificate dependencies

Certificate chains and keys are stored by their individual SHA-256 content
identities. Leaf fingerprints remain certificate identity for operator reports;
they cannot identify a complete chain or its exact key encoding. Replacing the
current metadata pointer retains previous files, because retained releases may
still name them. Garbage collection is deliberately conservative: historical
material is retained until reference-aware cleanup is implemented.

Prepared desired state includes the expected digests. Before copying material,
the Nginx role verifies controller files against those captured values, including
when deploying an older release. Missing or modified material refuses the run.

## Code and verification

- [Canonical documents](../../src/blitzecdn/core/domain/documents.py)
- [Artifact and compiled values](../../src/blitzecdn/capabilities/releases/domain/release.py)
- [Verified persistence](../../src/blitzecdn/capabilities/releases/adapters/persistence.py)
- [Integrity regressions](../../tests/capabilities/releases/test_integrity.py)

- [Versioned certificate storage](../../packages/blitzecdn-certificates/src/blitzecdn_certificates/certificates/adapters/store.py)
- [Material verification](../../src/blitzecdn/ansible/roles/blitzecdn_nginx/tasks/verify-material.yml)
- [Executable material contracts](../../tests/contract/test_validation_contracts.py)
