from apertus_frontend.azure_services import (
    _blocked_category,
    _extract_foundry_result,
)
from apertus_frontend.pipeline import Citation


def test_extracts_foundry_text_and_unique_citations():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Grounded summary",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "Primary source",
                                "url": "https://example.com/source",
                            },
                            {
                                "type": "url_citation",
                                "title": "Duplicate",
                                "url": "https://example.com/source",
                            },
                        ],
                    }
                ],
            }
        ]
    }

    summary, citations = _extract_foundry_result(payload)

    assert summary == "Grounded summary"
    assert len(citations) == 1
    assert citations[0].url == "https://example.com/source"


def test_extracts_included_web_search_sources_without_inline_annotations():
    payload = {
        "output_text": "Evidence without inline annotations",
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "type": "search",
                    "sources": [
                        {
                            "type": "url",
                            "title": "Primary source",
                            "url": "https://example.com/primary",
                        }
                    ],
                },
            }
        ],
    }

    summary, citations = _extract_foundry_result(payload)

    assert summary == "Evidence without inline annotations"
    assert citations == (
        Citation(title="Primary source", url="https://example.com/primary"),
    )


def test_prefers_inline_citations_and_limits_display_sources():
    payload = {
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "sources": [
                        {
                            "type": "url",
                            "title": f"Source {index}",
                            "url": f"https://example.com/{index}",
                        }
                        for index in range(10)
                    ]
                },
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Evidence",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "Cited source",
                                "url": "https://cited.example.com",
                            }
                        ],
                    }
                ],
            },
        ]
    }

    _, citations = _extract_foundry_result(payload)

    assert len(citations) == 5
    assert citations[0].url == "https://cited.example.com"


def test_content_safety_threshold_is_inclusive():
    payload = {
        "categoriesAnalysis": [
            {"category": "Hate", "severity": 2},
            {"category": "Violence", "severity": 4},
        ]
    }

    assert _blocked_category(payload, 4) == "Violence severity 4"
    assert _blocked_category(payload, 6) is None