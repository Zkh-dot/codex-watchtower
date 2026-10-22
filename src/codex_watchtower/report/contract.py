"""Bounded model output, independent of the existing advisory assessment contract."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=600)]
Refs = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=128)]], Field(min_length=1, max_length=3)
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Claim(StrictModel):
    text: Text
    refs: Refs


class Progress(StrictModel):
    low: Annotated[int, Field(ge=0, le=100)]
    high: Annotated[int, Field(ge=0, le=100)]

    @model_validator(mode="after")
    def ordered(self) -> "Progress":
        if self.low > self.high:
            raise ValueError("progress low exceeds high")
        return self


class ETA(StrictModel):
    low_minutes: Annotated[int, Field(ge=0, le=100000)]
    high_minutes: Annotated[int, Field(ge=0, le=100000)]
    confidence: Literal["low", "medium", "high"]
    assumption: Text

    @model_validator(mode="after")
    def ordered(self) -> "ETA":
        if self.low_minutes > self.high_minutes:
            raise ValueError("ETA low exceeds high")
        return self


class Task(StrictModel):
    id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
    title: Annotated[str, Field(min_length=1, max_length=160)]
    status: Literal["pending", "in_progress", "done", "blocked", "unknown"]
    origin: Literal["plan", "user", "inferred"]
    active: bool
    comment: Text
    refs: Refs


class ProgressReport(StrictModel):
    schema_version: Literal["1.0"]
    goal: Text
    scope_changed: bool
    summary: Claim
    done: Annotated[list[Claim], Field(max_length=12)]
    remaining: Annotated[list[Claim], Field(max_length=12)]
    current_action: Claim | None
    blockers: Annotated[list[Claim], Field(max_length=8)]
    progress: Progress | None
    progress_reason: Text
    eta: ETA | None
    uncertainty: Text
    tasks: Annotated[list[Task], Field(max_length=40)]

    @model_validator(mode="after")
    def unique_tasks(self) -> "ProgressReport":
        if len({task.id for task in self.tasks}) != len(self.tasks):
            raise ValueError("duplicate task IDs")
        return self

    def references(self) -> set[str]:
        claims = [self.summary, *self.done, *self.remaining, *self.blockers]
        if self.current_action:
            claims.append(self.current_action)
        return {ref for item in claims for ref in item.refs} | {
            ref for task in self.tasks for ref in task.refs
        }


def validate_evidence(
    report: ProgressReport, allowed_refs: set[str], previous: ProgressReport | None
) -> None:
    if report.references() - allowed_refs:
        raise ValueError("report cites events outside the observation packet")
    if previous:
        old = {task.id for task in previous.tasks}
        if old - {task.id for task in report.tasks}:
            raise ValueError("previous tasks must be retained, inactive if excluded from scope")
