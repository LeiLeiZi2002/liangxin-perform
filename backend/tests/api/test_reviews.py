from fastapi.testclient import TestClient


def test_review_routes_are_not_exposed_in_current_release(client: TestClient) -> None:
    assert client.get("/api/reviews/queue").status_code == 404
    assert client.post("/api/reviews/report-id", json={}).status_code == 404
