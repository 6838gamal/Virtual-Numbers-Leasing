"""نماذج استجابة مسارات التشخيص."""
from __future__ import annotations

from pydantic import BaseModel, Field


class DiagnosticsHealthResponse(BaseModel):
    """استجابة /api/v1/diagnostics/health."""
    ok: bool = Field(True, description="دائماً True إذا استجاب الخادم")
    debug: bool = Field(..., description="هل DEBUG مفعّل")
    admin_configured: bool = Field(..., description="هل ADMIN_SECRET مضبوط")
    python: str = Field(..., description="إصدار بايثون")
    pytest_available: bool = Field(..., description="هل pytest مثبّت")
    project_root: str = Field(..., description="جذر المشروع المطلق")
    default_test_file: str = Field(..., description="مسار ملف الاختبار الافتراضي")
    default_test_exists: bool = Field(..., description="هل الملف الافتراضي موجود")


class DiagnosticsTestSummary(BaseModel):
    """ملخّص نتائج pytest (حقول اختيارية حسب ما يُستخرج)."""
    raw: str | None = Field(None, description="السطر الأخير الخام من pytest")
    passed: int | None = Field(None, ge=0)
    failed: int | None = Field(None, ge=0)
    error: int | None = Field(None, ge=0)
    errors: int | None = Field(None, ge=0)
    skipped: int | None = Field(None, ge=0)


class DiagnosticsRunTestsResponse(BaseModel):
    """استجابة /api/v1/diagnostics/run-tests."""
    ok: bool = Field(..., description="True إذا كان exit_code=0")
    exit_code: int = Field(..., description="رمز خروج pytest (0 = نجاح)")
    duration_s: float = Field(..., ge=0, description="مدة التنفيذ بالثواني")
    target: str = Field(..., description="ملف الاختبار المُشغَّل")
    marker: str | None = Field(None, description="علامة التصفية إن استُخدمت")
    summary: DiagnosticsTestSummary = Field(..., description="ملخّص سريع للنتائج")
    output: str = Field(..., description="مخرجات pytest (مقتطعة عند الحاجة)")
