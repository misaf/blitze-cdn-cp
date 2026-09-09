# Decision records

Design rationale that spans more than one module, so that the modules
themselves can say what they do and point here for why.

A record is written when the reasoning is architectural — it constrains
several files, or explains why an arrangement a reader would expect was
rejected. Rationale local to one function stays in that function's docstring.

| # | Decision |
|---|---|
| [0001](0001-zone-policy-and-composition.md) | Zone policy, derived hosts, and composition ownership |
| [0002](0002-capability-configuration-ownership.md) | Capability configuration ownership |
| [0003](0003-what-a-capability-puts-on-an-edge.md) | What a capability puts on an edge host |
| [0004](0004-what-each-delivery-surface-carries.md) | What each delivery surface carries |
| [0005](0005-canonical-writes-and-derived-state.md) | Canonical writes, and what a derivation does with state it refuses |

Each record ends with a *Code and verification* section listing the modules it
governs and the tests that hold it. When a decision changes, the record changes
with it — a record describing an arrangement the code no longer has is worse
than no record.
