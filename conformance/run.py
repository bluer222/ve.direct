#!/usr/bin/env python3
"""Run the pinned host-protocol acceptance gate and render its requirement ledger.

No network access is required. The serial acceptance tests require pyserial and
pyserial-asyncio, and use a POSIX pseudo-terminal rather than physical hardware.
A skip, missing test, unreviewed source range, or evidence gap cannot pass.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import datetime
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
for directory in ('src', 'tools', 'tests', 'conformance'):
    sys.path.insert(0, str(ROOT / directory))

ALLOWED = {'tested', 'ambiguous', 'device_obligation', 'excluded', 'gap'}


def read_json(path):
    return json.loads(path.read_text())


def lines_in(path):
    return len(path.read_text().split('\n'))


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


class Results(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.outcomes = {}
        self.details = collections.defaultdict(list)
        self.subtests = 0

    def startTest(self, test):
        super().startTest(test)
        self.outcomes[test.id()] = 'RUNNING'

    def addSuccess(self, test):
        super().addSuccess(test)
        if self.outcomes[test.id()] == 'RUNNING':
            self.outcomes[test.id()] = 'PASS'

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.outcomes[test.id()] = 'FAIL'
        self.details[test.id()].append(self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.outcomes[test.id()] = 'ERROR'
        self.details[test.id()].append(self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.outcomes[test.id()] = 'UNVERIFIED'
        self.details[test.id()].append(reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.outcomes[test.id()] = 'FAIL'
        self.details[test.id()].append('Expected failure remains an acceptance failure.\n' +
                                       self._exc_info_to_string(err, test))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.outcomes[test.id()] = 'UNVERIFIED'
        self.details[test.id()].append('Remove expectedFailure and rerun this requirement.')

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        self.subtests += 1
        if err is not None:
            self.outcomes[test.id()] = 'FAIL' if issubclass(err[0], test.failureException) else 'ERROR'
            # Keep reports bounded when an exhaustive domain has many failures.
            if len(self.details[test.id()]) < 5:
                self.details[test.id()].append(str(subtest) + '\n' +
                                               self._exc_info_to_string(err, test))


def load_ledger():
    manifest = read_json(ROOT / 'conformance/scope.json')
    requirements, sections, issues = [], [], []
    files = sorted((ROOT / 'conformance').glob('*_requirements.json'))
    for path in files:
        data = read_json(path)
        requirements.extend(data['requirements'])
        sections.extend(data.get('source_sections', []))
    if len(files) != 3:
        issues.append(f'Expected three domain ledgers, found {len(files)}.')
    inventory = [{key: req.get(key) for key in
                  ('id', 'requirement', 'disposition', 'sources', 'tests')}
                 for req in sorted(requirements, key=lambda req: req['id'])]
    inventory_hash = hashlib.sha256(json.dumps(
        inventory, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if inventory_hash != manifest.get('requirement_inventory_sha256'):
        issues.append('Frozen requirement inventory changed or is not sealed. '
                      'Review any removed, reclassified or changed obligations before updating its hash.')
    sections.extend(manifest.get('source_exclusions', []))
    documents = {item['file']: item for item in manifest['documents']}
    for name, document in documents.items():
        path = ROOT / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != document['sha256']:
            issues.append(f'Pinned source changed: {name}; review scope before updating its hash.')
    ids = [r['id'] for r in requirements]
    if len(ids) != len(set(ids)):
        issues.append('Requirement IDs are not unique.')
    known_ids = set(ids)
    for req in requirements:
        if req['disposition'] not in ALLOWED:
            issues.append(f"{req['id']}: invalid disposition {req['disposition']}")
        if not req.get('requirement') or not req.get('sources'):
            issues.append(f"{req['id']}: missing requirement or source evidence")
        if req['disposition'] == 'tested' and not req.get('tests'):
            issues.append(f"{req['id']}: tested requirement has no executable evidence")
        if req['disposition'] != 'tested' and not req.get('notes'):
            issues.append(f"{req['id']}: exception or gap needs a reason")
        for source in req.get('sources', []):
            path = ROOT / source['file']
            if not path.is_file():
                issues.append(f"{req['id']}: missing source {source['file']}")
            elif not 1 <= source['start'] <= source['end'] <= lines_in(path):
                issues.append(f"{req['id']}: invalid source range {source}")
        for anchor in req.get('implementation', []):
            filename, separator, line = anchor.rpartition(':')
            if not separator or not line.isdigit():
                issues.append(f"{req['id']}: invalid implementation anchor {anchor}")
            elif not (ROOT / filename).is_file() or not 1 <= int(line) <= lines_in(ROOT / filename):
                issues.append(f"{req['id']}: missing implementation anchor {anchor}")
    reviewed = {name: set() for name in documents}
    for section in sections:
        name = section['file']
        if name not in documents:
            issues.append(f'Unknown source section document: {name}')
            continue
        if not 1 <= section['start'] <= section['end'] <= lines_in(ROOT / name):
            issues.append(f'Invalid section range: {section}')
            continue
        linked = section.get('requirements', [])
        if not linked and section.get('disposition') not in ('excluded', 'device_obligation'):
            issues.append(f'Unclassified section: {section}')
        if set(linked) - known_ids:
            issues.append(f'Unknown requirements in section: {section}')
        reviewed[name].update(range(section['start'], section['end'] + 1))
    for name in documents:
        meaningful = {i for i, line in enumerate((ROOT / name).read_text().split('\n'), 1)
                      if line.strip()}
        missing = sorted(meaningful - reviewed[name])
        if missing:
            ranges = []
            for number in missing:
                if ranges and number == ranges[-1][1] + 1:
                    ranges[-1][1] = number
                else:
                    ranges.append([number, number])
            issues.append(f'Unreviewed nonblank source lines in {name}: {ranges}')
    return manifest, requirements, issues


def requirement_status(req, outcomes):
    states = [outcomes.get(name, 'MISSING') for name in req.get('tests', [])]
    if any(state in ('FAIL', 'ERROR') for state in states):
        return 'FAIL'
    if any(state != 'PASS' for state in states):
        return 'GAP'
    if req['disposition'] == 'tested':
        return 'PASS' if states else 'GAP'
    return {'gap': 'GAP', 'ambiguous': 'AMBIGUOUS',
            'device_obligation': 'DEVICE', 'excluded': 'EXCLUDED'}[req['disposition']]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-ledger', action='store_true', help='Validate traceability without running tests.')
    parser.add_argument('--output', type=Path, default=ROOT / 'conformance/RESULTS.md')
    args = parser.parse_args()
    manifest, requirements, issues = load_ledger()
    names = sorted({name for req in requirements for name in req.get('tests', [])})
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    seen = set()
    for name in names:
        loaded = list(flatten(loader.loadTestsFromName(name)))
        if len(loaded) != 1 or loaded[0].id() != name:
            issues.append(f'Test reference must resolve to one method: {name}')
        for test in loaded:
            if test.id() not in seen:
                seen.add(test.id())
                suite.addTest(test)
    if loader.errors:
        issues.extend(loader.errors)
    acceptance = loader.discover(str(ROOT / 'conformance'), pattern='test_*_acceptance.py')
    unreferenced = sorted({test.id() for test in flatten(acceptance)} - set(names))
    if unreferenced:
        issues.append(f'Acceptance tests without a requirement: {unreferenced}')
    if args.check_ledger:
        for issue in issues:
            print(issue)
        print(f'{len(requirements)} requirements; {len(names)} referenced tests; {len(issues)} ledger issues')
        return 1 if issues or any(r['disposition'] == 'gap' for r in requirements) else 0
    results = Results()
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        suite.run(results)
    statuses = {req['id']: requirement_status(req, results.outcomes) for req in requirements}
    counts = collections.Counter(statuses.values())
    nonpassing = {name: status for name, status in results.outcomes.items() if status != 'PASS'}
    # setUpModule/tearDownClass failures use _ErrorHolder IDs, rather than
    # a referenced method ID. They must block even if every method passed.
    blocked = bool(issues or counts['FAIL'] or counts['GAP'] or nonpassing)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    source_hash = hashlib.sha256()
    for path in sorted((ROOT / 'src').rglob('*.py')):
        source_hash.update(str(path.relative_to(ROOT)).encode() + b'\0' + path.read_bytes())
    report = {
        'generated_at': now, 'scope': manifest['scope_id'],
        'source_fingerprint': source_hash.hexdigest(), 'python': sys.version,
        'verdict': 'BLOCKED' if blocked else 'PASS_WITH_STATED_SCOPE',
        'test_count': results.testsRun, 'subtest_count': results.subtests,
        'requirement_counts': dict(counts), 'ledger_issues': issues,
        'nonpassing_checks': nonpassing,
        'requirements': statuses, 'tests': results.outcomes,
        'details': dict(results.details), 'captured_output': captured.getvalue(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix('.json').write_text(json.dumps(report, indent=2) + '\n')
    markdown = [f"Acceptance verdict: {report['verdict']}", '',
                f"Scope `{manifest['scope_id']}`. Generated {now}.", '',
                f"{len(requirements)} requirement groups; {results.testsRun} distinct test methods; "
                f"{results.subtests} unittest subtests.", '',
                'Requirement results: ' + ', '.join(f'{key}={value}' for key, value in sorted(counts.items())) + '.', '',
                'This is a finite host-protocol acceptance result, not a claim that all possible programs or hardware behavior were proved correct. '
                'PASS applies only to the evidence named in each requirement. DEVICE and EXCLUDED are outside the host gate. '
                'AMBIGUOUS records a source conflict; it must remain visible in the supported-scope claim. '
                'FAIL, GAP, missing tests, skipped tests, and unreviewed source sections block acceptance.', '',
                f"Implementation fingerprint: `{report['source_fingerprint']}`.", '',
                'The source section ledger accounts for every nonblank snapshot line. This detects omitted sections; '
                'it does not mechanically prove that the human interpretation of every sentence is correct.', '']
    if issues:
        markdown.extend(['Ledger issues:', ''] + [f'- {issue}' for issue in issues] + [''])
    if nonpassing:
        markdown.extend(['Nonpassing checks:', ''] +
                        [f'- `{name}`: {status}' for name, status in nonpassing.items()] + [''])
    markdown.extend(['| Requirement | Result | Requirement and evidence |', '| --- | --- | --- |'])
    for req in requirements:
        evidence = ', '.join('`' + name + '`' for name in req.get('tests', [])) or req.get('notes', '')
        label = req['requirement'].replace('|', '\\|').replace('\n', ' ')
        markdown.append(f"| {req['id']} | {statuses[req['id']]} | {label}<br>{evidence.replace('|', '&#124;')} |")
    for name, detail in results.details.items():
        markdown.extend(['', f'Failure or missing evidence in `{name}`:', '', '```text',
                         '\n'.join(detail).rstrip(), '```'])
    args.output.write_text('\n'.join(markdown) + '\n')
    print(f"{report['verdict']}: {len(requirements)} requirements, {results.testsRun} tests, "
          f"{counts['FAIL']} failing requirements, {counts['GAP']} evidence gaps, "
          f"{len(nonpassing)} nonpassing checks, {len(issues)} ledger issues")
    print(f'Report: {args.output}')
    return int(blocked)


if __name__ == '__main__':
    raise SystemExit(main())
