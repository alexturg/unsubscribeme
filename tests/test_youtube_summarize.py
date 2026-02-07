from rssbot.youtube_summarize import _format_llm_summary_output


def test_format_llm_summary_output_splits_inline_bullets():
    raw = (
        "- First point with details. - Second point with details. "
        "- Third point with details."
    )
    formatted = _format_llm_summary_output(raw, max_sentences=10)
    assert formatted == (
        "- First point with details.\n"
        "- Second point with details.\n"
        "- Third point with details."
    )


def test_format_llm_summary_output_normalizes_plain_text():
    raw = "One useful takeaway.\nSecond useful takeaway."
    formatted = _format_llm_summary_output(raw, max_sentences=10)
    assert formatted == "- One useful takeaway.\n- Second useful takeaway."
