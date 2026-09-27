# Conformance audit findings

The implementation passes the frozen scope `public-host-2026-09-27`. All 11 defect groups found by the audit have been fixed, and their independent acceptance tests remain in place as regressions.

The three domain reviews produced 191 requirement groups. Every nonblank line in the five pinned specification snapshots belongs to a reviewed section or an explicit exclusion. The acceptance gate runs 122 distinct test methods, including 2,114 subtests. All 125 executable host requirement groups pass, with zero failures or evidence gaps. The other groups retain their explicit ambiguity, device/caller, or exclusion dispositions. The original 15 failing groups mapped to the 11 defects below.

There are no unreviewed source sections or missing executable-evidence gaps in this inventory. That is a traceability result, not a proof that every human interpretation or possible execution is correct. The [scope and stopping rule](SCOPE.md) define the claim; [RESULTS.md](RESULTS.md) contains the generated acceptance matrix. Exact source passages, implementation anchors and test bindings are in the [Text](text_requirements.json), [HEX](hex_requirements.json) and [register](register_requirements.json) ledgers.

## Resolved defects

Every row below is fixed. The defect column records the observed baseline behavior; the acceptance conditions remain regression requirements.

| ID | Defect and observed consequence | Acceptance condition | Requirements |
| --- | --- | --- | --- |
| F01 | **Corrupt Text measurements are published.** After a known valid block, changing `I=100` to `I=101` without updating the checksum still emits the changed measurement because the parser finds an accidentally valid suffix. | Reject that complete invalid block, then recover for the next valid block. Preserve documented mixed Text/HEX behavior. | TXT-010, TXT-054 |
| F02 | **Cancellation during write/drain leaves response routing trusted.** Bytes may already be transmitted before the code sets its `wrote` flag. A subsequent request can receive the cancelled request's response. | Fail closed whenever transmission may have occurred; waiting for a lock before any transmission remains safe to cancel. Cover both GET and SET. | HEX-032 |
| F03 | **Restart makes buffered stale replies eligible for new requests.** Clearing the desynchronization flag does not remove complete or partial responses already buffered on the host. | Deal with stale host input and parser state before allowing routing to resume. A fresh request must receive its own reply. | HEX-035 |
| F04 | **ProductId command replies use the wrong format rules.** The documented Orion reply `:1F0A3C1` is rejected; MPPT RS and unspecified-family handling have the same problem. The simulator repeats the mistaken register-format assumption. | Accept the documented two-byte command reply independently of family. Keep the four-byte product-information register layout separate, and correct the simulator. | HEX-011, HEX-012, HEX-042 |
| F05 | **Async notifications can wait indefinitely despite a queued event.** A concurrent Text reader can queue the notification while its consumer waits for the reader lock; that consumer then waits for additional serial input. | Deliver the already queued event without requiring another byte. | HEX-037 |
| F06 | **Boot failure is delivered to a later unrelated request.** `boot()` leaves `:40051` pending, so a subsequent successful ping receives the boot error. | Consume or report the error as belonging to boot and preserve later response routing; never assign it to the next command. | HEX-019, HEX-021 |
| F07 | **Hexadecimal Text off-reason values stay strings.** For example, `OR=0x00000010` has no numeric mask or human interpretation. | Decode the documented hexadecimal representation into its numeric mask and meanings. | TXT-035 |
| F08 | **BMV String20 writes exceed their maximum width.** Encoding 20 ASCII characters appends NUL and sends 21 bytes. Longer inputs are also accepted. | Never produce more than 20 wire bytes. The boundary test permits explicit rejection where a terminator is required. | REG-062 |
| F09 | **Small MPPT battery types cannot be selected through the typed register API.** Small 10A/15A/20A models use raw `1` for `TYPE_2`; generic MPPT uses the large-model table. | Provide explicit small-model context for typed reads and writes while preserving the large-model mapping. Tests propose `MPPT_SMALL`; an equivalent explicit selector is acceptable. | REG-016 |
| F10 | **Valid unpadded FWE values are rejected.** `0008FF` decodes as version 0.08, but equivalent `8FF` fails the implementation's undocumented five-character minimum. | Padded and unpadded encodings decode equivalently under the documented optional-padding rule. | TXT-030 |
| F11 | **Phoenix APP_VER is decoded using Orion's layout.** `00 17 41 00` yields an unknown version type instead of application version 1.17. | Use Phoenix's minor byte and major/software-type byte layout independently of Orion. The test gives no meaning to the unspecified fourth byte. | REG-074 |

F01 through F09 reproduce the earlier findings. F10 and F11 were found while traversing the frozen source requirements. All 13 previously nonpassing methods now pass. Requirement statements, dispositions, pinned specification references and test bindings remain unchanged. README citations were relocated during its rewrite, with the inventory review recorded in `scope.json`; no acceptance obligation was removed or waived.

Validation on 27 September 2026: the frozen acceptance gate passes all 122 methods and 2,114 subtests on Python 3.9 and 3.14. The ordinary suite passes all 269 tests with no skips on both Python 3.9 and 3.14, including live PDF freshness checks through Poppler. Three ordinary test fixtures were moved into async execution contexts for Python 3.9; their assertions did not change. The ledger has zero issues, and the generated implementation fingerprint matches the current production source.

## Source conflicts and device obligations

The report separately identifies 14 ambiguous requirement groups, 48 device/caller obligations, and 4 excluded groups. Their exact reasons remain in the ledgers. Ambiguities include contradictory register IDs or enum meanings, unspecified byte layouts, and an erroneous published checksum example. Device obligations include electrical behavior, charging actions, persistence, telemetry timing, and model/firmware availability. Passing a host test does not establish those physical behaviors.

These classifications constrain the supported-scope claim. They are not a claim that the ambiguous parts of the specification have been settled. A clear failing requirement cannot be reclassified as an ambiguity merely to pass the gate.

## Completion

F01 through F11 are closed. Superseded ordinary expectations for ProductId command replies and unpadded FWE were corrected against the source evidence. The CI workflow now runs the frozen acceptance gate as well as the ordinary suite on Python 3.9, 3.12 and 3.14.

The implementation meets this acceptance scope with the stated limitations. Further review needs a reproducible failure, a demonstrated omitted requirement, a scope expansion, or a specification revision. Repeating unrestricted searches for hypothetical problems is not part of this stopping rule.
