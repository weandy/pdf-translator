import pytest
from pydantic import ValidationError

from pdftr.config import Settings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_upload_mb", 0),
        ("max_pages", 0),
        ("max_concurrent_jobs", 0),
        ("max_concurrent_translations", 0),
        ("max_queued_jobs", 0),
        ("llm_concurrency", -1),
        ("shutdown_grace_s", 0),
    ],
)
def test_runtime_limits_reject_values_that_can_stall_or_disable_the_service(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})  # type: ignore[call-arg]


def test_zero_llm_concurrency_keeps_provider_auto_mode() -> None:
    assert Settings(_env_file=None).llm_concurrency == 0  # type: ignore[call-arg]
