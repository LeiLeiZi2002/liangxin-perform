import json
from pathlib import Path

from pydantic import BaseModel, ValidationError

from app.cases.actor_policy import ActorPolicy
from app.cases.domain import CasePackage, CaseSpec, CaseStatus
from app.cases.measurement import MeasurementSpec
from app.sessions.models import CaseType, Scene


class CaseNotFoundError(LookupError):
    pass


class CaseLoadError(ValueError):
    pass


class CaseRepository:
    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or Path(__file__).parent / "data"
        self._cases: dict[str, CasePackage] = {}
        self._load()

    def _load(self) -> None:
        for path in sorted(item for item in self._data_dir.iterdir() if item.is_dir()):
            package = _load_package(path)
            case_id = package.case.case_id
            if case_id in self._cases:
                raise ValueError(f"duplicate case id: {case_id}")
            self._cases[case_id] = package

    def get(self, case_id: str) -> CasePackage:
        try:
            package = self._cases[case_id]
        except KeyError as exc:
            raise CaseNotFoundError(case_id) from exc
        return package.model_copy(deep=True)

    def list_published(
        self,
        *,
        scene: Scene | None = None,
        case_type: CaseType | None = None,
    ) -> list[CasePackage]:
        packages = [
            package
            for package in self._cases.values()
            if package.case.status is CaseStatus.published
            and (case_type is None or package.case.case_type is case_type)
            and (scene is None or scene in package.case.supported_scenes)
        ]
        return [
            package.model_copy(deep=True)
            for package in sorted(packages, key=lambda item: item.case.case_id)
        ]


def _load_package(path: Path) -> CasePackage:
    case = _load_model(path / "case.json", CaseSpec)
    actor = _load_model(path / "actor.json", ActorPolicy)
    measurement = _load_model(path / "measurement.json", MeasurementSpec)
    try:
        return CasePackage.model_validate(
            {"case": case, "actor": actor, "measurement": measurement}
        )
    except ValidationError as exc:
        raise CaseLoadError(f"invalid cross-file case package {path}: {exc}") from exc


def _load_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CaseLoadError(f"invalid JSON in case file: {path}") from exc
    try:
        return model_type.model_validate(payload)
    except ValidationError as exc:
        raise CaseLoadError(f"invalid fields in case file {path}: {exc}") from exc
