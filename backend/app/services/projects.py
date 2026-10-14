import time
import uuid
from typing import NoReturn

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, aliased

from ..errors import AppError, is_busy
from ..generation import rules
from ..generation.state import ACTIVE_VALUES
from ..models import GeneratedVersion, GenerationSession, Project, Screenshot
from ..schemas import ProjectCreate, ProjectDetailOut, ProjectOut, ScreenshotOut
from . import project_rules


def screenshot_out(s: Screenshot) -> ScreenshotOut:
    return ScreenshotOut(
        id=s.id,
        project_id=s.project_id,
        filename=s.original_filename,
        content_type=s.content_type,
        size_bytes=s.size_bytes,
        created_at=s.created_at,
        url=f"/api/screenshots/{s.id}",
    )


_KNOWN_CONSTRAINTS = (
    "uq_projects_master_base_path",
    "fk_projects_master_is_master",
    "ck_projects_master_feature_shape",
)


def _constraint_name(exc: IntegrityError) -> str:
    """MySQL does not expose the violated constraint as a field; it is named inside the error message
    (e.g. "Duplicate entry '...' for key 'projects.uq_projects_master_base_path'")."""
    message = str(getattr(exc.orig, "args", ("", ""))[-1])
    return next((name for name in _KNOWN_CONSTRAINTS if name in message), "")


def _project_query():
    master = aliased(Project)
    shots = (
        select(Screenshot.project_id, func.count().label("n")).group_by(Screenshot.project_id).subquery()
    )
    query = (
        select(Project, master.name, func.coalesce(shots.c.n, 0))
        .outerjoin(master, Project.master_id == master.id)
        .outerjoin(shots, shots.c.project_id == Project.id)
    )
    return query


def _to_out(project: Project, master_name: str | None, count: int) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        type=project.type,
        master_id=project.master_id,
        master_name=master_name,
        base_path=project.base_path,
        initial_prompt=project.initial_prompt,
        status=project.status,
        screenshot_count=count,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def list_projects(session: Session) -> list[ProjectOut]:
    rows = session.execute(_project_query().order_by(Project.updated_at.desc(), Project.id)).all()
    return [_to_out(p, mname, n) for p, mname, n in rows]


def delete_project(session: Session, storage, generated, project_id: uuid.UUID) -> None:
    """Remove the project and everything it owns. Foreign keys are RESTRICT, so rows go in dependency order in one transaction."""
    project = session.scalar(select(Project).where(Project.id == project_id).with_for_update())
    if project is None:
        raise AppError(404, "project_not_found", "Project not found.")
    active = session.scalar(
        select(func.count()).select_from(GenerationSession).where(GenerationSession.project_id == project_id, GenerationSession.status.in_(ACTIVE_VALUES))
    )
    features = session.scalar(select(func.count()).select_from(Project).where(Project.master_id == project_id))
    rules.assert_project_deletable(active or 0, features or 0)
    keys = [s.storage_key for s in session.scalars(select(Screenshot).where(Screenshot.project_id == project_id))]
    # versions <-> sessions reference each other: break the parent links, then delete children before parents.
    session.execute(update(GeneratedVersion).where(GeneratedVersion.project_id == project_id).values(parent_version_id=None))
    session.execute(update(GenerationSession).where(GenerationSession.project_id == project_id).values(parent_version_id=None))
    session.execute(delete(GeneratedVersion).where(GeneratedVersion.project_id == project_id))
    session.execute(delete(GenerationSession).where(GenerationSession.project_id == project_id))
    session.execute(delete(Screenshot).where(Screenshot.project_id == project_id))
    session.execute(delete(Project).where(Project.id == project_id))
    session.commit()
    for key in keys:
        storage.remove_quietly(key)
    generated.discard(generated.root / str(project_id))


def get_project_detail(session: Session, project_id: uuid.UUID) -> ProjectDetailOut:
    row = session.execute(_project_query().where(Project.id == project_id)).first()
    if row is None:
        raise AppError(404, "project_not_found", "Project not found.")
    project, master_name, count = row
    shots = session.scalars(
        select(Screenshot).where(Screenshot.project_id == project_id).order_by(Screenshot.created_at, Screenshot.id)
    ).all()
    base = _to_out(project, master_name, count)
    return ProjectDetailOut(**base.model_dump(), screenshots=[screenshot_out(s) for s in shots])


def create_project(session: Session, data: ProjectCreate) -> ProjectDetailOut:
    new = project_rules.normalize_project(data)
    if new.type == "master":
        project = Project(name=new.name, type="master", initial_prompt=new.initial_prompt)
    else:
        master = session.get(Project, new.master_id)
        project_rules.assert_master(master.type if master else None)
        project = Project(
            name=new.name,
            type="feature",
            master_id=master.id,
            master_type="master",
            base_path=new.base_path,
            initial_prompt=new.initial_prompt,
        )

    for attempt in range(3):
        session.add(project)
        try:
            session.commit()
            break
        except OperationalError as exc:
            session.rollback()
            session.expunge(project)  # transient state is rebuilt on the next attempt
            if not is_busy(exc) or attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))
            continue
        except IntegrityError as exc:
            session.rollback()
            constraint = _constraint_name(exc)
            _map_integrity_error(project, exc, constraint)
    return get_project_detail(session, project.id)


def _map_integrity_error(project: Project, exc: IntegrityError, constraint: str) -> NoReturn:
    if constraint == "uq_projects_master_base_path":
        raise project_rules.base_path_conflict(project.base_path) from exc
    if constraint in ("fk_projects_master_is_master", "ck_projects_master_feature_shape"):
        msg = "The selected Master Project is not valid."
        raise AppError(422, "master_invalid", msg, {"masterId": msg}) from exc
    raise exc
