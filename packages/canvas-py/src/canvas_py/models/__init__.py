"""Pydantic models for Canvas API objects.

All models inherit from `CanvasModel` which allows unknown fields (Canvas
adds fields over time; we don't want strict validation to break on them).
"""
from __future__ import annotations

from .activity import ActivityStreamItem
from .assignment import Assignment
from .base import CanvasModel
from .conversation import Conversation, ConversationMessage, ConversationParticipant
from .course import Course
from .discussion import DiscussionEntry, DiscussionTopic
from .file import CanvasFile
from .module import Module, ModuleItem
from .planner import PlannerItem, PlannerNote
from .submission import RubricCriterionAssessment, Submission, SubmissionComment
from .upload import UploadTarget

__all__ = [
    "CanvasModel",
    "Course",
    "Assignment",
    "Module",
    "ModuleItem",
    "CanvasFile",
    "UploadTarget",
    "DiscussionTopic",
    "DiscussionEntry",
    "PlannerItem",
    "PlannerNote",
    "ActivityStreamItem",
    "Submission",
    "SubmissionComment",
    "RubricCriterionAssessment",
    "Conversation",
    "ConversationMessage",
    "ConversationParticipant",
]
