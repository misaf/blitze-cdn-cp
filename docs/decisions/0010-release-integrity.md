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

## Code and verification

- [Canonical documents](../../src/blitzecdn/core/domain/documents.py)
- [Artifact and compiled values](../../src/blitzecdn/capabilities/releases/domain/release.py)
- [Verified persistence](../../src/blitzecdn/capabilities/releases/adapters/persistence.py)
- [Integrity regressions](../../tests/capabilities/releases/test_integrity.py)
