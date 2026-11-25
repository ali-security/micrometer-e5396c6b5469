"""Summarise Gradle JUnit XML results and name every skipped test with its reason.

* Outcomes are counted per test (task + class + name), by FINAL outcome: upstream's
  build applies Develocity test retry (develocity.testRetry, maxRetries = 3), and
  every retry is an extra execution. A test whose first attempt failed
  and whose retry passed is listed as FLAKY; it is a failure only when no attempt passed.
* Results come from build/seal-test-results.tsv (one row per test execution,
  written by the seal init script's afterTest hook).
* Gradle has no skip reason for @Disabled tests, so the reason is read from the test source
  (@Disabled / @Disabled*/@Enabled* conditions / assume* calls), following the
  class's `extends` chain; parameterized cases use the parent method name that the
  init script records in build/seal-skipped.tsv.
"""
import collections
import glob
import re

COND = re.compile(r'@(Disabled\w*|Enabled\w*)\b(\((?:[^()]|\([^()]*\))*\))?', re.S)
ASSUME = re.compile(r'\b(assume\w*\s*\((?:[^;])*?\))\s*;', re.S)

_src = {}


def source(simple_or_fqcn):
    if simple_or_fqcn not in _src:
        if '.' in simple_or_fqcn:
            rel = simple_or_fqcn.replace('.', '/')
            hits = glob.glob('**/src/*/java/' + rel + '.java', recursive=True) + \
                glob.glob('**/src/*/kotlin/' + rel + '.kt', recursive=True)
        else:
            hits = glob.glob('**/src/**/' + simple_or_fqcn + '.java', recursive=True) + \
                glob.glob('**/src/**/' + simple_or_fqcn + '.kt', recursive=True)
        if not hits and '.' in simple_or_fqcn:
            return source(simple_or_fqcn.split('.')[-1])
        _src[simple_or_fqcn] = open(hits[0], encoding='utf-8').read() if hits else None
    return _src[simple_or_fqcn]


def annotations_above(src, pos):
    """Annotation block directly above the declaration starting at `pos`."""
    lines = src[:pos].split('\n')
    block = [lines.pop()]
    depth = 0
    while lines:
        line = lines.pop()
        s = line.strip()
        depth += s.count(')') - s.count('(')
        if depth <= 0 and (not s or s.endswith(';') or s == '}' or s.endswith('{') or s.endswith('*/')):
            break
        block.append(line)
    return '\n'.join(reversed(block))


def conds(text):
    return ' '.join('@' + a + re.sub(r'\s+', ' ', b) for a, b in COND.findall(text))


def class_chain(fqcn):
    """(source, simple class name) for the class and its superclasses."""
    parts = fqcn.split('$')
    src = source(parts[0])
    name = parts[-1].split('.')[-1]
    seen = set()
    while src is not None and name not in seen:
        seen.add(name)
        yield src, name
        m = re.search(r'class\s+' + re.escape(name) + r'\b[^{]*?\bextends\s+([\w.]+)', src) or \
            re.search(r'class\s+' + re.escape(name) + r'\b(?:\s*<[^>]*>)?(?:\s*\([^)]*\))?\s*:\s*([\w.]+)', src)
        if not m:
            return
        name = m.group(1).split('.')[-1]
        src = source(name)


