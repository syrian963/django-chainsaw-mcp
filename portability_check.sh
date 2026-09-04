#!/usr/bin/env bash
# Nothing tracked in this repository may contain a path from the machine it was
# written on. This is a tool other people install; an absolute path to somebody's
# home directory is a defect, and the kind that survives review because it still
# works for the author.
#
# Checked against tracked files only, so throwaway tooling outside the repo is
# irrelevant.
cd "$(dirname "$0")" || exit 2

fails=0

report() {
  printf '  FAIL  %s\n' "$1"
  shift
  printf '        %s\n' "$@"
  fails=$((fails + 1))
}

echo "Portability"

# 1. Home directories and host-specific mounts.
hits=$(git ls-files -z | xargs -0 grep -n -E \
  '/home/[a-z]|/Users/[A-Za-z]|wsl\.localhost|[A-Z]:\\\\|\bC:/' 2>/dev/null || true)
if [ -n "$hits" ]; then
  report "a host path is baked into a tracked file" $hits
else
  printf '  ok    %s\n' "no home directories or host mounts in tracked files"
fi

# 2. Absolute paths assigned to a constant, the shape that hides in a helper.
hits=$(git ls-files -z '*.py' | xargs -0 grep -n -E '^[A-Za-z_]+ *= *r?"(/|//)[a-z]' 2>/dev/null || true)
if [ -n "$hits" ]; then
  report "an absolute path is assigned to a module constant" $hits
else
  printf '  ok    %s\n' "no absolute paths assigned to constants"
fi

# 3. Scripts must locate themselves rather than assume a working directory.
for script in *.sh; do
  [ "$script" = "portability_check.sh" ] && continue
  if ! grep -q 'dirname "\$0"' "$script"; then
    report "$script does not cd to its own directory" \
      "add: cd \"\$(dirname \"\$0\")\" || exit 2"
  fi
done
printf '  ok    %s\n' "shell scripts resolve their own location"

# 4. Python tests must anchor on __file__, not the caller's cwd.
for test_file in smoke_test.py analysis_test.py client_test.py; do
  [ -f "$test_file" ] || continue
  if ! grep -q '__file__' "$test_file"; then
    report "$test_file does not anchor on __file__"
  fi
done
printf '  ok    %s\n' "python tests anchor on __file__"

# 5. The one legitimate place for a path is documentation, and even there it
#    must be an obvious placeholder rather than a real machine.
hits=$(git ls-files -z '*.md' | xargs -0 grep -n -E '/home/[a-z]+/|/Users/[A-Za-z]+/' 2>/dev/null || true)
if [ -n "$hits" ]; then
  report "documentation shows a real home directory instead of a placeholder" $hits
else
  printf '  ok    %s\n' "documentation uses placeholders"
fi

echo
# A shell script that is not executable is a suite that silently does not run.
# This has happened twice: an editing helper that writes a temp file and
# renames it does not carry the original mode over, and nothing noticed until
# a check reported "Permission denied" inside a larger run.
printf '%s\n' "--- executable bit on every shell script"
notexec=""
for script in *.sh; do
  [ -x "$script" ] || notexec="$notexec $script"
done
if [ -n "$notexec" ]; then
  echo "  FAIL  not executable:$notexec"
  fails=$((fails + 1))
else
  echo "  ok    every .sh is executable"
fi

if [ "$fails" -gt 0 ]; then
  echo "$fails Portabilitaetsproblem(e)"
  exit 1
fi
echo "keine hartkodierten Pfade im getrackten Baum"
