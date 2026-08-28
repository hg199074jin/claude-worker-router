#!/usr/bin/env zsh
# Live smoke-test driver for the Claude Code worker router.
#
# Steps (per task-6 brief):
#   1. Copy the fixture into a fresh mktemp -d directory.
#   2. Initialize a local Git repository and commit the failing baseline.
#   3. Record the main-checkout file hash (the one inside this worktree).
#   4. Submit a read-only request and verify it cannot mutate the repository.
#   5. Submit an edit request through this checkout's CLI.
#   6. Use the exact test argv
#      ['uv', 'run', '--python', '3.12', 'python', '-m', 'unittest', '-v'].
#   7. Verify the result status is ``ready-for-review``.
#   8. Verify the main-checkout file hash did not change.
#   9. Verify the worker commit changes ``+`` to ``-`` in compute_price.
#  10. Verify test evidence reports exit code ``0``.
#  11. Print the exact retained worktree and run-record paths for review.
#
# This script does NOT delete the live evidence automatically. The smoke-test
# directory and the executor run records are retained for Codex review.
#
# Allowed shell actions (per task brief): uv, git, mkdir, zsh, chmod.

set -euo pipefail

emulate -L zsh
setopt interactivecomments

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
readonly REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
readonly FIXTURE_DIR="${REPO_ROOT}/tests/live/fixture"
UV="${UV:-$(command -v uv)}"
[[ -n "${UV}" ]] || { print -- "[smoke] FAIL: uv is not on PATH" >&2; exit 1; }
readonly UV
readonly RUN_RECORDS_DIR="${RUN_RECORDS_DIR:-${HOME}/.codex/model-router/runs}"

# ----- helpers ---------------------------------------------------------------

note() { print -- "[smoke] $*"; }
fail() { print -- "[smoke] FAIL: $*" >&2; exit 1; }

router() {
  "${UV}" run --project "${REPO_ROOT}" claude-worker-router "$@"
}

# Run a tiny Python helper via uv to keep JSON parsing out of the shell.
py_eval() {
  local snippet="$1"
  "${UV}" run --project "${REPO_ROOT}" --python 3.12 --quiet python -c "${snippet}" "${2:-}"
}

json_field() {
  # Usage: json_field <json-string> <python-expression-on-data>
  local payload="$1"
  local expr="$2"
  py_eval "
import json, sys
data = json.loads(sys.argv[1])
${expr}
" "${payload}"
}

# ----- 1. copy fixture into a fresh mktemp -d directory -----------------------

smoke_root="$(mktemp -d -t claude-worker-smoke.XXXXXX)"
readonly SMOKE_ROOT="${smoke_root}"

work_dir="${SMOKE_ROOT}/discount"
mkdir -p "${work_dir}"

cp "${FIXTURE_DIR}/discount.py" "${work_dir}/discount.py"
cp "${FIXTURE_DIR}/test_discount.py" "${work_dir}/test_discount.py"

note "smoke root:           ${SMOKE_ROOT}"
note "fixture worktree:     ${work_dir}"

# ----- 3. record the main-checkout file hash (before the worker runs) --------

main_hash_before="$(shasum -a 256 "${FIXTURE_DIR}/discount.py" | awk '{print $1}')"
note "main hash before:     ${main_hash_before}"

# ----- 2. init git and commit the failing baseline ---------------------------

git -C "${work_dir}" init --initial-branch=main --quiet
git -C "${work_dir}" config user.email "smoke@example.invalid"
git -C "${work_dir}" config user.name "Smoke Test"
git -C "${work_dir}" config commit.gpgsign "false"
git -C "${work_dir}" add discount.py test_discount.py
git -C "${work_dir}" commit --quiet -m "seed failing discount baseline"

baseline_commit="$(git -C "${work_dir}" rev-parse HEAD)"
note "baseline commit:      ${baseline_commit}"
read_only_repo_hash_before="$(shasum -a 256 "${work_dir}/discount.py" | awk '{print $1}')"

