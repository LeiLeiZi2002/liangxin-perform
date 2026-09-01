from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
def unavailable_review(path: str) -> None:
    del path
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="本期暂不提供报告复核写入。",
    )
