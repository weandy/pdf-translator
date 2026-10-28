from pdftr.pipeline.memory import release_memory


def test_release_memory_is_safe_to_call():
    release_memory()
    release_memory()