# Sanity check: baseline must fail the unittest before the worker runs.
# Use unittest discovery so the pre-worker baseline exercises the fixture.
baseline_test_output="$(
  PYTHONDONTWRITEBYTECODE=1 "${UV}" run --python 3.12 --project "${REPO_ROOT}" --quiet python -m unittest discover \
    -s "${work_dir}" -p "test_discount.py" -v 2>&1 || true
)"
if [[ "${baseline_test_output}" != *"FAIL"* ]]; then
  fail "expected baseline test to FAIL before worker edit; got:\n${baseline_test_output}"
fi
note "baseline test result: failing as expected"

# ----- 4. submit read-only request through this checkout's CLI --------------

read_only_request_json="$(printf '%s' \
  '{"repository":"'"${work_dir}"'","task":"Inspect discount.py and report the arithmetic defect in compute_price. Do not edit any file.","acceptance_criteria":["identify that a discount must subtract rather than add"],"mode":"read-only","test_commands":[],"allowed_paths":["discount.py"]}')"

note "invoking read-only worker via: ${REPO_ROOT}"
read_only_result_json="$(
  printf '%s' "${read_only_request_json}" \
    | router \
    || true
)"

if [[ -z "${read_only_result_json}" ]]; then
  fail "read-only router produced empty output"
fi

print -r -- "${read_only_result_json}" > "${SMOKE_ROOT}/read_only_result.json"

read_only_status="$(json_field "${read_only_result_json}" "print(data['status'])")"
read_only_run_id="$(json_field "${read_only_result_json}" "print(data['run_id'])")"
read_only_commit="$(json_field "${read_only_result_json}" "print(data['commit'] or '')")"
read_only_test_count="$(json_field "${read_only_result_json}" "print(len(data['tests']))")"
read_only_repo_hash_after="$(shasum -a 256 "${work_dir}/discount.py" | awk '{print $1}')"

note "read-only status:     ${read_only_status}"
note "read-only run id:     ${read_only_run_id}"
if [[ "${read_only_status}" != "read-only" ]]; then
  fail "expected read-only status, got ${read_only_status}"
fi
if [[ -n "${read_only_commit}" || "${read_only_test_count}" != "0" ]]; then
  fail "read-only run unexpectedly committed or ran tests"
fi
if [[ "${read_only_repo_hash_before}" != "${read_only_repo_hash_after}" ]]; then
  fail "read-only worker changed the task repository"
fi

read_only_run_record="${RUN_RECORDS_DIR}/${read_only_run_id}"
if [[ ! -d "${read_only_run_record}" ]]; then
  fail "read-only run record is missing: ${read_only_run_record}"
fi

# ----- 5. submit edit request through this checkout's CLI -------------------

request_json="$(printf '%s' \
  '{"repository":"'"${work_dir}"'","task":"In discount.py change the sign in compute_price so a 25%% discount on 200 yields 150.0. Do not modify any other file.","acceptance_criteria":["compute_price(200.0, 25.0) == 150.0","uv run --python 3.12 python -m unittest -v reports exit code 0"],"mode":"edit","test_commands":[["uv","run","--python","3.12","python","-m","unittest","-v"]],"allowed_paths":["discount.py"]}')"

note "invoking router:      ${REPO_ROOT}"

# Feed the request to this checkout's CLI.
result_json="$(
  printf '%s' "${request_json}" \
    | router \
    || true
)"

if [[ -z "${result_json}" ]]; then
  fail "router produced empty output"
fi

# Persist a copy of the raw result for review.
print -r -- "${result_json}" > "${SMOKE_ROOT}/result.json"

# ----- 7. verify the result status is ready-for-review ----------------------

result_status="$(json_field "${result_json}" "print(data['status'])")"
note "result status:        ${result_status}"
if [[ "${result_status}" != "ready-for-review" ]]; then
  fail "expected ready-for-review, got ${result_status}"
fi

