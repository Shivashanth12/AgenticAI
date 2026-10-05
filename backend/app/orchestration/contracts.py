"""Typed contracts at the untrusted generation boundary."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Capability = Literal["aliases", "analytics", "expiration"]


class AcceptanceCriterion(BaseModel):
    id: Capability
    behavior: str = Field(min_length=10)


class RequirementContract(BaseModel):
    problem: str = Field(min_length=10)
    features: list[Capability] = Field(min_length=1, max_length=3)
    ambiguities: list[str]
    acceptance_criteria: list[AcceptanceCriterion]
    scope: str
    non_goals: list[str]

    @model_validator(mode="after")
    def exact_coverage(self):
        if len(set(self.features)) != len(self.features):
            raise ValueError("Capabilities must be unique")
        if {item.id for item in self.acceptance_criteria} != set(self.features):
            raise ValueError("Every capability needs an acceptance criterion")
        return self


class WorkItem(BaseModel):
    name: Capability
    objective: str = Field(min_length=10)
    depends_on: list[Capability]
    completion_gate: Capability
    acceptance_ids: list[Capability]


class WorkPlan(BaseModel):
    tasks: list[WorkItem] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def acyclic(self):
        seen = set()
        for task in self.tasks:
            if task.name in seen or not set(task.depends_on) <= seen:
                raise ValueError("Duplicate, cyclic or unknown work dependency")
            if task.acceptance_ids != [task.name] or task.completion_gate != task.name:
                raise ValueError("Work must reference its acceptance condition")
            seen.add(task.name)
        return self


class GeneratedFile(BaseModel):
    path: Literal["main.py", "test_generated.py", "README.md"]
    language: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    source_code: str = Field(min_length=1, max_length=100_000)


class ImplementationContract(BaseModel):
    generated_files: list[GeneratedFile] = Field(min_length=1, max_length=3)
