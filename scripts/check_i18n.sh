#!/usr/bin/env bash
# Returns 0 iff no Cyrillic remains in user-facing Python source.
#
# Skip-list reflects deliberate UA-only files; update only by ADR:
#   - lib/i18n/        UA dictionary file itself
#   - admin_stats.py   admin-only dashboard, only the user (admin) sees it
#   - openai_voice.py  Whisper UA dish-name bias dict, not user-visible
#
# Per-line allowlist: trailing  "# noqa: i18n"  marks a Cyrillic-bearing
# line as deliberate (regex matchers, UA-only helper bodies, etc.).
set -euo pipefail
# Force UTF-8 so the Cyrillic character class works correctly. Under C
# locale, grep treats the multi-byte UTF-8 sequences as raw bytes and
# the [А-я] range matches a much broader (incorrect) set.
export LC_ALL=en_US.UTF-8
remaining=$(grep -rn --include="*.py" "[А-яЇїІіЄєҐґ]" api/ lib/ \
  | grep -v -E "(/i18n/|admin_stats\.py|openai_voice\.py)" \
  | grep -v -E "(# noqa: i18n|// noqa: i18n)" \
  || true)
if [ -z "$remaining" ]; then
  printf '✅ i18n: 0 Cyrillic lines remain in scope\n'
  exit 0
fi
count=$(printf '%s\n' "$remaining" | wc -l | tr -d ' ')
printf '❌ i18n: %s Cyrillic line(s) still in scope:\n' "$count"
printf '%s\n' "$remaining"
exit 1
