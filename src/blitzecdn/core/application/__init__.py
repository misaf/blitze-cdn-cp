"""The little application logic core is allowed to have.

One module: durable workflow progress, which is how any capability records
that external work reached a checkpoint and how a restarted process finds
what was left unfinished. It is here rather than in a capability because
deployments and certificate issuance both keep a journal, and neither owns
the idea.

The package is not an invitation. A service that orchestrates capabilities is
a vertical slice and belongs in `capabilities/`, which is where
`MaintenanceService` went; `test_core_carries_no_cross_capability_application_service`
is what keeps one from reappearing here.
"""
