"""Expected ingestion failures, suitable for a concise CLI diagnostic."""

from enum import StrEnum


class FailureCategory(StrEnum):
    WORKBOOK = "workbook"
    STRUCTURE = "structure"
    MAPPING = "mapping"
    INFERENCE = "schema-inference"
    DUPLICATE = "duplicate-conflict"
    DATA = "invalid-data"
    PERSISTENCE = "persistence"


class IngestionError(Exception):
    def __init__(self, category: FailureCategory, message: str):
        self.category = category
        super().__init__(message)
