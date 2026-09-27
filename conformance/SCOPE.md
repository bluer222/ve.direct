Finite VE.Direct host conformance audit

The acceptance scope is `public-host-2026-09-27`. It covers the public Text receiver, HEX commands and responses, documented register codecs, and the async request and delivery contracts advertised by this library. It uses the five specific document revisions and content hashes in [scope.json](scope.json). The snapshots matched freshly downloaded official PDFs on 27 September 2026 after layout extraction and whitespace normalization.

This audit has a stopping rule. Every substantive document section receives a disposition in the three requirement ledgers. Host requirements need executable evidence. A demonstrated violation, an untested host requirement, a skipped required test, or an unclassified source section blocks acceptance. Device behavior and genuine specification conflicts remain visible with their reasons. They do not silently become test passes.

The ledger records requirement groups. A table-wide schema check can cover a whole table; a test using one example cannot establish every value or behavior in that section. The notes describe the bounds of the evidence. Register presence, scalar types, units, scales, permissions, and the independently checked table maps reuse the existing specification tests. New tests cover wire examples, parser behavior, structured records, and transaction transitions. The static disposition `tested` means executable evidence exists; it does not mean the test passes. The generated result supplies PASS or FAIL, so fixing an implementation defect can change the result without rewriting its requirement.

No finite test suite proves that every possible defect has been found. This audit closes a defined checklist and records which evidence supports it. Its conclusion must retain the scope, source conflicts, and device obligations below.

Run from the repository root, with Python 3.9 or newer and the project's serial extra installed:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 conformance/run.py
```

The pseudo-terminal transport tests need a POSIX host. On Nix, a complete environment is:

```sh
nix-shell -p 'python314.withPackages (ps: [ ps.pyserial ps.pyserial-asyncio ])' --run 'PYTHONDONTWRITEBYTECODE=1 python3 conformance/run.py'
```

The command writes [RESULTS.md](RESULTS.md) and its machine-readable JSON companion. Exit code 0 means every required check passed within this stated scope. Exit code 1 means the implementation or its evidence remains blocked. Failing acceptance tests are ordinary failures; they are not marked expected failures. The ordinary test suite remains a separate check:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

`python3 conformance/run.py --check-ledger` verifies source hashes, source-section accounting, requirement IDs, file anchors, test references, and whether every new acceptance test belongs to a requirement. It does not execute protocol tests. It exits unsuccessfully for explicit evidence gaps as well as malformed ledger entries. Use the serial environment even for this command so test imports resolve.

The gate is offline and repeatable. Live document freshness is a separate maintenance task; a remote re-upload cannot move the finish line during this audit. A changed pinned snapshot makes the gate fail until the change is reviewed and the scope is deliberately revised. A separate hash freezes the requirement statements, dispositions, source references, and test bindings. Removing a failing obligation or reclassifying it as excluded therefore cannot silently turn the gate green. Changing that inventory requires a recorded scope review before resealing it.

The host contract is bounded as follows:

- Text processing accepts documented records, block structure, signed values, missing values, firmware encodings, units, and family field variants. It validates block checksums and handles mixed Text/HEX traffic. It must not turn a known invalid block into an apparently validated measurement.
- HEX processing implements the public request and response shapes, byte order, checksums, status flags, documented command examples, and firmware encodings. A command response and a register value with a similar name remain distinct formats.
- Register codecs cover the public rows and documented structured layouts for MPPT, MPPT RS where BlueSolar documents it, Orion XS, BMV-7xx/compatible SmartShunt behavior, and Phoenix inverters. A codec catalog does not guarantee that every firmware/model implements every catalog entry. Unsupported-ID replies must remain accessible as failures.
- Async transactions serialize access and must not assign a cancelled or timed-out request's response to a newer request. Parsed Text and async notifications must be deliverable to the appropriate consumer without an unrelated new byte arriving. A link reset can restore routing only after stale host input is dealt with.
- Raw register access is an intentional escape hatch. Local rejection of every device-specific setting range is not required; documented device error flags are required. A typed encoder must still produce the documented representation, including string-width limits.

The following obligations belong to the device or caller and do not block the host-library gate:

- Electrical voltage, isolation, connector pinout, and physical DTR/RTS voltage. The audit checks the settings the host requests, not the electrical state of a real adapter.
- Device emission intervals, changes in Text suppression between firmware releases, hardware sensor availability, and model-specific register availability. The host must handle documented messages when they arrive; it does not generate device telemetry.
- Device charging algorithms, relay actuation, setting consistency enforcement, flash persistence, and the effects of register writes. Tests establish the encoded command and decoded response. Physical effects need separate hardware acceptance when an application depends on them.
- Periodic external-control writes, keepalive scheduling, and selection of an operating mode. The protocol specifies how a controlling application must use these registers; this library supplies explicit register access, not an autonomous charge controller.

Firmware image transfer, private commands, VE.Can framing, and undocumented experimental Multi RS register meanings are excluded. Python packaging on every supported platform, performance targets, and additional API conveniences are separate concerns unless they prevent a tested host-protocol requirement from working.

A source ambiguity needs a concrete conflicting source passage and a recorded interpretation. It cannot excuse a reproduced violation where the specification is clear. Existing `SPEC_EXCEPTIONS.md` entries are evidence to reconsider, not automatic waivers. For example, the two-byte Orion command-4 reply is explicit in the specification; the register-0x0100 warning does not make rejecting that command response an acceptable ambiguity.

Completion has two distinct meanings:

1. The audit is complete when every source section is reviewed, every applicable requirement has a disposition, required tests exist, and failures are recorded. A red acceptance report is a complete audit of an implementation that still needs work.
2. The implementation meets this acceptance scope when the audit has no evidence gaps, all required tests pass, and the ordinary suite passes after any superseded expectations are corrected. The generated result establishes the requirement-gate status; a separate ordinary-suite run is also required.

Once acceptance passes, further work needs new evidence: a reproducible failure of a requirement, a demonstrated omission from a pinned source, an explicitly expanded supported scope, or a new specification revision. An untested hypothetical or a preference for a different API does not reopen the release checklist.
