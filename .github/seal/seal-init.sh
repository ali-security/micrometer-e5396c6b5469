#!/usr/bin/env bash
# Writes the Gradle init script used by .github/workflows/seal-build.yml.
#  - disables the non-test gates the build applies as part of `check`
#    (checkstyle incl. nohttp, license-header check, spotless, spring-javaformat
#    checkFormat, japicmp) — they never execute code;
#  - logs failed test attempts with full stack traces;
#  - records every test execution (task, class, parent/method descriptor, name,
#    result, assumption/failure message) in build/seal-test-results.tsv, so
#    seal-test-report.py can count each test once by its final outcome and name the
#    skip reason of parameterized cases whose own name is only "[n] <args>".
set -euo pipefail
out="$1"
cat > "$out" <<'EOF'
allprojects {
    tasks.configureEach { t ->
        def n = t.name
        if (n.startsWith('checkstyle') || n ==~ /license(Main|Test|Java11|Java11Test|Jmh|Jcstress)?/
                || (n.startsWith('spotless') && n.endsWith('Check')) || n == 'spotlessCheck'
                || n.startsWith('checkFormat') || n == 'japicmp' || n == 'downloadBaseline') {
            t.enabled = false
        }
    }
    tasks.withType(Test).configureEach { Test t ->
        t.testLogging {
            events 'failed'
            exceptionFormat 'full'
        }
        def resultsFile = t.project.layout.buildDirectory.file('seal-test-results.tsv').get().asFile
        t.afterTest { desc, result ->
            resultsFile.parentFile.mkdirs()
            def msg = (result.exception?.message ?: '').replaceAll('[\\t\\r\\n]+', ' ').take(300)
            resultsFile << "${t.path}\t${desc.className}\t${desc.parent?.name}\t${desc.name}\t${result.resultType}\t${msg}\n"
        }
    }
}
EOF
echo "Wrote $out"
