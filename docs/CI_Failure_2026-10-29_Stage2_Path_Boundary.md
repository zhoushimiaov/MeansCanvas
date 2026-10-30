# CI Failure Record: Stage 2 Path Boundary

Date: 2026-10-29

## Failed Run

- Workflow: [MeansCanvas CI run 40909957245](https://github.com/zhoushimiaov/MeansCanvas/actions/runs/40909957245)
- Commit: `de21d8e2a433c2ea2f23a3e15a5832b5150d59da`
- Python 3.12: `1 failed, 375 passed, 16 skipped`
- Python 3.10: the same test failed before the job was marked cancelled

The failing test was:

`tests/test_stage2_security_regressions.py::test_t10_normalize_local_image_path_boundary`

Failure:

`Failed: DID NOT RAISE HTTPException`

## Root Cause

On the Linux CI runner, `normalize_local_image_path()` treated a Windows drive path such as `C:/Windows/System32/notepad.exe` as a relative path. `os.path.abspath()` therefore resolved it under the repository root, so the path remained inside an allowed root and the expected HTTP 400 was not raised.

## Fix

Fixed by `ff5caf91578b86e3ff3e823d9e0616c087644d24`:

- Reject Windows drive paths on non-Windows platforms before path resolution in `routers/online_image.py`.
- Extend the boundary regression test with a POSIX absolute-path case in `tests/test_stage2_security_regressions.py`.

The functional code change is limited to the non-Windows guard and preserves normal Windows drive-path handling.

## Verification

Local Linux verification used WSL with Python 3.12.3:

- Pre-fix target test: failed, reproducing CI.
- Post-fix target test: passed.
- Full Python suite: `376 passed, 16 skipped`.
- Python compile, Ruff, file-length, SSRF, and typography checks passed.
- Frontend JavaScript syntax, concurrency tests, and i18n validation passed.

GitHub Actions verification:

- [Successful run 40910461192](https://github.com/zhoushimiaov/MeansCanvas/actions/runs/40910461192)
- Python 3.10 and Python 3.12 jobs both passed.

No deployment was performed for this fix.