def reason_for(fqcn, method):
    chain = list(class_chain(fqcn))
    if not chain:
        return 'skipped by JUnit condition (test source not found)'
    for src, cname in chain:
        m = re.search(r'class\s+' + re.escape(cname) + r'\b', src)
        if m:
            c = conds(annotations_above(src, src.rfind('\n', 0, m.start()) + 1))
            if c:
                return 'class-level ' + c
    for src, _ in chain:
        m = re.search(r'(fun\s+`?|void\s+)' + re.escape(method) + r'\b', src) if method else None
        if not m:
            continue
        c = conds(annotations_above(src, src.rfind('\n', 0, m.start()) + 1))
        if c:
            return c
        body = src[m.end():m.end() + 2500]
        a = ASSUME.search(body)
        if a:
            return 'assumption not met in test method: ' + re.sub(r'\s+', ' ', a.group(1))[:220]
        for helper in dict.fromkeys(re.findall(r'\b(\w+)\s*\(', body[:1200])):
            for hsrc, _ in chain:
                hm = re.search(r'(void|fun)\s+' + re.escape(helper) + r'\s*\(', hsrc)
                if hm:
                    a = ASSUME.search(hsrc[hm.end():hm.end() + 800])
                    if a:
                        return ('assumption not met in ' + helper + '(): '
                                + re.sub(r'\s+', ' ', a.group(1))[:260])
        break
    for src, _ in chain:
        for bm in re.finditer(r'@BeforeEach', src):
            a = ASSUME.search(src[bm.end():bm.end() + 1500])
            if a:
                return 'assumption not met in @BeforeEach: ' + re.sub(r'\s+', ' ', a.group(1))[:220]
    return 'skipped at runtime by a JUnit assumption/condition (no static reason in source)'


def method_of(name):
    return re.split(r'[(\[ ]', name, 1)[0]


# One row per test execution (retries included), written by the seal init script.
outcomes = collections.OrderedDict()  # (task, class, parent, name) -> [(result, message)]
for f in sorted(glob.glob('**/build/seal-test-results.tsv', recursive=True)):
    for line in open(f, encoding='utf-8'):
        p = line.rstrip('\n').split('\t')
        if len(p) < 6:
            continue
        task, cls, parent, name, res, msg = p[:6]
        o = {'SUCCESS': 'passed', 'FAILURE': 'failed', 'SKIPPED': 'skipped'}.get(res, 'failed')
        outcomes.setdefault((task, cls, parent, name), []).append((o, msg))

totals = collections.OrderedDict()
skipped = collections.defaultdict(list)
flaky, failed = [], []
for (task, cls, parent, name), runs in outcomes.items():
    os_ = [o for o, _ in runs]
    c = totals.setdefault(task, collections.Counter())
    c['tests'] += 1
    short = cls.split('.')[-1] + '.' + name
    if 'passed' in os_:
        c['passed'] += 1
        if 'failed' in os_:
            c['flaky'] += 1
            flaky.append(f'{task} {short}')
    elif 'failed' in os_:
        c['failed'] += 1
        failed.append(f'{task} {short}')
    else:
        c['skipped'] += 1
        meth = method_of(name)
        if not meth or name.startswith('['):
            meth = method_of(parent or '')
            short = cls.split('.')[-1] + '.' + (parent or '') + ' ' + name
        msg = next((m for _, m in runs if m), '')
        reason = reason_for(cls, meth)
        if msg and reason.startswith('skipped'):
            reason = 'assumption not met: ' + msg
        skipped[reason].append(short)

grand = collections.Counter()
print('==== Test results per test task (final outcome per test; retries folded in) ====')
for task, c in totals.items():
    grand.update(c)
    print(f"{task}: {c['tests']} tests, {c['passed']} passed ({c['flaky']} after retry), "
          f"{c['failed']} failed, {c['skipped']} skipped")
print(f"TOTAL: {grand['tests']} tests, {grand['passed']} passed, {grand['failed']} failed, "
      f"{grand['skipped']} skipped; {grand['flaky']} of the passed tests failed a first attempt "
      f"and passed on upstream's develocity.testRetry")
for t in flaky:
    print(f'FLAKY (failed first attempt, passed on retry): {t}')
for t in failed:
    print(f'FAILED (no attempt passed): {t}')
print('==== Skipped tests by reason ====')
for reason, tests in sorted(skipped.items(), key=lambda kv: -len(kv[1])):
    print(f"SKIPPED ({len(tests)}) reason: {reason} -> {', '.join(sorted(set(tests)))}")
