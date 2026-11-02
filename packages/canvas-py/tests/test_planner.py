"""Unit tests for planner endpoints — request shape only (no HTTP)."""
from __future__ import annotations

import pytest

from canvas_py.endpoints.planner import (
    CreatePlannerNote,
    CreatePlannerOverride,
    DeletePlannerNote,
    ListPlannerItems,
    ListPlannerNotes,
    UpdatePlannerNote,
    UpdatePlannerOverride,
)
from canvas_py.models.planner import PlannerItem, PlannerNote


# --- ListPlannerItems -------------------------------------------------------


def test_list_planner_items_default_path_and_per_page():
    req = ListPlannerItems()
    assert req.method == "GET"
    assert req.path == "planner/items"
    # Only per_page when nothing else is set.
    assert req.query_pairs() == [("per_page", 50)]
    assert req.response_is_paginated is True


def test_list_planner_items_builds_filtered_query_with_context_codes():
    req = ListPlannerItems(
        start_date="2026-04-01",
        end_date="2026-04-30",
        context_codes=["course_2966"],
        filter="new_activity",
    )
    assert req.query_pairs() == [
        ("start_date", "2026-04-01"),
        ("end_date", "2026-04-30"),
        ("context_codes[]", "course_2966"),
        ("filter", "new_activity"),
        ("per_page", 50),
    ]


def test_list_planner_items_context_codes_expand_into_repeated_keys():
    # Canvas's repeated-key idiom: each code becomes its own context_codes[]=
    # pair so urlencode(doseq=False) serializes them in order.
    req = ListPlannerItems(context_codes=["course_2966", "course_2995", "course_3050"])
    keys_for_ctx = [k for k, _ in req.query_pairs() if k == "context_codes[]"]
    values = [v for k, v in req.query_pairs() if k == "context_codes[]"]
    assert keys_for_ctx == ["context_codes[]", "context_codes[]", "context_codes[]"]
    assert values == ["course_2966", "course_2995", "course_3050"]


def test_list_planner_items_decode_builds_models():
    req = ListPlannerItems()
    payload = [
        {
            "context_type": "Course",
            "context_name": "Biology",
            "course_id": 2966,
            "plannable_id": 12345,
            "plannable_type": "assignment",
            "plannable_date": "2026-04-25T03:59:00Z",
            "plannable": {"id": 12345, "name": "Lab report"},
            "planner_override": None,
            "submissions": {
                "submitted": True,
                "graded": False,
                "late": False,
                "missing": False,
                "excused": False,
                "needs_grading": True,
                "has_feedback": False,
            },
            "html_url": "https://imsa.instructure.com/courses/2966/assignments/12345",
        }
    ]
    result = req.decode(payload)
    assert len(result) == 1
    assert isinstance(result[0], PlannerItem)
    assert result[0].plannable_type == "assignment"
    assert result[0].course_id == 2966


def test_list_planner_items_decode_handles_submissions_false():
    # Canvas returns `false` (not a dict) when the item isn't gradable.
    req = ListPlannerItems()
    payload = [
        {
            "plannable_type": "planner_note",
            "plannable_id": 99,
            "submissions": False,
            "plannable": {"id": 99, "title": "Remember to stretch"},
        }
    ]
    [item] = req.decode(payload)
    assert item.submissions is False


def test_list_planner_items_decode_rejects_non_list():
    req = ListPlannerItems()
    with pytest.raises(TypeError, match="expected list"):
        req.decode({"not": "a list"})


# --- ListPlannerNotes -------------------------------------------------------


def test_list_planner_notes_path_and_query():
    req = ListPlannerNotes()
    assert req.method == "GET"
    assert req.path == "planner_notes"
    assert req.query_pairs() == [("per_page", 50)]
    assert req.response_is_paginated is True


def test_list_planner_notes_decode_builds_models():
    req = ListPlannerNotes()
    result = req.decode([{"id": 1, "title": "x"}])
    assert len(result) == 1
    assert isinstance(result[0], PlannerNote)
    assert result[0].id == 1


# --- CreatePlannerNote ------------------------------------------------------


def test_create_planner_note_sends_json_body_with_right_keys():
    req = CreatePlannerNote(
        title="Read Chapter 4",
        details="Bio molecular & cellular",
        todo_date="2026-04-23T23:59:00Z",
        course_id=2966,
    )
    assert req.method == "POST"
    assert req.path == "planner_notes"
    body = req.json_body()
    assert body == {
        "title": "Read Chapter 4",
        "details": "Bio molecular & cellular",
        "todo_date": "2026-04-23T23:59:00Z",
        "course_id": 2966,
    }


def test_create_planner_note_omits_unspecified_fields():
    # We deliberately skip None-valued optional fields to avoid Canvas
    # rejecting nulls for some keys.
    req = CreatePlannerNote(title="Minimal")
    assert req.json_body() == {"title": "Minimal"}


def test_create_planner_note_passes_linked_object():
    req = CreatePlannerNote(
        title="Review lecture notes",
        linked_object_type="wiki_page",
        linked_object_id=4242,
    )
    body = req.json_body()
    assert body["linked_object_type"] == "wiki_page"
    assert body["linked_object_id"] == 4242


# --- UpdatePlannerNote ------------------------------------------------------


def test_update_planner_note_path_and_kwargs_into_body():
    req = UpdatePlannerNote(77, title="updated", details="new body")
    assert req.method == "PUT"
    assert req.path == "planner_notes/77"
    assert req.json_body() == {"title": "updated", "details": "new body"}


def test_update_planner_note_empty_body_when_no_fields():
    req = UpdatePlannerNote(77)
    assert req.json_body() == {}


# --- DeletePlannerNote ------------------------------------------------------


def test_delete_planner_note_path_and_method():
    req = DeletePlannerNote(88)
    assert req.method == "DELETE"
    assert req.path == "planner_notes/88"


# --- CreatePlannerOverride --------------------------------------------------


def test_create_planner_override_body():
    req = CreatePlannerOverride(
        "assignment",
        12345,
        marked_complete=True,
    )
    assert req.method == "POST"
    assert req.path == "planner/overrides"
    assert req.json_body() == {
        "plannable_type": "assignment",
        "plannable_id": 12345,
        "marked_complete": True,
        "dismissed": False,
    }


# --- UpdatePlannerOverride --------------------------------------------------


def test_update_planner_override_only_sends_provided_fields():
    req = UpdatePlannerOverride(99, marked_complete=True)
    assert req.method == "PUT"
    assert req.path == "planner/overrides/99"
    # dismissed=None → field stays absent.
    assert req.json_body() == {"marked_complete": True}


def test_update_planner_override_both_fields_none_is_empty_body():
    req = UpdatePlannerOverride(99)
    assert req.json_body() == {}
