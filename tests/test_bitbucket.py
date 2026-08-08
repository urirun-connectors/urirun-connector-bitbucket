from __future__ import annotations

from typing import Any

import urirun
import urirun_connector_bitbucket.core as core


class FakeClient:
    def __init__(self) -> None:
        self.branch: dict[str, Any] | None = {"target": {"hash": "c" * 40}}
        self.pull = {"state": "OPEN", "source": {"commit": {"hash": "c" * 40}}}

    def request(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any, dict[str, str]]:
        if path.endswith("/decline"):
            self.pull["state"] = "DECLINED"
            return 200, dict(self.pull), {}
        if "/pullrequests/" in path:
            return 200, dict(self.pull), {}
        if "/refs/branches/" in path:
            if method == "DELETE":
                self.branch = None
                return 204, {}, {}
            return (200, dict(self.branch), {}) if self.branch else (404, {}, {})
        raise AssertionError((method, path, kwargs))


def test_change_request_and_branch_receipts(monkeypatch: Any) -> None:
    client = FakeClient()
    monkeypatch.setattr(core, "_client", lambda: client)
    closed = core.close_change_request("org", "repo", 3, "c" * 40, "plan:close")
    assert closed["status"] == "validated"
    deleted = core.delete_branch("org", "repo", "fix", "c" * 40, "plan:delete")
    assert deleted["observed_after"] == "absent"


def test_bindings_match_manifest() -> None:
    registry = urirun.compile_registry(core.urirun_bindings())
    assert {row["uri"] for row in urirun.list_routes(registry)} == set(core.connector_manifest()["routes"])


def test_account_twin_preserves_workspace_project_repository_links(monkeypatch: Any) -> None:
    class TwinClient:
        def request(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any, dict[str, str]]:
            if path == "/user":
                return 200, {"uuid": "u1", "username": "tom", "display_name": "Tom"}, {}
            if path == "/user/workspaces":
                return 200, {"values": [{"administrator": True, "workspace": {"uuid": "w1", "slug": "subactor"}}]}, {}
            if path.endswith("/projects"):
                return 200, {"values": [{"uuid": "p1", "key": "CORE"}]}, {}
            if path == "/repositories/subactor":
                return 200, {"values": [{"uuid": "r1", "full_name": "subactor/core",
                                          "project": {"uuid": "p1"}, "mainbranch": {"name": "main"}}]}, {}
            raise AssertionError(path)

    monkeypatch.setattr(core, "_client", TwinClient)
    result = core.account_query_twin()
    assert result["ok"] and result["counts"] == {"scopes": 2, "repositories": 1}
    assert result["repositories"][0]["project_id"] == "project:p1"
    assert result["twin_fact"]["fact_quality"] == "fresh"