# ----- 8. verify the main-checkout file hash did not change -----------------

main_hash_after="$(shasum -a 256 "${FIXTURE_DIR}/discount.py" | awk '{print $1}')"
note "main hash after:      ${main_hash_after}"
if [[ "${main_hash_before}" != "${main_hash_after}" ]]; then
  fail "main checkout hash changed: ${main_hash_before} -> ${main_hash_after}"
fi

# ----- extract worker evidence ----------------------------------------------

run_id="$(json_field "${result_json}" "print(data['run_id'])")"
worker_branch="$(json_field "${result_json}" "print(data['branch'] or '')")"
worker_worktree="$(json_field "${result_json}" "print(data['worktree'] or '')")"
worker_commit="$(json_field "${result_json}" "print(data['commit'] or '')")"
attempts="$(json_field "${result_json}" "print(data['attempts'])")"
diff_lines="$(json_field "${result_json}" "print(data['diff_lines'])")"
provider_host="$(json_field "${result_json}" "print(data['provider']['endpoint_host'])")"
provider_model="$(json_field "${result_json}" "print(data['provider']['model'])")"

note "worker branch:        ${worker_branch}"
note "worker worktree:      ${worker_worktree}"
note "worker commit:        ${worker_commit}"
note "attempts:             ${attempts}"
note "diff_lines:           ${diff_lines}"
note "provider host:        ${provider_host}"
note "provider model:       ${provider_model}"

# ----- 9. verify the worker commit changes + to - ----------------------------

if [[ -z "${worker_worktree}" || -z "${worker_commit}" ]]; then
  fail "worker did not record worktree/commit; cannot inspect diff"
fi

diff_text="$(git -C "${worker_worktree}" show "${worker_commit}" -- discount.py || true)"
print -r -- "${diff_text}" > "${SMOKE_ROOT}/worker_commit.diff"

if ! print -- "${diff_text}" | grep -Eq '^\+[[:space:]]*return price - price'; then
  fail "worker commit did not change + to - in compute_price. Diff:\n${diff_text}"
fi
if print -- "${diff_text}" | grep -Eq '^[[:space:]]*return price \+ price'; then
  fail "worker commit still contains the buggy + line. Diff:\n${diff_text}"
fi
note "worker diff verified: + replaced with - in compute_price"

# ----- 10. verify test evidence reports exit code 0 --------------------------

exit_codes="$(
  json_field "${result_json}" "print(' '.join(str(t['exit_code']) for t in data['tests']))"
)"
note "test exit codes:      ${exit_codes}"
if [[ "${exit_codes}" != "0" ]]; then
  fail "expected test exit code 0; got: ${exit_codes}"
fi

# ----- 11. print the exact retained worktree and run-record paths ------------

# Locate the exact run record directory returned for this invocation.
run_record="${RUN_RECORDS_DIR}/${run_id}"
if [[ -d "${run_record}" ]]; then
  cp "${run_record}/request.json" "${SMOKE_ROOT}/request.json" 2>/dev/null || true
  cp "${run_record}/result.json" "${SMOKE_ROOT}/executor_result.json" 2>/dev/null || true
  note "run record:           ${run_record}"
  note "run request.json:     ${run_record}/request.json"
  note "run result.json:      ${run_record}/result.json"
else
  note "run record absent:    ${run_record}"
fi

note "smoke root (retained): ${SMOKE_ROOT}"
note "worker worktree:       ${worker_worktree}"

print -- ""
print -- "LIVE SMOKE TEST PASSED."
print -- ""
print -- "Retained evidence paths (do NOT delete):"
print -- "  smoke root:           ${SMOKE_ROOT}"
print -- "  read-only run record: ${read_only_run_record}"
print -- "  worker worktree:      ${worker_worktree}"
print -- "  worker commit:        ${worker_commit}"
if [[ -d "${run_record}" ]]; then
  print -- "  run record:           ${run_record}"
fi
print -- ""

# Intentionally exit 0 only on the success path.
exit 0
