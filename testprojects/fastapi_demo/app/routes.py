"""Endpoints written the way they accumulate in a real service."""

from fastapi import APIRouter, Depends

from .schemas import UserAdminOut, UserOut

router = APIRouter()


def get_db():
    """A dependency, but not an authentication one."""
    return None


def get_current_user():
    """This one establishes who is asking."""
    return None


@router.get("/users/{pk}")
def leaky_user(pk: int, db=Depends(get_db)):
    """No response_model, no annotation, returns an ORM object.

    Three lines that serialise every column, including the ones added next
    month, to anyone who can reach the URL.
    """
    return db.query("User").get(pk)


@router.get("/me")
def me(user=Depends(get_current_user)):
    """Same shape, but behind authentication: high rather than critical."""
    return user


@router.get("/admin/users/{pk}", response_model=UserAdminOut)
def admin_user(pk: int, db=Depends(get_db)):
    """A declared model that declares too much, and no auth on the route."""
    return db.query("User").get(pk)


@router.get("/safe/{pk}", response_model=UserOut)
def safe_user(pk: int, db=Depends(get_db)):
    """Declared and narrow. Nothing to report."""
    return db.query("User").get(pk)


@router.get("/annotated/{pk}")
def annotated_user(pk: int, db=Depends(get_db)) -> UserOut:
    """FastAPI uses the return annotation, so this is bounded too."""
    return db.query("User").get(pk)


@router.get("/status")
def status():
    """Returns a dict the author wrote. Not a leak."""
    return {"ok": True}
