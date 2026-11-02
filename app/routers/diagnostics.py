"""تشخيص التطبيق: فحص صحة سريع + تشغيل اختبارات pytest من داخل التطبيق.

⚠️ أمان:
- /health متاح دائماً (لا يلمس شيئاً).
- /run-tests يتطلب DEBUG=True + ADMIN_SECRET صحيح في الهيدر.
- في الإنتاج (DEBUG=False) يُرجع 404 حتى لا يُكتشف.
- مهلة تنفيذ محدودة + لا قراءة من stdin.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query

from app.config import DEBUG, ADMIN_SECRET
from app.models.diagnostics import (
    DiagnosticsHealthResponse,
    DiagnosticsRunTestsResponse,
    DiagnosticsTestSummary,
)


router = APIRouter(prefix="/api/v1/diagnostics", tags=["diagnostics"])

# مسار جذر المشروع — نبحث عن tests/ و pytest.ini/pyproject.toml
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEST_FILE = PROJECT_ROOT / "tests" / "test_orders_single.py"

# مهلة قصوى لتشغيل الاختبارات (ثانية)
TEST_TIMEOUT = 120.0

# حد أقصى لحجم مخرجات pytest التي نعيدها
MAX_OUTPUT_BYTES = 64 * 1024


# ══════════════════════════════════════════════════════════════
# مساعدات داخلية
# ══════════════════════════════════════════════════════════════
def _require_admin(admin_secret: str | None) -> None:
    """يتحقق من صلاحية الأدمن — يُرجع 404 في الإنتاج حتى لا يُكتشف الـ endpoint."""
    if not DEBUG:
        # في الإنتاج: نظهر كأن المسار غير موجود
        raise HTTPException(404, "Not Found")

    if not ADMIN_SECRET:
        raise HTTPException(503, "ADMIN_SECRET غير مضبوط — التشخيص معطّل")

    if not admin_secret or admin_secret != ADMIN_SECRET:
        # نستخدم 401 بدل 403 لإخفاء وجود المسار
        raise HTTPException(401, "unauthorized")


def _resolve_target(target: str) -> Path:
    """
    يحوّل target (اسم ملف أو مسار نسبي) إلى مسار مطلق داخل المشروع.
    يرفض أي محاولة للخروج من جذر المشروع (path traversal).
    """
    candidate = (PROJECT_ROOT / target).resolve()
    try:
        candidate.relative_to(PROJECT_ROOT)
    except ValueError:
        raise HTTPException(400, "مسار غير مسموح")
    if not candidate.exists():
        raise HTTPException(404, f"ملف الاختبار غير موجود: {target}")
    if candidate.suffix not in (".py",):
        raise HTTPException(400, "يُسمح بملفات .py فقط")
    return candidate


def _truncate(text: str) -> str:
    """يقيّد حجم المخرجات المُعادة."""
    if len(text.encode("utf-8", errors="ignore")) <= MAX_OUTPUT_BYTES:
        return text
    truncated = text.encode("utf-8", errors="ignore")[:MAX_OUTPUT_BYTES]
    return truncated.decode("utf-8", errors="ignore") + "\n... [مخترق — تجاوز الحد]"


def _parse_pytest_summary(output: str) -> dict:
    """يستخرج ملخّصاً سريعاً (passed/failed/...) من مخرجات pytest."""
    summary: dict = {}
    for line in reversed(output.splitlines()):
        if " passed" in line or " failed" in line or " error" in line:
            summary["raw"] = line.strip()
            for token in line.replace("=", " ").split():
                if token.isdigit():
                    nxt_idx = line.find(token) + len(token)
                    rest = line[nxt_idx:nxt_idx + 15].strip()
                    for key in ("passed", "failed", "error", "errors", "skipped"):
                        if rest.startswith(key):
                            summary[key] = int(token)
                            break
            break
    return summary


# ══════════════════════════════════════════════════════════════
# GET /api/v1/diagnostics/health
# ══════════════════════════════════════════════════════════════
@router.get(
    "/health",
    response_model=DiagnosticsHealthResponse,
    summary="فحص صحة التشخيص",
    description=(
        "يعيد حالة سريعة عن بيئة التشخيص: DEBUG، توفّر pytest، "
        "ومسار الملف الافتراضي. متاح دائماً بلا مصادقة."
    ),
)
async def diagnostics_health() -> DiagnosticsHealthResponse:
    pytest_bin = shutil.which("pytest") or shutil.which("pytest.exe")
    return DiagnosticsHealthResponse(
        ok=True,
        debug=DEBUG,
        admin_configured=bool(ADMIN_SECRET),
        python=sys.version.split()[0],
        pytest_available=bool(pytest_bin),
        project_root=str(PROJECT_ROOT),
        default_test_file=str(DEFAULT_TEST_FILE),
        default_test_exists=DEFAULT_TEST_FILE.exists(),
    )


# ══════════════════════════════════════════════════════════════
# POST /api/v1/diagnostics/run-tests
# ══════════════════════════════════════════════════════════════
@router.post(
    "/run-tests",
    response_model=DiagnosticsRunTestsResponse,
    summary="تشغيل اختبارات pytest",
    description=(
        "يشغّل pytest على ملف اختبار داخل المشروع ويعيد ملخّص النتائج. "
        "يتطلب DEBUG=true + ترويسة X-Admin-Secret مطابقة لـ ADMIN_SECRET. "
        "في الإنتاج (DEBUG=false) يعيد 404."
    ),
    responses={
        401: {"description": "X-Admin-Secret غير صحيح"},
        404: {"description": "المسار معطّل (DEBUG=false) أو ملف غير موجود"},
        503: {"description": "pytest غير مثبّت أو ADMIN_SECRET غير مضبوط"},
        504: {"description": "تجاوز مهلة التنفيذ"},
    },
)
async def diagnostics_run_tests(
    target: str = Query(
        "test_orders_single.py",
        description="مسار نسبي لملف اختبار .py داخل المشروع",
    ),
    marker: str | None = Query(
        None,
        description="pytest -m قيمة اختيارية لتصفية الاختبارات (مثل: slow)",
    ),
    maxfail: int = Query(50, ge=1, le=500),
    x_admin_secret: str | None = Header(default=None, alias="X-Admin-Secret"),
) -> DiagnosticsRunTestsResponse:
    """
    يشغّل pytest على ملف الاختبار المحدّد ويعيد ملخّص النتائج.

    مثال:
        curl -X POST http://localhost:8000/api/v1/diagnostics/run-tests \\
             -H "X-Admin-Secret: $ADMIN_SECRET"
    """
    _require_admin(x_admin_secret)

    test_path = _resolve_target(target)

    pytest_bin = shutil.which("pytest") or shutil.which("pytest.exe")
    if not pytest_bin:
        raise HTTPException(
            503,
            "pytest غير مثبّت في البيئة الحالية — ثبّته بـ: pip install pytest",
        )

    cmd = [
        pytest_bin,
        str(test_path),
        "-v",
        "--tb=short",
        "--no-header",
        f"--maxfail={maxfail}",
        "-p", "no:cacheprovider",  # لا نكتب .pytest_cache
    ]
    if marker:
        cmd += ["-m", marker]

    env = os.environ.copy()
    # ضمانات إضافية: لا تفعيل 5SIM الحقيقي حتى لو ضُبط في البيئة
    env["SIM5_ENABLED"] = "false"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    started = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            raw, _ = await asyncio.wait_for(
                proc.communicate(), timeout=TEST_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(
                504, f"تجاوزت الاختبارات المهلة ({TEST_TIMEOUT:.0f}s) وأُوقفت"
            )
    except FileNotFoundError:
        raise HTTPException(503, "تعذّر تشغيل pytest")

    duration = round(time.perf_counter() - started, 3)
    output = _truncate((raw or b"").decode("utf-8", errors="replace"))
    summary_dict = _parse_pytest_summary(output)

    return DiagnosticsRunTestsResponse(
        ok=proc.returncode == 0,
        exit_code=proc.returncode,
        duration_s=duration,
        target=target,
        marker=marker,
        summary=DiagnosticsTestSummary(**summary_dict),
        output=output,
    )
