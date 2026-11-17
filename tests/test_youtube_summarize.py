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


def test_long_summary_covers_every_section_and_retries_incomplete_output(monkeypatch):
    from time import monotonic
    from types import SimpleNamespace

    import openai

    from rssbot.youtube_summarize import summarize_long_transcript_with_openai

    calls = []

    def create(**kwargs):
        prompt, source = kwargs["input"][1]["content"].split("\n\nSource:\n", 1)
        calls.append((prompt, source, kwargs["max_output_tokens"]))
        if len(calls) == 1:
            return SimpleNamespace(
                status="incomplete",
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                output_text="Discard this partial response.",
            )
        return SimpleNamespace(status="completed", output_text=source)

    monkeypatch.setattr(openai, "OpenAI", lambda **_: SimpleNamespace(
        responses=SimpleNamespace(create=create),
    ))
    sections = [f"Section {number}." for number in range(9)]
    result = summarize_long_transcript_with_openai(
        iter(sections),
        model="test-model",
        api_key="test-key",
        custom_prompt="Focus on the conclusions.",
        target_language="English",
        deadline=monotonic() + 30,
    )

    assert result.split("\n\n") == sections
    assert calls[0][1] == calls[1][1] == sections[0]
    assert calls[1][2] == 2 * calls[0][2]
    assert sum("one block summary" in prompt for prompt, _, _ in calls) == 3
    assert all("Focus on the conclusions." in prompt for prompt, _, _ in calls)
    assert "summary of the entire video" in calls[-1][0]
